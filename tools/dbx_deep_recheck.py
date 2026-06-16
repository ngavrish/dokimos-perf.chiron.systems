#!/usr/bin/env python3
"""Deep re-check: for IFP jobs the shallow survey marked non-'ok', scan much
deeper run history (up to LIMIT runs) for the most recent run that actually has
driver logs, download + parse it. Updates reports/_dbx_survey/summary.json in
place. Read-only against Databricks (never triggers a run)."""
import json, os, shutil, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import spa
from databricks_logs import fetch_databricks_logs

ENV = "prod"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 80
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "_dbx_survey")
SUMMARY = os.path.join(OUT, "summary.json")
BUSINESS = ("OlapIngestTaskDatabricks", "DbAccessor", "DatabricksAccessor",
            "DataIngestDatabricks", "JdbcManager", "pipeline_drain", "optimize_table")

def log(m): print(m, flush=True)

def deep_pick(w, job_id):
    """Most recent run (scanning up to LIMIT, via the auto-paginating iterator
    since the API caps per-page limit at 25) that has a log-bearing cluster."""
    import itertools
    gen = w.jobs.list_runs(job_id=job_id, expand_tasks=False)
    for br in itertools.islice(gen, LIMIT):
        rid = br.run_id
        if br.start_time is None:
            continue
        try:
            if spa._dbx_run_targets(w, rid):
                return {"run_id": rid, "start_ms": br.start_time, "end_ms": br.end_time or None}
        except Exception:
            continue
    return None

def main():
    rows = json.load(open(SUMMARY))
    w = spa._dbx_client(ENV)
    todo = [r for r in rows if r["status"] != "ok"]
    log(f"deep re-check of {len(todo)} non-ok jobs, scanning up to {LIMIT} runs each")
    for i, row in enumerate(todo, 1):
        name, jid = row["name"], int(row["job_id"])
        log(f"[{i}/{len(todo)}] {name}")
        try:
            r = deep_pick(w, jid)
            if not r:
                row["status"] = f"no log-bearing run in last {LIMIT}"
                _save(rows); continue
            res = spa._dbx_download_run_logs(ENV, r["run_id"])
            d = res.get("dir")
            if not d:
                row["status"] = "empty download"; _save(rows); continue
            dest = os.path.join(OUT, name)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(d, dest)
            ev = fetch_databricks_logs(dest)
            row.update({
                "run_id": str(r["run_id"]),
                "run_window": f'{spa._epoch_ms_to_iso(r["start_ms"])} -> {spa._epoch_ms_to_iso(r.get("end_ms"))}',
                "bytes": res.get("total_bytes", 0), "files": len(res.get("files") or []),
                "events": len(ev),
                "business_events": sum(1 for e in ev if e.level != "PHASE" and e.logger.startswith(BUSINESS)),
                "phase_events": sum(1 for e in ev if e.level == "PHASE"),
                "top_loggers": [f"{n}x {lg}" for lg, n in Counter(e.logger for e in ev).most_common(6)],
                "status": "ok",
            })
            log(f"    FOUND: {row['bytes']//1024} KB, {row['events']} events, "
                f"business={row['business_events']} phase={row['phase_events']} run {r['run_id']}")
        except Exception as exc:
            row["status"] = f"ERROR: {type(exc).__name__}: {exc}"
            log(f"    {row['status']}")
        _save(rows)
    log("DEEP RE-CHECK DONE")

def _save(rows):
    json.dump(rows, open(SUMMARY, "w"), indent=2)

if __name__ == "__main__":
    main()
