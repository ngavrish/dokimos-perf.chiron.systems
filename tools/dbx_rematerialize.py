import json, os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import spa
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX=os.path.join(ROOT,"reports","_dbx_fixtures"); MANIFEST=os.path.join(FIX,"manifest.json")
m=json.load(open(MANIFEST))
# wipe existing (collided) pipeline dirs; keep manifest.json + run.log
for d in os.listdir(FIX):
    p=os.path.join(FIX,d)
    if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
for r in m:
    if r.get("status")!="ok": continue
    env=r["env"]; rid=int(r["run_id"])
    w=spa._dbx_client(env)
    jid=w.jobs.get_run(run_id=rid).job_id
    res=spa._dbx_download_run_logs(env, rid)
    if not res.get("dir"):
        r["status"]="re-pull empty"; print("EMPTY", env, r["name"]); continue
    safe="".join(c if (c.isalnum() or c in "._-") else "_" for c in r["name"])
    dest=os.path.join(FIX, f"{env}__{jid}__{safe}")
    shutil.rmtree(dest, ignore_errors=True); shutil.move(res["dir"], dest)
    r["job_id"]=str(jid); r["fixture_dir"]=os.path.relpath(dest, ROOT)
    print("OK", env, "job", jid, "->", os.path.basename(dest), res["total_bytes"]//1048576,"MB")
    json.dump(m, open(MANIFEST,"w"), indent=2)
print("REMATERIALIZE DONE")
