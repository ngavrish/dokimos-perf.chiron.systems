#!/usr/bin/env python3
"""Regenerate an existing report in place, adding the Timeline tab from a
Databricks job log directory. Preserves `share_hash` and `id` so any
existing `/r/<hash>` or `/reports/<sprint>/<id>/` URL keeps working.

A new AES-256-GCM password is generated for the regenerated report -- the
original password is *not* recoverable, so anyone who had it will need the
new one (printed to stdout once at the end). This matches the system's
"password shown once at generation, never persisted" trust model.

Usage:
    python3 tools/regenerate_report.py <share_hash> [--dbx-logs <dir>]

If --dbx-logs is omitted, falls back to $DOKIMOS_DATABRICKS_LOG_DIR, then
to /Users/boberit/Downloads/logs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Make sibling modules importable when running directly.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import analyzer  # noqa: E402
from databricks_logs import fetch_databricks_logs  # noqa: E402
from spa import _generate_report_password, REPORTS_DIR  # noqa: E402


def _find_report_by_hash(share_hash: str):
    for sprint_dir in sorted(Path(REPORTS_DIR).iterdir()):
        if not sprint_dir.is_dir():
            continue
        for report_dir in sorted(sprint_dir.iterdir()):
            meta_path = report_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                data = json.loads(meta_path.read_text())
            except Exception:
                continue
            if data.get("share_hash") == share_hash:
                return report_dir, data
    return None, None


def _wrap_logs(entries):
    return entries if isinstance(entries, dict) else {"content": entries}


def _earliest_time(entries) -> str:
    items = entries if isinstance(entries, list) else entries.get("content", [])
    times = []
    for e in items:
        t = e.get("time") if isinstance(e, dict) else None
        if not t:
            continue
        try:
            times.append(datetime.fromisoformat(t.replace("Z", "+00:00")))
        except Exception:
            pass
    if not times:
        return ""
    return min(times).strftime("%H:%M:%S")


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("share_hash", help="share_hash of the report to regenerate")
    ap.add_argument(
        "--dbx-logs",
        default=os.environ.get("DOKIMOS_DATABRICKS_LOG_DIR", "/Users/boberit/Downloads/logs"),
        help="Directory containing Databricks log4j/stdout/stderr files",
    )
    args = ap.parse_args(argv)

    report_dir, meta = _find_report_by_hash(args.share_hash)
    if not report_dir:
        print(f"error: no report found with share_hash {args.share_hash!r}", file=sys.stderr)
        return 1

    urls = meta.get("urls") or []
    if not urls:
        print(f"error: report {report_dir} has no urls in metadata", file=sys.stderr)
        return 1

    print(f"Regenerating: {report_dir}")
    print(f"  share_hash:   {args.share_hash}")
    print(f"  launches:     {len(urls)}")

    # Build inputs from cached launch JSONs -- no RP API calls.
    logs_list: list = []
    launch_labels: list = []
    launch_ids: list = []
    missing: list = []
    for url in urls:
        launch_id = analyzer.extract_launch_id(url)
        cache_path = Path(analyzer.LOGS_DIR) / f"{launch_id}.json"
        if not cache_path.exists():
            missing.append(launch_id)
            continue
        entries = json.loads(cache_path.read_text())
        logs_list.append(_wrap_logs(entries))
        launch_labels.append(_earliest_time(entries) or launch_id)
        launch_ids.append(launch_id)

    if missing:
        print(f"error: missing cached logs for launches: {', '.join(missing)}", file=sys.stderr)
        print(f"       (looked in {analyzer.LOGS_DIR})", file=sys.stderr)
        return 1

    timings_list = [analyzer.analyze_timings(l) for l in logs_list]
    detailed_timings_list = [analyzer.analyze_timings_detailed(l) for l in logs_list]

    events: list = []
    dbx_dir = args.dbx_logs
    if dbx_dir and os.path.isdir(dbx_dir):
        events = fetch_databricks_logs(dbx_dir)
        print(f"  databricks:   {len(events)} events from {dbx_dir}")
    else:
        print(f"  databricks:   no log dir at {dbx_dir} -- Timeline tab will be omitted")

    password = _generate_report_password()
    html = analyzer.generate_multi_comparison_html(
        launch_labels, logs_list, timings_list, detailed_timings_list,
        launch_ids=launch_ids,
        report_password=password,
        databricks_events=events,
    )

    # Atomic-ish write: rename through a tempfile next to the target so a
    # crash mid-write doesn't truncate the live report.
    index_path = report_dir / "index.html"
    tmp_path = report_dir / "index.html.tmp"
    tmp_path.write_text(html, encoding="utf-8")
    os.replace(tmp_path, index_path)

    # Persist the Databricks logs alongside the report (replacing any prior
    # snapshot) so the source data sticks with the artifact.
    dbx_files_saved = 0
    dbx_bytes_saved = 0
    if events and dbx_dir and os.path.isdir(dbx_dir):
        dst = report_dir / "databricks_logs"
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir()
        for f in sorted(Path(dbx_dir).iterdir()):
            if not f.is_file():
                continue
            if not (f.name.startswith("log4j-")
                    or f.name.startswith("stdout")
                    or f.name.startswith("stderr")):
                continue
            shutil.copy2(f, dst / f.name)
            dbx_files_saved += 1
            dbx_bytes_saved += f.stat().st_size

    # Update metadata. Keep id/sprint/share_hash/title/urls; bump timestamp;
    # record databricks snapshot.
    meta["regenerated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if dbx_files_saved:
        meta["databricks"] = {
            "files_saved": dbx_files_saved,
            "bytes_saved": dbx_bytes_saved,
        }
    (report_dir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print()
    print(f"Wrote: {index_path}  ({index_path.stat().st_size:,} bytes)")
    if dbx_files_saved:
        print(f"Persisted {dbx_files_saved} Databricks log files ({dbx_bytes_saved:,} bytes) under {report_dir}/databricks_logs/")
    print()
    print("===== NEW REPORT PASSWORD (save this -- shown only once) =====")
    print(f"  {password}")
    print("==============================================================")
    print(f"URL is unchanged: /r/{args.share_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
