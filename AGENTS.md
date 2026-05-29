# Agent Guidelines — rp_perf_report

## What this project is

`rp_perf_report` is the **Dokimos: Performance Report** Python package and
single-page web app. It pulls Report Portal launch logs, computes per-endpoint
timing aggregates, optionally correlates with Datadog APM, and renders an
AES-256-GCM-encrypted HTML report served on `http://localhost:9999`.

Full feature docs: see [`README.md`](./README.md) -- specifically the
**"Run the project (agent-friendly cheat sheet)"** section at the top, which
has copy-pasteable commands for every workflow below.

## How to run

The canonical invocation:

```bash
source ~/work/disney/ad-apps-test-automation/NAS_components/InventoryForecasting/tests/.envrc
cd ~/work && python3 -m rp_perf_report
```

That starts the SPA on `http://localhost:9999`. The startup banner
(`Dokimos: Performance Report available at: ...`) confirms readiness.

**Before starting**, always:

1. `lsof -nP -iTCP:9999 -sTCP:LISTEN` -- bail out if anything is already
   listening (a previous instance is still up; reuse it or stop it first).
2. `test -f ~/work/rp_perf_report/spa.py` -- bail out if the package is
   missing from the expected location.

## How to stop it

```bash
lsof -nP -iTCP:9999 -sTCP:LISTEN | awk 'NR>1 {print $2}' | xargs -r kill
```

`SIGTERM` only. Never `kill -9` unless `SIGTERM` was ignored -- the SPA has
no in-memory state that needs preserving but a clean shutdown still flushes
the access log.

## How to verify changes you make

After editing any Python file:

```bash
python3 -c "import ast; ast.parse(open('PATH').read()); print('syntax OK')"
```

After editing `spa.py` or `analyzer.py`, restart the SPA (step "How to
stop it" + step "How to run") and curl-check the three endpoints documented
in the README:

```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:9999/
curl -s -o /dev/null -w 'HTTP %{http_code} %{content_type}\n' \
    http://localhost:9999/assets/chiron-icon-light.svg
curl -s -o /dev/null -w 'HTTP %{http_code} %{content_type}\n' \
    http://localhost:9999/api/reports
```

All three should return `200`.

## Hard rules

- **Never persist a `report_password`** in `metadata.json` or anywhere else
  on disk. Every per-report password is shown once via the API response and
  then forgotten by the server. The startup backfill (`spa.py:_backfill_legacy_reports`)
  actively scrubs any legacy passwords it finds in old reports.
- **Never weaken the AES-256-GCM gate**. Key derivation is PBKDF2-HMAC-SHA256
  with 300 000 iterations and a fresh random salt; encryption is AES-256-GCM
  with a fresh random 12-byte nonce. These parameters live in `analyzer.py`
  and must not be lowered.
- **Never serve files outside `reports/` or `assets/`** from the package
  dir. The `_serve_report_file` and `_serve_static_asset` handlers both
  enforce a `realpath` + prefix check; preserve those guards on any
  refactor.
- **Never hard-code an API token, RP password, or Datadog key** in source.
  Read them from `os.environ` (`RP_TOKEN`, `DISNEY_DD_API_KEY`,
  `DISNEY_DD_APP_KEY`, `REPORT_PASSWORD`). The IFP `.envrc` is the
  documented way to set them locally.

## Package-local cursor rules

Cursor rules co-located with the package live at
[`.cursor/rules/`](./.cursor/rules). Currently they document
**how to launch the upstream test suite that generates the data** this
package consumes (i.e. behavex `@perf` runs in
`ad-apps-test-automation/NAS_components/InventoryForecasting/`). They do
**not** apply to changes inside this package itself -- only to the IFP
test runs whose Report Portal launches are this tool's input.
