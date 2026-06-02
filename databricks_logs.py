#!/usr/bin/env python3
"""
Parse Databricks job logs (log4j / stdout / stderr) into normalized
LogEvent records, suitable for overlaying on a shared timeline with
Report Portal test logs.

Three input streams are recognized:
  - log4j-*.log[.gz]     primary, per-line timestamped business events
  - stdout*.txt          phase-completion markers + GC lines (anchors)
  - stderr*.txt          Datadog APM agent lines

Usage:
    python3 -m rp_perf_report.databricks_logs <log_dir>
    python3 -m rp_perf_report.databricks_logs <log_dir> --summary
    python3 -m rp_perf_report.databricks_logs <log_dir> \\
        --start 2026-05-28T09:00:00Z --end 2026-05-28T15:00:00Z \\
        --loggers OlapIngestTaskDatabricks,DbAccessor,DatabricksAccessor,DataIngestDatabricks
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class LogEvent:
    timestamp: datetime
    source: str          # "log4j" | "stdout" | "stderr"
    level: str           # "INFO" | "WARN" | "ERROR" | "DEBUG" | "GC" | "PHASE" | "ANCHOR"
    logger: str
    message: str
    approx: bool = False                      # True if timestamp inferred from nearby anchor
    end_timestamp: Optional[datetime] = None  # PHASE intervals: completion time
    duration_s: Optional[float] = None        # PHASE intervals: known duration from log line

    def to_dict(self) -> dict:
        d = {
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat(),
            "source": self.source,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
            "approx": self.approx,
        }
        if self.end_timestamp is not None:
            d["end_timestamp"] = self.end_timestamp.astimezone(timezone.utc).isoformat()
        if self.duration_s is not None:
            d["duration_s"] = self.duration_s
        return d


# log4j:  YY/MM/DD HH:MM:SS LEVEL Logger$: message
LOG4J_HEADER_RE = re.compile(
    r"^(\d{2})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2}) "
    r"(INFO|WARN|ERROR|DEBUG|TRACE) (\S+?): (.*)$"
)

# stdout wall-clock anchor:  "Thu May 28 09:07:05 UTC 2026 <tail>"
STDOUT_ANCHOR_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) UTC (\d{4})\b(.*)$"
)

# JVM GC log line:  [2026-05-28T13:53:34.786+0000][17189.519s][info][gc] <msg>
GC_LINE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+\-]\d{4})\]"
    r"\[[^\]]*\]\[(\w+)\]\[gc[^\]]*\] (.*)$"
)

# stdout phase-completion lines (timestamp inferred from latest anchor)
PHASE_DRAIN_RE = re.compile(r"^Pipeline (\S+) drained in (\d+)s\s*$")
PHASE_OPTIMIZE_RE = re.compile(r"^OPTIMIZE TABLE (\S+) completed in (\d+)s\s*$")

# stdout SQL-execution markers; caller is e.g. "DbAccessor.upDateRecordState"
STDOUT_SQL_RE = re.compile(r"^Executing SQL query from (\S+):\s*$")

# Hourly-file naming convention: stdout--YYYY-MM-DD--HH-MM.txt
HOURLY_FILE_RE = re.compile(
    r"^(?:stdout|stderr)--(\d{4})-(\d{2})-(\d{2})--(\d{2})-(\d{2})\.txt$"
)

# Datadog APM agent line:
# [dd.trace 2026-05-28 09:07:06:457 +0000] [main] INFO datadog.x.y - msg
DD_TRACE_RE = re.compile(
    r"^\[dd\.trace (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):(\d{3}) "
    r"([+\-]\d{4})\] \[\S+\] (INFO|WARN|ERROR|DEBUG) (\S+) - (.*)$"
)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
)}


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, mode="rt", encoding="utf-8", errors="replace")
    return open(path, mode="r", encoding="utf-8", errors="replace")


def parse_log4j_file(path: Path) -> Iterator[LogEvent]:
    """Yields one LogEvent per log4j header line. Continuation lines
    (stack traces, etc.) are appended to the preceding event's message."""
    current: Optional[LogEvent] = None
    with _open_text(path) as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = LOG4J_HEADER_RE.match(line)
            if m:
                if current is not None:
                    yield current
                yy, mo, dd, hh, mm, ss, level, logger, msg = m.groups()
                ts = datetime(2000 + int(yy), int(mo), int(dd),
                              int(hh), int(mm), int(ss), tzinfo=timezone.utc)
                current = LogEvent(
                    timestamp=ts, source="log4j", level=level,
                    logger=logger, message=msg,
                )
            elif current is not None:
                current.message += "\n" + line
    if current is not None:
        yield current


