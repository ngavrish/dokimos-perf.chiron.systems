"""Dokimos Performance -- single-page-application server for rp_perf_report.

Visual style mirrors the Dokimos pitch deck (dark navy #001428 + bronze
#CD7F32 hero accent, Courier New display type / Calibri body). The reports
rendered inside the viewer iframe still use the analyzer's own styling --
they're independent HTML documents.

Two tabs:
  - "New":     vertical list of RP launch URL inboxes with +/- buttons and a
               "Generate Report" button at the bottom. On click, the SPA POSTs
               to /api/generate, blurs the page, shows a spinner, and switches
               to the "Reports" tab when the run completes.
  - "Reports": vertical sidebar of saved reports grouped by sprint (14-day
               windows anchored at SPRINT_ANCHOR). The right pane embeds the
               selected report's HTML via <iframe>.

Auth:
  The SPA itself is NOT password-protected. The only password gate is
  per-report: every generated report has its own random AES-256-GCM
  encryption password. That password is returned to the user EXACTLY once
  (shown on the New tab right after the report is generated) and is then
  forgotten by the server -- nothing recoverable is persisted on disk. If
  the user fails to capture the password from the one-time display the
  report becomes unreadable for good.

Persistence:
  Each generated report is stored under reports/<sprint_dir>/<report_id>/
  with an index.html (the full encrypted report) and metadata.json.

Run with:
    python -m rp_perf_report           # no URLs -> SPA at http://localhost:9999
    python -m rp_perf_report --spa     # explicit SPA mode
"""
from __future__ import annotations

import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import urllib.error
from datetime import datetime, date, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

# Import works in two modes:
#   * package install (`python -m rp_perf_report`)  -> relative
#   * direct script (`python spa.py`)               -> absolute via sys.path[0]
try:
    from .analyzer import (
        PORT,
        REPORT_CSS_BASE,
        generate_report_for_urls,
    )
except ImportError:
    from analyzer import (  # type: ignore[no-redef]
        PORT,
        REPORT_CSS_BASE,
        generate_report_for_urls,
    )

# --------------------------------------------------------------------------- #
# Sprint window
# --------------------------------------------------------------------------- #
SPRINT_ANCHOR = date(2026, 5, 27)
SPRINT_DAYS   = 14
_LA_TZ        = ZoneInfo("America/Los_Angeles")


def sprint_window_for(d: date) -> Tuple[date, date]:
    """Return ``(start, end)`` for the 14-day sprint containing ``d``.
    ``start`` is inclusive, ``end`` is the start of the NEXT sprint (exclusive)
    so the display "May 27 - Jun 10" matches the user-stated cadence (Jun 10
    is when the next sprint begins)."""
    idx = (d - SPRINT_ANCHOR).days // SPRINT_DAYS
    start = SPRINT_ANCHOR + timedelta(days=idx * SPRINT_DAYS)
    end   = start + timedelta(days=SPRINT_DAYS)
    return start, end


def sprint_dir_name(d: date) -> str:
    s, e = sprint_window_for(d)
    return f"{s.isoformat()}_{e.isoformat()}"


def _sprint_label(start: date, end: date) -> str:
    """Human label, e.g. ``May 27 - Jun 10, 2026``."""
    if start.year == end.year:
        return f"{start.strftime('%b %-d')} - {end.strftime('%b %-d')}, {start.year}"
    return f"{start.strftime('%b %-d, %Y')} - {end.strftime('%b %-d, %Y')}"


# --------------------------------------------------------------------------- #
# Storage paths
# --------------------------------------------------------------------------- #
PACKAGE_DIR    = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR    = os.path.join(PACKAGE_DIR, "reports")
ASSETS_DIR     = os.path.join(PACKAGE_DIR, "assets")
PIPELINES_FILE = os.path.join(PACKAGE_DIR, "pipelines.json")

# Root for the Tests tab's file browser. Resolution order:
#   1. DOKIMOS_TESTS_DIR env var (explicit override, prod uses this)
#   2. First path in _TESTS_DIR_CANDIDATES that actually exists on disk
# If nothing is found, TESTS_DIR is the first candidate (so the error
# message in the UI still references a plausible path the operator can fix).
_TESTS_DIR_CANDIDATES = [
    # Common docker-mount locations in production
    "/workspace/ad-apps-test-automation/NAS_components/InventoryForecasting",
    "/workspace/NAS_components/InventoryForecasting",
    "/app/ad-apps-test-automation/NAS_components/InventoryForecasting",
    "/app/NAS_components/InventoryForecasting",
    "/srv/ad-apps-test-automation/NAS_components/InventoryForecasting",
    # Common local-dev checkouts
    os.path.expanduser("~/work/disney/ad-apps-test-automation/NAS_components/InventoryForecasting"),
    os.path.expanduser("~/work/ad-apps-test-automation/NAS_components/InventoryForecasting"),
    os.path.expanduser("~/ad-apps-test-automation/NAS_components/InventoryForecasting"),
    # Sibling of the dokimos-perf checkout (works when both repos are cloned
    # under the same parent directory, e.g. ~/work/dokimos-perf.chiron.systems
    # alongside ~/work/ad-apps-test-automation).
    os.path.join(os.path.dirname(PACKAGE_DIR),
                 "ad-apps-test-automation", "NAS_components", "InventoryForecasting"),
]


# TEMPORARY DEBUG: until production deploys are stable, the 404 response from
# /api/tests/tree includes the output of `ls -la` for a handful of likely
# parent paths, so an operator can see what's actually on the production
# filesystem from the SPA's process perspective without shell access.
_DEBUG_LS_TARGETS = [
    "~/work/disney/",
    "~/work/",
    "/workspace/",
    "/app/",
    "/srv/",
]


def _debug_filesystem_listings() -> list:
    """Run `ls -la` on each of _DEBUG_LS_TARGETS and return a list of
    {target, expanded, output} dicts. Failures (path missing, etc.) are
    rendered as the ls binary's own stderr -- exactly what a human would see
    if they typed the command. Capped at ~64 KB per listing to bound the
    response size."""
    import subprocess
    out = []
    for target in _DEBUG_LS_TARGETS:
        expanded = os.path.expanduser(target)
        try:
            r = subprocess.run(
                ["ls", "-la", expanded],
                capture_output=True, text=True, timeout=5,
            )
            text = (r.stdout or "") + (r.stderr or "")
        except FileNotFoundError:
            text = "ls binary not found"
        except subprocess.TimeoutExpired:
            text = "ls timed out after 5s"
        except Exception as exc:
            text = f"ls failed: {type(exc).__name__}: {exc}"
        if len(text) > 64 * 1024:
            text = text[:64 * 1024] + "\n... (truncated)"
        out.append({"target": target, "expanded": expanded, "output": text})
    return out


def _resolve_tests_dir() -> str:
    env = os.environ.get("DOKIMOS_TESTS_DIR", "").strip()
    if env:
        return env
    for p in _TESTS_DIR_CANDIDATES:
        if p and os.path.isdir(p):
            return p
    # Nothing found -- return the first candidate so the UI can still display
    # a coherent error message about where it was looking.
    return _TESTS_DIR_CANDIDATES[0]


TESTS_DIR = _resolve_tests_dir()

# Cap a single served file at ~2 MB so a stray binary in the tree can't
# crash the browser or starve memory.
_TESTS_FILE_MAX_BYTES = 2 * 1024 * 1024
# Directories to skip entirely when walking the tree.
_TESTS_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules",
                    ".pytest_cache", ".mypy_cache", ".ruff_cache"}
# Suffixes we refuse to serve (binary / huge / not useful as source).
_TESTS_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".jar", ".so", ".dylib", ".dll", ".exe", ".bin",
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    ".pyc", ".pyo", ".class",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".mov", ".mp3", ".wav",
    ".db", ".sqlite", ".sqlite3",
}


def _safe_test_path(rel: str) -> Optional[str]:
    """Resolve a relative path against TESTS_DIR, refusing any escape via
    symlinks or `..`. Returns the absolute path if safe, else None."""
    if not TESTS_DIR or not os.path.isdir(TESTS_DIR):
        return None
    root = os.path.realpath(TESTS_DIR)
    candidate = os.path.realpath(os.path.join(root, rel))
    # Must live strictly under root (or be root itself).
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


# --------------------------------------------------------------------------- #
# Pipelines: lightweight on-disk queue of test-run requests
# --------------------------------------------------------------------------- #
# A "pipeline" is a single requested test run -- captured from the Tests tab's
# Schedule/Run-now buttons. We persist these as a JSON list so the queue
# survives SPA restarts. There is intentionally no execution backend yet:
# pipelines created here sit in `scheduled`/`running` until an out-of-band
# runner (Docker, Spinnaker, etc.) advances them. Until then this is just a
# durable record of what was requested.

_PIPELINE_KINDS    = ("schedule", "now")
_PIPELINE_STATUSES = ("scheduled", "running", "finished", "failed")

# Default Docker image tag (overridable via env). The repo's perf README
# documents this exact tag as the canonical local build target.
DOCKER_IMAGE = os.environ.get("DOKIMOS_DOCKER_IMAGE", "ifp-fcap-perf:latest")

# Where in the InventoryForecasting tree the perf Dockerfile + README live.
# We auto-discover by scanning, but this constant is the documented spot.
_DOCKERFILE_BASENAMES = ("Dockerfile",)
_DOCKER_README_BASENAMES = ("README.md", "Readme.md", "readme.md")

# Persistence write lock -- pipeline runner threads and the scheduler can
# race on _save_pipelines() otherwise.
_PIPELINES_LOCK = None  # lazily initialised on first use to avoid import-time threading


def _pipelines_lock():
    global _PIPELINES_LOCK
    if _PIPELINES_LOCK is None:
        import threading
        _PIPELINES_LOCK = threading.Lock()
    return _PIPELINES_LOCK


def _discover_docker_assets() -> dict:
    """Walk TESTS_DIR for a Dockerfile + an adjacent README. Returns
    {'dockerfile': abs_path | None, 'readme': abs_path | None,
     'context_dir': abs_path | None, 'rel_root': abs_path | None}.

    `context_dir` is the directory the README's documented `docker build`
    expects to run from (the repo root, two levels up from
    .../perf/Dockerfile). We look for the nearest ancestor that has a
    `pyproject.toml` -- that pins it without hardcoding."""
    if not os.path.isdir(TESTS_DIR):
        return {"dockerfile": None, "readme": None, "context_dir": None, "rel_root": None}
    # Prefer perf/ subdir (canonical), fall back to anything under TESTS_DIR.
    candidates = []
    perf_dir = os.path.join(TESTS_DIR, "perf")
    if os.path.isdir(perf_dir):
        candidates.append(perf_dir)
    candidates.append(TESTS_DIR)

    found_df, found_readme = None, None
    for cand in candidates:
        for root, dirs, files in os.walk(cand):
            dirs[:] = [d for d in dirs if d not in _TESTS_SKIP_DIRS]
            if not found_df:
                for fn in files:
                    if fn in _DOCKERFILE_BASENAMES:
                        found_df = os.path.join(root, fn)
                        break
            if found_df and not found_readme:
                # README in the same directory as the Dockerfile wins.
                df_dir = os.path.dirname(found_df)
                for fn in _DOCKER_README_BASENAMES:
                    rp = os.path.join(df_dir, fn)
                    if os.path.isfile(rp):
                        found_readme = rp
                        break
            if found_df and found_readme:
                break
        if found_df and found_readme:
            break

    # Walk up from the Dockerfile until we find a pyproject.toml; that's the
    # documented `docker build` context root.
    context_dir = None
    if found_df:
        p = os.path.dirname(found_df)
        for _ in range(8):
            if os.path.isfile(os.path.join(p, "pyproject.toml")):
                context_dir = p
                break
            new_p = os.path.dirname(p)
            if new_p == p:
                break
            p = new_p

    return {"dockerfile": found_df, "readme": found_readme,
            "context_dir": context_dir, "rel_root": context_dir}


def _build_docker_command(pipeline: dict, assets: dict) -> tuple:
    """Build the `docker run` argv from a pipeline record + discovered assets.

    Follows the documented invocation in the perf README: mount tests/.envrc
    read-only so credentials don't need to live in the SPA process env, and
    pass ENV / PARALLEL_THREADS / optional TESTS+BEHAVEX_EXTRA as env vars.

    Returns (argv, envrc_host_path | None)."""
    argv = ["docker", "run", "--rm"]
    # Mount the local .envrc so secrets aren't echoed via -e flags.
    envrc_host = None
    if assets.get("rel_root"):
        envrc_candidate = os.path.join(
            assets["rel_root"],
            "NAS_components", "InventoryForecasting", "tests", ".envrc",
        )
        if os.path.isfile(envrc_candidate):
            envrc_host = envrc_candidate
            argv += ["-v", f"{envrc_host}:/secrets/.envrc:ro",
                     "-e", "PERF_ENVRC=/secrets/.envrc"]

    # Env vars from the pipeline config.
    env_pairs = [
        ("ENV",              pipeline["env"]),
        ("PARALLEL_THREADS", str(pipeline["parallel"])),
    ]
    if pipeline.get("feature"):
        env_pairs.append(("TESTS", pipeline["feature"]))
    extras = (pipeline.get("tests") or "").strip()
    if extras:
        # Whatever the user typed in the Tests filter field -- pass through
        # as raw behavex args. The entrypoint forwards BEHAVEX_EXTRA verbatim.
        env_pairs.append(("BEHAVEX_EXTRA", extras))

    for k, v in env_pairs:
        argv += ["-e", f"{k}={v}"]

    argv.append(DOCKER_IMAGE)
    return argv, envrc_host


# Regex for spotting a Report Portal launch URL in behavex output. The IFP
# suite logs lines like "Launch URL: https://ads-report-portal.../launches/all/<id>".
_RP_URL_RE = None
def _rp_url_re():
    global _RP_URL_RE
    if _RP_URL_RE is None:
        import re as _re
        _RP_URL_RE = _re.compile(r"(https?://[^\s'\"]*report-portal[^\s'\"]*launches[^\s'\"]+)")
    return _RP_URL_RE


def _append_pipeline_log(pipeline_id: str, *lines: str, **mut) -> None:
    """Atomically append log lines (and optionally mutate top-level fields)
    on a single pipeline record. Held under the persistence lock so the
    runner thread and the scheduler can't trample each other."""
    with _pipelines_lock():
        pipelines = _load_pipelines()
        for p in pipelines:
            if p.get("id") == pipeline_id:
                p.setdefault("logs", []).extend(lines)
                for k, v in mut.items():
                    p[k] = v
                break
        _save_pipelines(pipelines)


def _run_pipeline(pipeline_id: str) -> None:
    """Execute a pipeline's docker container. Runs in a worker thread.

    Hard invariant: this function NEVER lets a pipeline stay in `running`
    when it returns. Any exit path -- success, docker non-zero exit, missing
    binary, missing image, unexpected exception in the stdout-read loop --
    sets a final status (`finished` or `failed`) before returning. The
    outermost try/except ensures even a bug here can't strand a record."""
    import shutil
    import subprocess
    import traceback

    final_status = None  # set by inner paths; the outer finally enforces it.
    proc = None
    try:
        with _pipelines_lock():
            pipelines = _load_pipelines()
            rec = next((p for p in pipelines if p.get("id") == pipeline_id), None)
        if rec is None:
            return

        assets = _discover_docker_assets()
        if not assets["dockerfile"]:
            _append_pipeline_log(
                pipeline_id,
                f"[{_now_iso()}] ERROR: no Dockerfile found under {TESTS_DIR}",
                f"[{_now_iso()}] expected one of: {TESTS_DIR}/perf/Dockerfile",
                status="failed", finished_at=_now_iso(),
            )
            final_status = "failed"
            return

        if shutil.which("docker") is None:
            _append_pipeline_log(
                pipeline_id,
                f"[{_now_iso()}] ERROR: `docker` not found on PATH.",
                f"[{_now_iso()}] install Docker Desktop (or colima) and retry.",
                status="failed", finished_at=_now_iso(),
            )
            final_status = "failed"
            return

        argv, envrc = _build_docker_command(rec, assets)
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] discovered Dockerfile: {assets['dockerfile']}",
            f"[{_now_iso()}] readme: {assets['readme'] or '(none)'}",
            f"[{_now_iso()}] build context: {assets['rel_root'] or '(unknown)'}",
            f"[{_now_iso()}] mounted secrets: {envrc or '(none, expect missing-env failures)'}",
            f"[{_now_iso()}] running: {' '.join(argv)}",
            status="running",
            started_at=rec.get("started_at") or _now_iso(),
        )

        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            _append_pipeline_log(
                pipeline_id,
                f"[{_now_iso()}] ERROR: docker invocation failed (binary missing?)",
                status="failed", finished_at=_now_iso(),
            )
            final_status = "failed"
            return
        except Exception as exc:
            _append_pipeline_log(
                pipeline_id,
                f"[{_now_iso()}] ERROR: failed to start docker: {type(exc).__name__}: {exc}",
                status="failed", finished_at=_now_iso(),
            )
            final_status = "failed"
            return

        image_missing_hint_emitted = False
        rp_url_found = None
        try:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                # Surface a useful hint if Docker tells us the image isn't built.
                if (not image_missing_hint_emitted and (
                    "Unable to find image" in line or "pull access denied" in line
                )):
                    image_missing_hint_emitted = True
                    _append_pipeline_log(
                        pipeline_id,
                        line,
                        f"[{_now_iso()}] HINT: build the image first --",
                        f"[{_now_iso()}]   cd {assets['rel_root']}",
                        f"[{_now_iso()}]   DOCKER_BUILDKIT=1 docker build -f {assets['dockerfile']} -t {DOCKER_IMAGE} .",
                    )
                    continue
                # Capture the first Report Portal URL we see.
                if rp_url_found is None:
                    m = _rp_url_re().search(line)
                    if m:
                        rp_url_found = m.group(1)
                        _append_pipeline_log(pipeline_id, line, rp_url=rp_url_found)
                        continue
                _append_pipeline_log(pipeline_id, line)
        except Exception as exc:
            # Unexpected error while streaming output -- log it but make sure
            # we still drain + wait the process and mark the pipeline failed.
            _append_pipeline_log(
                pipeline_id,
                f"[{_now_iso()}] ERROR: runner streaming loop crashed: "
                f"{type(exc).__name__}: {exc}",
                f"[{_now_iso()}] traceback: {traceback.format_exc()}",
            )
            final_status = "failed"

        # Wait for the process even if streaming was interrupted.
        try:
            rc = proc.wait(timeout=30)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            rc = -1

        # If we didn't already decide to fail (no exception above), use the
        # process exit code to choose between finished and failed.
        if final_status is None:
            final_status = "finished" if rc == 0 else "failed"
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] docker exited with code {rc} -- status: {final_status}",
            status=final_status, finished_at=_now_iso(),
        )

    except Exception as exc:
        # Catch-all outer guard: anything weird (lock errors, JSON corruption,
        # whatever) lands the pipeline in `failed` rather than leaving it
        # stuck in `running` forever.
        try:
            _append_pipeline_log(
                pipeline_id,
                f"[{_now_iso()}] FATAL: runner thread crashed: "
                f"{type(exc).__name__}: {exc}",
                f"[{_now_iso()}] traceback: {traceback.format_exc()}",
                status="failed", finished_at=_now_iso(),
            )
        except Exception:
            pass
        final_status = "failed"
    finally:
        # Defence in depth: if some path above forgot to set a final status,
        # do it here. The record cannot stay in `running` after this thread.
        try:
            with _pipelines_lock():
                pipelines = _load_pipelines()
                for p in pipelines:
                    if p.get("id") == pipeline_id and p.get("status") == "running":
                        p["status"] = "failed"
                        p["finished_at"] = _now_iso()
                        p.setdefault("logs", []).append(
                            f"[{p['finished_at']}] WATCHDOG: runner exited "
                            "without setting a final status; flipping to failed."
                        )
                        _save_pipelines(pipelines)
                        break
        except Exception:
            pass


