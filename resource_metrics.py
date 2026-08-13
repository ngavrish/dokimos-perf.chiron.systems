#!/usr/bin/env python3
"""JVM resource series for the backends, pulled from Datadog.

The report already answers "how long did this endpoint take". It could not answer
"what was the service doing at the time" - so a run that slowed down because a
worker pool had ballooned looked the same as one that hit a slow query. These
series put threads, heap and CPU on the same time axis as the timings.

Auth: the metrics API takes either a ddpat_ personal token as a Bearer, or the
API+APP key pair. The pair is what the trace code uses, but only the token is
present in tests/.envrc today, and the pair is rejected outright there
(DD-API-KEY with a ddpat_ value answers 401), so both paths are supported and the
token is tried first.

Metric names were confirmed against the live org rather than assumed;
jvm.cpu.recent_utilization exists in the catalogue but carries no points for
these services, so process CPU comes from jvm.cpu_load.process.

CLI:
    python3 -m rp_perf_report.resource_metrics --minutes 120
    python3 -m rp_perf_report.resource_metrics --from 1786500000 --to 1786507200
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

DD_SITE = os.environ.get("DISNEY_DD_SITE", "https://disneystreaming.datadoghq.com")

# The two JVMs behind the portal. ifp-service answers /v1/inventory/**; the portal
# services layer is what the browser talks to.
DEFAULT_SERVICES = ("ifp-service", "ads-ifp-portal-services")

# Metric -> label. Only metrics that were verified to carry points for these
# services are listed; adding a name that reports nothing produces an empty lane
# in the report, which reads as "the service was idle" rather than "not collected".
METRICS: Dict[str, str] = {
    "jvm.thread_count": "threads",
    "jvm.heap_memory": "heap_bytes",
    "jvm.non_heap_memory": "non_heap_bytes",
    "jvm.gc.old_gen_size": "old_gen_bytes",
    "jvm.cpu_load.process": "cpu_process",
    "jvm.cpu_load.system": "cpu_system",
}


class DatadogAuthError(RuntimeError):
    pass


def _auth_headers() -> Dict[str, str]:
    token = os.environ.get("DISNEY_DD_LOGS_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    api_key = os.environ.get("DISNEY_DD_API_KEY", "").strip()
    app_key = os.environ.get("DISNEY_DD_APP_KEY", "").strip()
    if api_key and app_key:
        return {"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key}
    raise DatadogAuthError(
        "No Datadog credentials. Set DISNEY_DD_LOGS_TOKEN (a ddpat_ token), or "
        "DISNEY_DD_API_KEY together with DISNEY_DD_APP_KEY."
    )


def query_series(query: str, from_ts: int, to_ts: int, timeout: int = 30) -> List[List[float]]:
    """Return [[epoch_ms, value], ...] for a Datadog timeseries query.

    An empty list means the query resolved but carried no points - a real answer
    (nothing was reporting) rather than a failure, so it is not raised.
    """
    params = urllib.parse.urlencode({"from": from_ts, "to": to_ts, "query": query})
    req = urllib.request.Request(f"{DD_SITE}/api/v1/query?{params}")
    for key, value in _auth_headers().items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("errors"):
        raise RuntimeError(f"Datadog rejected {query!r}: {payload['errors']}")
    series = payload.get("series") or []
    if not series:
        return []
    return [[p[0], p[1]] for p in series[0].get("pointlist", []) if p[1] is not None]


def collect(
    services=DEFAULT_SERVICES,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    env: Optional[str] = None,
) -> dict:
    """Pull every metric for every service over one window.

    Shaped as {service: {label: [[ts_ms, value], ...]}} so a caller can drop a
    lane straight into a chart without reshaping.
    """
    to_ts = int(to_ts or time.time())
    from_ts = int(from_ts or to_ts - 3600)
    scope = f"service:{{svc}}" + (f",env:{env}" if env else "")

    out = {"from": from_ts, "to": to_ts, "site": DD_SITE, "services": {}}
    for svc in services:
        lanes: Dict[str, list] = {}
        for metric, label in METRICS.items():
            query = f"avg:{metric}{{{scope.format(svc=svc)}}}"
            try:
                lanes[label] = query_series(query, from_ts, to_ts)
            except Exception as exc:  # one bad lane must not lose the rest
                lanes[label] = []
                lanes.setdefault("_errors", {})[label] = str(exc)[:200]
        out["services"][svc] = lanes
    return out


def summarise(data: dict) -> List[str]:
    """One line per service per lane: min, max and the drift across the window.

    Drift is the point of the whole thing - a pool that ends far above where it
    started is the shape worth looking at.
    """
    lines = []
    for svc, lanes in data.get("services", {}).items():
        lines.append(f"{svc}:")
        for label, points in lanes.items():
            if label.startswith("_"):
                continue
            values = [p[1] for p in points]
            if not values:
                lines.append(f"    {label:16} no data")
                continue
            drift = values[-1] - values[0]
            lines.append(
                f"    {label:16} n={len(values):<4} min={min(values):<14.2f} "
                f"max={max(values):<14.2f} drift={drift:+.2f}"
            )
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pull JVM resource series from Datadog.")
    ap.add_argument("--minutes", type=int, default=60, help="window size, ending now")
    ap.add_argument("--from", dest="from_ts", type=int, help="epoch seconds")
    ap.add_argument("--to", dest="to_ts", type=int, help="epoch seconds")
    ap.add_argument("--env", help="restrict to an env tag, e.g. uat")
    ap.add_argument("--service", action="append", help="repeatable; defaults to both backends")
    ap.add_argument("--json", dest="as_json", action="store_true", help="emit raw series")
    args = ap.parse_args(argv)

    to_ts = args.to_ts or int(time.time())
    from_ts = args.from_ts or to_ts - args.minutes * 60

    try:
        data = collect(
            services=tuple(args.service) if args.service else DEFAULT_SERVICES,
            from_ts=from_ts,
            to_ts=to_ts,
            env=args.env,
        )
    except DatadogAuthError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(data, indent=2))
    else:
        window = (to_ts - from_ts) / 60
        print(f"window: {window:.0f} min ending {time.strftime('%H:%M:%S', time.localtime(to_ts))}")
        for line in summarise(data):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
