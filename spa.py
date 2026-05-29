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

from .analyzer import (
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
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(PACKAGE_DIR, "reports")
ASSETS_DIR  = os.path.join(PACKAGE_DIR, "assets")


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
            sprints_by_dir.setdefault(sprint_dirname, []).append({
                "id":           report_dirname,
                "sprint":       sprint_dirname,
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


def _save_report(html: str, urls: list, analyzer_meta: dict, report_password: str) -> dict:
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
    report_path = os.path.join(sprint_path, report_id)
    os.makedirs(report_path, exist_ok=True)

    with open(os.path.join(report_path, "index.html"), "w") as fp:
        fp.write(html)

    title = now_la.strftime("%Y-%m-%d %H:%M") + f"  ({num_launches} launch{'es' if num_launches != 1 else ''})"
    metadata = {
        "id":           report_id,
        "sprint":       sprint_dirname,
        "title":        title,
        "generated_at": now_la.isoformat(timespec="seconds"),
        "num_launches": num_launches,
        "urls":         urls,
        "analyzer":     analyzer_meta,
    }
    with open(os.path.join(report_path, "metadata.json"), "w") as fp:
        json.dump(metadata, fp, indent=2)

    return {
        "id":             report_id,
        "sprint":         sprint_dirname,
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


def _run_generation(urls: list, payload_key: Optional[str] = None) -> dict:
    report_password = _generate_report_password()
    html, meta = generate_report_for_urls(
        urls,
        payload_key=payload_key,
        log=lambda m: None,
        report_password=report_password,
    )
    return _save_report(html, urls, meta, report_password)


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
    <title>Dokimos: Performance Report</title>
    <link rel="icon" href="data:,">
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
            <span class="brand">Dokimos: Performance Report</span>
            <span class="brand-sub">Prototype &middot; Encrypted reports</span>
        </header>

        <nav class="tabs">
            <button class="tab-btn active" data-tab="new"     onclick="showTab('new')"><span class="num">01.</span>New</button>
            <button class="tab-btn"        data-tab="reports" onclick="showTab('reports')"><span class="num">02.</span>Reports</button>
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

        function showTab(name) {{
            document.querySelectorAll('.tab-btn').forEach(function(b) {{
                b.classList.toggle('active', b.getAttribute('data-tab') === name);
            }});
            document.querySelectorAll('.tab-panel').forEach(function(p) {{
                p.classList.toggle('active', p.getAttribute('data-panel') === name);
            }});
            if (name === 'reports') refreshReports();
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
                let res;
                try {{
                    res = await fetch('/api/generate', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{urls: urls}}),
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
            document.getElementById('newError').textContent = '';
            renderInboxes(['']);
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

        function openReportFromClick(el) {{
            openReport({{id: el.getAttribute('data-id'), url: el.getAttribute('data-url')}});
        }}

        function openReport(rep) {{
            _selectedId = rep.id;
            renderReports();
            const viewer = document.getElementById('reportViewer');
            viewer.innerHTML = '<iframe src="' + rep.url + '"></iframe>';
        }}

        renderInboxes(['']);
        _applySidebarState();
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
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", SPA_HTML.encode("utf-8"))
            return

        if path == "/api/reports":
            self._send_json(200, _list_reports())
            return
        if path.startswith("/reports/"):
            self._serve_report_file(path)
            return
        if path.startswith("/assets/"):
            self._serve_static_asset(path)
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
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def _read_json_body(self, max_bytes: int = 1_000_000):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > max_bytes:
            return None, "missing or oversized request body"
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")), None
        except (ValueError, UnicodeDecodeError) as e:
            return None, f"invalid JSON body: {e}"

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

        try:
            entry = _run_generation(urls, payload_key=payload_key)
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


class _ThreadingTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded so a long-running /api/generate doesn't block other requests."""
    allow_reuse_address = True
    daemon_threads = True


def serve_spa(port: int = PORT) -> None:
    """Start the Dokimos Performance SPA HTTP server. Blocks until Ctrl+C."""
    _ensure_reports_dir()
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
        print(f"  Dokimos: Performance Report available at: http://localhost:{port}")
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