def _start_runner(pipeline_id: str) -> None:
    """Spawn a daemon thread to execute the pipeline."""
    import threading
    t = threading.Thread(target=_run_pipeline, args=(pipeline_id,),
                         daemon=True, name=f"pipeline-{pipeline_id}")
    t.start()


def _reconcile_orphan_pipelines() -> int:
    """Any pipeline marked `running` at boot is an orphan -- its runner
    thread died with the previous SPA process. The docker container itself
    was started with `--rm` so it's already gone (or will be gone shortly).
    We flip those records to `failed` so they don't sit in Running forever.

    Returns the number of records reconciled."""
    with _pipelines_lock():
        pipelines = _load_pipelines()
        n = 0
        for p in pipelines:
            if p.get("status") == "running":
                p["status"] = "failed"
                p["finished_at"] = _now_iso()
                p.setdefault("logs", []).append(
                    f"[{p['finished_at']}] ORPHANED: SPA restarted while this "
                    "pipeline was running. Runner thread did not survive the "
                    "restart, so its docker container is no longer tracked. "
                    "Marking failed. Re-trigger the run if needed."
                )
                n += 1
        if n:
            _save_pipelines(pipelines)
        return n


_SCHEDULER_STARTED = False
def _ensure_pipeline_scheduler() -> None:
    """Start the singleton scheduler thread on first call. Picks up scheduled
    pipelines whose `scheduled_for` has arrived and kicks off their runners."""
    global _SCHEDULER_STARTED
    if _SCHEDULER_STARTED:
        return
    _SCHEDULER_STARTED = True
    import threading

    def loop():
        import time as _time
        while True:
            try:
                now = datetime.now().astimezone()
                with _pipelines_lock():
                    pipelines = _load_pipelines()
                    changed = False
                    to_launch = []
                    for p in pipelines:
                        if p.get("status") != "scheduled":
                            continue
                        sf = p.get("scheduled_for")
                        if not sf:
                            continue
                        try:
                            sf_dt = datetime.fromisoformat(sf.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if sf_dt <= now:
                            p["status"] = "running"
                            p["started_at"] = _now_iso()
                            p.setdefault("logs", []).append(
                                f"[{p['started_at']}] scheduler: due, launching docker"
                            )
                            to_launch.append(p["id"])
                            changed = True
                    if changed:
                        _save_pipelines(pipelines)
                for pid in to_launch:
                    _start_runner(pid)
            except Exception:
                pass
            _time.sleep(15)

    threading.Thread(target=loop, daemon=True, name="pipeline-scheduler").start()


def _load_pipelines() -> list:
    if not os.path.isfile(PIPELINES_FILE):
        return []
    try:
        with open(PIPELINES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_pipelines(pipelines: list) -> None:
    """Atomic write so a crash mid-save doesn't truncate the file."""
    tmp = PIPELINES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pipelines, f, indent=2)
    os.replace(tmp, PIPELINES_FILE)


def _new_pipeline_id() -> str:
    return "pl_" + secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _create_pipeline(cfg: dict, iteration_index: int = 1, iteration_count: int = 1) -> dict:
    """Build a new pipeline record from the run config posted by the SPA,
    persist it, and return the full record. When part of a multi-iteration
    burst, ``iteration_index`` / ``iteration_count`` drive name suffixing
    (e.g. ``nightly-smoke-1``, ``nightly-smoke-2``)."""
    kind = cfg.get("kind") if cfg.get("kind") in _PIPELINE_KINDS else "now"
    now = datetime.now().astimezone()
    if kind == "now":
        status, started_at, scheduled_for = "running", now.isoformat(timespec="seconds"), None
    else:
        status, started_at = "scheduled", None
        # Honour a client-provided scheduled_for (from the datetime modal) if
        # it parses; otherwise fall back to "now + 1h".
        scheduled_str = cfg.get("scheduled_for")
        scheduled_for = None
        if scheduled_str:
            try:
                parsed = datetime.fromisoformat(str(scheduled_str).replace("Z", "+00:00"))
                scheduled_for = parsed.astimezone().isoformat(timespec="seconds")
            except (ValueError, TypeError):
                scheduled_for = None
        if not scheduled_for:
            scheduled_for = (now + timedelta(hours=1)).isoformat(timespec="seconds")

    name = (cfg.get("name") or "").strip()
    if not name:
        # Generate a sensible default name.
        name = f"{kind}-{now.strftime('%Y%m%d-%H%M%S')}"
    # When the user requested >1 iteration, suffix the name so each pipeline
    # is individually identifiable in the queue.
    if iteration_count > 1:
        name = f"{name}-{iteration_index}"

    record = {
        "id":            _new_pipeline_id(),
        "name":          name,
        "status":        status,
        "kind":          kind,
        "env":           (cfg.get("env") or "prod").lower(),
        "parallel":      max(1, min(6, int(cfg.get("parallel") or 2))),
        "feature":       (cfg.get("feature") or "").strip(),
        "tests":         (cfg.get("tests") or "").strip(),
        "created_at":    now.isoformat(timespec="seconds"),
        "scheduled_for": scheduled_for,
        "started_at":    started_at,
        "finished_at":   None,
        "rp_url":        None,
        "logs": [
            f"[{_now_iso()}] pipeline created: id={record_id_placeholder()}"  # noqa: F821
        ],
    }
    # Reify the log line with the real ID now that we have one.
    record["logs"][0] = (
        f"[{record['created_at']}] pipeline {record['id']} created "
        f"(kind={kind}, env={record['env']}, parallel={record['parallel']})"
    )
    if record["feature"]:
        record["logs"].append(f"[{record['created_at']}] feature: {record['feature']}")
    if record["tests"]:
        record["logs"].append(f"[{record['created_at']}] tests filter: {record['tests']}")
    if status == "running":
        record["logs"].append(f"[{record['created_at']}] state: running (waiting for runner)")
    else:
        record["logs"].append(
            f"[{record['created_at']}] state: scheduled for {scheduled_for}"
        )

    pipelines = _load_pipelines()
    pipelines.append(record)
    _save_pipelines(pipelines)
    return record


def record_id_placeholder() -> str:
    # Tiny shim so the f-string above stays readable; the value is replaced
    # immediately after with record["id"].
    return "?"


def _discover_environments() -> list:
    """Read tests/.envrc and extract environment names from variable suffixes.

    Matches variable names ending in `_dev`, `_qa`, `_staging`, `_preprod`,
    `_prod` (and similar) -- those are how the IFP test suite signals which
    environment each credential belongs to. Returns the unique env names in
    a stable canonical order (most-stable → least-stable, prod first).
    """
    import re
    canonical_order = ["prod", "preprod", "staging", "uat", "qa", "dev"]
    envrc_path = os.path.join(TESTS_DIR, "tests", ".envrc")
    if not os.path.isfile(envrc_path):
        return ["prod"]
    found: set = set()
    pat = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)
    try:
        with open(envrc_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ["prod"]
    for m in pat.finditer(text):
        var = m.group(1).lower()
        for env in canonical_order:
            if var.endswith("_" + env):
                found.add(env)
                break
    # Preserve canonical order, drop missing, ensure 'prod' is always offered
    # so the default works even if .envrc is sparse.
    out = [e for e in canonical_order if e in found]
    if "prod" not in out:
        out.insert(0, "prod")
    return out


def _build_tests_tree(root: str) -> dict:
    """Walk TESTS_DIR and return a nested dict {name, type, path, children?}.
    Folders are listed before files; both alphabetised. Skips common
    machine-generated directories so the UI stays tidy."""
    def _node(abs_path: str, rel: str) -> dict:
        name = os.path.basename(abs_path) or rel or "(root)"
        if os.path.isdir(abs_path):
            children = []
            try:
                entries = sorted(os.listdir(abs_path), key=str.lower)
            except OSError:
                entries = []
            for entry in entries:
                if entry.startswith(".DS_Store"):
                    continue
                if entry in _TESTS_SKIP_DIRS:
                    continue
                child_abs = os.path.join(abs_path, entry)
                child_rel = os.path.join(rel, entry) if rel else entry
                children.append(_node(child_abs, child_rel))
            # Folders first, then files, each alphabetical.
            children.sort(key=lambda n: (0 if n["type"] == "dir" else 1, n["name"].lower()))
            return {"name": name, "type": "dir", "path": rel, "children": children}
        return {"name": name, "type": "file", "path": rel,
                "size": os.path.getsize(abs_path) if os.path.exists(abs_path) else 0}
    return _node(os.path.realpath(root), "")


def _ensure_reports_dir() -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)


# --------------------------------------------------------------------------- #
# Legacy-report backfill
# --------------------------------------------------------------------------- #
# Reports carry the current REPORT_CSS_BASE in a single <style> block. A
# version marker (`report-css-v: N`) embedded in REPORT_CSS_BASE lets the
# backfill decide whether an on-disk report's CSS is current or needs a
# rewrite. When you bump the marker in analyzer.py, the very next call to
# serve_spa() walks every existing report and substitutes the fresh CSS
# in place (the body content stays encrypted and untouched).
_CURRENT_CSS_VERSION_RE = re.compile(r"report-css-v:\s*(\d+)")
# Pattern for the legacy "🔒" lock emoji in pre-v2 reports' auth box.
# We replace it with the textual "[ Locked ]" header so the locked screen
# matches the Grafana technical aesthetic the rest of the report adopted.
_LEGACY_LOCK_EMOJI = '<div class="auth-icon">&#128274;</div>'
_NEW_LOCK_HEADER   = '<div class="auth-icon">[ Locked ]</div>'

# Greedy-matches the OUTER <style>...</style> block; both single- and
# multi-launch reports have exactly one such block in <head>.
_STYLE_BLOCK_RE = re.compile(r"<style>(.*?)</style>", re.DOTALL)
# Pull the dynamic .timings-grid column count out of the existing CSS so we
# can preserve N when we substitute the new theme into a multi-launch report.
_LEGACY_GRID_N_RE = re.compile(
    r"\.timings-grid\s*\{[^}]*grid-template-columns:\s*repeat\(\s*(\d+)\s*,\s*1fr\s*\)",
    re.DOTALL,
)

# Marker we stamp into every backfilled report's <body> so the per-report
# "Generated:" timestamp gets re-rendered in the viewer's local timezone --
# even though the original analyzer output baked in PT wall-clock time.
# Bump _LOCALIZE_TIMES_VERSION when you change the injected script to force
# a re-injection on every existing report.
_LOCALIZE_TIMES_VERSION = 1
_LOCALIZE_SCRIPT_MARKER_RE = re.compile(
    r'data-purpose="localize-times"\s+data-v="(\d+)"'
)
_BODY_CLOSE_RE = re.compile(r"</body>\s*</html>\s*$", re.IGNORECASE)


def _localize_times_script() -> str:
    """Return the inline <script> we inject into every report so the visible
    "Generated:" timestamp matches the viewer's local timezone.

    The script is self-contained and safe to inject into any report:

    - It localizes any pre-existing ``<time data-localize="datetime">`` tags
      (these come from analyzer.py output).
    - It walks the DOM looking for raw ``Generated: YYYY-MM-DD HH:MM:SS``
      text -- which is what older encrypted payloads have baked in -- and
      converts it on the fly. We assume the embedded wall-clock time is in
      America/Los_Angeles, which is consistent with how every report was
      generated up to this point.
    - A MutationObserver on #protectedContent re-runs localization whenever
      the AES-GCM decryption injects fresh HTML.

    SECURITY-REVIEW: the script reads from the live DOM only, never from
    network, and contains no user-supplied data; it executes in the same
    origin as the rest of the report.
    """
    return (
        '<script data-purpose="localize-times" data-v="'
        + str(_LOCALIZE_TIMES_VERSION) + '">\n'
        '(function() {\n'
        '  "use strict";\n'
        '  var GEN_RE = /Generated:\\s*(\\d{4})-(\\d{2})-(\\d{2})\\s+(\\d{2}):(\\d{2}):(\\d{2})/;\n'
        '  function _laOffsetMin(year, month, day) {\n'
        '    try {\n'
        '      var probe = new Date(Date.UTC(year, month - 1, day, 12, 0, 0));\n'
        '      var fmt = new Intl.DateTimeFormat("en-US", {\n'
        '        timeZone: "America/Los_Angeles", timeZoneName: "longOffset"\n'
        '      });\n'
        '      var parts = fmt.formatToParts(probe);\n'
        '      for (var i = 0; i < parts.length; i++) {\n'
        '        if (parts[i].type === "timeZoneName") {\n'
        '          var m = parts[i].value.match(/GMT([+-])(\\d{1,2}):?(\\d{2})?/);\n'
        '          if (m) {\n'
        '            var sign = m[1] === "-" ? -1 : 1;\n'
        '            return sign * (parseInt(m[2], 10) * 60 + parseInt(m[3] || "0", 10));\n'
        '          }\n'
        '        }\n'
        '      }\n'
        '    } catch (_) {}\n'
        '    return -480;\n'
        '  }\n'
        '  function _laNaiveToDate(y, mo, d, h, mi, s) {\n'
        '    var off = _laOffsetMin(y, mo, d);\n'
        '    return new Date(Date.UTC(y, mo - 1, d, h, mi, s) - off * 60000);\n'
        '  }\n'
        '  function _fmt(d) {\n'
        '    try {\n'
        '      return new Intl.DateTimeFormat(undefined, {\n'
        '        year: "numeric", month: "2-digit", day: "2-digit",\n'
        '        hour: "2-digit", minute: "2-digit", second: "2-digit",\n'
        '        hour12: false, timeZoneName: "short"\n'
        '      }).format(d);\n'
        '    } catch (_) { return d.toString(); }\n'
        '  }\n'
        '  function _localizePre(root) {\n'
        '    var els = root.querySelectorAll("time[data-localize=\\"datetime\\"]");\n'
        '    for (var i = 0; i < els.length; i++) {\n'
        '      var el = els[i];\n'
        '      if (el.dataset.localized === "1") continue;\n'
        '      var iso = el.getAttribute("datetime");\n'
        '      if (!iso) continue;\n'
        '      var d = new Date(iso);\n'
        '      if (isNaN(d.getTime())) continue;\n'
        '      el.textContent = _fmt(d);\n'
        '      el.dataset.localized = "1";\n'
        '      if (!el.title) el.title = "Report generated at " + iso;\n'
        '    }\n'
        '  }\n'
        '  function _wrapGenerated(root) {\n'
        '    if (!root || !document.createTreeWalker) return;\n'
        '    var walker = document.createTreeWalker(\n'
        '      root, NodeFilter.SHOW_TEXT,\n'
        '      { acceptNode: function(n) {\n'
        '        if (!n.nodeValue || !GEN_RE.test(n.nodeValue)) return NodeFilter.FILTER_SKIP;\n'
        '        var p = n.parentNode;\n'
        '        while (p) {\n'
        '          if (p.tagName === "TIME") return NodeFilter.FILTER_SKIP;\n'
        '          p = p.parentNode;\n'
        '        }\n'
        '        return NodeFilter.FILTER_ACCEPT;\n'
        '      }}\n'
        '    );\n'
        '    var targets = [];\n'
        '    var cur;\n'
        '    while ((cur = walker.nextNode())) targets.push(cur);\n'
        '    for (var i = 0; i < targets.length; i++) {\n'
        '      var node = targets[i];\n'
        '      var m = GEN_RE.exec(node.nodeValue);\n'
        '      if (!m) continue;\n'
        '      var y = +m[1], mo = +m[2], d = +m[3], h = +m[4], mi = +m[5], s = +m[6];\n'
        '      var dt = _laNaiveToDate(y, mo, d, h, mi, s);\n'
        '      var iso = dt.toISOString();\n'
        '      var localized = _fmt(dt);\n'
        '      var before = node.nodeValue.substring(0, m.index) + "Generated: ";\n'
        '      var after  = node.nodeValue.substring(m.index + m[0].length);\n'
        '      var parent = node.parentNode;\n'
        '      var beforeText = document.createTextNode(before);\n'
        '      var timeEl = document.createElement("time");\n'
        '      timeEl.setAttribute("datetime", iso);\n'
        '      timeEl.setAttribute("data-localize", "datetime");\n'
        '      timeEl.dataset.localized = "1";\n'
        '      timeEl.title = "Report generated at " + iso + " (assumed PT)";\n'
        '      timeEl.textContent = localized;\n'
        '      var afterText = document.createTextNode(after);\n'
        '      parent.insertBefore(beforeText, node);\n'
        '      parent.insertBefore(timeEl, node);\n'
        '      parent.insertBefore(afterText, node);\n'
        '      parent.removeChild(node);\n'
        '    }\n'
        '  }\n'
        '  function _run(root) {\n'
        '    var r = root || document.body || document;\n'
        '    _localizePre(r);\n'
        '    _wrapGenerated(r);\n'
        '  }\n'
        '  function _init() {\n'
        '    _run(document);\n'
        '    var slot = document.getElementById("protectedContent");\n'
        '    if (slot && typeof MutationObserver !== "undefined") {\n'
        '      new MutationObserver(function() { _run(slot); })\n'
        '        .observe(slot, { childList: true, subtree: true });\n'
        '    }\n'
        '  }\n'
        '  if (document.readyState === "loading") {\n'
        '    document.addEventListener("DOMContentLoaded", _init);\n'
        '  } else {\n'
        '    _init();\n'
        '  }\n'
        '})();\n'
        '</script>'
    )


def _inject_localize_script(html: str) -> Optional[str]:
    """Return new HTML with the localize-times script injected just before
    ``</body></html>``, or ``None`` if no change is needed.

    Idempotent: if the script (at the current version) is already present
    we leave the file alone. Older versions of the script get replaced.
    """
    existing = _LOCALIZE_SCRIPT_MARKER_RE.search(html)
    if existing and int(existing.group(1)) >= _LOCALIZE_TIMES_VERSION:
        return None

    new_script = _localize_times_script()

    if existing:
        # Replace the older script block (script tag + body) with the new
        # one. Conservative: find the <script ... data-purpose="localize-times" ...>
        # element and swap it out.
        block_re = re.compile(
            r'<script[^>]*data-purpose="localize-times"[^>]*>.*?</script>',
            re.DOTALL,
        )
        if block_re.search(html):
            return block_re.sub(lambda _m: new_script, html, count=1)

    # No previous version on disk -- inject just before the closing tags.
    closing_match = _BODY_CLOSE_RE.search(html)
    if closing_match:
        insertion_point = closing_match.start()
        return html[:insertion_point] + new_script + "\n" + html[insertion_point:]

    # Last-resort fallback: append to the document.
    return html + "\n" + new_script


