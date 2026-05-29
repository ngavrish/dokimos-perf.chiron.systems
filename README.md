# rp_perf_report

Pull Report Portal launch logs, aggregate per-endpoint response times across
runs, optionally correlate with Datadog APM traces, and serve a self-contained
HTML report with an AES-256-GCM password gate.

The package bundles three things into one CLI:

1. **Comparison engine** — fetches RP logs for one or many launches, groups
   timings by endpoint and request payload, computes per-launch avg / min /
   max / median / p95 plus cross-run aggregates (median across all calls,
   fastest / slowest run per endpoint), and renders an interactive HTML view.
2. **Local HTTP server** — serves the rendered report at
   `http://localhost:9999` so `crypto.subtle` (the browser WebCrypto API used
   by the password gate) gets a secure context.
3. **AES-256-GCM password gate** — when `REPORT_PASSWORD` is set the entire
   timing payload (HTML chrome + raw `reportData` + `trendChartData`) is
   encrypted at generation time. The rendered HTML carries only ciphertext;
   the report can only be read after a successful in-browser decryption.

---

## Quick start

### SPA mode (recommended)

Launch with no arguments to get the single-page web UI: paste URLs into
inboxes, click **Generate Report**, and the result is saved to disk and shown
in the **Reports** tab grouped by sprint (14-day windows).

```bash
# 1. Source the IFP test repo's .envrc (gets you RP_TOKEN + REPORT_PORTAL).
source ~/work/disney/ad-apps-test-automation/NAS_components/InventoryForecasting/tests/.envrc

# 2. Start the SPA. The package lives under ~/work/rp_perf_report, so
#    invoking it via ``python3 -m`` from ~/work picks it up off sys.path.
cd ~/work
python3 -m rp_perf_report

# 3. Open http://localhost:9999 in a browser.
#    - "New" tab: + adds an inbox, - removes one, "Generate Report" runs the
#      pipeline (page blurs + spinner while it works).
#    - "Reports" tab: sidebar lists every saved report grouped by sprint;
#      click one to view it inline.
```

