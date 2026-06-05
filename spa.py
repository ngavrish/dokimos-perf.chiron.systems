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
# Iteration groups: an N-iteration burst is one "report package". Group records
# live here; the per-iteration pipelines stay in pipelines.json linked by group_id.
GROUPS_FILE    = os.path.join(PACKAGE_DIR, "groups.json")
# The report generator (generate_report_for_urls) compares up to this many
# launches; a group with more iterations than this caps its report inputs.
GROUP_REPORT_MAX_LAUNCHES = int(os.environ.get("DOKIMOS_GROUP_REPORT_MAX_LAUNCHES", "20"))
# group_id -> one-time report password. IN MEMORY ONLY, never written to disk:
# revealed once in the group view and dropped when the user leaves it (or on
# process restart), matching the "shown once, never recoverable" report model.
_group_report_passwords: dict = {}

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

# Hard ceiling on a single pipeline run. A hung container (e.g. behavex
# deadlock, stuck Report-Portal upload) would otherwise block the single-slot
# queue forever, so the runner watchdog kills it and marks the pipeline failed.
MAX_RUNTIME_SEC = int(os.environ.get("DOKIMOS_MAX_RUNTIME_SEC", "2700"))  # 45 min

# Upper bound the operator can dial in from the Tests panel (minutes). The
# DOKIMOS_MAX_RUNTIME_SEC env value remains the *default*; the per-run field
# can only set something between 1 minute and this ceiling.
MAX_RUNTIME_CEILING_MIN = int(os.environ.get("DOKIMOS_MAX_RUNTIME_CEILING_MIN", "240"))

# Per-scenario retry budget passed through to the suite as IFP_SCENARIO_RETRY
# (see environment.py: max attempts per scenario, 1 = run once). Default 3
# mirrors the suite default; the Tests-panel field can dial it in [1, ceiling].
SCENARIO_RETRY_DEFAULT = int(os.environ.get("DOKIMOS_SCENARIO_RETRY_DEFAULT", "3"))
SCENARIO_RETRY_CEILING = int(os.environ.get("DOKIMOS_SCENARIO_RETRY_CEILING", "5"))

# Rough per-scenario wall time (seconds) for the *pre-run* duration estimate on
# the job-config panel. Calibrated from observed @perf runs (~15s/scenario for
# the heavier forecast mix; SUMMARY-only calls are faster). This is only the
# initial guess -- once a run starts, the panel shows a live rate-based ETA that
# supersedes it. Override via env if the suite's profile changes.
EST_SEC_PER_SCENARIO = float(os.environ.get("DOKIMOS_EST_SEC_PER_SCENARIO", "15"))
# Fixed per-iteration overhead (seconds) that happens regardless of scenario
# count: container start, behave import, the before_feature Report Portal launch,
# and teardown. Without it, a 1-scenario run is wildly underestimated. NOT
# divided by parallelism (it's paid once per container run).
EST_FIXED_OVERHEAD_SEC = float(os.environ.get("DOKIMOS_EST_FIXED_OVERHEAD_SEC", "40"))


def _clamp_scenario_retry(val) -> int:
    """Translate the Tests-panel 'Retries' field into an IFP_SCENARIO_RETRY
    value (max attempts per scenario; 1 = run once, no retry).

    Blank/garbage -> 3 (the suite's environment.py default). Otherwise clamp
    to [1, SCENARIO_RETRY_CEILING] so a typo can't spin every failing scenario
    forever (each retry re-runs the scenario + backoff)."""
    if val is None or (isinstance(val, str) and not val.strip()):
        return SCENARIO_RETRY_DEFAULT
    try:
        n = int(val)
    except (TypeError, ValueError):
        return SCENARIO_RETRY_DEFAULT
    return max(1, min(SCENARIO_RETRY_CEILING, n))


def _clamp_max_runtime_sec(min_val) -> int:
    """Translate the Tests-panel 'Max runtime (min)' field into seconds.

    Blank/garbage -> the MAX_RUNTIME_SEC default. Anything supplied is clamped
    to [1, MAX_RUNTIME_CEILING_MIN] minutes so a typo can't disable the
    watchdog or pin the single-slot queue for days."""
    if min_val is None or (isinstance(min_val, str) and not min_val.strip()):
        return MAX_RUNTIME_SEC
    try:
        m = int(min_val)
    except (TypeError, ValueError):
        return MAX_RUNTIME_SEC
    m = max(1, min(MAX_RUNTIME_CEILING_MIN, m))
    return m * 60

# Cap per-pipeline log lines. The whole pipelines.json is re-serialised on every
# append, so an unbounded `logs` array (chatty container) bloats the file and
# slows every save. Keep the head (creation/context) + the most recent tail.
MAX_LOG_LINES = int(os.environ.get("DOKIMOS_MAX_LOG_LINES", "4000"))

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
    # Stable container name so the runtime watchdog can `docker kill` it on timeout.
    argv = ["docker", "run", "--rm", "--name", f"dokimos-{pipeline['id']}"]
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
        ("ENV",                pipeline["env"]),
        ("PARALLEL_THREADS",   str(pipeline["parallel"])),
        # Per-scenario retry budget, consumed by the suite's environment.py.
        ("IFP_SCENARIO_RETRY", str(pipeline.get("scenario_retry") or SCENARIO_RETRY_DEFAULT)),
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


# The IFP suite never prints a clickable Report Portal URL -- it prints
# "ReportPortal Launch ID: <uuid>" and "ReportPortal launch_info: {... 'id': N ...}"
# (see Base/Misc/ReportPortalAgent.py). We build the launch deep-link ourselves
# from the launch id + the known RP endpoint/project. Both are env-overridable.
RP_ENDPOINT = os.environ.get(
    "DOKIMOS_RP_ENDPOINT", "https://ads-report-portal.staging.hulu.com").rstrip("/")
RP_PROJECT = os.environ.get("DOKIMOS_RP_PROJECT", "ad-apps-automation")

_RP_LAUNCH_INFO_RE = None
_RP_LAUNCH_ID_RE = None
def _extract_rp_url(line: str):
    """Find a Report Portal launch URL in (or derivable from) `line`.

    Returns ``(url, is_numeric)`` or ``None``. ``is_numeric`` is True when the
    URL uses Report Portal's numeric launch id (the form the UI deep-link
    actually resolves) rather than the launch UUID -- the suite prints the UUID
    (`ReportPortal Launch ID:`) *before* the numeric id (`launch_info: {... 'id': N ...}`),
    so the caller uses this flag to upgrade a provisional UUID link to the numeric one.

    Preference: a full printed URL > numeric launch_info id > UUID Launch ID."""
    m = _rp_url_re().search(line)
    if m:
        return (m.group(1), True)
    global _RP_LAUNCH_INFO_RE, _RP_LAUNCH_ID_RE
    if _RP_LAUNCH_INFO_RE is None:
        import re as _re
        _RP_LAUNCH_INFO_RE = _re.compile(r"launch_info:.*?['\"]id['\"]\s*:\s*(\d+)")
        _RP_LAUNCH_ID_RE = _re.compile(r"ReportPortal Launch ID:\s*(\S+)")
    m = _RP_LAUNCH_INFO_RE.search(line)
    if m:
        return (f"{RP_ENDPOINT}/ui/#{RP_PROJECT}/launches/all/{m.group(1)}", True)
    m = _RP_LAUNCH_ID_RE.search(line)
    if m and m.group(1).lower() != "none":
        tok = m.group(1)
        return (f"{RP_ENDPOINT}/ui/#{RP_PROJECT}/launches/all/{tok}", tok.isdigit())
    return None


# behavex/behave logs one line per scenario start: "... Running Scenario <name>".
# We count distinct names to drive the live progress + ETA on the logs panel.
_SCENARIO_MARKER_RE = None
def _scenario_marker_re():
    global _SCENARIO_MARKER_RE
    if _SCENARIO_MARKER_RE is None:
        import re as _re
        _SCENARIO_MARKER_RE = _re.compile(r"Running Scenario\s+(.+?)\s*$")
    return _SCENARIO_MARKER_RE


def _append_pipeline_log(pipeline_id: str, *lines: str, **mut) -> None:
    """Atomically append log lines (and optionally mutate top-level fields)
    on a single pipeline record. Held under the persistence lock so the
    runner thread and the scheduler can't trample each other."""
    with _pipelines_lock():
        pipelines = _load_pipelines()
        for p in pipelines:
            if p.get("id") == pipeline_id:
                logs = p.setdefault("logs", [])
                logs.extend(lines)
                if len(logs) > MAX_LOG_LINES:
                    head, tail = logs[:50], logs[-(MAX_LOG_LINES - 51):]
                    dropped = len(logs) - len(head) - len(tail)
                    p["logs"] = head + [f"[... {dropped} earlier log lines truncated ...]"] + tail
                for k, v in mut.items():
                    p[k] = v
                break
        _save_pipelines(pipelines)


def _docker_image_exists(image: str) -> bool:
    """True if `image` is already present in the local Docker image store."""
    import subprocess
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except Exception:
        return False


def _ensure_base_submodule(pipeline_id: str, context: str) -> bool:
    """Ensure the `Base` git submodule is checked out under the build context;
    the perf image build needs it. On a fresh checkout it's uninitialised, so
    init it (the runner is User=bober, so git uses bober's cached enterprise
    token via the credential helper). Returns True if Base is present/ready
    (or not registered as a submodule at all), False if an init was needed but
    failed."""
    import subprocess
    try:
        st = subprocess.run(
            ["git", "-C", context, "submodule", "status", "Base"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except Exception as exc:
        _append_pipeline_log(
            pipeline_id, f"[{_now_iso()}] WARN: Base submodule check failed: {exc}; continuing")
        return True
    line = (st.stdout or "").strip()
    # `git submodule status` prefixes uninitialised entries with '-'. A checked-out
    # one has ' ' or '+'. Empty output = no Base submodule registered -> nothing to do.
    if not line or not line.startswith("-"):
        return True

    _append_pipeline_log(
        pipeline_id, f"[{_now_iso()}] Base submodule not checked out -- initialising it")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.Popen(
            ["git", "-C", context, "submodule", "update", "--init", "Base"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env,
        )
        for raw in proc.stdout:
            ln = raw.rstrip("\r\n")
            if ln:
                _append_pipeline_log(pipeline_id, ln)
        rc = proc.wait()
    except Exception as exc:
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] ERROR: Base submodule init failed: {type(exc).__name__}: {exc}")
        return False
    if rc != 0:
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] ERROR: Base submodule init failed (exit {rc}). Needs VPN + a "
            f"cached github.twdcgrid.net token (git credential.helper).",
        )
        return False
    _append_pipeline_log(pipeline_id, f"[{_now_iso()}] Base submodule checked out OK")
    return True


def _ensure_docker_image(pipeline_id: str, assets: dict) -> bool:
    """Guarantee DOCKER_IMAGE is available before running.

    If the image is **already built locally, reuse it as-is** (no rebuild).
    Otherwise build it once from the discovered Dockerfile with the repo root
    as context (BuildKit), streaming the build output into the pipeline log.
    Returns True if the image is ready to run, False if the build failed (in
    which case the pipeline has already been marked `failed`)."""
    import subprocess
    if _docker_image_exists(DOCKER_IMAGE):
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] image {DOCKER_IMAGE} already built -- reusing it (no rebuild)",
        )
        return True

    dockerfile = assets.get("dockerfile")
    context = assets.get("rel_root")
    if not dockerfile or not context:
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] ERROR: image {DOCKER_IMAGE} missing and cannot build "
            f"(no Dockerfile/context discovered)",
            status="failed", finished_at=_now_iso(),
        )
        return False

    # The build context needs the Base submodule; auto-init it if a fresh
    # checkout left it empty.
    if not _ensure_base_submodule(pipeline_id, context):
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] aborting: cannot build {DOCKER_IMAGE} without the Base submodule",
            status="failed", finished_at=_now_iso(),
        )
        return False

    build_argv = ["docker", "build", "-f", dockerfile, "-t", DOCKER_IMAGE, context]
    _append_pipeline_log(
        pipeline_id,
        f"[{_now_iso()}] image {DOCKER_IMAGE} not found locally -- building it once",
        f"[{_now_iso()}] (needs VPN for artifactory.prod.hulu.com + the Base submodule checked out)",
        f"[{_now_iso()}] building: DOCKER_BUILDKIT=1 {' '.join(build_argv)}",
    )
    build_env = dict(os.environ, DOCKER_BUILDKIT="1")
    try:
        proc = subprocess.Popen(
            build_argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=build_env,
        )
    except Exception as exc:
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] ERROR: failed to start docker build: "
            f"{type(exc).__name__}: {exc}",
            status="failed", finished_at=_now_iso(),
        )
        return False
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line:
                _append_pipeline_log(pipeline_id, line)
    except Exception:
        pass
    rc = proc.wait()
    if rc != 0:
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] docker build failed (exit {rc}) -- see log above. "
            f"Common causes: VPN down (artifactory unreachable) or Base submodule "
            f"not checked out.",
            status="failed", finished_at=_now_iso(),
        )
        return False
    _append_pipeline_log(
        pipeline_id,
        f"[{_now_iso()}] image {DOCKER_IMAGE} built OK -- future runs reuse it",
    )
    return True