def _current_css_version() -> int:
    """Read the ``report-css-v: N`` marker from REPORT_CSS_BASE.

    Falling back to 0 means "no marker found" -- this would be a bug in
    analyzer.py (the marker is required), but we degrade gracefully by
    treating every on-disk report as out-of-date.
    """
    m = _CURRENT_CSS_VERSION_RE.search(REPORT_CSS_BASE)
    return int(m.group(1)) if m else 0


def _restyle_report_html(html: str) -> Optional[str]:
    """Return restyled HTML if the report's CSS is behind the current
    version, ``None`` otherwise.

    The substitution replaces the first ``<style>...</style>`` block with
    ``REPORT_CSS_BASE`` plus, for multi-launch reports, a one-line
    ``.timings-grid { grid-template-columns: repeat(N, 1fr); }`` re-emit
    that preserves the original column count.
    """
    current = _current_css_version()
    on_disk_m = _CURRENT_CSS_VERSION_RE.search(html)
    on_disk = int(on_disk_m.group(1)) if on_disk_m else 0
    if on_disk >= current:
        return None

    legacy_match = _STYLE_BLOCK_RE.search(html)
    if not legacy_match:
        return None
    legacy_body = legacy_match.group(1)

    # Preserve the dynamic column count for multi-launch reports.
    grid_match = _LEGACY_GRID_N_RE.search(legacy_body)
    grid_extra = ""
    if grid_match:
        grid_extra = (
            "\n        /* Multi-launch needs N columns; preserved from the\n"
            "           pre-restyle report so the layout doesn't collapse. */\n"
            f"        .timings-grid {{ grid-template-columns: repeat({grid_match.group(1)}, 1fr); }}"
        )

    new_style_block = f"<style>\n        {REPORT_CSS_BASE}{grid_extra}\n    </style>"
    new_html = _STYLE_BLOCK_RE.sub(new_style_block, html, count=1)
    new_html = new_html.replace(_LEGACY_LOCK_EMOJI, _NEW_LOCK_HEADER)
    return new_html


def _backfill_legacy_reports(log=print) -> dict:
    """Walk ``REPORTS_DIR`` once and bring every legacy report up to spec:

    1. Replace the report's ``<style>`` block with ``REPORT_CSS_BASE`` so
       its visuals match the current Grafana-flat theme.
    2. Inject / refresh the client-side time-localization script so old
       reports honour the viewer's local timezone too.
    3. SECURITY-REVIEW: scrub ``report_password`` from any pre-existing
       metadata.json. Earlier versions of this server persisted the
       per-report AES-256-GCM password on disk so a "reveal" button could
       redisplay it later; the new contract is that the password is shown
       exactly once on the New tab and never recoverable from the server
       after that.

    Idempotent: a report already on the current theme + script version
    and without a persisted password is left alone. Safe to call on every
    server startup.
    """
    if not os.path.isdir(REPORTS_DIR):
        return {
            "restyled": 0, "passwords_scrubbed": 0, "skipped": 0, "errors": 0,
            "localize_injected": 0,
        }

    stats = {
        "restyled": 0, "passwords_scrubbed": 0, "skipped": 0, "errors": 0,
        "localize_injected": 0,
    }

    for sprint_dirname in sorted(os.listdir(REPORTS_DIR)):
        sprint_path = os.path.join(REPORTS_DIR, sprint_dirname)
        if not os.path.isdir(sprint_path):
            continue
        for report_dirname in sorted(os.listdir(sprint_path)):
            report_path = os.path.join(sprint_path, report_dirname)
            html_path = os.path.join(report_path, "index.html")
            meta_path = os.path.join(report_path, "metadata.json")
            if not os.path.isfile(html_path):
                continue

            try:
                with open(html_path, encoding="utf-8") as fp:
                    html = fp.read()
            except OSError as e:
                log(f"[backfill] could not read {html_path}: {e}")
                stats["errors"] += 1
                continue

            mutated = False
            restyled_html = _restyle_report_html(html)
            if restyled_html is not None:
                html = restyled_html
                stats["restyled"] += 1
                mutated = True
            else:
                stats["skipped"] += 1

            localized_html = _inject_localize_script(html)
            if localized_html is not None:
                html = localized_html
                stats["localize_injected"] += 1
                mutated = True

            if mutated:
                try:
                    with open(html_path, "w", encoding="utf-8") as fp:
                        fp.write(html)
                except OSError as e:
                    log(f"[backfill] could not write {html_path}: {e}")
                    stats["errors"] += 1
                    continue

            # Scrub any persisted per-report password from metadata.json.
            # We don't want this secret sitting on disk; the only thing
            # the SPA needs from metadata.json now is the title /
            # generated_at / num_launches used by the sidebar.
            try:
                if not os.path.isfile(meta_path):
                    continue
                with open(meta_path) as fp:
                    meta = json.load(fp)
                if "report_password" in meta:
                    meta.pop("report_password", None)
                    with open(meta_path, "w") as fp:
                        json.dump(meta, fp, indent=2)
                    stats["passwords_scrubbed"] += 1
            except (OSError, ValueError) as e:
                log(f"[backfill] could not update metadata {meta_path}: {e}")
                stats["errors"] += 1
    return stats


# --------------------------------------------------------------------------- #
# Report bookkeeping
# --------------------------------------------------------------------------- #
def _new_report_id(generated_at: datetime, num_launches: int) -> str:
    """Stable, filesystem-safe id like ``20260529-114723-3launches``."""
    ts = generated_at.strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{num_launches}launches"


def _list_reports() -> dict:
    """Walk ``reports/`` and return ``{"sprints": [...]}`` ready for the
    SPA's reports sidebar. Each sprint contains its reports sorted
    newest-first; sprints are sorted newest-first too."""
    if not os.path.isdir(REPORTS_DIR):
        return {"sprints": []}

    sprints_by_dir: dict[str, list[dict]] = {}
    for sprint_dirname in os.listdir(REPORTS_DIR):
        sprint_path = os.path.join(REPORTS_DIR, sprint_dirname)
        if not os.path.isdir(sprint_path):
            continue
        for report_dirname in os.listdir(sprint_path):
            report_path = os.path.join(sprint_path, report_dirname)
            meta_path   = os.path.join(report_path, "metadata.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path) as fp:
                    meta = json.load(fp)
            except (OSError, ValueError):
                continue
            # Lazy backfill: older reports (generated before share-hash
            # was a thing) don't have one yet. Generate + persist now
            # so future requests are stable.
            if not meta.get("share_hash"):
                meta["share_hash"] = _generate_share_hash()
                try:
                    with open(meta_path, "w") as fp:
                        json.dump(meta, fp, indent=2)
                except OSError:
                    pass
            share_hash = meta["share_hash"]
            sprints_by_dir.setdefault(sprint_dirname, []).append({
                "id":           report_dirname,
                "sprint":       sprint_dirname,
                "share_hash":   share_hash,
                "share_url":    f"/r/{share_hash}",
                "title":        meta.get("title", report_dirname),
                "generated_at": meta.get("generated_at"),
                "num_launches": meta.get("num_launches"),
                "url":          f"/reports/{sprint_dirname}/{report_dirname}/index.html",
            })

    out_sprints = []
    for sprint_dirname, items in sprints_by_dir.items():
        try:
            start_iso, end_iso = sprint_dirname.split("_")
            start = date.fromisoformat(start_iso)
            end   = date.fromisoformat(end_iso)
        except ValueError:
            continue
        items.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
        out_sprints.append({
            "dirname": sprint_dirname,
            "start":   start.isoformat(),
            "end":     end.isoformat(),
            "label":   _sprint_label(start, end),
            "reports": items,
        })
    out_sprints.sort(key=lambda s: s["start"], reverse=True)
    return {"sprints": out_sprints}


def _generate_share_hash() -> str:
    """Short random URL-safe ID for shareable report links (~64 bits).

    11 url-safe base64 chars. Birthday-paradox collisions are negligible
    at any realistic report count, and the hash is opaque so it leaks no
    metadata about the report (unlike the timestamp-derived ``report_id``).
    """
    return secrets.token_urlsafe(8)


def _save_report(html: str, urls: list, analyzer_meta: dict, report_password: str,
                 dbx_log_dir: Optional[str] = None) -> dict:
    """Persist a generated report to disk and return the public list entry.

    SECURITY-REVIEW: ``report_password`` is the AES-256-GCM password the
    report was encrypted with. We deliberately do NOT persist it -- it is
    returned to the caller exactly once (so the browser can show it on the
    New tab) and then dropped. If the user fails to capture it from that
    one-time display, the report becomes permanently unreadable. This is
    the intended trust model: the password is treated as a secret that
    only the person who generated the report should know.
    """
    now_la = datetime.now(_LA_TZ)
    sprint_dirname = sprint_dir_name(now_la.date())
    sprint_path = os.path.join(REPORTS_DIR, sprint_dirname)
    os.makedirs(sprint_path, exist_ok=True)

    num_launches = int(analyzer_meta.get("num_launches", len(urls)))
    report_id = _new_report_id(now_la, num_launches)
    share_hash = _generate_share_hash()
    report_path = os.path.join(sprint_path, report_id)
    os.makedirs(report_path, exist_ok=True)

    with open(os.path.join(report_path, "index.html"), "w") as fp:
        fp.write(html)

    # Persist the uploaded Databricks log set alongside the report so the
    # source data sticks with the artifact -- useful for regenerating later,
    # or just reading a particular log file from the report's directory.
    # We copy rather than move because the caller is still responsible for
    # cleaning up the tempdir in their finally block.
    databricks_files_saved = 0
    databricks_bytes_saved = 0
    if dbx_log_dir and os.path.isdir(dbx_log_dir):
        import shutil
        dst = os.path.join(report_path, "databricks_logs")
        os.makedirs(dst, exist_ok=True)
        for fname in sorted(os.listdir(dbx_log_dir)):
            src = os.path.join(dbx_log_dir, fname)
            if not os.path.isfile(src):
                continue
            shutil.copy2(src, os.path.join(dst, fname))
            databricks_files_saved += 1
            databricks_bytes_saved += os.path.getsize(src)

    title = now_la.strftime("%Y-%m-%d %H:%M") + f"  ({num_launches} launch{'es' if num_launches != 1 else ''})"
    metadata = {
        "id":           report_id,
        "sprint":       sprint_dirname,
        "share_hash":   share_hash,
        "title":        title,
        "generated_at": now_la.isoformat(timespec="seconds"),
        "num_launches": num_launches,
        "urls":         urls,
        "analyzer":     analyzer_meta,
        "databricks": {
            "files_saved": databricks_files_saved,
            "bytes_saved": databricks_bytes_saved,
        } if databricks_files_saved else None,
    }
    with open(os.path.join(report_path, "metadata.json"), "w") as fp:
        json.dump(metadata, fp, indent=2)

    return {
        "id":             report_id,
        "sprint":         sprint_dirname,
        "share_hash":     share_hash,
        "share_url":      f"/r/{share_hash}",
        "title":          title,
        "generated_at":   metadata["generated_at"],
        "num_launches":   num_launches,
        "url":            f"/reports/{sprint_dirname}/{report_id}/index.html",
        # Returned to the caller (and ultimately the browser) ONCE on
        # generation. The server forgets it the moment this response is
        # written.
        "report_password": report_password,
    }


# --------------------------------------------------------------------------- #
# Generate-report worker
# --------------------------------------------------------------------------- #
def _generate_report_password() -> str:
    """Return a fresh URL-safe report password.

    16 url-safe base64 chars -> 96 bits of entropy, short enough to copy by
    hand but well beyond brute-force range. Generated with ``secrets`` so
    it's drawn from the OS CSPRNG.
    """
    # token_urlsafe(n) returns ~ceil(n*4/3) chars; n=12 -> 16 chars.
    return secrets.token_urlsafe(12)


def _run_generation(urls: list, payload_key: Optional[str] = None,
                    databricks_log_dir: Optional[str] = None) -> dict:
    report_password = _generate_report_password()
    html, meta = generate_report_for_urls(
        urls,
        payload_key=payload_key,
        log=lambda m: None,
        report_password=report_password,
        databricks_log_dir=databricks_log_dir,
    )
    return _save_report(html, urls, meta, report_password, dbx_log_dir=databricks_log_dir)


# Filenames the Databricks log parser knows how to read. Used to gate the
# files we'll accept from the SPA upload area -- anything else is dropped
# silently to avoid spilling stray data into the tempdir.
_DBX_FILENAME_PREFIXES = ("log4j-", "stdout", "stderr")


def _materialize_databricks_uploads(files: list) -> Optional[str]:
    """Decode a list of {name, data_b64} dicts into a fresh tempdir. Returns
    the directory path, or None if no recognized files were provided. The
    caller is responsible for shutil.rmtree() on the returned path."""
    if not isinstance(files, list) or not files:
        return None
    import base64
    import os
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="dokimos_dbx_")
    written = 0
    for f in files:
        if not isinstance(f, dict):
            continue
        name = os.path.basename(str(f.get("name") or ""))
        if not name or not name.startswith(_DBX_FILENAME_PREFIXES):
            continue
        try:
            raw = base64.b64decode(f.get("data") or "", validate=False)
        except Exception:
            continue
        with open(os.path.join(tmpdir, name), "wb") as out:
            out.write(raw)
        written += 1

    if written == 0:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None
    return tmpdir


