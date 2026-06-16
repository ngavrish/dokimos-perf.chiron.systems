#!/usr/bin/env python3
"""Richest-run pass: for the business-logger-emitting IFP pipelines, scan several
recent log-bearing runs, download+parse each, and keep the single run with the
MOST business-logger events (tiebreak: size) as a canonical fixture under
reports/_dbx_fixtures/<env>__<pipeline>/. Writes a manifest. Read-only against
Databricks (never triggers a run). Usage: dbx_richest_run.py [max_runs]"""
import json, os, shutil, sys, itertools
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import spa
from databricks_logs import fetch_databricks_logs

MAXRUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "reports", "_dbx_fixtures")
MANIFEST = os.path.join(FIX, "manifest.json")
BUSINESS = ("OlapIngestTaskDatabricks", "DbAccessor", "DatabricksAccessor",
            "DataIngestDatabricks", "JdbcManager", "pipeline_drain", "optimize_table")
# Families worth deep-scanning for richness (emit business loggers / phase markers).
FAMILIES = ("evaluation", "forecast", "allocation_availability")

def log(m): print(m, flush=True)

def candidates():
    seen, out = set(), []
    for env, p in [("prod", "reports/_dbx_survey/summary.json"),
                   ("uat", "reports/_dbx_survey_uat/summary.json")]:
        try:
            rows = json.load(open(os.path.join(ROOT, p)))
        except Exception:
            continue
        for r in rows:
            if r["status"] != "ok":
                continue
            nm = r["name"]
            if r.get("business_events", 0) > 0 or any(f in nm for f in FAMILIES):
                key = (env, r["job_id"])
                if key not in seen:
                    seen.add(key); out.append((env, int(r["job_id"]), nm))
    return out

def score(env, run_id):
    """Download a run's logs, return (business, phase, events, bytes, tmpdir) or None."""
    res = spa._dbx_download_run_logs(env, run_id)
    d = res.get("dir")
    if not d:
        return None
    ev = fetch_databricks_logs(d)
    biz = sum(1 for e in ev if e.level != "PHASE" and e.logger.startswith(BUSINESS))
    ph = sum(1 for e in ev if e.level == "PHASE")
    return {"business": biz, "phase": ph, "events": len(ev),
            "bytes": res.get("total_bytes", 0), "dir": d, "run_id": str(run_id)}

def main():
    os.makedirs(FIX, exist_ok=True)
    cands = candidates()
    log(f"richest-run pass over {len(cands)} pipelines, up to {MAXRUNS} runs each")
    manifest = []
    for i, (env, jid, name) in enumerate(cands, 1):
        log(f"[{i}/{len(cands)}] {env}:{name}")
        w = spa._dbx_client(env)
        best = None
        scanned = 0
        for br in itertools.islice(w.jobs.list_runs(job_id=jid, expand_tasks=False), MAXRUNS):
            if br.start_time is None:
                continue
            try:
                if not spa._dbx_run_targets(w, br.run_id):
                    continue
            except Exception:
                continue
            scanned += 1
            try:
                s = score(env, br.run_id)
            except Exception as exc:
                log(f"    run {br.run_id} score err: {type(exc).__name__}"); continue
            if not s:
                continue
            s["start"] = spa._epoch_ms_to_iso(br.start_time)
            if best is None or (s["business"], s["bytes"]) > (best["business"], best["bytes"]):
                if best:
                    shutil.rmtree(best["dir"], ignore_errors=True)
                best = s
            else:
                shutil.rmtree(s["dir"], ignore_errors=True)
        if not best:
            log("    no scorable runs");
            manifest.append({"env": env, "name": name, "status": "no logs"}); _save(manifest); continue
        # Key the dir by env+job_id so different jobs that share a display name
        # (e.g. a scheduled job and its dev clone) don't overwrite each other.
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
        dest = os.path.join(FIX, f"{env}__{jid}__{safe}")
        shutil.rmtree(dest, ignore_errors=True)
        shutil.move(best["dir"], dest)
        rec = {"env": env, "name": name, "job_id": str(jid), "run_id": best["run_id"],
               "run_start": best["start"], "scanned_runs": scanned, "bytes": best["bytes"],
               "events": best["events"], "business_events": best["business"],
               "phase_events": best["phase"], "fixture_dir": os.path.relpath(dest, ROOT),
               "status": "ok"}
        manifest.append(rec); _save(manifest)
        log(f"    RICHEST: run {best['run_id']} ({best['start']}) {best['bytes']//1048576}MB "
            f"biz={best['business']} phase={best['phase']} (of {scanned} runs)")
    log("RICHEST-RUN PASS DONE -> " + MANIFEST)

def _save(m): json.dump(m, open(MANIFEST, "w"), indent=2)

if __name__ == "__main__":
    main()