# The perf entrypoint (NAS_components/.../perf/run_perf.sh) always runs
# `behavex tests/api_tests --tags=@perf`, regardless of the UI feature/tests
# filters. So the meaningful pre-run count is: total scenarios in api_tests vs
# how many carry the @perf tag (= what actually executes).
PERF_FEATURES_SUBDIR = os.path.join("tests", "api_tests")
PERF_RUN_TAG = "@perf"


def _scan_feature_file(path: str):
    """Static parse of one .feature file -> a list of scenario dicts
    ``{name, tags(set, feature tags inherited), count}``. A Scenario Outline
    contributes one per Examples data row (header excluded); a plain Scenario
    counts as one. Best-effort, not a full behave parser."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []

    feature_tags = set()
    pending_tags = set()
    blocks = []          # {tags, outline, rows, name}
    cur = None
    in_examples = False
    ex_header_seen = False

    def _close():
        if cur is not None:
            blocks.append(cur)

    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("@"):
            pending_tags.update(t for t in s.split() if t.startswith("@"))
            continue
        if s.startswith("Feature:"):
            feature_tags = set(pending_tags)
            pending_tags = set()
            in_examples = False
            continue
        if s.startswith("Scenario Outline:") or s.startswith("Scenario:"):
            _close()
            label = "Scenario Outline:" if s.startswith("Scenario Outline:") else "Scenario:"
            cur = {"tags": set(pending_tags), "outline": label.startswith("Scenario Outline"),
                   "rows": 0, "name": s[len(label):].strip()}
            pending_tags = set()
            in_examples = False
            ex_header_seen = False
            continue
        if s.startswith("Background:"):
            pending_tags = set()
            in_examples = False
            continue
        if s.startswith("Examples:"):
            in_examples = True
            ex_header_seen = False
            continue
        if in_examples and s.startswith("|"):
            if not ex_header_seen:
                ex_header_seen = True          # first row is the column header
            elif cur is not None:
                cur["rows"] += 1
            continue
        if in_examples:
            in_examples = False                # any non-table line ends the block
    _close()

    out = []
    for b in blocks:
        n = b["rows"] if (b["outline"] and b["rows"] > 0) else 1
        out.append({"name": b["name"], "tags": feature_tags | b["tags"], "count": n})
    return out


def _parse_tests_filter(s):
    """Pull behave ``--name`` regexes and ``--tags`` expressions out of the
    free-form Tests-filter string (the SPA's `tests`/BEHAVEX_EXTRA field).
    Returns (name_patterns, tag_values). Unknown tokens are ignored."""
    import shlex
    try:
        toks = shlex.split(s or "")
    except ValueError:
        toks = (s or "").split()
    names, tags, i = [], [], 0
    while i < len(toks):
        t = toks[i]
        if t in ("--name", "-n") and i + 1 < len(toks):
            names.append(toks[i + 1]); i += 2; continue
        if t.startswith("--name="):
            names.append(t[len("--name="):]); i += 1; continue
        if t in ("--tags", "-t") and i + 1 < len(toks):
            tags.append(toks[i + 1]); i += 2; continue
        if t.startswith("--tags="):
            tags.append(t[len("--tags="):]); i += 1; continue
        i += 1
    return names, tags


def _name_match(name, patterns):
    """True if `name` matches any --name pattern (behave uses regex search)."""
    if not patterns:
        return True
    import re as _re
    for p in patterns:
        try:
            if _re.search(p, name):
                return True
        except _re.error:
            if p in name:        # fall back to substring on a bad regex
                return True
    return False


def _tag_match(tags, tag_values):
    """True if the scenario's tag set satisfies every --tags value (ANDed
    across values; comma within one value is OR; ~/not negates a tag)."""
    if not tag_values:
        return True
    def _norm(t):
        return t if t.startswith("@") else "@" + t
    for val in tag_values:
        ok = False
        for part in val.split(","):
            part = part.strip()
            if not part:
                continue
            neg = False
            if part.startswith("~"):
                neg, part = True, part[1:]
            elif part.lower().startswith("not "):
                neg, part = True, part[4:].strip()
            present = _norm(part) in tags
            if present != neg:
                ok = True
                break
        if not ok:
            return False
    return True


def _discover_perf_scenarios(tests_filter=None):
    """Pre-run, host-side discovery of what the perf container will execute.

    Scans tests/api_tests and, for each scenario, counts it as @perf if tagged,
    and as *running* if it also satisfies the Tests filter (--name / --tags).
    Returns counts {total, perf, running, has_filter, per_file:[(rel, perf, run)]}
    or None if the tests dir isn't present. Pure static scan, fast."""
    api_dir = os.path.join(TESTS_DIR, PERF_FEATURES_SUBDIR)
    if not os.path.isdir(api_dir):
        return None
    names, tags = _parse_tests_filter(tests_filter)
    has_filter = bool(names or tags)
    per_file, total, perf, running = [], 0, 0, 0
    for root, dirs, fnames in os.walk(api_dir):
        dirs.sort()
        for fn in sorted(fnames):
            if not fn.endswith(".feature"):
                continue
            p = os.path.join(root, fn)
            f_perf = f_run = 0
            for sc in _scan_feature_file(p):
                total += sc["count"]
                if PERF_RUN_TAG in sc["tags"]:
                    f_perf += sc["count"]
                    if _name_match(sc["name"], names) and _tag_match(sc["tags"], tags):
                        f_run += sc["count"]
            perf += f_perf
            running += f_run
            per_file.append((os.path.relpath(p, TESTS_DIR), f_perf, f_run))
    return {
        "dir": os.path.relpath(api_dir, TESTS_DIR),
        "tag": PERF_RUN_TAG,
        "files": len(per_file),
        "total": total,
        "perf": perf,
        "running": running,
        "has_filter": has_filter,
        "per_file": per_file,
    }


def _log_scenario_discovery(pipeline_id: str) -> None:
    """Emit the discovered-vs-running scenario summary at the start of a run."""
    with _pipelines_lock():
        rec = next((p for p in _load_pipelines() if p.get("id") == pipeline_id), None)
    tests_filter = (rec or {}).get("tests") or None
    disco = _discover_perf_scenarios(tests_filter)
    if disco is None:
        _append_pipeline_log(
            pipeline_id,
            f"[{_now_iso()}] test discovery: tests dir not found on host "
            f"({TESTS_DIR}) -- cannot pre-count scenarios; the container will "
            f"discover them itself.",
        )
        return
    filt = (f" matching the Tests filter [{tests_filter}]"
            if disco["has_filter"] else "")
    running_files = [f"{rel} ({run})" for rel, _perf, run in disco["per_file"] if run]
    lines = [
        f"[{_now_iso()}] test discovery: scanned {disco['files']} feature file(s) "
        f"under {disco['dir']} -- found {disco['total']} scenario(s); "
        f"{disco['perf']} tagged {disco['tag']} -> RUNNING {disco['running']}{filt}.",
    ]
    if running_files:
        lines.append(
            f"[{_now_iso()}]   running scenarios in: {', '.join(running_files)}")
    else:
        lines.append(
            f"[{_now_iso()}]   nothing matches -- this run will execute nothing "
            f"and exit quickly.")
    _append_pipeline_log(pipeline_id, *lines)