def _filename_default_ts(name: str) -> Optional[datetime]:
    """If the filename embeds an hour (stdout--YYYY-MM-DD--HH-MM.txt), return
    that as a UTC datetime to seed latest_ts. Used as a coarse fallback for
    untimestamped phase markers in files that lack any inline anchor."""
    m = HOURLY_FILE_RE.match(name)
    if not m:
        return None
    yr, mo, dd, hh, mm = (int(x) for x in m.groups())
    return datetime(yr, mo, dd, hh, mm, 0, tzinfo=timezone.utc)


def _scan_anchors(lines: list) -> tuple:
    """Return parallel lists (line_indices, timestamps) for every GC log line
    and wall-clock anchor in the file."""
    line_idx: list = []
    ts_list: list = []
    for i, raw in enumerate(lines):
        line = raw.rstrip("\n")
        m = GC_LINE_RE.match(line)
        if m:
            iso, _level, _msg = m.groups()
            ts = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%f%z")
            line_idx.append(i)
            ts_list.append(ts)
            continue
        m = STDOUT_ANCHOR_RE.match(line)
        if m:
            mon, day, hh, mm, ss, yr, _tail = m.groups()
            ts = datetime(int(yr), _MONTHS[mon], int(day),
                          int(hh), int(mm), int(ss), tzinfo=timezone.utc)
            line_idx.append(i)
            ts_list.append(ts)
    return line_idx, ts_list


def _nearest_ts(
    idx: int, line_idx: list, ts_list: list, fallback: Optional[datetime]
) -> Optional[datetime]:
    """Return the timestamp of the anchor whose line number is closest to idx.
    line_idx is assumed sorted ascending (it is, since we collect top-down)."""
    if not line_idx:
        return fallback
    pos = bisect.bisect_left(line_idx, idx)
    best: Optional[tuple] = None  # (distance, ts)
    if pos > 0:
        best = (abs(idx - line_idx[pos - 1]), ts_list[pos - 1])
    if pos < len(line_idx):
        cand = (abs(line_idx[pos] - idx), ts_list[pos])
        if best is None or cand[0] < best[0]:
            best = cand
    return best[1] if best else fallback


def parse_stdout_file(path: Path) -> Iterator[LogEvent]:
    """Yields anchor events (wall-clock stamps, GC lines) plus phase-completion
    and SQL-execution markers. Two-pass: anchors are gathered first, then each
    untimestamped marker is snapped to the nearest anchor by line distance.
    Filename hour is used as a coarse fallback when no inline anchors exist."""
    fallback = _filename_default_ts(path.name)
    with _open_text(path) as f:
        lines = f.readlines()

    anchor_lines, anchor_ts = _scan_anchors(lines)

    for i, raw in enumerate(lines):
        line = raw.rstrip("\n")

        m = GC_LINE_RE.match(line)
        if m:
            iso, level, msg = m.groups()
            ts = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%f%z")
            yield LogEvent(timestamp=ts, source="stdout", level="GC",
                           logger="gc", message=msg)
            continue

        m = STDOUT_ANCHOR_RE.match(line)
        if m:
            mon, day, hh, mm, ss, yr, tail = m.groups()
            ts = datetime(int(yr), _MONTHS[mon], int(day),
                          int(hh), int(mm), int(ss), tzinfo=timezone.utc)
            yield LogEvent(timestamp=ts, source="stdout", level="ANCHOR",
                           logger="stdout", message=tail.strip())
            continue

        m = PHASE_DRAIN_RE.match(line)
        if m:
            end_ts = _nearest_ts(i, anchor_lines, anchor_ts, fallback)
            if end_ts is None:
                continue
            pipeline, duration_str = m.groups()
            duration = float(duration_str)
            yield LogEvent(
                timestamp=end_ts - timedelta(seconds=duration),
                source="stdout", level="PHASE",
                logger="pipeline_drain",
                message=f"{pipeline} drained in {duration_str}s",
                approx=True,
                end_timestamp=end_ts,
                duration_s=duration,
            )
            continue

        m = PHASE_OPTIMIZE_RE.match(line)
        if m:
            end_ts = _nearest_ts(i, anchor_lines, anchor_ts, fallback)
            if end_ts is None:
                continue
            table, duration_str = m.groups()
            duration = float(duration_str)
            yield LogEvent(
                timestamp=end_ts - timedelta(seconds=duration),
                source="stdout", level="PHASE",
                logger="optimize_table",
                message=f"OPTIMIZE TABLE {table} completed in {duration_str}s",
                approx=True,
                end_timestamp=end_ts,
                duration_s=duration,
            )
            continue

        m = STDOUT_SQL_RE.match(line)
        if m:
            ts = _nearest_ts(i, anchor_lines, anchor_ts, fallback)
            if ts is None:
                continue
            caller = m.group(1)
            yield LogEvent(
                timestamp=ts, source="stdout", level="SQL",
                logger=caller,
                message=f"Executing SQL query from {caller}",
                approx=True,
            )