# --------------------------------------------------------------------------- #
# Shared HTML chrome
# --------------------------------------------------------------------------- #
# NOTE: the SPA itself is no longer password-gated. Encryption applies only
# to individual reports -- each one has its own AES-256-GCM password set at
# generation time and shown to the user once on the New tab; the server
# never persists or re-displays it after that.
DOKIMOS_CSS = """\
/* Match the Chiron / Dokimos brand fonts so the inline horizontal logo
   (icon + CHIRON SYSTEMS wordmark) renders the same as on the marketing
   site (dokimos.chiron.systems). System fallbacks are listed in each
   font-family declaration below so the page is still legible offline
   while the webfonts are swapping in. */
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600;700&family=Plus+Jakarta+Sans:wght@700;800&display=swap');

:root {
    --bg-content:  #001428;
    --bg-card:     #051F3F;
    --bg-card-hi:  #0A2853;
    --bg-dark:     #010A16;
    --header-navy: #001F3F;    /* matches dokimos.chiron.systems nav bg */
    --bronze:      #CD7F32;
    --bronze-lt:   #E6A45A;
    --bronze-dk:   #8B4F1A;
    --teal:        #4FD1C5;
    --white:       #F7FAFC;
    --white-soft:  #E2E8F0;
    --gray:        #A0AEC0;
    --gray-dark:   #718096;
    --divider:     #2D3748;
    --red:         #E26565;

    --font-body:   "Calibri", "Segoe UI", system-ui, -apple-system, sans-serif;
    --font-mono:   "Courier New", "Menlo", "Monaco", monospace;
    --font-brand:  "Plus Jakarta Sans", "Segoe UI", system-ui, -apple-system, sans-serif;
    --font-brand-mono: "JetBrains Mono", "Courier New", "Menlo", monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body {
    background: var(--bg-content);
    color: var(--white);
    font-family: var(--font-body);
    font-size: 14px;
    min-height: 100vh;
}

/* ----- App chrome ----- */
.app {
    width: 100%;
    margin: 0;
    /* Header takes care of its own top padding so it can sit flush at the
       top of the viewport. Horizontal padding is applied per-section so
       the sticky header can span full bleed. */
    padding: 0 0 32px;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
}

/* Header: ported from dokimos.chiron.systems (no menu).
   80px tall, deep-navy translucent bar with backdrop blur, hairline
   divider underneath, sticky to the top of the viewport.

   3-column grid (logo | title | prototype tag) keeps the centre column
   perfectly centred in the viewport regardless of how wide the logo or
   subtitle become. */
.app-header {
    height: 80px;
    flex: 0 0 80px;
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    gap: 24px;
    padding: 0 28px;
    background: rgba(0, 31, 63, 0.9);
    -webkit-backdrop-filter: blur(12px);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
    position: sticky;
    top: 0;
    z-index: 50;
    margin-bottom: 24px;
}
.app-header > .brand-link { justify-self: start; }
.app-header > .brand      { justify-self: center; text-align: center; }
.app-header > .brand-sub  { justify-self: end; }

/* Wrapper link around the horizontal Chiron logo. Opens the marketing
   site (dokimos.chiron.systems) in a new tab. */
.app-header .brand-link {
    display: inline-flex;
    align-items: center;
    flex-shrink: 0;
    border-radius: 6px;
    text-decoration: none;
    transition: opacity 0.15s, transform 0.15s;
}
.app-header .brand-link:hover    { opacity: 0.92; transform: translateY(-1px); }
.app-header .brand-link:focus    { outline: none; }
.app-header .brand-link:focus-visible {
    outline: 2px solid var(--bronze);
    outline-offset: 4px;
}

/* Horizontal Chiron Systems logo -- icon + "CHIRON SYSTEMS" wordmark.
   Geometry mirrors <ChironLogo layout="horizontal" color="light" size={120}>
   from the dokimos repo (src/app/components/ChironLogo.tsx). */
.app-header .chiron-logo {
    display: inline-flex;
    align-items: center;
    gap: 12px;            /* tailwind gap-3 in dokimos */
    line-height: 1;
}
.app-header .chiron-logo .chiron-icon {
    width: 54px;          /* size 120 * 0.45 = 54 in dokimos component */
    height: 54px;
    display: block;
    flex-shrink: 0;
    filter: drop-shadow(0 1px 1px rgba(0, 0, 0, 0.18));
}
.app-header .chiron-logo .chiron-text {
    display: inline-flex;
    flex-direction: column;
    align-items: flex-start;
    justify-content: center;
    line-height: 1;
}
.app-header .chiron-logo .chiron-word {
    font-family: var(--font-brand);
    font-weight: 800;
    font-size: 33.6px;    /* 120 * 0.28 */
    color: var(--white);
    letter-spacing: -0.02em;
    margin-left: -0.06em; /* optical alignment for the "C" glyph */
    line-height: 1;
}
.app-header .chiron-logo .chiron-tag {
    font-family: var(--font-brand-mono);
    font-weight: 600;
    font-size: 7.8px;     /* 120 * 0.065 */
    color: var(--gray);
    letter-spacing: 0.45em;
    text-transform: uppercase;
    margin-top: 3.6px;    /* 120 * 0.03 */
}

/* Centre column: product title sharing the same font treatment as the
   "CHIRON" wordmark on the left (Plus Jakarta Sans 800) so the header
   reads as one cohesive brand strip. Sized at 2x the previous nav-link
   size for prominence -- still slightly smaller than the CHIRON glyphs
   themselves (33.6px) to keep visual hierarchy. */
.app-header .brand {
    font-family: var(--font-brand);
    font-size: 28px;
    font-weight: 800;
    color: var(--white);
    letter-spacing: -0.02em;
    line-height: 1.1;
    white-space: nowrap;
}

/* Right column: prototype / password-gate tag. */
.app-header .brand-sub {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--gray-dark);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    text-align: right;
}

/* ----- Tabs ----- */
.tabs {
    display: flex;
    gap: 4px;
    margin-bottom: 22px;
    /* Match the inner padding the header uses so tabs line up with the
       Chiron logo / brand block above them. */
    padding: 0 28px;
}
.tab-btn {
    background: transparent;
    border: 1px solid var(--divider);
    border-bottom: none;
    color: var(--gray);
    padding: 8px 22px 9px;
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border-radius: 0;
    transition: color .15s, background .15s, border-color .15s;
}
.tab-btn:hover    { color: var(--white-soft); background: var(--bg-card); }
.tab-btn.active   {
    color: var(--bronze);
    background: var(--bg-card);
    border-color: var(--divider);
    border-bottom: 1px solid var(--bg-card);
    position: relative;
    top: 1px;
}
.tab-btn .num     { color: var(--bronze); margin-right: 6px; }

/* ----- Tests tab: file tree + code viewer ----- */
.tests-layout {
    display: grid;
    grid-template-columns: 320px 1fr;
    gap: 0;
    min-height: 70vh;
    border: 1px solid var(--divider);
    border-radius: 6px;
    overflow: hidden;
}
.tests-tree {
    background: var(--bg-card);
    overflow: auto;
    padding: 10px 6px;
    font-family: var(--font-mono);
    font-size: 12px;
    border-right: 1px solid var(--divider);
    max-height: 80vh;
}
.tests-tree-loading,
.tests-tree-error {
    color: rgba(255,255,255,0.45);
    padding: 8px 10px;
}
.tests-tree-error { color: var(--red); }
.tests-tree-error code { color: rgba(255,255,255,0.75); background: rgba(255,255,255,0.04); padding: 1px 4px; border-radius: 2px; }
.tests-debug-sep    { border: none; border-top: 1px solid var(--divider); margin: 14px 0 10px; }
.tests-debug-title  {
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: var(--bronze);
    margin-bottom: 8px;
}
.tests-debug-target {
    font-family: var(--font-mono);
    font-size: 11px;
    color: rgba(255,255,255,0.70);
    margin-top: 8px;
}
.tests-debug-output {
    background: var(--bg-dark);
    border: 1px solid var(--divider);
    border-radius: 4px;
    padding: 8px 10px;
    margin: 4px 0 0;
    color: rgba(220,230,242,0.92);
    font-family: var(--font-mono);
    font-size: 10.5px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 240px;
    overflow: auto;
}
.tests-tree ul { list-style: none; margin: 0; padding-left: 14px; }
.tests-tree ul.tree-root { padding-left: 0; }
.tests-tree li { line-height: 1.6; white-space: nowrap; }
.tests-tree .tree-dir-label {
    cursor: pointer;
    color: rgba(255,255,255,0.85);
    user-select: none;
    padding: 1px 4px;
    border-radius: 3px;
}
.tests-tree .tree-dir-label:hover { background: var(--bg-card-hi); }
.tests-tree .tree-dir-label::before { content: "▾ "; color: var(--bronze); font-size: 9px; }
.tests-tree .tree-dir.collapsed > .tree-dir-label::before { content: "▸ "; }
.tests-tree .tree-dir.collapsed > .tree-children { display: none; }
.tests-tree .tree-file {
    cursor: pointer;
    color: rgba(255,255,255,0.72);
    padding: 1px 4px 1px 14px;
    border-radius: 3px;
}
.tests-tree .tree-file:hover { background: var(--bg-card-hi); color: var(--white-soft); }
.tests-tree .tree-file.active {
    background: rgba(205,127,50,0.18);
    color: var(--bronze);
}

.tests-view {
    display: flex;
    flex-direction: column;
    background: var(--bg-dark);
    overflow: hidden;
    min-width: 0;
}
.tests-view-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 14px;
    border-bottom: 1px solid var(--divider);
    background: var(--bg-card);
    font-family: var(--font-mono);
    font-size: 12px;
}
.tests-view-path {
    color: var(--white-soft);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.tests-view-size { color: rgba(255,255,255,0.45); margin-left: 12px; }
.tests-view-body {
    flex: 1;
    overflow: auto;
    margin: 0;
    padding: 14px 16px;
    font-family: var(--font-mono);
    font-size: 12.5px;
    line-height: 1.5;
    background: var(--bg-dark);
}
.tests-view-body code.hljs {
    background: transparent !important;
    padding: 0 !important;
    color: rgba(220,230,242,0.92);
}

/* Tests tab toolbar */
.tests-toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 12px 14px;
    align-items: flex-end;
    margin-bottom: 12px;
    padding: 12px 14px;
    background: var(--bg-card);
    border: 1px solid var(--divider);
    border-radius: 6px;
}
.tests-control { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.tests-control-grow { flex: 1 1 200px; }
.tests-control label {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 1.1px;
    text-transform: uppercase;
    color: rgba(255,255,255,0.5);
}
.tests-control label .opt {
    color: rgba(255,255,255,0.30);
    text-transform: none;
    letter-spacing: 0;
    margin-left: 2px;
}
.tests-control select,
.tests-control input[type="text"] {
    background: var(--bg-dark);
    color: var(--white-soft);
    border: 1px solid var(--divider);
    border-radius: 4px;
    padding: 7px 10px;
    font-family: var(--font-mono);
    font-size: 12px;
    min-width: 110px;
    outline: none;
    transition: border-color 120ms ease;
}
.tests-control input[type="text"] { width: 100%; }
.tests-control select:focus,
.tests-control input[type="text"]:focus { border-color: var(--bronze); }
.tests-toolbar-actions { display: flex; gap: 8px; align-self: flex-end; }
.tests-toolbar-actions .btn-primary,
.tests-toolbar-actions .btn-secondary {
    padding: 8px 16px;
    font-size: 11px;
    white-space: nowrap;
}
.tests-parallel-warning {
    flex-basis: 100%;
    padding: 8px 12px;
    background: rgba(245,158,11,0.10);
    border-left: 2px solid rgba(245,158,11,0.7);
    color: rgba(255,229,180,0.92);
    font-size: 11.5px;
    border-radius: 0 4px 4px 0;
    font-family: var(--font-mono);
}

/* Pipelines tab: list (left) + log viewer (right) */
.pipelines-layout {
    display: grid;
    grid-template-columns: 360px 1fr;
    min-height: 70vh;
    border: 1px solid var(--divider);
    border-radius: 6px;
    overflow: hidden;
}
.pipelines-list {
    background: var(--bg-card);
    overflow: auto;
    padding: 12px 10px;
    border-right: 1px solid var(--divider);
    max-height: 80vh;
}
.pipelines-list-loading { color: rgba(255,255,255,0.45); padding: 8px 6px; }
.pipelines-list-section {
    margin-bottom: 14px;
}
.pipelines-list-section-title {
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: rgba(255,255,255,0.45);
    padding: 4px 6px 6px;
    border-bottom: 1px solid var(--divider);
    margin-bottom: 6px;
    display: flex;
    justify-content: space-between;
    align-items: baseline;
}
.pipelines-list-section-title .count {
    background: var(--bg-card-hi);
    color: rgba(255,255,255,0.65);
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 9px;
}
.pipelines-list-empty {
    color: rgba(255,255,255,0.30);
    font-size: 11px;
    padding: 6px 8px;
    font-style: italic;
}
.pipeline-item {
    padding: 8px 10px;
    margin-bottom: 5px;
    border-radius: 4px;
    cursor: pointer;
    transition: background 100ms ease;
    border-left: 3px solid transparent;
}
.pipeline-item:hover { background: var(--bg-card-hi); }
.pipeline-item.active { background: var(--bg-card-hi); border-left-color: var(--bronze); }
.pipeline-item-name {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--white-soft);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.pipeline-item-meta {
    font-family: var(--font-mono);
    font-size: 10.5px;
    color: rgba(255,255,255,0.55);
    margin-top: 3px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.pipeline-item .rp-link {
    color: var(--bronze);
    text-decoration: none;
    border-bottom: 1px dotted var(--bronze-dk);
    font-size: 11px;
    margin-top: 5px;
    display: inline-block;
}
.pipeline-item .rp-link:hover { color: #ffa867; }
.pipeline-item.status-running   { border-left-color: #60a5fa; }
.pipeline-item.status-scheduled { border-left-color: #f59e0b; }
.pipeline-item.status-finished  { border-left-color: #10b981; }
.pipeline-item.status-failed    { border-left-color: var(--red); }

.pipelines-view {
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: var(--bg-dark);
}
.pipelines-view-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 14px;
    border-bottom: 1px solid var(--divider);
    background: var(--bg-card);
    font-family: var(--font-mono);
    font-size: 12px;
    flex-wrap: wrap;
    gap: 8px;
}
.pipelines-view-title { color: var(--white-soft); }
.pipelines-view-status {
    color: rgba(255,255,255,0.55);
    font-size: 11px;
}
.pipelines-view-status .status-pill {
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: var(--white-soft);
}
.pipelines-view-status .status-running   { background: rgba(96,165,250,0.25); color: #93c5fd; }
.pipelines-view-status .status-scheduled { background: rgba(245,158,11,0.20); color: #fcd34d; }
.pipelines-view-status .status-finished  { background: rgba(16,185,129,0.22); color: #6ee7b7; }
.pipelines-view-status .status-failed    { background: rgba(226,101,101,0.22); color: #fca5a5; }

/* Pulsing "live" indicator for running pipelines. Sits inline next to the
   pipeline name in the list and next to the status pill in the header. */
.live-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #10b981;
    box-shadow: 0 0 4px rgba(16, 185, 129, 0.55);
    margin-right: 6px;
    vertical-align: middle;
    animation: dokimos-pulse-green 1.5s ease-in-out infinite;
}
@keyframes dokimos-pulse-green {
    0%, 100% {
        opacity: 0.55;
        box-shadow: 0 0 3px rgba(16, 185, 129, 0.45),
                    0 0 0 0 rgba(16, 185, 129, 0.0);
    }
    50% {
        opacity: 1;
        box-shadow: 0 0 8px rgba(16, 185, 129, 0.95),
                    0 0 0 4px rgba(16, 185, 129, 0.12);
    }
}

/* Schedule-run modal */
.schedule-modal {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 9000;
    backdrop-filter: blur(2px);
}
.schedule-modal[hidden] { display: none; }
.schedule-modal-box {
    background: var(--bg-card);
    border: 1px solid var(--divider);
    border-radius: 8px;
    padding: 22px 26px;
    width: 100%;
    max-width: 440px;
    box-shadow: 0 18px 60px rgba(0,0,0,0.55);
}
.schedule-modal-box h3 {
    margin: 0 0 8px;
    color: var(--bronze);
    font-family: var(--font-mono);
    font-size: 14px;
    letter-spacing: 1.2px;
    text-transform: uppercase;
}
.schedule-modal-sub {
    margin: 0 0 16px;
    color: rgba(255,255,255,0.65);
    font-size: 12.5px;
    line-height: 1.5;
}
.schedule-modal-sub span { color: var(--bronze); font-family: var(--font-mono); }
.schedule-modal-label {
    display: block;
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    color: rgba(255,255,255,0.5);
    margin-bottom: 4px;
}
.schedule-modal input[type="datetime-local"] {
    width: 100%;
    background: var(--bg-dark);
    color: var(--white-soft);
    border: 1px solid var(--divider);
    border-radius: 4px;
    padding: 9px 12px;
    font-family: var(--font-mono);
    font-size: 13px;
    outline: none;
    color-scheme: dark;
}
.schedule-modal input[type="datetime-local"]:focus { border-color: var(--bronze); }
.schedule-modal-iter {
    margin: 10px 0 0;
    color: rgba(255,255,255,0.45);
    font-size: 11.5px;
    font-family: var(--font-mono);
}
.schedule-modal-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 18px;
}

/* Config strip above the log body -- shows the pipeline's params at a glance
   so the viewer doesn't have to scroll to the top of the log to remember
   what env / parallel / feature this run is. Deliberately styled as a
   distinct card so it doesn't visually blur into the log stream below. */
.pipelines-view-config {
    margin: 12px 14px 0;
    padding: 12px 14px 14px 18px;
    background: var(--bg-card);
    border: 1px solid var(--divider);
    border-left: 3px solid var(--bronze);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.25);
    position: relative;
}
.pipelines-view-config[hidden] { display: none; }
.pipelines-view-config::before {
    content: "Configuration";
    display: block;
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--bronze);
    margin-bottom: 10px;
}
.pipelines-view-config-grid {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 6px 16px;
    font-family: var(--font-mono);
    font-size: 11.5px;
    color: rgba(255,255,255,0.92);
}
.pipelines-view-config-grid strong {
    color: rgba(255,255,255,0.42);
    font-weight: 500;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    align-self: center;
}
.pipelines-view-config-grid span.val { word-break: break-all; }
.pipelines-view-config-grid .empty   { color: rgba(255,255,255,0.25); font-style: italic; }

.pipelines-view-logs {
    flex: 1;
    overflow: auto;
    margin: 0;
    padding: 14px 16px;
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.55;
    color: rgba(220,230,242,0.92);
    background: var(--bg-dark);
    white-space: pre-wrap;
    word-break: break-word;
}

/* ----- Cards (Dokimos "std_card") ----- */
.card {
    position: relative;
    background: var(--bg-card);
    border: 1px solid var(--divider);
    border-top: 2px solid var(--bronze);
    border-radius: 0;
    padding: 22px 24px;
}
.card.featured {
    background: var(--bg-card-hi);
    border: 1px solid var(--bronze);
    border-top: 2px solid var(--bronze);
}
.card h2 {
    font-family: var(--font-mono);
    font-size: 15px;
    font-weight: 700;
    color: var(--bronze);
    letter-spacing: 0.04em;
    margin-bottom: 6px;
}
.card p.subtle {
    color: var(--gray);
    font-size: 12px;
    margin-bottom: 18px;
    line-height: 1.45;
}

/* ----- Tab panel ----- */
.tab-panel { display: none; }
/* ``flex: 1`` makes the active panel consume any leftover vertical space
   inside the flex-column ``.app`` container so the footer is pinned to
   the bottom of the viewport on short pages. */
.tab-panel.active { display: block; flex: 1; padding: 0 28px; }

/* ----- "New" tab: inboxes ----- */
.tip {
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.55;
    color: var(--gray);
    background: var(--bg-dark);
    border-left: 2px solid var(--bronze);
    padding: 9px 12px;
    margin-bottom: 14px;
}
.tip .tip-label {
    color: var(--bronze);
    font-weight: 700;
    letter-spacing: 0.06em;
    margin-right: 6px;
    text-transform: uppercase;
}
.tip code {
    color: var(--white-soft);
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 2px;
}
.inboxes { display: flex; flex-direction: column; gap: 8px; margin-bottom: 18px; }
.inbox   { display: flex; gap: 8px; align-items: center; }
.inbox input {
    flex: 1;
    padding: 10px 12px;
    font-size: 13px;
    font-family: var(--font-mono);
    background: var(--bg-dark);
    border: 1px solid var(--divider);
    color: var(--white);
    outline: none;
}
.inbox input::placeholder { color: var(--gray-dark); }
.inbox input:focus { border-color: var(--bronze); background: #010E1E; }
.inbox-btn {
    width: 34px; height: 34px;
    border: 1px solid var(--divider);
    background: var(--bg-dark);
    color: var(--gray);
    font-family: var(--font-mono);
    font-size: 16px;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background .12s, color .12s, border-color .12s;
}
.inbox-btn:hover { background: var(--bg-card-hi); color: var(--white); }
.inbox-btn.add    { color: var(--bronze);    border-color: var(--bronze-dk); }
.inbox-btn.add:hover    { background: rgba(205,127,50,0.18); }
.inbox-btn.remove { color: var(--red); border-color: rgba(226,101,101,0.5); }
.inbox-btn.remove:hover { background: rgba(226,101,101,0.14); }
.inbox-btn:disabled { opacity: 0.25; cursor: not-allowed; }

/* ----- Databricks-logs upload section ----- */
.dbx-section { margin: 6px 0 22px; }
.dbx-section-head {
    display: flex; align-items: baseline; gap: 12px;
    margin-bottom: 8px;
}
.dbx-section-title {
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    color: var(--bronze);
}
.dbx-section-sub {
    font-size: 12px;
    color: rgba(255,255,255,0.45);
}
.dbx-dropzone {
    border: 1px dashed rgba(205,127,50,0.35);
    background: rgba(205,127,50,0.04);
    border-radius: 6px;
    padding: 18px 16px;
    text-align: center;
    cursor: pointer;
    transition: background 120ms ease, border-color 120ms ease;
    outline: none;
}
.dbx-dropzone:hover,
.dbx-dropzone:focus-visible {
    background: rgba(205,127,50,0.08);
    border-color: rgba(205,127,50,0.65);
}
.dbx-dropzone.dragover {
    background: rgba(205,127,50,0.15);
    border-color: var(--bronze);
    border-style: solid;
}
.dbx-dropzone-cta {
    font-family: var(--font-mono);
    font-size: 13px;
    color: rgba(255,255,255,0.85);
    margin-bottom: 4px;
}
.dbx-dropzone-hint {
    font-size: 11.5px;
    color: rgba(255,255,255,0.40);
}
.dbx-dropzone-hint code {
    background: rgba(255,255,255,0.06);
    padding: 1px 5px;
    border-radius: 3px;
    font-family: var(--font-mono);
}
.dbx-file-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 10px;
}
.dbx-file-list:empty { display: none; }
.dbx-file-chip {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 4px 6px 4px 10px;
    background: var(--bg-card-hi);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 4px;
    font-family: var(--font-mono);
    font-size: 11.5px;
    color: rgba(255,255,255,0.85);
}
.dbx-file-chip.invalid {
    border-color: rgba(226,101,101,0.45);
    color: rgba(226,101,101,0.9);
}
.dbx-file-chip-size {
    color: rgba(255,255,255,0.45);
    font-size: 11px;
}
.dbx-file-chip-remove {
    background: transparent;
    border: none;
    color: rgba(255,255,255,0.45);
    cursor: pointer;
    font-size: 14px;
    padding: 0 4px;
    line-height: 1;
}
.dbx-file-chip-remove:hover { color: var(--red); }
.dbx-file-summary {
    margin-top: 6px;
    font-size: 11.5px;
    color: rgba(255,255,255,0.55);
    font-family: var(--font-mono);
}
.dbx-folder-link {
    color: var(--bronze);
    text-decoration: none;
    border-bottom: 1px dotted rgba(205,127,50,0.6);
    padding-bottom: 1px;
}
.dbx-folder-link:hover { color: #ffa867; border-bottom-color: #ffa867; }
.busy-progress {
    width: 100%;
    height: 4px;
    background: rgba(255,255,255,0.06);
    border-radius: 2px;
    overflow: hidden;
    margin-top: 12px;
}
.busy-progress-bar {
    height: 100%;
    background: var(--bronze);
    width: 0%;
    transition: width 220ms ease;
}
.busy-progress.hidden { display: none; }

/* ----- Primary action button ----- */
.btn-primary {
    padding: 11px 26px;
    background: var(--bronze);
    border: 1px solid var(--bronze);
    color: var(--bg-dark);
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
    transition: background .12s, color .12s;
}
.btn-primary:hover { background: var(--bronze-lt); border-color: var(--bronze-lt); }
.btn-primary:disabled { background: var(--bg-card); color: var(--gray-dark); border-color: var(--divider); cursor: progress; }

.btn-secondary {
    padding: 11px 22px;
    background: transparent;
    border: 1px solid var(--divider);
    color: var(--white-soft);
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
    transition: background .12s, color .12s, border-color .12s;
}
.btn-secondary:hover { background: var(--bg-card-hi); color: var(--white); border-color: var(--gray-dark); }
.btn-secondary:disabled { opacity: 0.4; cursor: not-allowed; }

.err-line { margin-top: 12px; color: var(--red); font-size: 12px; min-height: 1.2em; }

/* ----- Password panel (shown after a successful Generate) ----- */
.pw-panel {
    margin-top: 18px;
    padding: 18px 20px;
    background: var(--bg-dark);
    border: 1px solid var(--divider);
    border-top: 2px solid var(--bronze);
}
.pw-panel[hidden] { display: none; }
.pw-heading {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--bronze);
    margin-bottom: 8px;
}
.pw-sub {
    font-size: 12px;
    color: var(--gray);
    margin-bottom: 14px;
    line-height: 1.55;
}
.pw-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 14px;
}
.pw-readout {
    flex: 1;
    padding: 10px 12px;
    font-family: var(--font-mono);
    font-size: 15px;
    letter-spacing: 0.04em;
    background: var(--bg-card);
    border: 1px solid var(--divider);
    color: var(--bronze);
    outline: none;
}
.pw-readout:focus { border-color: var(--bronze); background: #010E1E; }
.pw-actions { display: flex; gap: 10px; }

/* ----- Reports tab layout ----- */
.reports-layout {
    display: grid;
    grid-template-columns: 280px 1fr;
    gap: 12px;
    min-height: calc(100vh - 180px);
    transition: grid-template-columns .18s ease;
}
.reports-layout.sidebar-collapsed {
    /* Sidebar collapses to a thin strip with just the toggle. */
    grid-template-columns: 28px 1fr;
}
.reports-sidebar {
    background: var(--bg-card);
    border: 1px solid var(--divider);
    border-top: 2px solid var(--bronze);
    padding: 10px 12px 16px;
    overflow-y: auto;
    overflow-x: hidden;
    max-height: calc(100vh - 180px);
    position: relative;
}
.reports-layout.sidebar-collapsed .reports-sidebar {
    padding: 6px 0;
    border-top-width: 1px;
    border-left: 2px solid var(--bronze);
}
.reports-layout.sidebar-collapsed .sidebar-content { display: none; }
.reports-layout.sidebar-collapsed .sidebar-head {
    /* When collapsed only the toggle button is visible; the title text
       would overflow the 28px strip, so we hide it and let the button
       sit on its own. */
    flex-direction: column;
    padding: 4px 0;
    margin: 0;
    border-bottom: 0;
    align-items: center;
    gap: 0;
}
.reports-layout.sidebar-collapsed .sidebar-title { display: none; }

/* ----- Sidebar header (with collapse toggle) ----- */
.sidebar-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 8px;
    margin-bottom: 10px;
    border-bottom: 1px solid var(--divider);
}
.sidebar-title {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    color: var(--bronze);
    letter-spacing: 0.10em;
    text-transform: uppercase;
}
.sidebar-toggle {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    background: transparent;
    border: 1px solid var(--divider);
    color: var(--gray);
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background .12s, color .12s, border-color .12s;
}
.sidebar-toggle:hover { background: var(--bg-card-hi); color: var(--bronze); border-color: var(--bronze-dk); }
.reports-layout.sidebar-collapsed .sidebar-toggle {
    /* When collapsed, the only thing visible is the toggle, centered. */
    margin: 6px auto;
}

/* ----- Sprint groups (collapsible) ----- */
.sprint-group {
    margin-bottom: 4px;
    border-bottom: 1px solid var(--divider);
}
.sprint-group:last-child { border-bottom: 0; }
.sprint-label {
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    padding: 8px 4px;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--white-soft);
    user-select: none;
}
.sprint-label:hover { color: var(--white); }
.sprint-label.current { color: var(--bronze); }
.sprint-label .sprint-caret {
    display: inline-block;
    width: 10px;
    color: var(--gray-dark);
    font-size: 9px;
    transition: transform .15s ease;
    flex-shrink: 0;
}
.sprint-label.expanded .sprint-caret { transform: rotate(90deg); color: var(--bronze); }
.sprint-label .sprint-count {
    margin-left: auto;
    font-size: 10px;
    color: var(--gray-dark);
    font-weight: 600;
    letter-spacing: 0.04em;
}
.sprint-reports {
    display: none;
    padding: 2px 0 8px 14px;
}
.sprint-group.expanded .sprint-reports { display: block; }
.report-item {
    padding: 7px 10px;
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--white-soft);
    margin-bottom: 2px;
    transition: background .12s, color .12s;
    border-left: 2px solid transparent;
    display: flex;
    align-items: center;
    gap: 6px;
}
.report-item .report-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.report-item:hover    { background: var(--bg-card-hi); color: var(--white); }
.report-item.selected {
    background: var(--bg-card-hi);
    color: var(--bronze);
    border-left-color: var(--bronze);
}
.report-empty {
    color: var(--gray-dark);
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 28px 8px;
    text-align: center;
    line-height: 1.5;
}

.report-viewer {
    background: var(--bg-dark);
    border: 1px solid var(--divider);
    border-top: 2px solid var(--bronze);
    overflow: hidden;
    min-height: 70vh;
}
.report-viewer iframe { width: 100%; height: 100%; min-height: 75vh; border: 0; display: block; }
.viewer-empty {
    padding: 60px 30px;
    text-align: center;
    font-family: var(--font-mono);
    color: var(--gray-dark);
    font-size: 12px;
}
.viewer-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 14px;
    background: var(--bg-card);
    border-bottom: 1px solid var(--divider);
    font-family: var(--font-mono);
    font-size: 12px;
}
.viewer-title {
    color: var(--gray-dark);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.viewer-copy-btn {
    background: transparent;
    color: var(--bronze);
    border: 1px solid var(--bronze);
    padding: 4px 12px;
    font-family: var(--font-mono);
    font-size: 11px;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    flex-shrink: 0;
    transition: background 0.15s, color 0.15s;
}
.viewer-copy-btn:hover,
.viewer-copy-btn.copied { background: var(--bronze); color: #000; }

/* ----- Busy overlay ----- */
body.busy .app { filter: blur(8px); pointer-events: none; user-select: none; }
.busy-overlay {
    display: none;
    position: fixed; inset: 0;
    background: rgba(1, 10, 22, 0.7);
    backdrop-filter: blur(4px);
    z-index: 5000;
    align-items: center; justify-content: center;
}
body.busy .busy-overlay { display: flex; }
.busy-box {
    background: var(--bg-card);
    border: 1px solid var(--bronze);
    border-top: 2px solid var(--bronze);
    padding: 28px 36px;
    text-align: center;
    min-width: 300px;
}
.busy-spinner {
    width: 44px; height: 44px;
    border: 2px solid rgba(205,127,50,0.2);
    border-top-color: var(--bronze);
    border-radius: 50%;
    animation: spin .9s linear infinite;
    margin: 0 auto 14px;
}
@keyframes spin { to { transform: rotate(360deg); } }
.busy-text {
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--white);
    margin-bottom: 4px;
    letter-spacing: 0.02em;
}
.busy-sub  { font-family: var(--font-mono); font-size: 11px; color: var(--gray-dark); }

/* ----- Footer (mirrors deck convention) ----- */
.app-footer {
    margin: 32px 28px 0;
    padding-top: 14px;
    border-top: 1px solid var(--divider);
    display: flex;
    justify-content: space-between;
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--gray-dark);
    letter-spacing: 0.08em;
}
.app-footer .bronze { color: var(--bronze); font-weight: 700; }
"""