def _run_pipeline(pipeline_id: str) -> None:
    """Execute a pipeline's docker container. Runs in a worker thread.

    Hard invariant: this function NEVER lets a pipeline stay in `running`
    when it returns. Any exit path -- success, docker non-zero exit, missing
    binary, missing image, unexpected exception in the stdout-read loop --
    sets a final status (`finished` or `failed`) before returning. The
    outermost try/except ensures even a bug here can't strand a record."""
    import shutil
    import subprocess
    import threading
    import traceback

    final_status = None  # set by inner paths; the outer finally enforces it.
    proc = None
    try:
        with _pipelines_lock():
            pipelines = _load_pipelines()
            rec = next((p for p in pipelines if p.get("id") == pipeline_id), None)
        if rec is None:
            return

        # Surface, at the top of the run log, how many scenarios were discovered
        # and how many will actually execute (@perf) -- before the container starts.
        _log_scenario_discovery(pipeline_id)

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

        # Reuse the image if it's already built; otherwise build it once now.
        if not _ensure_docker_image(pipeline_id, assets):
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
                text=True, bufsize=1, close_fds=True,
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

        # Runtime watchdog: a hung container (or a leaked stdout fd that never
        # EOFs) would block this thread -- and thus the single-slot queue --
        # forever. After MAX_RUNTIME_SEC, kill the container + docker client and
        # close our read end so the loop below unblocks.
        container_name = f"dokimos-{pipeline_id}"
        timed_out = {"flag": False}
        def _on_timeout():
            timed_out["flag"] = True
            try:
                subprocess.run(["docker", "kill", container_name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            except Exception:
                pass
            try: proc.kill()
            except Exception: pass
            try: proc.stdout.close()   # unblocks a wedged readline in this thread
            except Exception: pass
        run_timeout_sec = int(rec.get("max_runtime_sec") or MAX_RUNTIME_SEC)
        watchdog = threading.Timer(run_timeout_sec, _on_timeout)
        watchdog.daemon = True
        watchdog.start()

        # Stream output, BATCHED. Writing pipelines.json per line is O(file size)
        # each and degrades to O(n^2) on chatty containers -- the real cause of the
        # runner grinding in json.dump and appearing wedged. Flush the buffer every
        # ~40 lines or ~2s instead of once per line.
        import time as _time
        log_buf, pending_mut, last_flush = [], {}, [_time.monotonic()]
        def _flush_logs(force=False):
            if not log_buf and not pending_mut:
                return
            if force or len(log_buf) >= 40 or (_time.monotonic() - last_flush[0]) >= 2.0:
                _append_pipeline_log(pipeline_id, *log_buf, **pending_mut)
                log_buf.clear(); pending_mut.clear(); last_flush[0] = _time.monotonic()

        # Live scenario progress: count distinct "Running Scenario <name>" lines
        # and derive a rate-based ETA. `scenario_count` (the @perf total) is the
        # denominator; if it's unknown we still report a running tally.
        _scen_re = _scenario_marker_re()
        _seen_scen = set()
        _first_scen = [None]
        _prog_total = rec.get("scenario_count")
        def _update_progress(line):
            m = _scen_re.search(line)
            if not m:
                return
            name = m.group(1).strip()
            if name in _seen_scen:
                return
            _seen_scen.add(name)
            if _first_scen[0] is None:
                _first_scen[0] = _time.monotonic()
            done = len(_seen_scen)
            elapsed = _time.monotonic() - _first_scen[0]
            eta_sec = rate_pm = pct = None
            if done > 0 and elapsed > 0:
                rate = done / elapsed  # scenarios/sec
                rate_pm = round(rate * 60, 1)
                if _prog_total:
                    pct = round(done / _prog_total * 100)
                    eta_sec = int(max(0, _prog_total - done) / rate) if rate > 0 else None
            pending_mut["progress"] = {
                "done": done, "total": _prog_total, "pct": pct,
                "eta_sec": eta_sec, "rate_per_min": rate_pm,
                "updated_at": _now_iso(),
            }

        image_missing_hint_emitted = False
        rp_url_found = None
        rp_url_locked = False   # True once we have the authoritative numeric-id URL
        try:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                _update_progress(line)
                # Surface a useful hint if Docker tells us the image isn't built.
                if (not image_missing_hint_emitted and (
                    "Unable to find image" in line or "pull access denied" in line
                )):
                    image_missing_hint_emitted = True
                    log_buf.extend([
                        line,
                        f"[{_now_iso()}] HINT: build the image first --",
                        f"[{_now_iso()}]   cd {assets['rel_root']}",
                        f"[{_now_iso()}]   DOCKER_BUILDKIT=1 docker build -f {assets['dockerfile']} -t {DOCKER_IMAGE} .",
                    ])
                    _flush_logs(force=True)
                    continue
                # Capture the Report Portal launch link. The suite prints a launch
                # id rather than a URL, so _extract_rp_url builds the deep-link. The
                # UUID line prints before the numeric one, so accept a provisional
                # UUID link and upgrade to the numeric id when it arrives, then lock.
                if not rp_url_locked:
                    rp_res = _extract_rp_url(line)
                    if rp_res:
                        cand, is_numeric = rp_res
                        if is_numeric or rp_url_found is None:
                            rp_url_found = cand
                            pending_mut["rp_url"] = cand
                            log_buf.append(line)
                            log_buf.append(f"[{_now_iso()}] Report Portal launch: {cand}")
                            _flush_logs(force=True)
                        if is_numeric:
                            rp_url_locked = True
                        continue
                log_buf.append(line)
                _flush_logs()
            _flush_logs(force=True)
        except Exception as exc:
            _flush_logs(force=True)
            # Unexpected error while streaming output -- log it but make sure
            # we still drain + wait the process and mark the pipeline failed.
            _append_pipeline_log(
                pipeline_id,
                f"[{_now_iso()}] ERROR: runner streaming loop crashed: "
                f"{type(exc).__name__}: {exc}",
                f"[{_now_iso()}] traceback: {traceback.format_exc()}",
            )
            final_status = "failed"

        watchdog.cancel()

        # Wait for the process even if streaming was interrupted.
        try:
            rc = proc.wait(timeout=30)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            rc = -1

        if timed_out["flag"]:
            final_status = "failed"
            _append_pipeline_log(
                pipeline_id,
                f"[{_now_iso()}] KILLED: exceeded max runtime ({run_timeout_sec}s); "
                f"container {container_name} stopped. Marking failed so the queue advances.",
                status="failed", finished_at=_now_iso(),
            )
        else:
            # No timeout: use the process exit code (unless an earlier path
            # already decided to fail) to choose between finished and failed.
            if final_status is None:
                final_status = "finished" if rc == 0 else "failed"
            end_mut = {"status": final_status, "finished_at": _now_iso()}
            # On a clean finish, peg the bar to 100% using the ACTUAL number of
            # scenarios that ran (distinct "Running Scenario" markers), not the
            # pre-run @perf estimate -- a Tests filter (e.g. --name) can narrow a
            # run to a single scenario, and we want 1/1, not 1/179.
            if final_status == "finished" and _seen_scen:
                actual = len(_seen_scen)
                end_mut["progress"] = {
                    "done": actual, "total": actual, "pct": 100,
                    "eta_sec": 0, "rate_per_min": None, "updated_at": _now_iso(),
                }
            _append_pipeline_log(
                pipeline_id,
                f"[{_now_iso()}] docker exited with code {rc} -- status: {final_status}",
                **end_mut,
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


def _dispatch_tick() -> None:
    """One scheduling pass. Enforces **a single running pipeline at a time**:

    1. Promote any `scheduled` job whose time has come into the queue, marked
       `priority` so it takes the *front* of the line (it was due to run, but a
       currently-running job is never preempted).
    2. If nothing is `running`, dequeue the next job and start it. Order:
       priority (scheduled-that-came-due) first, then FIFO by enqueue time.

    Safe to call from the dispatcher loop or inline after enqueuing (it holds
    the persistence lock and starts at most one runner)."""
    to_start = None
    with _pipelines_lock():
        pipelines = _load_pipelines()
        now = datetime.now().astimezone()
        changed = False

        # 1. Scheduled -> queued (front) when due.
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
                p["status"] = "queued"
                p["priority"] = True
                p["queued_at"] = _now_iso()
                p.setdefault("logs", []).append(
                    f"[{p['queued_at']}] scheduled time reached -> queued (front of line)")
                changed = True

        # 2. Start the next job only if the single run slot is free.
        if not any(p.get("status") == "running" for p in pipelines):
            queued = [p for p in pipelines if p.get("status") == "queued"]
            if queued:
                # Priority (scheduled-due) first; within each group FIFO by when
                # the job entered the queue.
                queued.sort(key=lambda p: (
                    not p.get("priority", False),
                    p.get("queued_at") or p.get("created_at") or "",
                ))
                nxt = queued[0]
                nxt["status"] = "running"
                nxt["started_at"] = _now_iso()
                nxt.setdefault("logs", []).append(
                    f"[{nxt['started_at']}] dequeued -> running (run slot acquired)")
                to_start = nxt["id"]
                changed = True

        if changed:
            _save_pipelines(pipelines)

    if to_start:
        _start_runner(to_start)


_SCHEDULER_STARTED = False
def _ensure_pipeline_scheduler() -> None:
    """Start the singleton dispatcher thread on first call. It enforces a single
    running pipeline at a time and drains the queue in FIFO order (scheduled
    jobs that come due jump to the front). See `_dispatch_tick`."""
    global _SCHEDULER_STARTED
    if _SCHEDULER_STARTED:
        return
    _SCHEDULER_STARTED = True
    import threading

    def loop():
        import time as _time
        while True:
            try:
                _dispatch_tick()
            except Exception:
                pass
            try:
                _check_group_completions()
            except Exception:
                pass
            _time.sleep(2)

    threading.Thread(target=loop, daemon=True, name="pipeline-dispatcher").start()


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


def _new_group_id() -> str:
    return "grp_" + secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]


def _load_groups() -> dict:
    if not os.path.isfile(GROUPS_FILE):
        return {}
    try:
        with open(GROUPS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_groups(groups: dict) -> None:
    """Atomic write (mirrors _save_pipelines). Guard with _pipelines_lock()."""
    tmp = GROUPS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(groups, f, indent=2)
    os.replace(tmp, GROUPS_FILE)


def _create_group_record(group_id: str, group_name: str, member_records: list) -> None:
    """Persist a group record linking the N iteration pipelines. The shared
    config snapshot drives the group view's config strip (all iterations share
    the same config)."""
    sample = member_records[0]
    with _pipelines_lock():
        groups = _load_groups()
        groups[group_id] = {
            "id":              group_id,
            "name":            group_name,
            "created_at":      _now_iso(),
            "iteration_count": len(member_records),
            "member_ids":      [r["id"] for r in member_records],
            "config": {k: sample.get(k) for k in (
                "env", "parallel", "feature", "tests", "max_runtime_sec",
                "scenario_retry", "scenario_count", "scenario_total", "est_runtime_sec")},
            "report_status":   "none",   # none | generating | ready | failed
            "report":          None,
            "report_error":    None,
        }
        _save_groups(groups)


def _check_group_completions() -> None:
    """Detect iteration groups whose every member reached a terminal state and
    auto-generate the aggregated perf report exactly once. Called by the
    dispatcher loop; the expensive generation runs in a worker thread."""
    to_generate = []
    with _pipelines_lock():
        groups = _load_groups()
        if not groups:
            return
        by_id = {p.get("id"): p for p in _load_pipelines()}
        changed = False
        for gid, g in groups.items():
            if g.get("report_status") != "none":
                continue
            members = [by_id.get(mid) for mid in g.get("member_ids", [])]
            members = [m for m in members if m]
            if not members or not all(
                    m.get("status") in ("finished", "failed") for m in members):
                continue
            g["report_status"] = "generating"
            g["report_error"] = None
            changed = True
            to_generate.append((gid, [m.get("rp_url") for m in members if m.get("rp_url")]))
        if changed:
            _save_groups(groups)
    for gid, urls in to_generate:
        _start_group_report(gid, urls)


def _start_group_report(group_id: str, urls: list) -> None:
    import threading
    threading.Thread(target=_run_group_report, args=(group_id, list(urls)),
                     daemon=True, name=f"group-report-{group_id}").start()


def _run_group_report(group_id: str, urls: list) -> None:
    """Generate the aggregated password-protected perf report for a finished
    group from its members' Report Portal launch links. Worker thread (the
    generation does network I/O to Report Portal). Reuses _run_generation, so
    the report is AES-256-GCM encrypted and stored under reports/ exactly like
    a manual Generate-tab report. The one-time password is kept in memory."""
    seen, deduped = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    dropped = max(0, len(deduped) - GROUP_REPORT_MAX_LAUNCHES)
    deduped = deduped[:GROUP_REPORT_MAX_LAUNCHES]
    with _pipelines_lock():
        g0 = _load_groups().get(group_id) or {}
    dbx_dir = g0.get("report_dbx_dir") or None
    dbx_temp = bool(g0.get("report_dbx_temp"))
    try:
        if not deduped:
            raise RuntimeError(
                "no Report Portal launches were produced by this group's runs "
                "(RP disabled / token missing, or no run reached a launch)")
        result = _run_generation(deduped, databricks_log_dir=dbx_dir)  # dict incl. report_password
        password = result.pop("report_password", None)
        result["dropped_launches"] = dropped
        result["has_databricks"] = bool(dbx_dir)
        with _pipelines_lock():
            groups = _load_groups()
            g = groups.get(group_id)
            if g is not None:
                g["report_status"] = "ready"
                g["report"] = result
                g["report_error"] = None
                _save_groups(groups)
        if password:
            _group_report_passwords[group_id] = password
    except Exception as exc:
        with _pipelines_lock():
            groups = _load_groups()
            g = groups.get(group_id)
            if g is not None:
                g["report_status"] = "failed"
                g["report_error"] = f"{type(exc).__name__}: {exc}"
                _save_groups(groups)
    finally:
        # Remove the materialized upload tempdir and clear the marker so a later
        # auto-regen doesn't point at a deleted dir.
        if dbx_temp and dbx_dir:
            import shutil
            shutil.rmtree(dbx_dir, ignore_errors=True)
            with _pipelines_lock():
                groups = _load_groups()
                g = groups.get(group_id)
                if g is not None and g.get("report_dbx_temp"):
                    g["report_dbx_dir"] = None
                    g["report_dbx_temp"] = False
                    _save_groups(groups)


def _cleanup_pipelines(statuses=("failed",)) -> dict:
    """Remove pipelines in the given terminal statuses (default: failed). Never
    removes a running/queued/scheduled job. Prunes group member refs and drops
    groups left with no members. Returns counts."""
    statuses = tuple(s for s in statuses if s in ("failed", "finished"))
    if not statuses:
        return {"removed": 0, "groups_removed": 0}
    with _pipelines_lock():
        pipelines = _load_pipelines()
        keep = [p for p in pipelines if p.get("status") not in statuses]
        removed = len(pipelines) - len(keep)
        if removed:
            _save_pipelines(keep)
        live_ids = {p.get("id") for p in keep}
        groups = _load_groups()
        groups_removed = 0
        changed = False
        for gid in list(groups.keys()):
            mids = [m for m in groups[gid].get("member_ids", []) if m in live_ids]
            if not mids:
                del groups[gid]
                _group_report_passwords.pop(gid, None)
                groups_removed += 1
                changed = True
            elif len(mids) != len(groups[gid].get("member_ids", [])):
                groups[gid]["member_ids"] = mids
                changed = True
        if changed:
            _save_groups(groups)
    return {"removed": removed, "groups_removed": groups_removed}


def _reconcile_orphan_groups() -> int:
    """A group left 'generating' at boot lost its worker thread on restart.
    Flip it back to 'none' so the dispatcher regenerates (its members are
    already terminal). Any in-memory password died with the old process."""
    with _pipelines_lock():
        groups = _load_groups()
        n = 0
        for g in groups.values():
            if g.get("report_status") == "generating":
                g["report_status"] = "none"
                n += 1
        if n:
            _save_groups(groups)
        return n


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _create_pipeline(cfg: dict, iteration_index: int = 1, iteration_count: int = 1,
                     group_id: Optional[str] = None,
                     group_name: Optional[str] = None) -> dict:
    """Build a new pipeline record from the run config posted by the SPA,
    persist it, and return the full record. When part of a multi-iteration
    burst, ``iteration_index`` / ``iteration_count`` drive name suffixing
    (e.g. ``nightly-smoke-1``, ``nightly-smoke-2``) and ``group_id`` links the
    iterations into one report package (see [groups.json])."""
    kind = cfg.get("kind") if cfg.get("kind") in _PIPELINE_KINDS else "now"
    now = datetime.now().astimezone()
    if kind == "now":
        # `now` jobs enter the queue immediately; the dispatcher promotes one to
        # `running` when the single run slot is free (FIFO).
        status, started_at, scheduled_for = "queued", None, None
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

    # For grouped bursts use the caller-provided stable group_name as the base
    # so every iteration shares one prefix; otherwise fall back to cfg/default.
    name = (group_name or cfg.get("name") or "").strip()
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
        "max_runtime_sec": _clamp_max_runtime_sec(cfg.get("max_runtime_min")),
        "scenario_retry": _clamp_scenario_retry(cfg.get("scenario_retry")),
        "group_id":      group_id,
        "group_name":    group_name,
        "iteration_index": iteration_index,
        "iteration_count": iteration_count,
        "created_at":    now.isoformat(timespec="seconds"),
        "scheduled_for": scheduled_for,
        "queued_at":     now.isoformat(timespec="seconds") if kind == "now" else None,
        "priority":      False,
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
    record["logs"].append(
        f"[{record['created_at']}] max runtime: {record['max_runtime_sec']}s "
        f"({record['max_runtime_sec'] // 60} min) -- watchdog kills the run past this"
    )
    record["logs"].append(
        f"[{record['created_at']}] scenario retries: {record['scenario_retry']} "
        f"attempt(s) per scenario (IFP_SCENARIO_RETRY; 1 = no retry)"
    )

    # Pre-run discovery + a rough duration estimate for the job-config panel.
    # @perf scenarios all live in one feature, and feature-scheme parallelism
    # can't split one file, so effective parallelism is the number of distinct
    # selected feature files (capped by the thread count) -- usually 1.
    disco = _discover_perf_scenarios(record["tests"])
    if disco:
        run_n = disco["running"]
        files_with_sel = sum(1 for _rel, _perf, run in disco["per_file"] if run)
        eff_par = max(1, min(record["parallel"], files_with_sel or 1))
        # Fixed per-iteration overhead + scenario time spread across the effective
        # parallelism (scenarios divide, overhead doesn't).
        est_sec = int(EST_FIXED_OVERHEAD_SEC + -(-(run_n * EST_SEC_PER_SCENARIO) // eff_par)) if run_n else 0
        record["scenario_count"] = run_n            # filter-aware: what actually runs
        record["scenario_total"] = disco["total"]
        record["scenario_files"] = files_with_sel
        record["effective_parallel"] = eff_par
        record["est_runtime_sec"] = est_sec
        filt = " (Tests filter applied)" if disco["has_filter"] else ""
        record["logs"].append(
            f"[{record['created_at']}] discovered {disco['perf']} @perf scenario(s) "
            f"(of {disco['total']} in api_tests); RUNNING {run_n}{filt} across "
            f"{files_with_sel} file(s); effective parallelism {eff_par} -> "
            f"est. runtime ~{max(1, round(est_sec / 60))} min "
            f"(@~{EST_SEC_PER_SCENARIO:g}s/scenario, retries excluded)"
        )
    else:
        record["scenario_count"] = None
        record["scenario_total"] = None
        record["scenario_files"] = None
        record["effective_parallel"] = None
        record["est_runtime_sec"] = None

    if status == "queued":
        record["logs"].append(
            f"[{record['created_at']}] state: queued (waiting for the run slot, FIFO)")
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


# Credential keys the report generator needs; sourced from tests/.envrc when
# absent from the SPA process env (systemd services don't inherit the shell env,
# so RP_TOKEN -- which the test suite keeps in .envrc -- isn't present here).
_RP_ENVRC_KEYS = ("RP_TOKEN", "RP_API_BASE", "DISNEY_DD_API_KEY", "DISNEY_DD_APP_KEY")


def _rp_envrc_path() -> Optional[str]:
    override = os.environ.get("DOKIMOS_PERF_ENVRC")
    if override and os.path.isfile(override):
        return override
    if TESTS_DIR:
        cand = os.path.join(TESTS_DIR, "tests", ".envrc")
        if os.path.isfile(cand):
            return cand
    return None


def _parse_envrc(path: str) -> dict:
    """Best-effort parse of `export KEY=value` lines for the RP/DD credential
    keys. Pure text parsing -- never executes the file. Skips values that need
    shell evaluation ($(...), backticks, ${...})."""
    import re as _re
    out = {}
    pat = _re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.lstrip().startswith("#"):
                    continue
                m = pat.match(line)
                if not m or m.group(1) not in _RP_ENVRC_KEYS:
                    continue
                v = m.group(2)
                if v[:1] in ("\"", "'") and v[-1:] == v[:1]:
                    v = v[1:-1]
                if "$(" in v or "`" in v or v.startswith("$"):
                    continue
                out[m.group(1)] = v
    except OSError:
        return {}
    return out


def _ensure_rp_credentials() -> None:
    """Make RP_TOKEN (+ RP_API_BASE, optional Datadog keys) available to the
    report generator. The SPA runs as a systemd service that does NOT inherit
    the shell env, so RP_TOKEN isn't present even though the test suite uses it
    constantly -- it lives in tests/.envrc (owned/readable by this user). Load
    it from there when missing, into BOTH os.environ and the analyzer module
    globals (analyzer.py binds TOKEN/API_BASE at import time)."""
    if not os.environ.get("RP_TOKEN"):
        path = _rp_envrc_path()
        if path:
            vals = _parse_envrc(path)
            for k in _RP_ENVRC_KEYS:
                if vals.get(k) and not os.environ.get(k):
                    os.environ[k] = vals[k]
    import sys as _sys
    mod = _sys.modules.get("rp_perf_report.analyzer") or _sys.modules.get("analyzer")
    if mod is not None:
        for env_key, attr in (("RP_TOKEN", "TOKEN"), ("RP_API_BASE", "API_BASE"),
                              ("DISNEY_DD_API_KEY", "DD_API_KEY"),
                              ("DISNEY_DD_APP_KEY", "DD_APP_KEY")):
            val = os.environ.get(env_key)
            if val and hasattr(mod, attr):
                setattr(mod, attr, val)


def _run_generation(urls: list, payload_key: Optional[str] = None,
                    databricks_log_dir: Optional[str] = None) -> dict:
    _ensure_rp_credentials()
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
.pipeline-item.status-queued    { border-left-color: #a78bfa; }
.pipeline-item.status-scheduled { border-left-color: #f59e0b; }
.pipeline-item.status-finished  { border-left-color: #10b981; }
.pipeline-item.status-failed    { border-left-color: var(--red); }
.pipeline-item-actions { margin-top: 6px; }
.pipeline-rerun-btn {
    background: transparent;
    color: var(--bronze);
    border: 1px solid var(--bronze-dk);
    border-radius: 6px;
    padding: 3px 10px;
    font-family: var(--font-mono);
    font-size: 11px;
    cursor: pointer;
}
.pipeline-rerun-btn:hover { background: rgba(205,127,50,0.18); color: var(--bronze-lt); }

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
.pipelines-view-status .status-queued    { background: rgba(167,139,250,0.20); color: #c4b5fd; }
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

/* ----- Iteration groups (visual wrapper around member pipelines) ----- */
.pipeline-group-wrap {
    border: 1px solid rgba(176,141,87,0.5); border-radius: 8px; margin: 8px 0;
    background: rgba(176,141,87,0.06); overflow: hidden;
}
.pipeline-group-head {
    display: flex; align-items: center; gap: 8px; padding: 8px 10px; cursor: pointer;
    font-weight: 600; background: rgba(176,141,87,0.12);
    border-bottom: 1px solid rgba(176,141,87,0.25);
}
.pipeline-group-head:hover { background: rgba(176,141,87,0.20); }
.pipeline-group-head.active { background: rgba(176,141,87,0.32); }
.pipeline-group-members { padding: 4px 6px 6px 12px; }
.pipeline-group-members .pipeline-item { margin: 3px 0; }
.group-count { opacity: 0.6; font-size: 12px; }
.group-badge { font-size: 11px; opacity: 0.75; margin-left: auto; }
.group-badge-done { color: #3fb950; opacity: 0.95; }
/* ----- Group view (right panel): hero ring + cards ----- */
.pipelines-view-group { flex: 1; overflow: auto; padding: 20px 22px; }
/* Fill 100% of the logs panel: left (config/iterations) + right (report) columns. */
.gv { display: flex; flex-direction: row; align-items: flex-start; gap: 18px; width: 100%; }
.gv-main { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; gap: 16px; }
.gv-side { flex: 1 1 0; min-width: 0; position: sticky; top: 0; display: flex; flex-direction: column; gap: 16px; }
.gv-side .gv-card { margin: 0; }
@media (max-width: 900px) {
    .gv { flex-direction: column; }
    .gv-main, .gv-side { flex: 1 1 auto; width: 100%; position: static; }
}
.gv-hero { display: flex; align-items: center; gap: 20px; padding: 18px 20px; border-radius: 14px;
    background: linear-gradient(135deg, rgba(176,141,87,0.12), rgba(255,255,255,0.015));
    border: 1px solid rgba(176,141,87,0.28); }
.gv-ring { width: 86px; height: 86px; border-radius: 50%; flex: 0 0 auto; display: flex;
    align-items: center; justify-content: center; transition: background .6s ease; }
.gv-ring-in { width: 64px; height: 64px; border-radius: 50%; background: var(--bg-dark, #0d1117);
    display: flex; align-items: center; justify-content: center; }
.gv-ring-pct { font: 700 21px var(--font-mono); color: #e6edf3; }
.gv-hero-meta { display: flex; flex-direction: column; gap: 7px; min-width: 0; }
.gv-hero-h1 { font-size: 24px; font-weight: 700; color: #e6edf3; }
.gv-hero-h2 { font-size: 16px; color: #9aa4af; }
.gv-chips { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 4px; }
.gv-chip { font-size: 13px; font-weight: 600; padding: 3px 12px; border-radius: 999px;
    background: rgba(255,255,255,0.07); color: #c9d1d9; }
.gv-chip-running { background: rgba(88,166,255,0.18); color: #79b8ff; }
.gv-chip-finished { background: rgba(63,185,80,0.18); color: #56d364; }
.gv-chip-failed { background: rgba(248,81,73,0.18); color: #f85149; }
.gv-chip-queued, .gv-chip-scheduled { background: rgba(176,141,87,0.20); color: #d6b98a; }
.gv-card { padding: 14px 16px; border-radius: 12px; background: rgba(255,255,255,0.025);
    border: 1px solid rgba(255,255,255,0.07); }
.gv-card-h { font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: .07em;
    color: #9aa4af; margin-bottom: 12px; }
.gv-card-sub { font-weight: 400; text-transform: none; letter-spacing: 0; opacity: 0.7; margin-left: 6px; }
.gv .pipelines-view-config-grid { font-size: 14px; row-gap: 9px; }
.gv .pipelines-view-config-grid strong { font-size: 13px; }
.gv-iters { display: flex; flex-direction: column; gap: 6px; }
.gv-iter { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 8px;
    background: rgba(255,255,255,0.02); cursor: pointer; font-size: 14px; transition: background .15s; }
.gv-iter:hover { background: rgba(255,255,255,0.07); }
.gv-iter-idx { font-family: var(--font-mono); font-size: 14px; opacity: 0.6; min-width: 34px; }
.gv-iter-status { min-width: 78px; font-size: 13px; font-weight: 600; text-transform: capitalize; }
.gv-st-finished { color: #3fb950; } .gv-st-failed { color: #f85149; }
.gv-st-running { color: #58a6ff; } .gv-st-queued, .gv-st-scheduled { color: #d6b98a; }
.gv-iter-bar { flex: 1; height: 6px; background: #21262d; border-radius: 3px; overflow: hidden; }
.gv-iter-bar > span { display: block; height: 100%; transition: width .6s ease; }
.gv-iter-num { font-family: var(--font-mono); font-size: 14px; opacity: 0.65; min-width: 58px; text-align: right; }
.gv-iter-norp { opacity: 0.4; font-size: 13px; min-width: 64px; text-align: right; }
/* report card */
.gv-report-ready { border-color: rgba(63,185,80,0.38); background: rgba(63,185,80,0.05); }
.gv-report-failed { border-color: rgba(248,81,73,0.38); background: rgba(248,81,73,0.05); }
.gv-report-badge { display: inline-block; font-size: 13px; font-weight: 700; padding: 2px 10px;
    border-radius: 999px; background: rgba(63,185,80,0.22); color: #56d364; margin-right: 8px; }
.gv-report-meta { font-size: 16px; color: #d6dde5; margin-bottom: 14px; }
.gv-report-busy { font-size: 15px; color: #9aa4af; display: flex; align-items: center; gap: 8px; }
.gv-report-wait { font-size: 15px; color: #9aa4af; }
.gv-report-err { font-family: var(--font-mono); font-size: 13px; color: #f85149; margin-bottom: 12px; word-break: break-word; }
.gv-report-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.gv-dbx { flex: 1; min-width: 240px; font-family: var(--font-mono); font-size: 14px; padding: 10px 12px;
    border-radius: 8px; border: 1px solid rgba(255,255,255,0.15); background: var(--bg-dark, #0d1117); color: #e6edf3; }
.gv-dbx:focus { outline: none; border-color: rgba(176,141,87,0.65); }
.gv-dbx-hint { font-size: 13px; opacity: 0.6; margin-top: 8px; }
.gv-dbx-section { margin: 4px 0 12px; }
.gv-dbx-section .dbx-dropzone { margin: 0; }
.gv-btn { font-family: var(--font-mono); font-size: 13px; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase; padding: 10px 18px; border-radius: 8px; cursor: pointer;
    border: 1px solid transparent; text-decoration: none; display: inline-flex; align-items: center; gap: 6px;
    white-space: nowrap; transition: background .12s, color .12s, border-color .12s; }
.gv-btn-primary { background: var(--bronze); color: var(--bg-dark); border-color: var(--bronze); }
.gv-btn-primary:hover { background: var(--bronze-lt); border-color: var(--bronze-lt); }
/* "Generate report" (regen) state stays in the gold palette, just brighter. */
.gv-btn-primary.regen { background: var(--bronze-lt); border-color: var(--bronze-lt); }
.gv-btn-primary.regen:hover { background: var(--bronze); border-color: var(--bronze); }
.gv-btn-ghost { background: transparent; color: var(--bronze-lt); border-color: var(--bronze-dk); }
.gv-btn-ghost:hover { background: rgba(205,127,50,0.15); color: var(--bronze-lt); border-color: var(--bronze); }
.gv-pw { margin-top: 16px; }
.gv-pw label { display: block; font-size: 14px; color: #c9d1d9; margin-bottom: 7px; }
.gv-pw-warn { color: #d29922; font-weight: 500; }
.gv-pw-row { display: flex; gap: 8px; max-width: 480px; }
.gv-pw-row input { flex: 1; font-family: var(--font-mono); font-size: 15px; letter-spacing: 1px;
    padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(210,153,34,0.4);
    background: rgba(210,153,34,0.08); color: #f0d999; }
.gv-pw-gone { margin-top: 16px; font-size: 14px; opacity: 0.85; }
.group-error { padding: 18px; color: #f85149; }
.pipelines-cleanup-btn {
    float: right; font-size: 11px; cursor: pointer; padding: 1px 8px; border-radius: 4px;
    border: 1px solid rgba(248,81,73,0.45); background: transparent; color: #f85149;
}
.pipelines-cleanup-btn:hover { background: rgba(248,81,73,0.14); }
.pipeline-kill-btn {
    font-size: 11px; cursor: pointer; padding: 1px 8px; border-radius: 4px;
    border: 1px solid rgba(248,81,73,0.5); background: transparent; color: #f85149;
}
.pipeline-kill-btn:hover { background: rgba(248,81,73,0.16); }
.group-rerun-btn { margin-left: 8px; }

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
                <div class="tests-control">
                    <label>Max runtime (min)</label>
                    <input type="number" id="testsMaxRuntime" min="1" max="{MAX_RUNTIME_CEILING_MIN}"
                           value="{MAX_RUNTIME_SEC // 60}"
                           title="Watchdog kills the run if it exceeds this many minutes">
                </div>
                <div class="tests-control">
                    <label>Retries</label>
                    <input type="number" id="testsRetries" min="1" max="{SCENARIO_RETRY_CEILING}"
                           value="{SCENARIO_RETRY_DEFAULT}"
                           title="Attempts per scenario (IFP_SCENARIO_RETRY); 1 = run once, no retry">
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
                    <div class="pipelines-view-progress" id="pipelinesViewProgress" hidden></div>
                    <pre class="pipelines-view-logs" id="pipelinesViewLogs">No pipeline selected.</pre>
                    <div class="pipelines-view-group" id="pipelinesViewGroup" hidden></div>
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
            if (name !== 'pipelines') _leaveGroupView();
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
            let maxrt = parseInt(document.getElementById('testsMaxRuntime').value, 10);
            if (!Number.isFinite(maxrt) || maxrt < 1) maxrt = {MAX_RUNTIME_SEC // 60};
            if (maxrt > {MAX_RUNTIME_CEILING_MIN}) maxrt = {MAX_RUNTIME_CEILING_MIN};
            let retries = parseInt(document.getElementById('testsRetries').value, 10);
            if (!Number.isFinite(retries) || retries < 1) retries = {SCENARIO_RETRY_DEFAULT};
            if (retries > {SCENARIO_RETRY_CEILING}) retries = {SCENARIO_RETRY_CEILING};
            return {{
                env:            document.getElementById('testsEnv').value,
                parallel:       parseInt(document.getElementById('testsParallel').value, 10),
                name:           document.getElementById('testsName').value.trim(),
                feature:        document.getElementById('testsFeature').value.trim(),
                tests:          document.getElementById('testsFilter').value.trim(),
                max_runtime_min: maxrt,
                scenario_retry: retries,
                iterations:     iters,
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
        let _selectedGroupId    = null;   // when set, the right panel shows the group view (no logs)
        let _expandedGroups     = new Set();
        let _pipelinesPollTimer = null;
        let _logsPollTimer      = null;
        let _medianRunSec = 0;   // updated each render from past run durations

        function _computeMedianRunSec(pipelines) {{
            const durs = [];
            (pipelines || []).forEach(function(p) {{
                if (p.started_at && p.finished_at) {{
                    const d = (new Date(p.finished_at) - new Date(p.started_at)) / 1000;
                    if (d > 10 && d < 3 * 3600) durs.push(d);
                }}
            }});
            if (!durs.length) return 0;
            durs.sort(function(a, b) {{ return a - b; }});
            return durs[Math.floor(durs.length / 2)];
        }}

        function _etaText(p) {{
            // Estimated finish for a running pipeline, from the median past duration.
            if (p.status !== 'running' || !p.started_at || _medianRunSec <= 0) return '';
            const started = new Date(p.started_at).getTime();
            if (isNaN(started)) return '';
            const etaMs = started + _medianRunSec * 1000;
            const remMin = Math.round((etaMs - Date.now()) / 60000);
            const clock = new Date(etaMs).toLocaleTimeString();
            return remMin > 0
                ? (' · ~' + remMin + ' min left (est. finish ' + clock + ')')
                : ' · past estimate, finishing…';
        }}

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
                : (p.status === 'queued')
                    ? ('queued ' + _humanWhen(p.queued_at || p.created_at) + (p.priority ? ' · priority' : ''))
                : (p.status === 'finished')
                    ? 'finished ' + _humanWhen(p.finished_at)
                    : (p.status === 'running')
                        ? ('started ' + _humanWhen(p.started_at || p.created_at) + _etaText(p))
                        : 'created ' + _humanWhen(p.created_at);
            const rp = p.rp_url
                ? '<a class="rp-link" href="' + _testsEscape(p.rp_url) + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">Report Portal &rarr;</a>'
                : (p.status === 'finished' ? '<span class="rp-link" style="border-bottom-style:dotted;opacity:.5">RP link pending</span>' : '');
            const cls = 'pipeline-item status-' + p.status + (p.id === _selectedPipelineId ? ' active' : '');
            // Pulsing green dot for the running ones -- visible-at-a-glance
            // signal that something is actually in flight.
            const liveDot = (p.status === 'running') ? '<span class="live-dot" title="running"></span>' : '';
            // Re-run button for terminal states. stopPropagation so clicking it
            // doesn't also select/open the pipeline. Enqueues a fresh pipeline
            // with the same config at the end of the queue.
            let actions = '';
            if (p.status === 'finished' || p.status === 'failed') {{
                actions = '<div class="pipeline-item-actions">'
                    +   '<button class="pipeline-rerun-btn" title="Re-run with the same configuration" '
                    +     'onclick="event.stopPropagation(); _rerunPipeline(\\'' + _testsEscape(p.id) + '\\')">&#8635; rerun</button>'
                    + '</div>';
            }} else if (p.status === 'running') {{
                actions = '<div class="pipeline-item-actions">'
                    +   '<button class="pipeline-kill-btn" title="Kill this running pipeline" '
                    +     'onclick="event.stopPropagation(); _killPipeline(\\'' + _testsEscape(p.id) + '\\')">&#10005; kill</button>'
                    + '</div>';
            }}
            return '<div class="' + cls + '" onclick="_selectPipeline(\\'' + _testsEscape(p.id) + '\\')">'
                 +   '<div class="pipeline-item-name">' + liveDot + _testsEscape(p.name) + '</div>'
                 +   '<div class="pipeline-item-meta">' + _testsEscape(meta) + '</div>'
                 +   '<div class="pipeline-item-meta">' + _testsEscape(timing) + '</div>'
                 +   (rp ? '<div>' + rp + '</div>' : '')
                 +   actions
                 + '</div>';
        }}

        function _groupRowHtml(gid, info) {{
            // A visual wrapper box: a clickable group header (-> aggregate group
            // view) with the individual iteration pipelines nested inside (each
            // still clickable -> its own logs).
            const members = info.members.slice().sort(function(a, b) {{
                return (a.iteration_index || 0) - (b.iteration_index || 0);
            }});
            const total = members.length;
            const done = members.filter(function(m) {{ return m.status === 'finished' || m.status === 'failed'; }}).length;
            const running = members.some(function(m) {{ return m.status === 'running'; }});
            const allDone = done === total && total > 0;
            const liveDot = running ? '<span class="live-dot" title="running"></span>' : '';
            const tag = allDone
                ? '<span class="group-badge group-badge-done">done &middot; open report</span>'
                : '<span class="group-badge">' + done + '/' + total + ' done</span>';
            const headCls = 'pipeline-group-head' + (gid === _selectedGroupId ? ' active' : '');
            // Re-run the whole group once it's terminal (enqueues a fresh group).
            const rerun = allDone
                ? '<button class="pipeline-rerun-btn group-rerun-btn" title="Re-run the whole group" '
                +   'onclick="event.stopPropagation(); _rerunGroup(\\'' + _testsEscape(gid) + '\\')">&#8635; rerun group</button>'
                : '';
            return '<div class="pipeline-group-wrap">'
                 +   '<div class="' + headCls + '" onclick="_selectGroup(\\'' + _testsEscape(gid) + '\\')">'
                 +     '<span class="pipeline-item-name">' + liveDot + _testsEscape(info.name) + '</span>'
                 +     '<span class="group-count">&times;' + total + '</span>'
                 +     tag + rerun
                 +   '</div>'
                 +   '<div class="pipeline-group-members">'
                 +     members.map(_pipelineItemHtml).join('')
                 +   '</div>'
                 + '</div>';
        }}

        // A group's aggregate status decides which status section it sits in,
        // alongside single pipelines (running wins, then queued/scheduled, then
        // failed if any failed, else finished).
        function _groupAggStatus(members) {{
            if (members.some(function(m) {{ return m.status === 'running'; }})) return 'running';
            if (members.some(function(m) {{ return m.status === 'queued'; }})) return 'queued';
            if (members.some(function(m) {{ return m.status === 'scheduled'; }})) return 'scheduled';
            if (members.some(function(m) {{ return m.status === 'failed'; }})) return 'failed';
            return 'finished';
        }}

        function _renderPipelinesList(pipelines) {{
            const panel = document.getElementById('pipelinesListPanel');
            if (!panel) return;
            _medianRunSec = _computeMedianRunSec(pipelines);
            // Each entry is either a single pipeline or a whole group wrapper, and
            // is filed into the SAME status section it belongs to. Grouped
            // iterations render inside their group wrapper (not as loose rows).
            const groups = {{}};   // gid -> {{name, members:[], created}}
            const singles = [];
            (pipelines || []).forEach(function(p) {{
                if (p.group_id) {{
                    (groups[p.group_id] || (groups[p.group_id] = {{name: p.group_name || p.group_id, members: [], created: p.created_at}})).members.push(p);
                }} else {{
                    singles.push(p);
                }}
            }});
            const buckets = {{ running: [], queued: [], scheduled: [], finished: [], failed: [] }};
            singles.forEach(function(p) {{
                const e = {{kind: 'single', p: p, ts: (p.finished_at || p.started_at || p.queued_at || p.created_at || '')}};
                (buckets[p.status] || buckets.finished).push(e);
            }});
            Object.keys(groups).forEach(function(gid) {{
                const info = groups[gid];
                const st = _groupAggStatus(info.members);
                const tss = info.members.map(function(m) {{ return (m.finished_at || m.started_at || m.queued_at || m.created_at || ''); }}).sort();
                buckets[st].push({{kind: 'group', gid: gid, info: info, ts: tss[tss.length - 1] || info.created || ''}});
            }});
            // Sort each section: scheduled ascending (soonest first), others most-recent first.
            Object.keys(buckets).forEach(function(st) {{
                buckets[st].sort(function(a, b) {{
                    return st === 'scheduled' ? a.ts.localeCompare(b.ts) : b.ts.localeCompare(a.ts);
                }});
            }});

            function renderEntry(e) {{
                return e.kind === 'group' ? _groupRowHtml(e.gid, e.info) : _pipelineItemHtml(e.p);
            }}
            function section(label, items, action) {{
                const body = items.length
                    ? items.map(renderEntry).join('')
                    : '<div class="pipelines-list-empty">none</div>';
                return '<div class="pipelines-list-section">'
                     +   '<div class="pipelines-list-section-title">' + label
                     +     ' <span class="count">' + items.length + '</span>'
                     +     (action || '')
                     +   '</div>'
                     +   body
                     + '</div>';
            }}
            const failedAction = buckets.failed.length
                ? '<button class="pipelines-cleanup-btn" title="Remove all failed pipelines" onclick="event.stopPropagation(); _cleanupStatus(\\'failed\\')">clear all</button>'
                : '';
            const finishedAction = buckets.finished.length
                ? '<button class="pipelines-cleanup-btn" title="Remove all finished pipelines" onclick="event.stopPropagation(); _cleanupStatus(\\'finished\\')">clear all</button>'
                : '';
            panel.innerHTML =
                  section('Running',   buckets.running)
                + section('Queued',    buckets.queued)
                + section('Scheduled', buckets.scheduled)
                + section('Finished',  buckets.finished, finishedAction)
                + section('Failed',    buckets.failed, failedAction);
        }}

        async function _cleanupStatus(status) {{
            if (!window.confirm('Remove all ' + status + ' pipelines? This cannot be undone.')) return;
            try {{
                const res = await fetch('/api/pipelines/cleanup', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{statuses: [status]}}),
                }});
                if (res.ok) {{
                    // If the open pipeline was just removed, reset the view.
                    _selectedPipelineId = null;
                    const logs = document.getElementById('pipelinesViewLogs');
                    if (logs) {{ logs.hidden = false; logs.textContent = 'No pipeline selected.'; }}
                    ['pipelinesViewConfig', 'pipelinesViewProgress'].forEach(function(id) {{
                        const el = document.getElementById(id); if (el) el.hidden = true;
                    }});
                }}
            }} catch (_e) {{}}
            await _refreshPipelines();
        }}

        async function _refreshPipelines() {{
            try {{
                const res = await fetch('/api/pipelines');
                if (!res.ok) return;
                const data = await res.json();
                _renderPipelinesList(data.pipelines || []);
            }} catch (_e) {{ /* leave previous render */ }}
        }}

        async function _rerunPipeline(id) {{
            try {{
                const res = await fetch('/api/pipelines/' + encodeURIComponent(id) + '/rerun', {{ method: 'POST' }});
                if (res.ok) {{
                    const data = await res.json();
                    if (data && data.pipeline) _selectedPipelineId = data.pipeline.id;
                }}
            }} catch (_e) {{ /* swallow; the refresh below reflects real state */ }}
            await _refreshPipelines();
        }}

        async function _killPipeline(id) {{
            if (!window.confirm('Kill this running pipeline? The run will be marked failed.')) return;
            try {{ await fetch('/api/pipelines/' + encodeURIComponent(id) + '/kill', {{ method: 'POST' }}); }} catch (_e) {{}}
            await _refreshPipelines();
            if (_selectedPipelineId === id) await _refreshLogs();
        }}

        async function _rerunGroup(gid) {{
            try {{
                const res = await fetch('/api/groups/' + encodeURIComponent(gid) + '/rerun', {{ method: 'POST' }});
                if (res.ok) {{
                    const data = await res.json();
                    if (data && data.group_id) await _selectGroup(data.group_id);
                }}
            }} catch (_e) {{}}
            await _refreshPipelines();
        }}

        function _leaveGroupView() {{
            // Switching away from a group view destroys its one-time report
            // password (server-side + DOM), per the "shown once" model.
            if (_selectedGroupId) {{
                const gid = _selectedGroupId;
                try {{ fetch('/api/groups/' + encodeURIComponent(gid) + '/forget-password', {{method: 'POST'}}); }} catch (_e) {{}}
            }}
            _selectedGroupId = null;
            const gv = document.getElementById('pipelinesViewGroup');
            if (gv) {{ gv.hidden = true; gv.innerHTML = ''; }}
            const logs = document.getElementById('pipelinesViewLogs');
            if (logs) logs.hidden = false;
        }}

        function _toggleGroup(gid) {{
            if (_expandedGroups.has(gid)) _expandedGroups.delete(gid);
            else _expandedGroups.add(gid);
            _refreshPipelines();
        }}

        async function _selectGroup(gid, opts) {{
            if (_selectedGroupId && _selectedGroupId !== gid) _leaveGroupView();
            _selectedGroupId = gid;
            _selectedPipelineId = null;
            // Group view replaces the per-pipeline panels (no logs for a group).
            ['pipelinesViewConfig', 'pipelinesViewProgress', 'pipelinesViewLogs'].forEach(function(id) {{
                const el = document.getElementById(id); if (el) el.hidden = true;
            }});
            const gv = document.getElementById('pipelinesViewGroup'); if (gv) gv.hidden = false;
            document.querySelectorAll('.pipeline-item.active, .pipeline-group.active').forEach(function(e) {{ e.classList.remove('active'); }});
            document.querySelectorAll('.pipeline-group').forEach(function(e) {{
                if (e.getAttribute('onclick') && e.getAttribute('onclick').indexOf(gid) >= 0) e.classList.add('active');
            }});
            await _refreshGroupView();
            if (_logsPollTimer) clearInterval(_logsPollTimer);
            _logsPollTimer = setInterval(_refreshGroupView, 4000);
            if (!opts || !opts.fromPop) {{
                const target = '/groups/' + gid;
                if (window.location.pathname !== target) history.pushState({{tab: 'pipelines', gid: gid}}, '', target);
            }}
        }}

        function _groupConfigRows(c) {{
            if (!c) return '';
            const rows = [
                ['env', c.env], ['parallel', c.parallel],
                ['retries', c.scenario_retry], ['max runtime', c.max_runtime_sec ? (Math.round(c.max_runtime_sec / 60) + ' min') : ''],
                ['scenarios/iter', c.scenario_count != null ? (c.scenario_count + (c.scenario_total ? ' @perf (of ' + c.scenario_total + ')' : '')) : ''],
                ['est/iter', c.est_runtime_sec != null ? ('~' + Math.max(1, Math.round(c.est_runtime_sec / 60)) + ' min') : ''],
                ['feature', c.feature || ''], ['tests', c.tests || ''],
            ];
            return '<div class="pipelines-view-config-grid">' + rows.map(function(r) {{
                const v = (r[1] == null || r[1] === '') ? '<span class="empty">&mdash;</span>' : ('<span class="val">' + _testsEscape(String(r[1])) + '</span>');
                return '<strong>' + r[0] + '</strong>' + v;
            }}).join('') + '</div>';
        }}

        function _groupReportHtml(g) {{
            const st = g.report_status;
            let inner, cls = 'gv-card gv-report';
            if (st === 'generating') {{
                inner = '<div class="gv-report-busy"><span class="live-dot"></span> Generating performance report from the run launches&hellip;</div>';
            }} else if (st === 'failed') {{
                cls += ' gv-report-failed';
                inner = '<div class="gv-report-err">' + _testsEscape(g.report_error || 'unknown error') + '</div>'
                      + '<button class="gv-btn gv-btn-primary" onclick="_groupGenerate(\\'' + _testsEscape(g.id) + '\\')">&#8635; Generate report</button>';
            }} else if (st === 'ready' && g.report) {{
                cls += ' gv-report-ready';
                const r = g.report;
                const link = r.share_url || r.url || '#';
                const meta = '<span class="gv-report-badge">&#10003; ready</span> '
                    + (r.num_launches || 0) + ' launch(es)'
                    + (r.dropped_launches ? ' &middot; ' + r.dropped_launches + ' omitted' : '')
                    + (r.has_databricks ? ' &middot; + Databricks logs' : '');
                let pw;
                if (g.report_password) {{
                    pw = '<div class="gv-pw">'
                        + '<label>One-time password <span class="gv-pw-warn">&mdash; shown once, destroyed when you leave this view</span></label>'
                        + '<div class="gv-pw-row">'
                        +   '<input type="text" id="groupReportPw" readonly value="' + _testsEscape(g.report_password) + '">'
                        +   '<button class="gv-btn gv-btn-ghost" onclick="_copyGroupPassword()">Copy</button>'
                        + '</div></div>';
                }} else {{
                    pw = '<div class="gv-pw-gone">Password was revealed and destroyed. '
                        + '<button class="gv-btn gv-btn-ghost" onclick="_groupGenerate(\\'' + _testsEscape(g.id) + '\\')">Regenerate for a new password</button></div>';
                }}
                // Open the existing report, OR drop Databricks logs to fold them in
                // and regenerate (the button flips to "Generate report" once files
                // are staged). _gvDbxWire() re-attaches handlers after each re-render.
                inner = '<div class="gv-report-meta">' + meta + '</div>'
                      + '<div class="gv-dbx-section">'
                      +   '<div class="dbx-dropzone" id="gvDbxDropzone" tabindex="0" '
                      +     'onclick="document.getElementById(\\'gvDbxFileInput\\').click()" '
                      +     'onkeydown="if(event.key===\\'Enter\\'||event.key===\\' \\'){{event.preventDefault();document.getElementById(\\'gvDbxFileInput\\').click();}}">'
                      +     '<input type="file" id="gvDbxFileInput" multiple accept=".log,.gz,.txt" style="display:none">'
                      +     '<input type="file" id="gvDbxFolderInput" multiple webkitdirectory directory style="display:none">'
                      +     '<div class="dbx-dropzone-cta">Drop files or a folder here, or click to browse</div>'
                      +     '<div class="dbx-dropzone-hint">'
                      +       '<a href="#" class="dbx-folder-link" onclick="event.preventDefault();event.stopPropagation();document.getElementById(\\'gvDbxFolderInput\\').click();">Pick a folder instead</a>'
                      +       '&nbsp;&middot;&nbsp; Accepts <code>log4j-*.log[.gz]</code>, <code>stdout*.txt</code>, <code>stderr*.txt</code>. Other files are ignored.'
                      +     '</div>'
                      +   '</div>'
                      +   '<div class="dbx-file-list" id="gvDbxFileList"></div>'
                      + '</div>'
                      + '<div class="gv-report-actions">'
                      +   '<button id="groupReportBtn" class="gv-btn gv-btn-primary" '
                      +     'data-gid="' + _testsEscape(g.id) + '" data-link="' + _testsEscape(link) + '" '
                      +     'onclick="_groupReportBtnClick()">Open report &rarr;</button>'
                      + '</div>'
                      + pw;
            }} else {{
                inner = '<div class="gv-report-wait">Generates automatically once every iteration finishes.</div>';
            }}
            return '<div class="' + cls + '"><div class="gv-card-h">Performance report</div>' + inner + '</div>';
        }}

        function _groupViewHtml(g) {{
            const pct = g.pct || 0;
            const eta = (g.eta_sec != null) ? ('~' + Math.max(1, Math.round(g.eta_sec / 60)) + ' min') : (g.all_done ? 'done' : '--');
            const sc = g.status_counts || {{}};
            const chips = ['running', 'queued', 'scheduled', 'finished', 'failed']
                .filter(function(s) {{ return sc[s]; }})
                .map(function(s) {{ return '<span class="gv-chip gv-chip-' + s + '">' + sc[s] + ' ' + s + '</span>'; }}).join('');
            const ringColor = g.all_done ? (sc.failed ? '#f85149' : '#3fb950') : '#58a6ff';
            const members = (g.members || []).map(function(m) {{
                const mp = m.progress || {{}};
                // Prefer the live/actual progress total (filter-aware) over the estimate.
                const tot = (mp.total != null ? mp.total : (m.scenario_count || 0));
                const mdone = mp.done || 0;
                const mpct = tot ? Math.min(100, Math.round(mdone / tot * 100)) : (m.status === 'finished' || m.status === 'failed' ? 100 : 0);
                const color = m.status === 'finished' ? '#3fb950' : (m.status === 'failed' ? '#f85149' : (m.status === 'running' ? '#58a6ff' : '#8b949e'));
                const rp = m.rp_url
                    ? '<a class="rp-link" href="' + _testsEscape(m.rp_url) + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">RP &rarr;</a>'
                    : '<span class="gv-iter-norp">no launch</span>';
                return '<div class="gv-iter" onclick="_selectPipeline(\\'' + _testsEscape(m.id) + '\\')">'
                     +   '<span class="gv-iter-idx">#' + (m.iteration_index || '?') + '</span>'
                     +   '<span class="gv-iter-status gv-st-' + m.status + '">' + m.status + '</span>'
                     +   '<span class="gv-iter-bar"><span style="width:' + mpct + '%;background:' + color + '"></span></span>'
                     +   '<span class="gv-iter-num">' + mdone + '/' + tot + '</span>'
                     +   rp
                     + '</div>';
            }}).join('');
            return '<div class="gv">'
                 +   '<div class="gv-main">'
                 +     '<div class="gv-hero">'
                 +       '<div class="gv-ring" style="background: conic-gradient(' + ringColor + ' ' + pct + '%, rgba(255,255,255,0.07) ' + pct + '%)">'
                 +         '<div class="gv-ring-in"><span class="gv-ring-pct">' + pct + '%</span></div>'
                 +       '</div>'
                 +       '<div class="gv-hero-meta">'
                 +         '<div class="gv-hero-h1">' + g.iterations_done + ' / ' + g.iteration_count + ' iterations</div>'
                 +         '<div class="gv-hero-h2">' + g.scenarios_done + ' / ' + g.scenarios_total + ' scenarios &middot; ETA ' + eta + '</div>'
                 +         (chips ? '<div class="gv-chips">' + chips + '</div>' : '')
                 +       '</div>'
                 +     '</div>'
                 +     '<div class="gv-card"><div class="gv-card-h">Configuration <span class="gv-card-sub">shared by all iterations</span></div>'
                 +       _groupConfigRows(g.config) + '</div>'
                 +     '<div class="gv-card"><div class="gv-card-h">Iterations</div><div class="gv-iters">' + members + '</div></div>'
                 +   '</div>'
                 +   '<div class="gv-side">' + _groupReportHtml(g) + '</div>'
                 + '</div>';
        }}

        async function _refreshGroupView() {{
            if (!_selectedGroupId) return;
            try {{
                const res = await fetch('/api/groups/' + encodeURIComponent(_selectedGroupId));
                const g = await res.json();
                const gv = document.getElementById('pipelinesViewGroup');
                const title = document.getElementById('pipelinesViewTitle');
                const status = document.getElementById('pipelinesViewStatus');
                if (!res.ok) {{ if (gv) gv.innerHTML = '<div class="group-error">' + _testsEscape(g.error || ('HTTP ' + res.status)) + '</div>'; return; }}
                if (title) title.textContent = (g.name || g.id) + '  (group of ' + g.iteration_count + ')';
                if (status) {{
                    const live = (g.status_counts && g.status_counts.running) ? '<span class="live-dot"></span>' : '';
                    const label = g.all_done ? (g.report_status === 'ready' ? 'report ready' : 'complete') : (g.iterations_done + '/' + g.iteration_count);
                    status.innerHTML = live + '<span class="status-pill">' + label + '</span>';
                }}
                if (gv) gv.innerHTML = _groupViewHtml(g);
                // Re-attach the Databricks dropzone + restore its staged files
                // after the poll replaced the report card's DOM.
                _gvDbxWire();
            }} catch (_e) {{ /* keep last render */ }}
        }}

        async function _groupReportBtnClick() {{
            const btn = document.getElementById('groupReportBtn');
            if (!btn) return;
            const accepted = _gvDbxAccepted();
            if (accepted.length) {{
                btn.disabled = true; btn.textContent = 'Reading files\\u2026';
                let files = [];
                try {{ files = await _readDbxFilesAsBase64(null, _gvDbxFiles); }} catch (_e) {{}}
                _groupGenerate(btn.getAttribute('data-gid'), files);
            }} else {{
                window.open(btn.getAttribute('data-link'), '_blank', 'noopener');
            }}
        }}

        async function _groupGenerate(gid, dbxFiles) {{
            const opts = {{method: 'POST'}};
            if (dbxFiles && dbxFiles.length) {{
                opts.headers = {{'Content-Type': 'application/json'}};
                opts.body = JSON.stringify({{databricks_files: dbxFiles}});
            }}
            try {{ await fetch('/api/groups/' + encodeURIComponent(gid) + '/generate', opts); }} catch (_e) {{}}
            _gvDbxFiles.length = 0;   // clear staged uploads once generation is kicked off
            _refreshGroupView();
        }}

        function _copyGroupPassword() {{
            const el = document.getElementById('groupReportPw');
            if (!el) return;
            el.select();
            try {{ document.execCommand('copy'); }} catch (_e) {{}}
            if (navigator.clipboard) {{ try {{ navigator.clipboard.writeText(el.value); }} catch (_e) {{}} }}
        }}

        async function _selectPipeline(id, opts) {{
            _leaveGroupView();
            _selectedPipelineId = id;
            // Highlight in list immediately.
            document.querySelectorAll('.pipeline-item.active, .pipeline-group.active').forEach(function(e) {{
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
                        ['max runtime',  data.max_runtime_sec ? (Math.round(data.max_runtime_sec / 60) + ' min') : ''],
                        ['retries',      data.scenario_retry != null ? String(data.scenario_retry) : ''],
                        ['scenarios',    data.scenario_count != null
                                            ? (data.scenario_count + ' @perf'
                                               + (data.scenario_total ? ' (of ' + data.scenario_total + ')' : ''))
                                            : ''],
                        ['est. runtime', data.est_runtime_sec != null
                                            ? ('~' + Math.max(1, Math.round(data.est_runtime_sec / 60)) + ' min')
                                            : ''],
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
                // Live scenario progress + ETA, pinned above the log body.
                const prog = document.getElementById('pipelinesViewProgress');
                if (prog) {{
                    const pr = data.progress;
                    const total = (pr && pr.total != null) ? pr.total
                                : (data.scenario_count != null ? data.scenario_count : null);
                    const done  = (pr && pr.done != null) ? pr.done : 0;
                    const hasProg = (pr && pr.done != null) || data.status === 'running';
                    if (hasProg && total) {{
                        const pct = (pr && pr.pct != null) ? pr.pct
                                  : Math.min(100, Math.round(done / total * 100));
                        let meta;
                        if (data.status === 'running') {{
                            const eta = (pr && pr.eta_sec != null)
                                ? ('ETA ~' + Math.max(1, Math.round(pr.eta_sec / 60)) + ' min')
                                : 'ETA --';
                            const rate = (pr && pr.rate_per_min != null) ? (' &middot; ' + pr.rate_per_min + '/min') : '';
                            meta = eta + rate;
                        }} else {{
                            meta = data.status + (data.est_runtime_sec != null
                                ? (' &middot; est was ~' + Math.max(1, Math.round(data.est_runtime_sec / 60)) + ' min') : '');
                        }}
                        const barColor = data.status === 'running' ? '#3fb950'
                                       : (data.status === 'finished' ? '#3fb950'
                                       : (data.status === 'failed' ? '#f85149' : '#8b949e'));
                        prog.innerHTML =
                            '<div style="display:flex;justify-content:space-between;font:600 12px/1.6 ui-monospace,monospace;color:#c9d1d9">'
                          +   '<span>Scenarios ' + done + ' / ' + total + ' (' + pct + '%)</span>'
                          +   '<span style="color:#8b949e;font-weight:500">' + meta + '</span>'
                          + '</div>'
                          + '<div style="height:6px;background:#21262d;border-radius:3px;overflow:hidden;margin-top:4px">'
                          +   '<div style="height:100%;width:' + pct + '%;background:' + barColor + ';transition:width .6s ease"></div>'
                          + '</div>';
                        prog.hidden = false;
                    }} else {{
                        prog.hidden = true;
                    }}
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
        function _readDbxFilesAsBase64(onProgress, filesArr) {{
            const accepted = (filesArr || _dbxFiles).filter(function(f) {{ return _dbxAccept(f.name); }});
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

        // ----- Group view: its own Databricks dropzone (re-wired after each poll
        // re-render; file state persists in _gvDbxFiles). Reuses the New-tab
        // helpers (_dbxAccept, _humanBytes, _escapeHtml, _walkEntry, _readDbxFilesAsBase64). -----
        const _gvDbxFiles = [];
        function _gvDbxAccepted() {{ return _gvDbxFiles.filter(function(f) {{ return _dbxAccept(f.name); }}); }}
        function _gvDbxAddFiles(fileList) {{
            const seen = new Set(_gvDbxFiles.map(function(f) {{ return f.name + ':' + f.size; }}));
            for (const f of fileList) {{
                const k = f.name + ':' + f.size;
                if (!seen.has(k)) {{ seen.add(k); _gvDbxFiles.push(f); }}
            }}
            _gvDbxRender();
        }}
        function _gvDbxRemove(idx) {{ if (idx >= 0 && idx < _gvDbxFiles.length) {{ _gvDbxFiles.splice(idx, 1); _gvDbxRender(); }} }}
        function _gvDbxRender() {{
            const list = document.getElementById('gvDbxFileList');
            const btn = document.getElementById('groupReportBtn');
            const accepted = _gvDbxAccepted();
            if (list) {{
                const rejected = _gvDbxFiles.filter(function(f) {{ return !_dbxAccept(f.name); }});
                const chips = _gvDbxFiles.map(function(f, idx) {{
                    const ok = _dbxAccept(f.name);
                    return '<span class="dbx-file-chip' + (ok ? '' : ' invalid') + '">'
                        + _escapeHtml(f.name)
                        + '<span class="dbx-file-chip-size">' + _humanBytes(f.size) + '</span>'
                        + '<button type="button" class="dbx-file-chip-remove" title="Remove" onclick="_gvDbxRemove(' + idx + ')">&times;</button>'
                        + '</span>';
                }}).join('');
                let summary = '';
                if (accepted.length) {{
                    const tot = accepted.reduce(function(a, f) {{ return a + f.size; }}, 0);
                    summary = accepted.length + ' file' + (accepted.length === 1 ? '' : 's') + ' ready &middot; '
                        + _humanBytes(tot) + ' total' + (rejected.length ? ' &middot; ' + rejected.length + ' ignored' : '');
                }} else if (rejected.length) {{ summary = 'No recognized files yet'; }}
                list.innerHTML = chips + (summary ? '<div class="dbx-file-summary">' + summary + '</div>' : '');
            }}
            if (btn) {{
                if (accepted.length) {{ btn.textContent = 'Generate report'; btn.classList.add('regen'); }}
                else {{ btn.innerHTML = 'Open report &rarr;'; btn.classList.remove('regen'); }}
            }}
        }}
        function _gvDbxWire() {{
            const dz = document.getElementById('gvDbxDropzone');
            if (!dz) return;
            if (!dz.dataset.wired) {{
                dz.dataset.wired = '1';
                const inp = document.getElementById('gvDbxFileInput');
                const dirInp = document.getElementById('gvDbxFolderInput');
                if (inp) inp.addEventListener('change', function(e) {{ _gvDbxAddFiles(e.target.files || []); inp.value = ''; }});
                if (dirInp) dirInp.addEventListener('change', function(e) {{ _gvDbxAddFiles(e.target.files || []); dirInp.value = ''; }});
                ['dragenter', 'dragover'].forEach(function(t) {{
                    dz.addEventListener(t, function(e) {{ e.preventDefault(); e.stopPropagation(); dz.classList.add('dragover'); }});
                }});
                ['dragleave', 'drop'].forEach(function(t) {{
                    dz.addEventListener(t, function(e) {{ e.preventDefault(); e.stopPropagation(); dz.classList.remove('dragover'); }});
                }});
                dz.addEventListener('drop', function(e) {{
                    const dt = e.dataTransfer; if (!dt) return;
                    const items = dt.items;
                    if (items && items.length && typeof items[0].webkitGetAsEntry === 'function') {{
                        const entries = [];
                        for (let i = 0; i < items.length; i++) {{ const ent = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry(); if (ent) entries.push(ent); }}
                        Promise.all(entries.map(_walkEntry)).then(function(arrs) {{ const files = [].concat.apply([], arrs); if (files.length) _gvDbxAddFiles(files); }});
                    }} else if (dt.files && dt.files.length) {{ _gvDbxAddFiles(dt.files); }}
                }});
            }}
            _gvDbxRender();   // restore chips + button label after a re-render
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
            const gm = (path || '/').match(/^\/groups\/([A-Za-z0-9_-]+)\/?$/);
            if (gm) {{
                showTab('pipelines', {{fromPop: fromPop}});
                _selectGroup(gm[1], {{fromPop: fromPop}});
                return;
            }}
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
            or path.startswith("/groups/")
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
        if path.startswith("/api/groups/"):
            self._handle_group_detail(path[len("/api/groups/"):])
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
        if path == "/api/pipelines/cleanup":
            self._handle_pipeline_cleanup()
            return
        if path.startswith("/api/pipelines/") and path.endswith("/rerun"):
            pid = path[len("/api/pipelines/"):-len("/rerun")]
            self._handle_pipeline_rerun(pid)
            return
        if path.startswith("/api/pipelines/") and path.endswith("/kill"):
            pid = path[len("/api/pipelines/"):-len("/kill")]
            self._handle_pipeline_kill(pid)
            return
        if path.startswith("/api/groups/") and path.endswith("/forget-password"):
            self._handle_group_forget_password(path[len("/api/groups/"):-len("/forget-password")])
            return
        if path.startswith("/api/groups/") and path.endswith("/generate"):
            self._handle_group_generate(path[len("/api/groups/"):-len("/generate")])
            return
        if path.startswith("/api/groups/") and path.endswith("/rerun"):
            self._handle_group_rerun(path[len("/api/groups/"):-len("/rerun")])
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
        # A multi-iteration burst becomes one report-package group; iterations
        # share a stable group_id + group_name. Single runs stay ungrouped.
        group_id = group_name = None
        if iterations > 1:
            group_id = _new_group_id()
            group_name = ((payload.get("name") or "").strip()
                          or f"group-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}")
        records = []
        for i in range(iterations):
            rec = _create_pipeline(payload, iteration_index=i + 1, iteration_count=iterations,
                                   group_id=group_id, group_name=group_name)
            records.append(rec)
        if group_id:
            _create_group_record(group_id, group_name, records)
        # `now` jobs are now `queued`; the single-slot dispatcher starts the next
        # one when the slot is free. Nudge it so a free slot fills immediately.
        _ensure_pipeline_scheduler()
        _dispatch_tick()
        self._send_json(200, {"pipelines": records, "count": len(records),
                              "group_id": group_id})

    def _handle_pipeline_cleanup(self):
        """Remove terminal pipelines (default: failed). Body may set
        {"statuses": ["failed", "finished"]}; running/queued are never touched."""
        payload, _err = self._read_json_body()
        statuses = ("failed",)
        if isinstance(payload, dict) and isinstance(payload.get("statuses"), list):
            statuses = tuple(payload["statuses"])
        result = _cleanup_pipelines(statuses)
        self._send_json(200, result)

    def _handle_pipeline_rerun(self, pid: str):
        """Re-run a finished/failed pipeline with the same configuration:
        build a fresh run config from the original record and create a new
        pipeline, which is appended to the **end of the queue** (FIFO) and run
        by the single-slot dispatcher. The original record is left untouched."""
        pid = (pid or "").strip()
        src = None
        for p in _load_pipelines():
            if p.get("id") == pid:
                src = p
                break
        if src is None:
            self._send_json(404, {"error": f"pipeline {pid!r} not found"})
            return
        cfg = {
            "kind":     "now",
            "name":     src.get("name") or "",
            "env":      src.get("env") or "prod",
            "parallel": src.get("parallel") or 2,
            "feature":  src.get("feature") or "",
            "tests":    src.get("tests") or "",
            # Preserve the original run's watchdog budget (seconds -> minutes;
            # blank falls back to the default inside _clamp_max_runtime_sec).
            "max_runtime_min": (src.get("max_runtime_sec") or MAX_RUNTIME_SEC) // 60,
            "scenario_retry":  src.get("scenario_retry") or SCENARIO_RETRY_DEFAULT,
        }
        rec = _create_pipeline(cfg)
        _ensure_pipeline_scheduler()
        _dispatch_tick()
        self._send_json(200, {"pipeline": rec, "rerun_of": pid})

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
                    "id":              pid,
                    "name":            p.get("name"),
                    "status":          p.get("status"),
                    "kind":            p.get("kind"),
                    "env":             p.get("env"),
                    "parallel":        p.get("parallel"),
                    "feature":         p.get("feature"),
                    "tests":           p.get("tests"),
                    "max_runtime_sec": p.get("max_runtime_sec"),
                    "scenario_retry":  p.get("scenario_retry"),
                    "scenario_count":  p.get("scenario_count"),
                    "scenario_total":  p.get("scenario_total"),
                    "est_runtime_sec": p.get("est_runtime_sec"),
                    "progress":        p.get("progress"),
                    "created_at":      p.get("created_at"),
                    "scheduled_for":   p.get("scheduled_for"),
                    "started_at":      p.get("started_at"),
                    "finished_at":     p.get("finished_at"),
                    "rp_url":          p.get("rp_url"),
                    "group_id":        p.get("group_id"),
                    "group_name":      p.get("group_name"),
                    "iteration_index": p.get("iteration_index"),
                    "iteration_count": p.get("iteration_count"),
                    "logs":            p.get("logs") or [],
                })
                return
        self._send_json(404, {"error": "not found"})

    # ------------------------------------------------------------------ #
    # Iteration groups
    # ------------------------------------------------------------------ #
    def _handle_group_detail(self, gid: str):
        gid = (gid or "").strip()
        if not gid or not all(c.isalnum() or c in "-_" for c in gid):
            self._send_json(404, {"error": "not found"})
            return
        with _pipelines_lock():
            groups = _load_groups()
            g = groups.get(gid)
            pipelines = _load_pipelines()
        if not g:
            self._send_json(404, {"error": "not found"})
            return
        members = [p for p in pipelines if p.get("group_id") == gid]
        members.sort(key=lambda p: p.get("iteration_index") or 0)

        status_counts, member_views = {}, []
        scen_done = scen_total = 0
        eta_sec, any_eta = 0, False
        for m in members:
            st = m.get("status")
            status_counts[st] = status_counts.get(st, 0) + 1
            prog = m.get("progress") or {}
            scen_done += int(prog.get("done") or 0)
            # Prefer the live/actual total from progress (accurate once a run has
            # started or finished -- and correct for Tests-filtered runs); fall
            # back to the pre-run @perf estimate only before a run produces any.
            scen_total += int(prog.get("total") or m.get("scenario_count") or 0)
            # Single-slot sequential model: the running iteration contributes its
            # live ETA; not-yet-started iterations contribute their full estimate.
            if st == "running":
                eta_sec += int(prog.get("eta_sec") or m.get("est_runtime_sec") or 0); any_eta = True
            elif st in ("queued", "scheduled"):
                eta_sec += int(m.get("est_runtime_sec") or 0); any_eta = True
            member_views.append({
                "id": m.get("id"), "name": m.get("name"),
                "iteration_index": m.get("iteration_index"),
                "status": st, "rp_url": m.get("rp_url"),
                "scenario_count": m.get("scenario_count"), "progress": prog,
            })
        total_iters = len(members)
        done_iters = sum(status_counts.get(s, 0) for s in ("finished", "failed"))
        pct = (round(scen_done / scen_total * 100) if scen_total
               else (round(done_iters / total_iters * 100) if total_iters else 0))
        report = g.get("report") or None
        self._send_json(200, {
            "id": gid, "name": g.get("name"), "config": g.get("config"),
            "iteration_count": total_iters, "iterations_done": done_iters,
            "status_counts": status_counts,
            "scenarios_done": scen_done, "scenarios_total": scen_total, "pct": pct,
            "eta_sec": (eta_sec if any_eta else None),
            "all_done": total_iters > 0 and done_iters == total_iters,
            "members": member_views,
            "report_status": g.get("report_status"),
            "report": report,
            "report_error": g.get("report_error"),
            # one-time password: present only while held in memory (see lifecycle)
            "report_password": _group_report_passwords.get(gid),
        })

    def _handle_group_forget_password(self, gid: str):
        """Drop the in-memory one-time report password -- the client calls this
        when leaving the group view, so the password is shown once and destroyed
        on view switch."""
        gid = (gid or "").strip()
        _group_report_passwords.pop(gid, None)
        self._send_json(200, {"ok": True})

    def _handle_group_generate(self, gid: str):
        """Manual (re)generate: re-arm the group so the dispatcher regenerates
        its report. Only valid once every iteration is terminal. An optional
        body {"databricks_log_dir": "<path>"} regenerates the report WITH those
        Databricks logs folded in (empty/absent -> a plain regenerate)."""
        gid = (gid or "").strip()
        payload, _err = self._read_json_body()
        # Validate the group is regeneratable BEFORE materializing any uploads,
        # so a bad request never leaks a temp dir.
        with _pipelines_lock():
            g = _load_groups().get(gid)
            if not g:
                self._send_json(404, {"error": "not found"})
                return
            by_id = {p.get("id"): p for p in _load_pipelines()}
            members = [m for m in (by_id.get(mid) for mid in g.get("member_ids", [])) if m]
            if not members or not all(
                    m.get("status") in ("finished", "failed") for m in members):
                self._send_json(409, {"error": "group still has running/queued iterations"})
                return
        # Optional Databricks logs: uploaded files (preferred) or a server path.
        dbx_dir, dbx_temp = None, False
        if isinstance(payload, dict):
            files = payload.get("databricks_files")
            if isinstance(files, list) and files:
                materialized = _materialize_databricks_uploads(files)
                if materialized:
                    dbx_dir, dbx_temp = materialized, True
            if not dbx_dir:
                d = (payload.get("databricks_log_dir") or "").strip()
                if d and os.path.isdir(d):
                    dbx_dir = os.path.realpath(d)
                elif d:
                    self._send_json(400, {"error": f"Databricks log dir not found: {d}"})
                    return
        with _pipelines_lock():
            groups = _load_groups()
            g = groups.get(gid)
            if not g:
                self._send_json(404, {"error": "not found"})
                return
            g["report_status"] = "none"
            g["report_error"] = None
            g["report_dbx_dir"] = dbx_dir      # consumed by _run_group_report
            g["report_dbx_temp"] = dbx_temp     # rmtree'd after generation if True
            _save_groups(groups)
        _group_report_passwords.pop(gid, None)
        _check_group_completions()
        self._send_json(200, {"ok": True, "report_status": "generating"})

    def _handle_group_rerun(self, gid: str):
        """Re-run an entire group: enqueue a fresh group with the same config and
        iteration count (a new group_id). The original group is left untouched."""
        gid = (gid or "").strip()
        with _pipelines_lock():
            g = _load_groups().get(gid)
        if not g:
            self._send_json(404, {"error": "not found"})
            return
        cfg = dict(g.get("config") or {})
        run_cfg = {
            "kind": "now",
            "env": cfg.get("env") or "prod",
            "parallel": cfg.get("parallel") or 2,
            "feature": cfg.get("feature") or "",
            "tests": cfg.get("tests") or "",
            "max_runtime_min": (cfg.get("max_runtime_sec") or MAX_RUNTIME_SEC) // 60,
            "scenario_retry": cfg.get("scenario_retry") or SCENARIO_RETRY_DEFAULT,
        }
        n = max(1, int(g.get("iteration_count") or len(g.get("member_ids") or []) or 1))
        new_gid = _new_group_id()
        new_name = g.get("name") or "group"
        records = [_create_pipeline(run_cfg, iteration_index=i + 1, iteration_count=n,
                                    group_id=new_gid, group_name=new_name) for i in range(n)]
        _create_group_record(new_gid, new_name, records)
        _ensure_pipeline_scheduler()
        _dispatch_tick()
        self._send_json(200, {"group_id": new_gid, "count": len(records)})

    def _handle_pipeline_kill(self, pid: str):
        """Kill a running pipeline's container. The runner's stream loop then
        sees the container exit and marks the pipeline failed."""
        pid = (pid or "").strip()
        if not pid or not all(c.isalnum() or c in "-_" for c in pid):
            self._send_json(404, {"error": "not found"})
            return
        with _pipelines_lock():
            p = next((x for x in _load_pipelines() if x.get("id") == pid), None)
        if not p:
            self._send_json(404, {"error": "not found"})
            return
        if p.get("status") != "running":
            self._send_json(409, {"error": "pipeline is not running"})
            return
        import subprocess
        container = f"dokimos-{pid}"
        try:
            subprocess.run(["docker", "kill", container],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except Exception:
            pass
        _append_pipeline_log(
            pid, f"[{_now_iso()}] KILL requested by operator -- stopping container "
                 f"{container}; the run will be marked failed.")
        self._send_json(200, {"ok": True})

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
    # Groups left mid-generation by the previous process re-arm for regeneration.
    requeued = _reconcile_orphan_groups()
    if requeued:
        print(f"  Re-armed {requeued} group report(s) interrupted by restart")
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