def parse_stderr_file(path: Path) -> Iterator[LogEvent]:
    """Yields Datadog APM agent lines. Other stderr noise is ignored."""
    with _open_text(path) as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = DD_TRACE_RE.match(line)
            if not m:
                continue
            date_part, ms, tz, level, logger, msg = m.groups()
            iso = f"{date_part}.{ms}{tz}"
            ts = datetime.strptime(iso, "%Y-%m-%d %H:%M:%S.%f%z")
            yield LogEvent(timestamp=ts, source="stderr", level=level,
                           logger=logger, message=msg)


def _refine_phase_intervals(events: list) -> None:
    """Refine PHASE event intervals by snapping each phase's end down to the
    next phase's start when they currently overlap. Application phases (drain
    -> optimize -> ...) run sequentially with sub-second handoff, so an overlap
    means the earlier phase's end timestamp was anchored to a GC line that
    actually fired during the later phase. Mutates events in place."""
    phases = [
        e for e in events
        if e.level == "PHASE" and e.end_timestamp is not None and e.duration_s is not None
    ]
    phases.sort(key=lambda e: e.end_timestamp)
    for i in range(len(phases) - 1):
        cur, nxt = phases[i], phases[i + 1]
        if cur.end_timestamp > nxt.timestamp:
            cur.end_timestamp = nxt.timestamp
            cur.timestamp = cur.end_timestamp - timedelta(seconds=cur.duration_s)


def fetch_databricks_logs(
    log_dir,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> list:
    """Walk a log directory, parse all three known stream types, and return
    a single time-sorted list of LogEvents, optionally filtered to a window."""
    log_dir = Path(log_dir)
    events: list = []

    for p in sorted(log_dir.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if name.startswith("log4j-") and (name.endswith(".log") or name.endswith(".log.gz")):
            events.extend(parse_log4j_file(p))
        elif name.startswith("stdout") and name.endswith(".txt"):
            events.extend(parse_stdout_file(p))
        elif name.startswith("stderr") and name.endswith(".txt"):
            events.extend(parse_stderr_file(p))

    _refine_phase_intervals(events)
    events.sort(key=lambda e: e.timestamp)

    if start is not None:
        events = [e for e in events if e.timestamp >= start]
    if end is not None:
        events = [e for e in events if e.timestamp <= end]

    return events


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def main(argv) -> int:
    ap = argparse.ArgumentParser(
        description="Parse Databricks job logs into normalized events."
    )
    ap.add_argument("log_dir", help="Directory containing log4j-*, stdout*, stderr* files")
    ap.add_argument("--start", help="ISO 8601 lower bound (inclusive)", default=None)
    ap.add_argument("--end", help="ISO 8601 upper bound (inclusive)", default=None)
    ap.add_argument(
        "--loggers",
        help="Comma-separated logger-name prefixes to keep (e.g. OlapIngestTaskDatabricks,DbAccessor)",
        default=None,
    )
    ap.add_argument("--summary", action="store_true",
                    help="Print counts per level + top loggers instead of JSON Lines")
    args = ap.parse_args(argv)

    start = _parse_iso(args.start) if args.start else None
    end = _parse_iso(args.end) if args.end else None

    events = fetch_databricks_logs(args.log_dir, start=start, end=end)

    if args.loggers:
        wanted = tuple(s.strip() for s in args.loggers.split(",") if s.strip())
        events = [e for e in events if e.logger.startswith(wanted)]

    if args.summary:
        by_logger = Counter(e.logger for e in events)
        by_level = Counter(e.level for e in events)
        by_source = Counter(e.source for e in events)
        total = len(events)
        print(f"Total events: {total}")
        if events:
            print(f"Time range:   {events[0].timestamp.isoformat()}  ->  {events[-1].timestamp.isoformat()}")
        print("\nBy source:")
        for src, n in by_source.most_common():
            print(f"  {src:<8s} {n}")
        print("\nBy level:")
        for lvl, n in by_level.most_common():
            print(f"  {lvl:<8s} {n}")
        print("\nTop loggers:")
        for lg, n in by_logger.most_common(25):
            print(f"  {n:>6d}  {lg}")
        return 0

    for e in events:
        sys.stdout.write(json.dumps(e.to_dict(), separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