# --------------------------------------------------------------------------- #
# SPA shell HTML (served after a valid session is presented)
# --------------------------------------------------------------------------- #
SPA_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Perf Runner</title>
    <link rel="icon" href="data:,">
    <!-- highlight.js for the Tests tab code viewer. Atom One Dark matches
         the existing dokimos dark palette closely enough to look native. -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/styles/atom-one-dark.min.css">
    <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/highlight.min.js"></script>
    <style>{DOKIMOS_CSS}</style>
</head>
<body>
    <div class="app">
        <header class="app-header">
            <a class="brand-link" href="https://dokimos.chiron.systems" target="_blank" rel="noopener noreferrer" title="Open dokimos.chiron.systems">
                <span class="chiron-logo">
                    <img class="chiron-icon" src="/assets/chiron-icon-light.svg" alt="" aria-hidden="true">
                    <span class="chiron-text">
                        <span class="chiron-word">CHIRON</span>
                        <span class="chiron-tag">SYSTEMS</span>
                    </span>
                </span>
            </a>
            <span class="brand">Perf Runner</span>
            <span class="brand-sub">Prototype &middot; Encrypted reports</span>
        </header>

        <nav class="tabs">
            <button class="tab-btn active" data-tab="new"       onclick="showTab('new')"><span class="num">01.</span>New</button>
            <button class="tab-btn"        data-tab="tests"     onclick="showTab('tests')"><span class="num">02.</span>Tests</button>
            <button class="tab-btn"        data-tab="pipelines" onclick="showTab('pipelines')"><span class="num">03.</span>Pipelines</button>
            <button class="tab-btn"        data-tab="reports"   onclick="showTab('reports')"><span class="num">04.</span>Reports</button>
        </nav>

        <section class="tab-panel active" data-panel="new">
            <div class="card">
                <h2>Generate a comparison report</h2>
                <p class="subtle">Paste Report Portal launch URLs &mdash; one per inbox, or many in one inbox separated by commas. Use <strong style="color:var(--bronze)">+</strong> to add inboxes (up to 20). At <strong style="color:var(--bronze)">4+</strong> launches the report renders as four comparison tabs.</p>

                <div class="tip">
                    <span class="tip-label">Tip</span>
                    Paste a comma-separated list into a single inbox to load several launches at once, e.g.&nbsp;<code>https://&hellip;/all/1687913,&nbsp;https://&hellip;/all/1687938,&nbsp;https://&hellip;/all/1687964</code>. Whitespace around commas is fine.
                </div>

                <div class="inboxes" id="inboxes"></div>

                <div class="dbx-section" id="dbxSection">
                    <div class="dbx-section-head">
                        <span class="dbx-section-title">Databricks logs (optional)</span>
                        <span class="dbx-section-sub">Adds a Timeline tab correlating job phases &amp; slow requests.</span>
                    </div>
                    <div class="dbx-dropzone" id="dbxDropzone" tabindex="0"
                         onclick="document.getElementById('dbxFileInput').click()"
                         onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();document.getElementById('dbxFileInput').click();}}">
                        <input type="file" id="dbxFileInput" multiple
                               accept=".log,.gz,.txt"
                               style="display:none">
                        <input type="file" id="dbxFolderInput" multiple
                               webkitdirectory directory
                               style="display:none">
                        <div class="dbx-dropzone-cta">Drop files or a folder here, or click to browse</div>
                        <div class="dbx-dropzone-hint">
                            <a href="#" class="dbx-folder-link" onclick="event.preventDefault();event.stopPropagation();document.getElementById('dbxFolderInput').click();">Pick a folder instead</a>
                            &nbsp;&middot;&nbsp; Accepts <code>log4j-*.log[.gz]</code>, <code>stdout*.txt</code>, <code>stderr*.txt</code>. Other files are ignored.
                        </div>
                    </div>
                    <div class="dbx-file-list" id="dbxFileList"></div>
                </div>

                <button class="btn-primary" id="generateBtn" onclick="generateReport()">Generate Report</button>
                <div class="err-line" id="newError"></div>

                <!-- Shown ONCE per generation, replacing the form area until the
                     user clicks "Show Report" or returns to the form. Holds the
                     fresh per-report AES password (the only chance to copy it
                     down before the iframe gate asks for it). -->
                <div class="pw-panel" id="pwPanel" hidden>
                    <div class="pw-heading">REPORT PASSWORD &mdash; SAVE THIS NOW</div>
                    <p class="pw-sub">This password unlocks the report you just generated. Copy it before you press <strong style="color:var(--bronze)">Show Report</strong> &mdash; the report itself is encrypted with it and will ask for it on open.</p>
                    <div class="pw-row">
                        <input type="text" class="pw-readout" id="pwReadout" readonly aria-label="Generated report password">
                        <button class="btn-secondary" type="button" id="pwCopyBtn" onclick="copyGeneratedPassword()">Copy</button>
                    </div>
                    <div class="pw-actions">
                        <button class="btn-primary"   type="button" id="pwShowBtn"  onclick="showGeneratedReport()">Show Report</button>
                        <button class="btn-secondary" type="button" id="pwBackBtn"  onclick="resetGenerateUI()">Generate Another</button>
                    </div>
                </div>
            </div>
        </section>

        <section class="tab-panel" data-panel="tests">
            <div class="tests-toolbar">
                <div class="tests-control">
                    <label>Environment</label>
                    <select id="testsEnv"><option value="prod" selected>prod</option></select>
                </div>
                <div class="tests-control">
                    <label>Parallel</label>
                    <select id="testsParallel">
                        <option value="2" selected>2</option>
                        <option value="3">3</option>
                        <option value="4">4</option>
                        <option value="5">5</option>
                        <option value="6">6</option>
                    </select>
                </div>
                <div class="tests-control">
                    <label>Iterations</label>
                    <input type="number" id="testsIterations" min="1" max="50" value="1"
                           title="Number of identical pipelines to enqueue">
                </div>
                <div class="tests-control tests-control-grow">
                    <label>Name <span class="opt">(optional)</span></label>
                    <input type="text" id="testsName" placeholder="e.g. nightly-smoke-2026-06-03">
                </div>
                <div class="tests-control tests-control-grow">
                    <label>Feature path <span class="opt">(optional)</span></label>
                    <input type="text" id="testsFeature" placeholder="tests/api_tests/some.feature">
                </div>
                <div class="tests-control tests-control-grow">
                    <label>Tests <span class="opt">(optional, e.g. --tags ~@wip)</span></label>
                    <input type="text" id="testsFilter" placeholder="--tags ~@wip">
                </div>
                <div class="tests-toolbar-actions">
                    <button class="btn-secondary" onclick="_dispatchRun('schedule')">Schedule run</button>
                    <button class="btn-primary"   onclick="_dispatchRun('now')">Run now</button>
                </div>
                <div class="tests-parallel-warning" id="testsParallelWarning" hidden>
                    Parallel &gt; 3 exceeds the local hard cap — expect "session not created" Chrome contention.
                </div>
            </div>
            <div class="tests-layout">
                <aside class="tests-tree" id="testsTreePanel">
                    <div class="tests-tree-loading">Loading tree&hellip;</div>
                </aside>
                <main class="tests-view">
                    <div class="tests-view-header">
                        <span class="tests-view-path" id="testsViewPath">Select a file from the tree</span>
                        <span class="tests-view-size" id="testsViewSize"></span>
                    </div>
                    <pre class="tests-view-body"><code id="testsViewCode" class="hljs">// Pick a file on the left to view its contents.</code></pre>
                </main>
            </div>
        </section>

        <section class="tab-panel" data-panel="pipelines">
            <div class="pipelines-layout">
                <aside class="pipelines-list" id="pipelinesListPanel">
                    <div class="pipelines-list-loading">Loading pipelines&hellip;</div>
                </aside>
                <main class="pipelines-view">
                    <div class="pipelines-view-header">
                        <span class="pipelines-view-title" id="pipelinesViewTitle">Select a pipeline on the left</span>
                        <span class="pipelines-view-status" id="pipelinesViewStatus"></span>
                    </div>
                    <div class="pipelines-view-config" id="pipelinesViewConfig" hidden></div>
                    <pre class="pipelines-view-logs" id="pipelinesViewLogs">No pipeline selected.</pre>
                </main>
            </div>
        </section>

        <section class="tab-panel" data-panel="reports">
            <div class="reports-layout" id="reportsLayout">
                <aside class="reports-sidebar" id="reportsSidebar">
                    <div class="sidebar-head">
                        <div class="sidebar-title">Sprints</div>
                        <button class="sidebar-toggle" type="button"
                                id="sidebarToggle"
                                title="Collapse sidebar"
                                onclick="toggleSidebar()">&laquo;</button>
                    </div>
                    <div class="sidebar-content" id="sidebarContent">
                        <div class="report-empty">Loading reports&hellip;</div>
                    </div>
                </aside>
                <div class="report-viewer" id="reportViewer">
                    <div class="viewer-empty">Pick a report from the sidebar.</div>
                </div>
            </div>
        </section>

        <footer class="app-footer">
            <span>DOKIMOS &middot; PERFORMANCE</span>
            <span>&copy; {datetime.now().year} &middot; DOKIMOS.<span class="bronze">CHIRON</span>.SYSTEMS &middot; ALL RIGHTS RESERVED</span>
        </footer>
    </div>

    <div class="busy-overlay" id="busyOverlay">
        <div class="busy-box">
            <div class="busy-spinner"></div>
            <div class="busy-text" id="busyText">Generating report&hellip;</div>
            <div class="busy-sub" id="busySub">Fetching Report Portal logs &amp; rendering HTML</div>
            <div class="busy-progress hidden" id="busyProgress"><div class="busy-progress-bar" id="busyProgressBar"></div></div>
        </div>
    </div>

    <!-- Schedule-run modal: shown when the user clicks "Schedule run" on
         the Tests tab. The datetime-local input is interpreted in the
         browser's local timezone; we convert to ISO-with-offset on confirm
         so the server stores an unambiguous instant. -->
    <div class="schedule-modal" id="scheduleModal" hidden>
        <div class="schedule-modal-box">
            <h3>Schedule run</h3>
            <p class="schedule-modal-sub">Pick when these pipelines should start. Time is in your browser's local timezone (<span id="scheduleTzHint"></span>).</p>
            <label class="schedule-modal-label" for="scheduleDateTime">Run at</label>
            <input type="datetime-local" id="scheduleDateTime" step="60">
            <div class="schedule-modal-iter" id="scheduleIterHint"></div>
            <div class="schedule-modal-actions">
                <button class="btn-secondary" type="button" onclick="_cancelSchedule()">Cancel</button>
                <button class="btn-primary"   type="button" onclick="_confirmSchedule()">Schedule</button>
            </div>
        </div>
    </div>

    <script>
        const MAX_INBOXES = 20;
        // Single user-facing error string -- never leak HTTP status codes,
        // server stack traces, or fetch-thrown messages. Client-side
        // validation messages (empty input, too many URLs) stay verbose
        // because they tell the user how to fix the problem; everything
        // else falls through to this line.
        const GENERIC_ERROR = 'Network problem or no access to report service.';

        let _reportList = {{sprints: []}};
        let _selectedId = null;
        // Holds the most-recently-generated report so "Show Report" can
        // navigate to it. Cleared by resetGenerateUI().
        let _pendingReport = null;

        function showTab(name, opts) {{
            document.querySelectorAll('.tab-btn').forEach(function(b) {{
                b.classList.toggle('active', b.getAttribute('data-tab') === name);
            }});
            document.querySelectorAll('.tab-panel').forEach(function(p) {{
                p.classList.toggle('active', p.getAttribute('data-panel') === name);
            }});
            if (name === 'reports') refreshReports();
            if (name === 'tests') {{ _loadTestsTree(); _loadTestsEnvs(); }}
            if (name === 'pipelines') {{ _enterPipelinesTab(); }}
            else _stopPipelinesPolling();
            // Keep the URL in sync with the active tab (but don't push a
            // history entry when we're just *reading* a URL on load).
            if (!opts || !opts.fromPop) {{
                let target;
                if (name === 'new') target = '/';
                else if (name === 'pipelines' && _selectedPipelineId) target = '/pipelines/' + _selectedPipelineId;
                else target = '/' + name;
                if (target !== window.location.pathname) {{
                    history.pushState({{tab: name, pid: _selectedPipelineId}}, '', target);
                }}
            }}
        }}

        async function _loadTestsEnvs() {{
            const sel = document.getElementById('testsEnv');
            if (!sel || sel.dataset.loaded) return;
            try {{
                const res = await fetch('/api/tests/envs');
                if (!res.ok) return;
                const data = await res.json();
                const envs = (data.environments || []);
                if (!envs.length) return;
                const prior = sel.value || 'prod';
                sel.innerHTML = envs.map(function(e) {{
                    return '<option value="' + e + '"' + (e === 'prod' ? ' selected' : '') + '>' + e + '</option>';
                }}).join('');
                // Preserve previous selection if still present, else default to prod.
                if (envs.indexOf(prior) >= 0) sel.value = prior;
                else if (envs.indexOf('prod') >= 0) sel.value = 'prod';
                sel.dataset.loaded = '1';
            }} catch (e) {{ /* leave default 'prod' */ }}
        }}

        function _testsParallelCheck() {{
            const sel = document.getElementById('testsParallel');
            const warn = document.getElementById('testsParallelWarning');
            if (!sel || !warn) return;
            warn.hidden = parseInt(sel.value, 10) <= 3;
        }}

        function _readRunForm() {{
            let iters = parseInt(document.getElementById('testsIterations').value, 10);
            if (!Number.isFinite(iters) || iters < 1) iters = 1;
            if (iters > 50) iters = 50;
            return {{
                env:        document.getElementById('testsEnv').value,
                parallel:   parseInt(document.getElementById('testsParallel').value, 10),
                name:       document.getElementById('testsName').value.trim(),
                feature:    document.getElementById('testsFeature').value.trim(),
                tests:      document.getElementById('testsFilter').value.trim(),
                iterations: iters,
            }};
        }}

        async function _dispatchRun(kind) {{
            if (kind === 'schedule') {{
                _openScheduleModal();
                return;
            }}
            await _submitRun({{kind: 'now', scheduled_for: null}});
        }}

        async function _submitRun(extras) {{
            const cfg = Object.assign(_readRunForm(), extras || {{}});
            try {{
                const res = await fetch('/api/pipelines', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(cfg),
                }});
                if (res.ok) {{
                    const data = await res.json();
                    const list = (data && data.pipelines) ? data.pipelines : [];
                    if (list.length) _selectedPipelineId = list[0].id;
                }}
            }} catch (_e) {{ /* tab will still navigate so the user sees the queue */ }}
            showTab('pipelines');
        }}

        // ----- Schedule-run modal -----
        function _pad2(n) {{ return String(n).padStart(2, '0'); }}
        function _localDateTimeInputValue(date) {{
            return date.getFullYear() + '-' + _pad2(date.getMonth() + 1) + '-' + _pad2(date.getDate())
                 + 'T' + _pad2(date.getHours()) + ':' + _pad2(date.getMinutes());
        }}
        function _openScheduleModal() {{
            const modal = document.getElementById('scheduleModal');
            const input = document.getElementById('scheduleDateTime');
            const tzHint = document.getElementById('scheduleTzHint');
            const iterHint = document.getElementById('scheduleIterHint');
            if (!modal || !input) return;
            // Default to now + 1h.
            const def = new Date(Date.now() + 60 * 60 * 1000);
            input.value = _localDateTimeInputValue(def);
            // Minimum: now (no scheduling in the past).
            input.min = _localDateTimeInputValue(new Date());
            // Surface the user's locale-friendly timezone label.
            if (tzHint) {{
                try {{
                    tzHint.textContent = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
                }} catch (_e) {{ tzHint.textContent = ''; }}
            }}
            if (iterHint) {{
                const it = _readRunForm().iterations;
                iterHint.textContent = it > 1
                    ? ('Will enqueue ' + it + ' identical pipelines for this start time.')
                    : '';
            }}
            modal.hidden = false;
            setTimeout(function() {{ try {{ input.focus(); }} catch (_e) {{}} }}, 30);
        }}
        function _cancelSchedule() {{
            const modal = document.getElementById('scheduleModal');
            if (modal) modal.hidden = true;
        }}
        async function _confirmSchedule() {{
            const input = document.getElementById('scheduleDateTime');
            if (!input || !input.value) return;
            // datetime-local has no timezone -- the browser will treat the
            // value as local time when constructed via `new Date(string)`,
            // which is exactly what we want. toISOString() then produces a
            // UTC instant the server stores unambiguously.
            const localDate = new Date(input.value);
            if (isNaN(localDate.getTime())) return;
            _cancelSchedule();
            await _submitRun({{kind: 'schedule', scheduled_for: localDate.toISOString()}});
        }}

        // ----- Pipelines tab -----
        let _selectedPipelineId = null;
        let _pipelinesPollTimer = null;
        let _logsPollTimer      = null;

        function _humanWhen(iso) {{
            if (!iso) return '';
            try {{
                const d = new Date(iso);
                return d.toLocaleString();
            }} catch (e) {{ return iso; }}
        }}

        function _pipelineItemHtml(p) {{
            const meta = [
                'env=' + p.env,
                'parallel=' + p.parallel,
                p.kind === 'schedule' ? 'kind=scheduled' : 'kind=now',
            ].join(' · ');
            const timing = (p.status === 'scheduled')
                ? 'scheduled for ' + _humanWhen(p.scheduled_for)
                : (p.status === 'finished')
                    ? 'finished ' + _humanWhen(p.finished_at)
                    : (p.status === 'running')
                        ? 'started ' + _humanWhen(p.started_at || p.created_at)
                        : 'created ' + _humanWhen(p.created_at);
            const rp = p.rp_url
                ? '<a class="rp-link" href="' + _testsEscape(p.rp_url) + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">Report Portal &rarr;</a>'
                : (p.status === 'finished' ? '<span class="rp-link" style="border-bottom-style:dotted;opacity:.5">RP link pending</span>' : '');
            const cls = 'pipeline-item status-' + p.status + (p.id === _selectedPipelineId ? ' active' : '');
            // Pulsing green dot for the running ones -- visible-at-a-glance
            // signal that something is actually in flight.
            const liveDot = (p.status === 'running') ? '<span class="live-dot" title="running"></span>' : '';
            return '<div class="' + cls + '" onclick="_selectPipeline(\\'' + _testsEscape(p.id) + '\\')">'
                 +   '<div class="pipeline-item-name">' + liveDot + _testsEscape(p.name) + '</div>'
                 +   '<div class="pipeline-item-meta">' + _testsEscape(meta) + '</div>'
                 +   '<div class="pipeline-item-meta">' + _testsEscape(timing) + '</div>'
                 +   (rp ? '<div>' + rp + '</div>' : '')
                 + '</div>';
        }}

        function _renderPipelinesList(pipelines) {{
            const panel = document.getElementById('pipelinesListPanel');
            if (!panel) return;
            const buckets = {{ running: [], scheduled: [], finished: [], failed: [] }};
            (pipelines || []).forEach(function(p) {{
                (buckets[p.status] || (buckets.finished)).push(p);
            }});
            // Sort: running by start desc, scheduled by scheduled_for asc, finished by finished_at desc.
            buckets.running.sort(function(a, b) {{ return (b.started_at || '').localeCompare(a.started_at || ''); }});
            buckets.scheduled.sort(function(a, b) {{ return (a.scheduled_for || '').localeCompare(b.scheduled_for || ''); }});
            buckets.finished.sort(function(a, b) {{ return (b.finished_at || b.created_at || '').localeCompare(a.finished_at || a.created_at || ''); }});
            buckets.failed.sort(function(a, b) {{ return (b.finished_at || b.created_at || '').localeCompare(a.finished_at || a.created_at || ''); }});

            function section(label, items) {{
                const body = items.length
                    ? items.map(_pipelineItemHtml).join('')
                    : '<div class="pipelines-list-empty">none</div>';
                return '<div class="pipelines-list-section">'
                     +   '<div class="pipelines-list-section-title">' + label
                     +     ' <span class="count">' + items.length + '</span>'
                     +   '</div>'
                     +   body
                     + '</div>';
            }}
            panel.innerHTML =
                  section('Running',   buckets.running)
                + section('Scheduled', buckets.scheduled)
                + section('Finished',  buckets.finished)
                + section('Failed',    buckets.failed);
        }}

        async function _refreshPipelines() {{
            try {{
                const res = await fetch('/api/pipelines');
                if (!res.ok) return;
                const data = await res.json();
                _renderPipelinesList(data.pipelines || []);
            }} catch (_e) {{ /* leave previous render */ }}
        }}

        async function _selectPipeline(id, opts) {{
            _selectedPipelineId = id;
            // Highlight in list immediately.
            document.querySelectorAll('.pipeline-item.active').forEach(function(e) {{
                e.classList.remove('active');
            }});
            document.querySelectorAll('.pipeline-item').forEach(function(e) {{
                if (e.getAttribute('onclick') && e.getAttribute('onclick').indexOf(id) >= 0) {{
                    e.classList.add('active');
                }}
            }});
            await _refreshLogs();
            if (_logsPollTimer) clearInterval(_logsPollTimer);
            _logsPollTimer = setInterval(_refreshLogs, 4000);
            // Deep-link this pipeline in the URL bar so a copy/paste of the
            // link opens the same view next time.
            if (!opts || !opts.fromPop) {{
                const target = '/pipelines/' + id;
                if (window.location.pathname !== target) {{
                    history.pushState({{tab: 'pipelines', pid: id}}, '', target);
                }}
            }}
        }}

        async function _refreshLogs() {{
            if (!_selectedPipelineId) return;
            try {{
                const res = await fetch('/api/pipelines/' + encodeURIComponent(_selectedPipelineId) + '/logs');
                const data = await res.json();
                const title = document.getElementById('pipelinesViewTitle');
                const status = document.getElementById('pipelinesViewStatus');
                const logs = document.getElementById('pipelinesViewLogs');
                if (!res.ok) {{
                    if (logs) logs.textContent = data.error || ('HTTP ' + res.status);
                    return;
                }}
                if (title) title.textContent = data.name ? (data.name + '  (' + data.id + ')') : data.id;
                if (status) {{
                    const cls = 'status-pill status-' + (data.status || '');
                    const rp = data.rp_url
                        ? ' &middot; <a class="rp-link" href="' + _testsEscape(data.rp_url) + '" target="_blank" rel="noopener noreferrer">Report Portal &rarr;</a>'
                        : '';
                    const live = (data.status === 'running')
                        ? '<span class="live-dot" title="running"></span>'
                        : '';
                    status.innerHTML = live + '<span class="' + cls + '">' + (data.status || '') + '</span>' + rp;
                }}
                // Render the config strip above the log body.
                const cfg = document.getElementById('pipelinesViewConfig');
                if (cfg) {{
                    const rows = [
                        ['name',         data.name],
                        ['id',           data.id],
                        ['env',          data.env],
                        ['parallel',     data.parallel],
                        ['kind',         data.kind],
                        ['feature',      data.feature || ''],
                        ['tests',        data.tests || ''],
                        ['created',      data.created_at],
                        ['scheduled',    data.scheduled_for || ''],
                        ['started',      data.started_at || ''],
                        ['finished',     data.finished_at || ''],
                    ];
                    cfg.innerHTML = '<div class="pipelines-view-config-grid">'
                        + rows.map(function(r) {{
                            const v = (r[1] == null || r[1] === '')
                                ? '<span class="empty">&mdash;</span>'
                                : ('<span class="val">' + _testsEscape(String(r[1])) + '</span>');
                            return '<strong>' + r[0] + '</strong>' + v;
                        }}).join('')
                        + '</div>';
                    cfg.hidden = false;
                }}
                if (logs) {{
                    const text = (data.logs || []).join('\\n');
                    const wasNearBottom = (logs.scrollHeight - logs.scrollTop - logs.clientHeight) < 60;
                    logs.textContent = text || '(no log lines yet)';
                    if (wasNearBottom) logs.scrollTop = logs.scrollHeight;
                }}
            }} catch (_e) {{ /* leave previous render */ }}
        }}

        function _stopPipelinesPolling() {{
            if (_pipelinesPollTimer) {{ clearInterval(_pipelinesPollTimer); _pipelinesPollTimer = null; }}
            if (_logsPollTimer)      {{ clearInterval(_logsPollTimer);      _logsPollTimer = null; }}
        }}

        async function _enterPipelinesTab() {{
            await _refreshPipelines();
            if (_selectedPipelineId) await _selectPipeline(_selectedPipelineId);
            if (_pipelinesPollTimer) clearInterval(_pipelinesPollTimer);
            _pipelinesPollTimer = setInterval(_refreshPipelines, 5000);
        }}

        // ----- Tests tab: tree + code viewer -----
        // Suffix -> highlight.js language hint. Falls back to auto-detect.
        const _TESTS_LANG_MAP = {{
            'py': 'python', 'feature': 'gherkin', 'yaml': 'yaml', 'yml': 'yaml',
            'json': 'json', 'md': 'markdown', 'sh': 'bash', 'sql': 'sql',
            'js': 'javascript', 'ts': 'typescript', 'html': 'xml', 'xml': 'xml',
            'css': 'css', 'toml': 'ini', 'conf': 'ini', 'cfg': 'ini',
            'ini': 'ini', 'env': 'bash', 'rb': 'ruby', 'go': 'go', 'rs': 'rust',
        }};
        let _testsTreeLoaded = false;

        function _testsEscape(s) {{
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }}
        function _renderTreeNode(node) {{
            if (node.type === 'file') {{
                const safePath = _testsEscape(node.path);
                return '<li class="tree-file" data-path="' + safePath + '" '
                     +     'onclick="_loadTestFile(this, this.getAttribute(\\'data-path\\'))">'
                     +   _testsEscape(node.name)
                     + '</li>';
            }}
            const kids = (node.children || []).map(_renderTreeNode).join('');
            return '<li class="tree-dir">'
                 +   '<div class="tree-dir-label" onclick="this.parentNode.classList.toggle(\\'collapsed\\')">'
                 +     _testsEscape(node.name)
                 +   '</div>'
                 +   '<ul class="tree-children">' + kids + '</ul>'
                 + '</li>';
        }}
        async function _loadTestsTree() {{
            if (_testsTreeLoaded) return;
            const panel = document.getElementById('testsTreePanel');
            if (!panel) return;
            try {{
                const res = await fetch('/api/tests/tree');
                if (!res.ok) {{
                    // 404 from server includes the configured path + the
                    // candidate paths it searched. We also surface the
                    // debug `ls -la` output so production operators can
                    // see what's actually on disk without shell access.
                    let body = 'Tree fetch failed: HTTP ' + res.status;
                    try {{
                        const data = await res.json();
                        if (data && data.error) {{
                            body = _testsEscape(data.error);
                            if (data.configured) body += '<br><br>configured: <code>' + _testsEscape(data.configured) + '</code>';
                            if (data.hint) body += '<br><br>' + _testsEscape(data.hint);
                            if (Array.isArray(data.searched) && data.searched.length) {{
                                body += '<br><br>searched paths:<br>'
                                     + data.searched.map(function(s) {{
                                         return '&nbsp;&nbsp;<code>' + _testsEscape(s) + '</code>';
                                       }}).join('<br>');
                            }}
                            // TEMPORARY DEBUG: show `ls -la` of likely parents.
                            if (Array.isArray(data.debug_ls) && data.debug_ls.length) {{
                                body += '<hr class="tests-debug-sep"><div class="tests-debug-title">filesystem snapshot (temporary debug)</div>';
                                data.debug_ls.forEach(function(item) {{
                                    body += '<div class="tests-debug-target">$ ls -la '
                                         + _testsEscape(item.target) + '</div>'
                                         + '<pre class="tests-debug-output">'
                                         + _testsEscape(item.output || '(empty)')
                                         + '</pre>';
                                }});
                            }}
                        }}
                    }} catch (_e) {{ /* fall through */ }}
                    panel.innerHTML = '<div class="tests-tree-error">' + body + '</div>';
                    return;
                }}
                const data = await res.json();
                panel.innerHTML = '<ul class="tree-root">' + _renderTreeNode(data.tree) + '</ul>';
                _testsTreeLoaded = true;
            }} catch (e) {{
                panel.innerHTML = '<div class="tests-tree-error">' + _testsEscape(e.message || String(e)) + '</div>';
            }}
        }}
        async function _loadTestFile(el, path) {{
            document.querySelectorAll('.tests-tree .tree-file.active').forEach(function(e) {{
                e.classList.remove('active');
            }});
            if (el) el.classList.add('active');
            // Clicking a .feature file auto-fills the toolbar's Feature path
            // (only when it's empty so a manual entry isn't clobbered).
            if (path && path.endsWith('.feature')) {{
                const feat = document.getElementById('testsFeature');
                if (feat && !feat.value) feat.value = path;
            }}
            const code = document.getElementById('testsViewCode');
            const pathEl = document.getElementById('testsViewPath');
            const sizeEl = document.getElementById('testsViewSize');
            if (!code || !pathEl || !sizeEl) return;
            pathEl.textContent = path;
            sizeEl.textContent = 'loading…';
            code.textContent = '';
            code.className = 'hljs';
            try {{
                const res = await fetch('/api/tests/file?path=' + encodeURIComponent(path));
                const data = await res.json();
                if (!res.ok) {{
                    code.textContent = data.error || ('HTTP ' + res.status);
                    sizeEl.textContent = data.size ? (data.size + ' bytes') : '';
                    return;
                }}
                sizeEl.textContent = data.size + ' bytes';
                code.textContent = data.content;
                const lang = _TESTS_LANG_MAP[data.suffix] || '';
                code.className = lang ? ('hljs language-' + lang) : 'hljs';
                if (window.hljs) {{
                    delete code.dataset.highlighted;
                    hljs.highlightElement(code);
                }}
            }} catch (e) {{
                code.textContent = 'error: ' + (e.message || String(e));
                sizeEl.textContent = '';
            }}
        }}

        function renderInboxes(initial) {{
            const root = document.getElementById('inboxes');
            root.innerHTML = '';
            const values = initial || [''];
            values.forEach(function(v) {{ appendInbox(v); }});
        }}

        function appendInbox(value) {{
            const root = document.getElementById('inboxes');
            if (root.children.length >= MAX_INBOXES) return;
            const row = document.createElement('div');
            row.className = 'inbox';
            row.innerHTML =
                '<input type="text" class="inbox-input" placeholder="Report Portal launch URL" value="">' +
                '<button class="inbox-btn remove" type="button" title="Remove">&minus;</button>' +
                '<button class="inbox-btn add"    type="button" title="Add">+</button>';
            if (typeof value === 'string') row.querySelector('input').value = value;
            row.querySelector('.add').addEventListener('click', function() {{
                appendInbox();
                refreshInboxButtons();
            }});
            row.querySelector('.remove').addEventListener('click', function() {{
                if (root.children.length === 1) return;
                row.remove();
                refreshInboxButtons();
            }});
            root.appendChild(row);
            refreshInboxButtons();
        }}

        function refreshInboxButtons() {{
            const root = document.getElementById('inboxes');
            const rows = root.querySelectorAll('.inbox');
            rows.forEach(function(row, i) {{
                row.querySelector('.remove').disabled = (i === 0);
                const isLast = (i === rows.length - 1);
                row.querySelector('.add').style.visibility = isLast ? 'visible' : 'hidden';
                row.querySelector('.add').disabled = (rows.length >= MAX_INBOXES);
            }});
        }}

        function collectUrls() {{
            // Each inbox may hold either a single URL or a comma-separated
            // list of URLs (matches the CLI shape). We flatten everything
            // into a single deduped array, dropping empty fragments and
            // trimming whitespace around commas.
            const urls = [];
            const seen = new Set();
            document.querySelectorAll('#inboxes .inbox-input').forEach(function(inp) {{
                const raw = inp.value;
                if (!raw) return;
                raw.split(',').forEach(function(part) {{
                    const u = part.trim();
                    if (u && !seen.has(u)) {{
                        seen.add(u);
                        urls.push(u);
                    }}
                }});
            }});
            return urls;
        }}

        function setBusy(on, text, sub) {{
            document.body.classList.toggle('busy', !!on);
            if (text) document.getElementById('busyText').textContent = text;
            if (sub  != null) document.getElementById('busySub').textContent = sub;
            document.getElementById('generateBtn').disabled = !!on;
            if (!on) {{ setProgress(null); }}
        }}
        function setProgress(pct) {{
            const wrap = document.getElementById('busyProgress');
            const bar  = document.getElementById('busyProgressBar');
            if (!wrap || !bar) return;
            if (pct == null) {{
                wrap.classList.add('hidden');
                bar.style.width = '0%';
                return;
            }}
            wrap.classList.remove('hidden');
            bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
        }}

        // ----- Databricks log upload area -----
        // Pending files live in this array; the chip list re-renders from it
        // on every change. We don't read the bytes until the user clicks
        // Generate so editing the selection is cheap.
        const _dbxFiles = [];
        const _DBX_PREFIXES = ['log4j-', 'stdout', 'stderr'];
        function _dbxAccept(name) {{
            for (const p of _DBX_PREFIXES) if (name.indexOf(p) === 0) return true;
            return false;
        }}
        function _humanBytes(n) {{
            if (n < 1024) return n + ' B';
            if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
            return (n / 1024 / 1024).toFixed(2) + ' MB';
        }}
        function _dbxRender() {{
            const list = document.getElementById('dbxFileList');
            if (!list) return;
            const accepted = _dbxFiles.filter(function(f) {{ return _dbxAccept(f.name); }});
            const rejected = _dbxFiles.filter(function(f) {{ return !_dbxAccept(f.name); }});
            const chips = _dbxFiles.map(function(f, idx) {{
                const ok = _dbxAccept(f.name);
                return '<span class="dbx-file-chip' + (ok ? '' : ' invalid') + '" title="'
                    + (ok ? '' : 'Filename will be ignored by the parser') + '">'
                    +   _escapeHtml(f.name)
                    +   '<span class="dbx-file-chip-size">' + _humanBytes(f.size) + '</span>'
                    +   '<button type="button" class="dbx-file-chip-remove" title="Remove" onclick="_dbxRemove(' + idx + ')">&times;</button>'
                    + '</span>';
            }}).join('');
            let summary = '';
            if (accepted.length) {{
                const total = accepted.reduce(function(a, f) {{ return a + f.size; }}, 0);
                summary = accepted.length + ' file' + (accepted.length === 1 ? '' : 's')
                    + ' ready &middot; ' + _humanBytes(total) + ' total';
                if (rejected.length) summary += ' &middot; ' + rejected.length + ' ignored';
            }} else if (rejected.length) {{
                summary = 'No recognized files yet';
            }}
            list.innerHTML = chips + (summary ? '<div class="dbx-file-summary">' + summary + '</div>' : '');
        }}
        function _dbxAddFiles(fileList) {{
            const seen = new Set(_dbxFiles.map(function(f) {{ return f.name + ':' + f.size; }}));
            for (const f of fileList) {{
                const key = f.name + ':' + f.size;
                if (seen.has(key)) continue;
                seen.add(key);
                _dbxFiles.push(f);
            }}
            _dbxRender();
        }}
        function _dbxRemove(idx) {{
            if (idx < 0 || idx >= _dbxFiles.length) return;
            _dbxFiles.splice(idx, 1);
            _dbxRender();
        }}
        function _dbxReset() {{
            _dbxFiles.length = 0;
            const inp = document.getElementById('dbxFileInput');
            if (inp) inp.value = '';
            _dbxRender();
        }}
        function _escapeHtml(s) {{
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }}
        // Reads files sequentially (not Promise.all) so we can update a real
        // progress bar in the busy overlay. For typical Databricks log sets
        // (~20 files, a few MB total) the time-to-base64 is sub-second
        // anyway, but a 100 MB upload feels much better with progress.
        function _readDbxFilesAsBase64(onProgress) {{
            const accepted = _dbxFiles.filter(function(f) {{ return _dbxAccept(f.name); }});
            if (!accepted.length) return Promise.resolve([]);
            const out = [];
            let i = 0;
            function next() {{
                if (i >= accepted.length) return Promise.resolve(out);
                const f = accepted[i];
                return new Promise(function(resolve, reject) {{
                    const reader = new FileReader();
                    reader.onerror = function() {{ reject(new Error('read failed: ' + f.name)); }};
                    reader.onload = function() {{
                        const u = String(reader.result || '');
                        const comma = u.indexOf(',');
                        out.push({{ name: f.name, data: comma >= 0 ? u.slice(comma + 1) : '' }});
                        i++;
                        if (onProgress) onProgress(i, accepted.length, f.name);
                        resolve(null);
                    }};
                    reader.readAsDataURL(f);
                }}).then(next);
            }}
            return next();
        }}

        // Walk a DataTransferItem entry (file or directory) recursively and
        // return a flat array of File objects.
        function _walkEntry(entry) {{
            return new Promise(function(resolve) {{
                if (!entry) return resolve([]);
                if (entry.isFile) {{
                    entry.file(function(f) {{ resolve([f]); }}, function() {{ resolve([]); }});
                }} else if (entry.isDirectory) {{
                    const reader = entry.createReader();
                    const collected = [];
                    function readBatch() {{
                        reader.readEntries(function(entries) {{
                            if (!entries.length) {{
                                Promise.all(collected.map(_walkEntry)).then(function(arrs) {{
                                    resolve([].concat.apply([], arrs));
                                }});
                            }} else {{
                                collected.push.apply(collected, entries);
                                readBatch();
                            }}
                        }}, function() {{ resolve(collected); }});
                    }}
                    readBatch();
                }} else {{
                    resolve([]);
                }}
            }});
        }}

        function _initDbxUploader() {{
            const dz = document.getElementById('dbxDropzone');
            const inp = document.getElementById('dbxFileInput');
            const dirInp = document.getElementById('dbxFolderInput');
            if (!dz || !inp) return;
            inp.addEventListener('change', function(e) {{
                _dbxAddFiles(e.target.files || []);
                inp.value = '';
            }});
            if (dirInp) {{
                dirInp.addEventListener('change', function(e) {{
                    _dbxAddFiles(e.target.files || []);
                    dirInp.value = '';
                }});
            }}
            ['dragenter', 'dragover'].forEach(function(t) {{
                dz.addEventListener(t, function(e) {{ e.preventDefault(); e.stopPropagation(); dz.classList.add('dragover'); }});
            }});
            ['dragleave', 'drop'].forEach(function(t) {{
                dz.addEventListener(t, function(e) {{ e.preventDefault(); e.stopPropagation(); dz.classList.remove('dragover'); }});
            }});
            dz.addEventListener('drop', function(e) {{
                const dt = e.dataTransfer;
                if (!dt) return;
                // Prefer the entries API so dropped folders work; fall back
                // to dt.files for browsers without webkitGetAsEntry.
                const items = dt.items;
                if (items && items.length && typeof items[0].webkitGetAsEntry === 'function') {{
                    const entries = [];
                    for (let i = 0; i < items.length; i++) {{
                        const ent = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
                        if (ent) entries.push(ent);
                    }}
                    Promise.all(entries.map(_walkEntry)).then(function(arrs) {{
                        const files = [].concat.apply([], arrs);
                        if (files.length) _dbxAddFiles(files);
                    }});
                }} else if (dt.files && dt.files.length) {{
                    _dbxAddFiles(dt.files);
                }}
            }});
        }}

        async function generateReport() {{
            const urls = collectUrls();
            const err = document.getElementById('newError');
            err.textContent = '';
            if (urls.length === 0) {{
                err.textContent = 'Add at least one Report Portal launch URL.';
                return;
            }}
            if (urls.length > MAX_INBOXES) {{
                err.textContent = 'Too many URLs (max ' + MAX_INBOXES + ').';
                return;
            }}
            setBusy(true,
                urls.length === 1 ? 'Generating single-launch report\u2026' : 'Generating comparison of ' + urls.length + ' launches\u2026',
                'Fetching Report Portal logs & rendering HTML');
            try {{
                let dbxFiles = [];
                const acceptedCount = _dbxFiles.filter(function(f) {{ return _dbxAccept(f.name); }}).length;
                if (acceptedCount > 0) {{
                    setBusy(true,
                        'Reading ' + acceptedCount + ' log file' + (acceptedCount === 1 ? '' : 's') + '…',
                        'Encoding for upload');
                    setProgress(0);
                    try {{
                        dbxFiles = await _readDbxFilesAsBase64(function(done, total, _name) {{
                            setProgress(Math.round((done / total) * 100));
                            setBusy(true,
                                'Reading log file ' + done + ' / ' + total + '…',
                                'Encoding for upload');
                        }});
                    }} catch (e) {{
                        err.textContent = 'Failed to read uploaded files: ' + (e && e.message ? e.message : 'unknown');
                        return;
                    }}
                    setProgress(null);
                }}
                setBusy(true,
                    urls.length === 1 ? 'Generating single-launch report…' : 'Generating comparison of ' + urls.length + ' launches…',
                    dbxFiles.length
                        ? 'Server: fetching RP logs, parsing Databricks events, rendering HTML'
                        : 'Fetching Report Portal logs & rendering HTML');
                let res;
                try {{
                    res = await fetch('/api/generate', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{
                            urls: urls,
                            databricks_files: dbxFiles,
                        }}),
                    }});
                }} catch (_netErr) {{
                    err.textContent = GENERIC_ERROR;
                    return;
                }}
                if (!res.ok) {{
                    err.textContent = GENERIC_ERROR;
                    return;
                }}
                const data = await res.json().catch(function() {{ return {{}}; }});
                if (!data || !data.id || !data.report_password) {{
                    err.textContent = GENERIC_ERROR;
                    return;
                }}
                // Stage the report behind the password panel. The user MUST
                // see / copy the password before they get to the report so
                // they're never locked out by missing it. Auto-forwarding
                // is intentionally NOT done here.
                _pendingReport = data;
                _showPasswordPanel(data.report_password);
            }} finally {{
                setBusy(false);
            }}
        }}

        function _showPasswordPanel(password) {{
            document.getElementById('pwReadout').value = password;
            document.getElementById('pwPanel').hidden = false;
            // Hide the form chrome so the panel takes focus.
            document.getElementById('inboxes').style.display       = 'none';
            document.getElementById('generateBtn').style.display   = 'none';
            const dbxSec = document.getElementById('dbxSection');
            if (dbxSec) dbxSec.style.display = 'none';
            // Re-focus the readout so the user can Ctrl/Cmd+C immediately
            // without having to mouse over.
            const ro = document.getElementById('pwReadout');
            ro.focus();
            ro.select();
        }}

        function resetGenerateUI() {{
            // Clear pending state, restore the form chrome, blank the form.
            _pendingReport = null;
            document.getElementById('pwPanel').hidden = true;
            document.getElementById('pwReadout').value = '';
            document.getElementById('pwCopyBtn').textContent = 'Copy';
            document.getElementById('inboxes').style.display     = '';
            document.getElementById('generateBtn').style.display = '';
            const dbxSec = document.getElementById('dbxSection');
            if (dbxSec) dbxSec.style.display = '';
            document.getElementById('newError').textContent = '';
            renderInboxes(['']);
            _dbxReset();
        }}

        async function copyGeneratedPassword() {{
            const ro = document.getElementById('pwReadout');
            const btn = document.getElementById('pwCopyBtn');
            const pw = ro.value;
            if (!pw) return;
            try {{
                if (navigator.clipboard && navigator.clipboard.writeText) {{
                    await navigator.clipboard.writeText(pw);
                }} else {{
                    // Fallback for browsers that don't expose the async
                    // Clipboard API on http:// origins; selects the text
                    // and uses the legacy execCommand path.
                    ro.focus();
                    ro.select();
                    document.execCommand('copy');
                }}
                btn.textContent = 'Copied';
                setTimeout(function() {{ btn.textContent = 'Copy'; }}, 1400);
            }} catch (_e) {{
                btn.textContent = 'Copy failed';
                setTimeout(function() {{ btn.textContent = 'Copy'; }}, 1800);
            }}
        }}

        async function showGeneratedReport() {{
            if (!_pendingReport) return;
            const rep = _pendingReport;
            _selectedId = rep.id;
            // Reset the form so returning to the New tab is clean.
            resetGenerateUI();
            showTab('reports');
            await refreshReports();
            openReport(rep);
        }}

        async function refreshReports() {{
            try {{
                const res = await fetch('/api/reports', {{cache: 'no-store'}});
                if (!res.ok) {{
                    _reportList = {{sprints: [], error: GENERIC_ERROR}};
                }} else {{
                    _reportList = await res.json();
                }}
            }} catch (_netErr) {{
                _reportList = {{sprints: [], error: GENERIC_ERROR}};
            }}
            renderReports();
        }}

        function renderReports() {{
            // We render INTO sidebarContent (the head + collapse button live
            // in sidebar-head, above it and outside the content scroll).
            const slot = document.getElementById('sidebarContent');
            if (_reportList.error) {{
                slot.innerHTML = '<div class="report-empty" style="color:var(--red)">' + escapeHtml(_reportList.error) + '</div>';
                return;
            }}
            if (!_reportList.sprints || _reportList.sprints.length === 0) {{
                slot.innerHTML = '<div class="report-empty">No reports yet.<br>Generate one from the <strong style="color:var(--bronze)">01. New</strong> tab.</div>';
                return;
            }}
            const today = new Date().toISOString().slice(0, 10);
            // Sprint expand state is per-sprint, persisted in localStorage so
            // the user's reading preference survives reloads. The current
            // sprint is open by default; older sprints are collapsed.
            const expandedMap = _readSprintExpandedMap();
            let html = '';
            _reportList.sprints.forEach(function(sprint) {{
                const isCurrent = (sprint.start <= today && today < sprint.end);
                // Default-open the current sprint, otherwise honor the user's
                // saved choice (default closed).
                const expanded = (expandedMap[sprint.dirname] != null)
                    ? !!expandedMap[sprint.dirname]
                    : isCurrent;
                const count = sprint.reports.length;
                html += '<div class="sprint-group' + (expanded ? ' expanded' : '') + '"'
                    + ' data-sprint="' + escapeHtml(sprint.dirname) + '">';
                html += '<div class="sprint-label' + (isCurrent ? ' current' : '')
                    + (expanded ? ' expanded' : '') + '"'
                    + ' onclick="toggleSprintGroup(this)">'
                    + '<span class="sprint-caret">&#9656;</span>'
                    + '<span class="sprint-name">' + escapeHtml(sprint.label) + '</span>'
                    + '<span class="sprint-count">' + count + '</span>'
                    + '</div>';
                html += '<div class="sprint-reports">';
                sprint.reports.forEach(function(rep) {{
                    const sel = (rep.id === _selectedId) ? ' selected' : '';
                    // Build the visible title client-side from generated_at
                    // (ISO 8601 w/ offset) so the viewer always sees their
                    // OWN local time. Fall back to the server-formatted
                    // title if generated_at is missing (very old reports).
                    const localTitle = _formatLocalReportTitle(
                        rep.generated_at, rep.num_launches, rep.title);
                    html += '<div class="report-item' + sel + '" data-id="' + escapeHtml(rep.id)
                        + '" data-url="' + escapeHtml(rep.url) + '"'
                        + ' data-share-url="' + escapeHtml(rep.share_url || '') + '"'
                        + ' onclick="openReportFromClick(this)">'
                        + '<span class="report-title" title="' + escapeHtml(localTitle) + '">'
                        + escapeHtml(localTitle) + '</span>'
                        + '</div>';
                }});
                html += '</div>';   // /.sprint-reports
                html += '</div>';   // /.sprint-group
            }});
            slot.innerHTML = html;
        }}

        // ---------- Sidebar / sprint collapse state ----------
        // Persist sidebar collapsed-ness and per-sprint expand state in
        // localStorage so the user's reading layout survives reloads. We
        // keep keys narrowly scoped to this app to avoid clashing with
        // other tools using the same origin.
        const _LS_SIDEBAR = 'dokimos_sidebar_collapsed_v1';
        const _LS_SPRINTS = 'dokimos_sprint_expanded_v1';

        function _readSprintExpandedMap() {{
            try {{
                const raw = localStorage.getItem(_LS_SPRINTS);
                if (!raw) return {{}};
                const parsed = JSON.parse(raw);
                return (parsed && typeof parsed === 'object') ? parsed : {{}};
            }} catch (_e) {{ return {{}}; }}
        }}
        function _writeSprintExpandedMap(map) {{
            try {{ localStorage.setItem(_LS_SPRINTS, JSON.stringify(map)); }} catch (_e) {{}}
        }}

        function toggleSprintGroup(labelEl) {{
            const group = labelEl.parentElement;
            const wasExpanded = group.classList.contains('expanded');
            group.classList.toggle('expanded', !wasExpanded);
            labelEl.classList.toggle('expanded', !wasExpanded);
            const sprint = group.getAttribute('data-sprint');
            if (sprint) {{
                const map = _readSprintExpandedMap();
                map[sprint] = !wasExpanded;
                _writeSprintExpandedMap(map);
            }}
        }}

        function _applySidebarState() {{
            const layout = document.getElementById('reportsLayout');
            const toggle = document.getElementById('sidebarToggle');
            if (!layout || !toggle) return;
            let collapsed = false;
            try {{ collapsed = (localStorage.getItem(_LS_SIDEBAR) === '1'); }} catch (_e) {{}}
            layout.classList.toggle('sidebar-collapsed', collapsed);
            toggle.innerHTML = collapsed ? '&raquo;' : '&laquo;';
            toggle.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
        }}

        function toggleSidebar() {{
            const layout = document.getElementById('reportsLayout');
            if (!layout) return;
            const willCollapse = !layout.classList.contains('sidebar-collapsed');
            try {{ localStorage.setItem(_LS_SIDEBAR, willCollapse ? '1' : '0'); }} catch (_e) {{}}
            _applySidebarState();
        }}

        function escapeHtml(s) {{
            return String(s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }}

        // Format a report's generated-at timestamp in the viewer's local
        // timezone. Server stores generated_at as ISO 8601 with offset (e.g.
        // "2026-05-29T05:32:14-07:00"), so `new Date(...)` parses it
        // unambiguously and we re-render via Intl in the browser locale.
        function _formatLocalReportTitle(iso, numLaunches, fallback) {{
            const n = (typeof numLaunches === 'number') ? numLaunches : 0;
            const suffix = (n === 1) ? '1 launch' : (n + ' launches');
            if (!iso) return fallback || ('— (' + suffix + ')');
            const d = new Date(iso);
            if (isNaN(d.getTime())) return fallback || ('— (' + suffix + ')');
            try {{
                const fmt = new Intl.DateTimeFormat(undefined, {{
                    year: 'numeric', month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit',
                    hour12: false
                }});
                // Intl emits a localized order ("MM/DD/YYYY HH:MM" in en-US,
                // "DD.MM.YYYY HH:MM" in de-DE, etc). That's the right thing
                // to do -- whichever locale the viewer's browser is in, the
                // title reads naturally for them.
                return fmt.format(d) + '  (' + suffix + ')';
            }} catch (_e) {{
                return fallback || (iso + '  (' + suffix + ')');
            }}
        }}

        const PUBLIC_REPORT_ORIGIN = 'https://dokimos-perf.chiron.systems';

        function openReportFromClick(el) {{
            const titleEl = el.querySelector('.report-title');
            openReport({{
                id:        el.getAttribute('data-id'),
                url:       el.getAttribute('data-url'),
                share_url: el.getAttribute('data-share-url') || '',
                title:     titleEl ? titleEl.textContent : ''
            }});
        }}

        function openReport(rep) {{
            _selectedId = rep.id;
            renderReports();
            const viewer = document.getElementById('reportViewer');
            const iframeUrl = rep.share_url || rep.url;
            const fullUrl   = PUBLIC_REPORT_ORIGIN + iframeUrl;
            const titleText = rep.title || rep.id || 'Report';
            viewer.innerHTML = ''
                + '<div class="viewer-header">'
                +   '<div class="viewer-title" title="' + escapeHtml(fullUrl) + '">'
                +     escapeHtml(titleText)
                +   '</div>'
                +   '<button class="viewer-copy-btn" type="button"'
                +     ' data-url="' + escapeHtml(fullUrl) + '"'
                +     ' onclick="copyReportLink(this)">Copy link</button>'
                + '</div>'
                + '<iframe src="' + escapeHtml(iframeUrl) + '"></iframe>';
        }}

        async function copyReportLink(btn) {{
            const url = btn.getAttribute('data-url');
            if (!url) return;
            try {{
                await navigator.clipboard.writeText(url);
            }} catch (_e) {{
                try {{ window.prompt('Copy this link:', url); return; }} catch (_e2) {{ return; }}
            }}
            const orig = btn.textContent;
            btn.textContent = 'Copied!';
            btn.classList.add('copied');
            setTimeout(function() {{
                btn.textContent = orig;
                btn.classList.remove('copied');
            }}, 1500);
        }}

        renderInboxes(['']);
        _initDbxUploader();
        _applySidebarState();
        // Live-update the parallel-warning band as the dropdown changes.
        const _pp = document.getElementById('testsParallel');
        if (_pp) _pp.addEventListener('change', _testsParallelCheck);

        // ----- URL routing -----
        // /                 -> New tab
        // /tests            -> Tests tab
        // /pipelines        -> Pipelines tab, no selection
        // /pipelines/<id>   -> Pipelines tab, that pipeline auto-selected
        // /reports          -> Reports tab
        function _routeFromPath(path, fromPop) {{
            const m = (path || '/').match(/^\/pipelines\/([A-Za-z0-9_-]+)\/?$/);
            if (m) {{
                _selectedPipelineId = m[1];
                showTab('pipelines', {{fromPop: fromPop}});
                // _enterPipelinesTab already selects _selectedPipelineId if set.
                return;
            }}
            if (path === '/pipelines' || path === '/pipelines/') {{
                _selectedPipelineId = null;
                showTab('pipelines', {{fromPop: fromPop}});
                return;
            }}
            if (path === '/tests'   || path === '/tests/')   {{ showTab('tests',   {{fromPop: fromPop}}); return; }}
            if (path === '/reports' || path === '/reports/') {{ showTab('reports', {{fromPop: fromPop}}); return; }}
            // default
            showTab('new', {{fromPop: fromPop}});
        }}
        window.addEventListener('popstate', function() {{
            _routeFromPath(window.location.pathname, true);
        }});
        _routeFromPath(window.location.pathname, false);
    </script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class _SpaHandler(http.server.BaseHTTPRequestHandler):
    server_version = "DokimosPerformance/1.0"

    def log_message(self, format, *args):  # noqa: A002 -- match base sig
        if os.environ.get("RP_PERF_REPORT_VERBOSE") == "1":
            super().log_message(format, *args)

    # ---- send helpers ----
    def _send(self, status, content_type, body, *,
              cache_control="no-store", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, obj, *, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body,
                   extra_headers=extra_headers)

    def _bad_request(self, msg):
        self._send_json(400, {"error": msg})

    def _server_error(self, msg):
        self._send_json(500, {"error": msg})

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]

        # Public routes:
        if path in ("/favicon.ico",):
            self._send(204, "image/x-icon", b"")
            return

        # / -> SPA. No login gate anymore -- the only password barrier is
        # the per-report encryption inside each report's HTML.
        # Tab and per-pipeline routes resolve to the same SPA HTML; the
        # client-side router reads `location.pathname` on load to choose
        # the right tab + (for pipelines) auto-select a pipeline.
        if (
            path in ("/", "/index.html")
            or path in ("/tests", "/pipelines", "/reports", "/new")
            or path.startswith("/pipelines/")
        ):
            self._send(200, "text/html; charset=utf-8", SPA_HTML.encode("utf-8"))
            return

        if path == "/api/reports":
            self._send_json(200, _list_reports())
            return
        if path == "/api/tests/tree":
            self._handle_tests_tree()
            return
        if path == "/api/tests/file":
            self._handle_tests_file()
            return
        if path == "/api/tests/envs":
            self._send_json(200, {"environments": _discover_environments()})
            return
        if path == "/api/pipelines":
            self._send_json(200, {"pipelines": _load_pipelines()})
            return
        if path.startswith("/api/pipelines/") and path.endswith("/logs"):
            pid = path[len("/api/pipelines/"):-len("/logs")]
            self._handle_pipeline_logs(pid)
            return
        if path.startswith("/reports/"):
            self._serve_report_file(path)
            return
        if path.startswith("/r/"):
            self._redirect_by_share_hash(path[len("/r/"):])
            return
        if path.startswith("/assets/"):
            self._serve_static_asset(path)
            return

        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def _redirect_by_share_hash(self, share_hash: str):
        """Resolve a /r/<hash> URL and serve the report's index.html in-place.

        We deliberately do NOT 302 to the canonical /reports/<...>/ URL --
        that would flip the browser address bar back to the long path and
        defeat the point of a short shareable link. Instead we look the
        hash up, read the file, and send its bytes verbatim under the
        original /r/<hash> URL.

        SECURITY-REVIEW: the hash itself is just a lookup key (~64 bits of
        entropy, no per-report secret); knowing it does NOT unlock the
        report -- the per-report AES-GCM password is still required.
        """
        if not share_hash or not all(c.isalnum() or c in "-_" for c in share_hash):
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        if not os.path.isdir(REPORTS_DIR):
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        for sprint_dirname in os.listdir(REPORTS_DIR):
            sprint_path = os.path.join(REPORTS_DIR, sprint_dirname)
            if not os.path.isdir(sprint_path):
                continue
            for report_dirname in os.listdir(sprint_path):
                meta_path = os.path.join(sprint_path, report_dirname, "metadata.json")
                if not os.path.isfile(meta_path):
                    continue
                try:
                    with open(meta_path) as fp:
                        meta = json.load(fp)
                except (OSError, ValueError):
                    continue
                if meta.get("share_hash") == share_hash:
                    abs_path = os.path.realpath(
                        os.path.join(sprint_path, report_dirname, "index.html"))
                    if not abs_path.startswith(os.path.realpath(REPORTS_DIR) + os.sep):
                        self._send(403, "text/plain; charset=utf-8", b"Forbidden")
                        return
                    if not os.path.isfile(abs_path):
                        self._send(404, "text/plain; charset=utf-8", b"Not found")
                        return
                    try:
                        with open(abs_path, "rb") as fp:
                            body = fp.read()
                    except OSError as e:
                        self._server_error(f"could not read report: {e}")
                        return
                    self._send(200, "text/html; charset=utf-8", body,
                               cache_control="public, max-age=3600")
                    return
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def _serve_report_file(self, path):
        rel = path[len("/reports/"):].lstrip("/")
        abs_path = os.path.realpath(os.path.join(REPORTS_DIR, rel))
        # SECURITY-REVIEW: prevent path traversal escapes outside REPORTS_DIR.
        # realpath() also resolves any encoded '../' so re-check the prefix.
        if not abs_path.startswith(os.path.realpath(REPORTS_DIR) + os.sep):
            self._send(403, "text/plain; charset=utf-8", b"Forbidden")
            return
        if not os.path.isfile(abs_path):
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        ext = os.path.splitext(abs_path)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        try:
            with open(abs_path, "rb") as fp:
                body = fp.read()
        except OSError as e:
            self._server_error(f"could not read report: {e}")
            return
        # Saved reports never change, so allow long caching.
        self._send(200, ctype, body, cache_control="public, max-age=3600")

    def _serve_static_asset(self, path: str):
        """Serve a file shipped with the package under ``assets/`` (icons,
        logos, etc.). Read-only -- the SPA never writes to this directory."""
        rel = path[len("/assets/"):].lstrip("/")
        abs_path = os.path.realpath(os.path.join(ASSETS_DIR, rel))
        # SECURITY-REVIEW: same path-traversal guard as the reports handler.
        # ASSETS_DIR is a small directory shipped with the package, so the
        # realpath + prefix check is enough -- we never construct paths
        # outside it.
        if not abs_path.startswith(os.path.realpath(ASSETS_DIR) + os.sep):
            self._send(403, "text/plain; charset=utf-8", b"Forbidden")
            return
        if not os.path.isfile(abs_path):
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        ext = os.path.splitext(abs_path)[1].lower()
        ctype = {
            ".svg":  "image/svg+xml",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif":  "image/gif",
            ".ico":  "image/x-icon",
        }.get(ext, "application/octet-stream")
        try:
            with open(abs_path, "rb") as fp:
                body = fp.read()
        except OSError as e:
            self._server_error(f"could not read asset: {e}")
            return
        # Assets are immutable per release; safe to cache aggressively.
        self._send(200, ctype, body, cache_control="public, max-age=86400")

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/generate":
            self._handle_generate()
            return
        if path == "/api/pipelines":
            self._handle_pipeline_create()
            return
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def _read_json_body(self, max_bytes: int = 100_000_000):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > max_bytes:
            return None, "missing or oversized request body"
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")), None
        except (ValueError, UnicodeDecodeError) as e:
            return None, f"invalid JSON body: {e}"

    def _handle_tests_tree(self):
        if not TESTS_DIR or not os.path.isdir(TESTS_DIR):
            # No configured path exists. Tell the operator exactly what was
            # tried and how to fix it, so production deploys can self-diagnose.
            # TEMPORARY DEBUG: also include the listing of common parent
            # directories so the operator can see what's actually on disk
            # from the SPA's process perspective. Remove once production
            # deploys are stable.
            self._send_json(404, {
                "error":      "tests root not found",
                "configured": TESTS_DIR,
                "hint":       "set DOKIMOS_TESTS_DIR env var to the absolute path of NAS_components/InventoryForecasting",
                "searched":   list(_TESTS_DIR_CANDIDATES),
                "debug_ls":   _debug_filesystem_listings(),
            })
            return
        tree = _build_tests_tree(TESTS_DIR)
        self._send_json(200, {"root": os.path.basename(TESTS_DIR.rstrip("/")) or "tests",
                              "resolved": TESTS_DIR,
                              "tree": tree})

    def _handle_tests_file(self):
        # Parse ?path=... from the query string.
        from urllib.parse import urlsplit, parse_qs
        qs = parse_qs(urlsplit(self.path).query)
        rel = (qs.get("path") or [""])[0]
        abs_path = _safe_test_path(rel)
        if not abs_path or not os.path.isfile(abs_path):
            self._send_json(404, {"error": "file not found"})
            return
        # Refuse binaries / huge files outright -- the code view can't render them.
        suffix = os.path.splitext(abs_path)[1].lower()
        if suffix in _TESTS_BINARY_SUFFIXES:
            self._send_json(415, {"error": f"binary file type {suffix} is not viewable",
                                   "path": rel, "size": os.path.getsize(abs_path)})
            return
        size = os.path.getsize(abs_path)
        if size > _TESTS_FILE_MAX_BYTES:
            self._send_json(413, {"error": f"file too large ({size} bytes); cap is {_TESTS_FILE_MAX_BYTES}",
                                   "path": rel, "size": size})
            return
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            self._send_json(500, {"error": f"read failed: {e}"})
            return
        self._send_json(200, {"path": rel, "size": size, "content": content,
                              "suffix": suffix.lstrip(".")})

    def _handle_pipeline_create(self):
        payload, err = self._read_json_body()
        if err:
            self._bad_request(err)
            return
        if not isinstance(payload, dict):
            self._bad_request("expected JSON object")
            return
        try:
            iterations = max(1, min(50, int(payload.get("iterations", 1))))
        except (TypeError, ValueError):
            iterations = 1
        records = []
        for i in range(iterations):
            rec = _create_pipeline(payload, iteration_index=i + 1, iteration_count=iterations)
            records.append(rec)
        # Kick off `run-now` pipelines immediately. `scheduled` ones get
        # picked up by the scheduler thread when their time arrives.
        for r in records:
            if r.get("status") == "running":
                _start_runner(r["id"])
        self._send_json(200, {"pipelines": records, "count": len(records)})

    def _handle_pipeline_logs(self, pid: str):
        if not pid or not all(c.isalnum() or c in "-_" for c in pid):
            self._send_json(404, {"error": "not found"})
            return
        pipelines = _load_pipelines()
        for p in pipelines:
            if p.get("id") == pid:
                # Full record minus the (potentially huge) logs list as a
                # separate field, so the UI can render a config strip
                # alongside the log tail without two round-trips.
                self._send_json(200, {
                    "id":            pid,
                    "name":          p.get("name"),
                    "status":        p.get("status"),
                    "kind":          p.get("kind"),
                    "env":           p.get("env"),
                    "parallel":      p.get("parallel"),
                    "feature":       p.get("feature"),
                    "tests":         p.get("tests"),
                    "created_at":    p.get("created_at"),
                    "scheduled_for": p.get("scheduled_for"),
                    "started_at":    p.get("started_at"),
                    "finished_at":   p.get("finished_at"),
                    "rp_url":        p.get("rp_url"),
                    "logs":          p.get("logs") or [],
                })
                return
        self._send_json(404, {"error": "not found"})

    def _handle_generate(self):
        payload, err = self._read_json_body()
        if err:
            self._bad_request(err)
            return
        urls = payload.get("urls") if isinstance(payload, dict) else None
        if not isinstance(urls, list) or not urls:
            self._bad_request("expected non-empty 'urls' array")
            return
        # Flatten + dedupe: each element may itself be a comma-separated
        # list, matching the CLI form (and the SPA tip on the New tab). We
        # preserve user-supplied order, drop empty fragments, and trim.
        flat: list[str] = []
        seen: set[str] = set()
        for raw in urls:
            if not isinstance(raw, str):
                continue
            for part in raw.split(","):
                u = part.strip()
                if u and u not in seen:
                    seen.add(u)
                    flat.append(u)
        if not flat:
            self._bad_request("all URLs were empty")
            return
        urls = flat

        payload_key = payload.get("payload_key") if isinstance(payload, dict) else None
        databricks_files = payload.get("databricks_files") if isinstance(payload, dict) else None

        dbx_dir = _materialize_databricks_uploads(databricks_files or [])
        try:
            try:
                entry = _run_generation(urls, payload_key=payload_key, databricks_log_dir=dbx_dir)
            except ValueError as e:
                self._bad_request(str(e))
                return
            except urllib.error.URLError as e:
                self._server_error(f"upstream fetch failed: {e}")
                return
            except RuntimeError as e:
                self._bad_request(str(e))
                return
            except Exception as e:  # noqa: BLE001
                self._server_error(f"{type(e).__name__}: {e}")
                return
            self._send_json(200, entry)
        finally:
            if dbx_dir:
                import shutil
                shutil.rmtree(dbx_dir, ignore_errors=True)


