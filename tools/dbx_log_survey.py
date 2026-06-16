#!/usr/bin/env python3
"""Survey IFP Databricks jobs: for each job, find its latest run that has driver
logs, download them (capped), parse, and score how analyzable they are. Writes
fixtures to reports/_dbx_survey/<job>/ and a summary JSON. Run from repo root with
the SPA venv so spa/databricks_logs import cleanly."""
import json, os, shutil, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import spa
from databricks_logs import fetch_databricks_logs

ENV = sys.argv[1] if len(sys.argv) > 1 else "prod"
_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
OUT = os.path.join(_BASE, f"_dbx_survey_{ENV}" if ENV != "prod" else "_dbx_survey")
SUMMARY = os.path.join(OUT, "summary.json")
BUSINESS = ("OlapIngestTaskDatabricks", "DbAccessor", "DatabricksAccessor",
            "DataIngestDatabricks", "JdbcManager", "pipeline_drain", "optimize_table")

def log(m): print(m, flush=True)

DEPTH = int(os.environ.get("DBX_SURVEY_DEPTH", "40"))

def pick_run_with_logs(w, job_id):
    """Newest run (scanning up to DEPTH, via the auto-paginating iterator since
    the API caps per-page at 25) that has at least one log-bearing cluster."""
    import itertools
    for br in itertools.islice(w.jobs.list_runs(job_id=job_id, expand_tasks=False), DEPTH):
        if br.start_time is None:
            continue
        try:
            if spa._dbx_run_targets(w, br.run_id):
                return {"run_id": br.run_id, "start_ms": br.start_time, "end_ms": br.end_time or None}
        except Exception:
            continue
    return None

def main():
    os.makedirs(OUT, exist_ok=True)
    w = spa._dbx_client(ENV)
    jobs = [j for j in spa._dbx_list_jobs(ENV) if "ifp" in j["name"].lower()]
    log(f"surveying {len(jobs)} IFP jobs in {ENV}")
    results = []
    for i, j in enumerate(jobs, 1):
        name, jid = j["name"], int(j["job_id"])
        row = {"job_id": j["job_id"], "name": name, "status": None,
               "run_id": None, "run_window": None, "bytes": 0, "files": 0,
               "events": 0, "business_events": 0, "phase_events": 0, "top_loggers": []}
        log(f"[{i}/{len(jobs)}] {name}")
        try:
            r = pick_run_with_logs(w, jid)
            if not r:
                row["status"] = "no log-bearing run (serverless / no log_conf)"
                results.append(row); _save(results); continue
            row["run_id"] = str(r["run_id"])
            row["run_window"] = f'{spa._epoch_ms_to_iso(r["start_ms"])} -> {spa._epoch_ms_to_iso(r.get("end_ms"))}'
            res = spa._dbx_download_run_logs(ENV, r["run_id"])
            d = res.get("dir")
            if not d:
                row["status"] = "empty download"; results.append(row); _save(results); continue
            dest = os.path.join(OUT, name)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(d, dest)
            row["bytes"] = res.get("total_bytes", 0)
            row["files"] = len(res.get("files") or [])
            ev = fetch_databricks_logs(dest)
            from collections import Counter
            row["events"] = len(ev)
            row["business_events"] = sum(1 for e in ev if e.level != "PHASE" and e.logger.startswith(BUSINESS))
            row["phase_events"] = sum(1 for e in ev if e.level == "PHASE")
            row["top_loggers"] = [f"{n}x {lg}" for lg, n in Counter(e.logger for e in ev).most_common(6)]
            row["status"] = "ok"
            log(f"    {row['bytes']//1024} KB, {row['files']} files, {row['events']} events, "
                f"business={row['business_events']} phase={row['phase_events']}")
        except Exception as exc:
            row["status"] = f"ERROR: {type(exc).__name__}: {exc}"
            log(f"    {row['status']}")
        results.append(row); _save(results)
    log(f"DONE -> {SUMMARY}")

def _save(results):
    with open(SUMMARY, "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    main()