Reports are persisted under `~/work/rp_perf_report/reports/<sprint>/<report_id>/`
(see [Storage layout](#storage-layout)). The directory is gitignored.

### CLI mode (one-shot)

Pass URLs directly to skip the SPA and serve a single report:

```bash
cd ~/work
python3 -m rp_perf_report \
  "https://ads-report-portal.staging.hulu.com/ui/#ad-apps-automation/launches/all/1687913,\
https://ads-report-portal.staging.hulu.com/ui/#ad-apps-automation/launches/all/1687938,\
https://ads-report-portal.staging.hulu.com/ui/#ad-apps-automation/launches/all/1687964"
```

There's also a backwards-compat shim at the original script path so existing
tooling keeps working:

```bash
python3 ~/work/rp_perf_report/analyze_rp_timings_with_dd.py "<urls>"
```

Both invocation forms run exactly the same analyzer code.

---

## Install

The package lives at `~/work/rp_perf_report/` as a standalone workspace and
does not need to be installed -- `python3 -m rp_perf_report ...` works as
long as `~/work` (the parent of the package directory) is the current
working directory or on `sys.path`.

The only third-party dep is **`cryptography>=38`** (used for AES-256-GCM and
PBKDF2-HMAC-SHA256). It's already in the ad-apps-test-automation venv used
for IFP testing, so sourcing that venv's `.envrc` is enough.

If you want a `rp-perf-report` console script on PATH, install in editable mode:

```bash
pip install -e ~/work/rp_perf_report
rp-perf-report "<urls>"
```

---

## Environment variables

| Variable                  | Required | Default | Purpose                                                              |
| ------------------------- | -------- | ------- | -------------------------------------------------------------------- |
| `RP_TOKEN`                | **yes**  | -       | Report Portal API token (sourced from `tests/.envrc`).               |
| `RP_API_BASE`             | no       | `https://ads-report-portal.staging.hulu.com/api/v1/ad-apps-automation/log` | RP API base. |
| `REPORT_PASSWORD`         | no       | `abcABC123(1)(1)` | Legacy / fallback in-report AES-GCM password. New SPA-generated reports get a unique random password instead. Empty string disables encryption for CLI mode. |
| `DISNEY_DD_API_KEY`       | no       | -       | Datadog API key. When set, slow requests (>3s) are correlated to traces. |
| `DISNEY_DD_APP_KEY`       | no       | -       | Datadog application key (required together with `DISNEY_DD_API_KEY`). |

> **Important.** `REPORT_PASSWORD` defaults to a hard-coded value for
> convenience; anyone with the rendered HTML *and* that password can decrypt
> it. For real protection, set `REPORT_PASSWORD` to a strong, secret value
> just before generating the report.

---

## CLI

```
python3 -m rp_perf_report [--key KEY] urls

positional:
  urls         Comma-separated Report Portal launch URL(s). Up to 20.

options:
  --key, -k KEY
               Group endpoints by presence of `KEY` in the request payload.
               Also filters out 4xx error responses.
```

### Single-launch mode

One URL -> a single-launch overview with per-endpoint timing breakdown by
payload variant.

### Multi-launch comparison mode

Two or more URLs -> a side-by-side comparison report.

- **2-3 launches**: flat layout showing every launch as a column per endpoint.
- **4+ launches**: tabbed view with:
  - **Median vs Slowest** — median across every call vs the worst-performing
    single launch (`+X% slower` badge).
  - **Fastest vs Slowest** — best vs worst launch per endpoint.
  - **Per-endpoint Trend** — Chart.js line graph of **max** and **p95** per
    iteration plus an "overall max" reference line. The median was dropped
    here because it stays flat -- tail latency is where the signal lives.
  - **All N Launches** — the original side-by-side N-column layout.

Launches are sorted by start time (oldest -> newest, left -> right). The
oldest is the baseline; every per-launch `+/- X%` is computed against it.

---

## AES-256-GCM password gate (encrypted HTML)

The report ships every byte of sensitive data inside an authenticated
ciphertext blob. There is no CSS blur, no `display: none` over plaintext --
the plaintext genuinely does not exist in the HTML until the user types the
password.

### How it works

| Stage          | Where                                  | Algorithm / parameters                                  |
| -------------- | -------------------------------------- | ------------------------------------------------------- |
| Key derivation | Python (rendering time)                | PBKDF2-HMAC-SHA256, 300 000 iterations, 16-byte random salt |
| Encryption     | Python                                 | AES-256-GCM, 12-byte random nonce                       |
| Storage        | Embedded in HTML as base64             | `{salt, iv, ct, kdf_iter, kdf, alg}`                    |
| Decryption     | Browser (`crypto.subtle.deriveKey` + `crypto.subtle.decrypt`) | Native WebCrypto, no third-party JS    |
| Session cache  | `sessionStorage`, keyed by ct prefix   | Cleared when the tab closes                             |

Empty / null `REPORT_PASSWORD` disables encryption entirely; the report
renders in clear and no password prompt appears.

### Security caveats (read these)

- **Brute-force resistance is bounded by password entropy.** 300k PBKDF2
  iterations cost about 0.05-0.5s per guess on a modern laptop. A dictionary
  word will still fall in seconds-to-minutes. **Pick a non-trivial password.**
- **The encrypted HTML still lives on disk.** Anyone who has the file *and*
  the password can decrypt it. Treat the password the same way you'd treat
  the data.
- **`crypto.subtle` requires a secure context.** `https://` or `localhost` /
  `127.0.0.1` are fine. Opening the file via `file://` does **not** work in
  Chromium-based browsers. The bundled HTTP server on `localhost:9999`
  satisfies this; do not just `open` the file from Finder.
- The default password (`abcABC123(1)(1)`) is committed in source for
  convenience and explicitly tagged `SECURITY-REVIEW`. **Always override it
  in real use.**

### Disable the gate

```bash
REPORT_PASSWORD='' python3 -m rp_perf_report "<urls>"
```

### Rotate the password

The salt / iv / ciphertext are freshly random on every generation, so just
re-run with a new `REPORT_PASSWORD` value. There is no key-rotation ceremony.

---

## Datadog correlation (optional)

If both `DISNEY_DD_API_KEY` and `DISNEY_DD_APP_KEY` are set, slow requests
(>= 3s, configurable in source) are fetched from Datadog APM, grouped by
launch, and rendered under each endpoint as collapsible trace rows that link
back to Datadog. The trace fetch is cached per launch in `logs/` so repeat
renders are fast and offline-friendly.

---

## Caching

Two on-disk caches sit under `logs/` next to this README:

- `<launch_id>.json`         -- RP launch log payload (no response bodies)
- `<launch_id>_full.json`    -- RP launch log payload + response bodies
                                (only populated when `--key` is used)
- `<launch_id>_dd_traces.json` -- Datadog traces fetched for that launch

Safe to delete; the next render will refetch.

---

## Companion: the perf runner

The data this package consumes is generated by behavex `@perf` runs. See
`.cursor/rules/perf-tests-defaults.mdc` for the canonical invocation. For
long unattended loops there's also `../perf_loop.sh` in the parent `scripts/`
directory which rotates `@perf` runs against an environment and appends each
launch URL to `all_rp_launches.txt`. Pipe that file into this package as a
comma-separated list to compare a 5-hour soak.

---

## Dokimos Performance SPA: tabs, sprints, and report storage

`python -m rp_perf_report` with no URLs (or with `--spa`) starts the
**Dokimos Performance** single-page web app on `http://localhost:9999`.
The UI uses the Dokimos pitch-deck visual language (dark navy `#001428` +
bronze `#CD7F32` hero accent, Courier display / Calibri body).

The SPA itself is not gated. The only password barrier is per-report: every
generated report has its own random AES-256-GCM password. The password is
displayed **exactly once** on the New tab right after the report is
generated, and is then forgotten by the server -- nothing recoverable is
persisted on disk. If you fail to capture the password from the one-time
display you will not be able to read that report again. See
[AES-256-GCM password gate](#aes-256-gcm-password-gate-encrypted-html).

It has two tabs:

- **New** — vertical list of URL inboxes with `+` and `−` buttons (the first
  inbox cannot be removed; up to 20 inboxes total). The **Generate Report**
  button POSTs the URLs to `/api/generate`, blurs the page and shows a
  spinner while the analyzer runs. On success the SPA switches to the
  **Reports** tab and auto-opens the new report.

- **Reports** — left sidebar lists every saved report grouped by sprint
  (oldest sprint at the bottom). Click a report to render its `index.html`
  inline in an `<iframe>` on the right.

### Sprint cadence

Sprints are 14-day windows anchored at **Wed, 27 May 2026**:

| Sprint              | Window               |
| ------------------- | -------------------- |
| Current             | 2026-05-27 → 2026-06-10 |
| Next                | 2026-06-10 → 2026-06-24 |
| Previous            | 2026-05-13 → 2026-05-27 |

The "current" sprint is highlighted in cyan in the sidebar. The label is
computed from the date the report was *generated*, not the launch dates --
that keeps things stable even when you compare launches from earlier sprints.

### Storage layout

```
reports/
  2026-05-27_2026-06-10/                  (sprint = "<start>_<end>")
    20260529-114723-3launches/            (<YYYYMMDD-HHMMSS>-<N>launches)
      index.html                          (the encrypted/clear report)
      metadata.json                       (title, urls, generated_at, ...)
    20260529-091811-1launches/
      ...
```

The whole `reports/` directory is gitignored.

### Authentication

The SPA shell itself is **not** password-protected -- anyone who can reach
`localhost:9999` can browse the sidebar and see report titles. The security
boundary is the per-report AES-256-GCM gate: report contents stay
encrypted until the viewer enters the correct password.

Every generated report has its **own** unique random password. The password
is:

- displayed **once** on the **New** tab right after generation, in a panel
  that blocks the auto-forward until the user clicks **Continue** (so the
  password can't be missed),
- **not** persisted by the server in any form -- nothing recoverable lives
  in `metadata.json`, the logs, or anywhere else,
- gone from server memory the moment the `/api/generate` response is
  written.

If you fail to capture the password from the one-time display, that report
is unreadable forever. On startup the server also scrubs any
`report_password` field left over in pre-existing `metadata.json` files
from earlier versions that persisted the secret.

`REPORT_PASSWORD=''` disables the in-report encryption entirely (legacy /
CI behaviour); `REPORT_PASSWORD=<something>` is only honoured in CLI mode
as a fallback when no per-call password is supplied.

### REST surface (only consumed by the SPA itself)

| Method | Path                                          | Body / response                                              |
| ------ | --------------------------------------------- | ------------------------------------------------------------ |
| GET    | `/`                                           | SPA shell HTML                                               |
| POST   | `/api/generate`                               | `{urls: [...]}` -> `{id, title, url, report_password, ...}`. `report_password` is returned exactly once and never persisted server-side. |
| GET    | `/api/reports`                                | `{sprints: [{label, start, end, reports: [...]}]}`           |
| GET    | `/reports/<sprint>/<id>/*`                    | Static report files (`index.html`, `metadata.json`)          |

Path traversal is blocked at the handler: paths outside `reports/` return
`403 Forbidden`.

---

## Layout

```
rp_perf_report/
├── README.md               (this file)
├── pyproject.toml          (cryptography>=38)
├── .gitignore              (logs/, reports/, __pycache__, *.html)
├── .cursor/rules/          (perf rules co-located so the package is self-contained)
│   ├── perf-tests-defaults.mdc
│   └── report-portal-for-regression-smoke.mdc
├── __init__.py             (re-exports main + serve_spa + key fns)
├── __main__.py             (no URLs -> SPA mode; with URLs -> legacy CLI)
├── analyzer.py             (single-file implementation; 2700+ LOC)
├── spa.py                  (SPA shell HTML + ThreadingHTTPServer + sprint logic)
├── logs/                   (RP / Datadog cache; gitignored)
└── reports/                (user-generated reports; gitignored)
```

The single-file `analyzer.py` covers the comparison engine, HTML renderer,
HTTP server, and AES-256-GCM gate. `spa.py` is the only new code path; it
calls `generate_report_for_urls()` from `analyzer.py` and saves the result.

---

## Programmatic use

```python
from rp_perf_report import (
    fetch_logs_with_cache,
    analyze_timings,
    generate_report_for_urls,
    generate_multi_comparison_html,
    serve_html,
    serve_spa,
)

# Build a report without going through the CLI:
html, meta = generate_report_for_urls(
    ["https://.../launches/all/1687913", "https://.../launches/all/1687938"],
)

# Start the SPA programmatically:
serve_spa(port=9999)
```

`main()` is the CLI entrypoint and is the same function `python -m
rp_perf_report "<urls>"` calls. `serve_spa()` is what `python -m
rp_perf_report` (no args) calls.