class _ThreadingTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded so a long-running /api/generate doesn't block other requests."""
    allow_reuse_address = True
    daemon_threads = True


def serve_spa(port: int = PORT) -> None:
    """Start the Dokimos Performance SPA HTTP server. Blocks until Ctrl+C."""
    _ensure_reports_dir()
    # Reconcile any pipelines left in `running` by the previous SPA process
    # (their runner threads died on shutdown, so they're orphans).
    orphaned = _reconcile_orphan_pipelines()
    if orphaned:
        print(f"  Reconciled {orphaned} orphaned pipeline(s) -> failed")
    # Start the pipeline scheduler so any pipelines created later -- and any
    # already-scheduled ones from prior server runs -- get picked up when
    # their `scheduled_for` arrives.
    _ensure_pipeline_scheduler()
    # One-shot, idempotent migration of legacy reports: bring their CSS in
    # line with the current theme, inject the localize-times script, and
    # scrub any per-report passwords that the previous server version
    # persisted in metadata.json. After the first run on a given filesystem
    # there's nothing left to do, so the second call is effectively a no-op
    # directory walk.
    backfill_stats = _backfill_legacy_reports()
    with _ThreadingTcpServer(("", port), _SpaHandler) as httpd:
        bar = "=" * 60
        print(f"\n{bar}")
        print(f"  Perf Runner available at: http://localhost:{port}")
        print(f"  Per-report password protection only (no SPA login)")
        print(f"  Reports persisted under: {REPORTS_DIR}")
        if (backfill_stats["restyled"]
                or backfill_stats["passwords_scrubbed"]
                or backfill_stats.get("localize_injected")):
            print(
                f"  Backfill: restyled {backfill_stats['restyled']} report(s), "
                f"scrubbed password from {backfill_stats['passwords_scrubbed']} metadata file(s), "
                f"localize-script in {backfill_stats.get('localize_injected', 0)} report(s)"
            )
        if backfill_stats["errors"]:
            print(f"  Backfill errors: {backfill_stats['errors']}")
        print(f"  Press Ctrl+C to stop the server")
        print(f"{bar}\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")


# Direct-script entry point. Lets the user run `python3 spa.py` from this
# directory without needing to `pip install` the package or fight with
# `python -m rp_perf_report` (which depends on the parent dir + package name
# matching, broken by the directory rename to dokimos-perf.chiron.systems).
if __name__ == "__main__":
    serve_spa()
