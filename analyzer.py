#!/usr/bin/env python3
"""
Analyze endpoint response times from Report Portal logs.

Usage:
    python3 analyze_rp_timings.py "<url>"
    python3 analyze_rp_timings.py "<url1>,<url2>"              # Compare 2 launches
    python3 analyze_rp_timings.py "<url1>,<url2>,<url3>"       # Compare 3 launches
    python3 analyze_rp_timings.py "<url1>,<url2>,<url3>,<url4>" # Compare 4 launches
    python3 analyze_rp_timings.py --key "frequency-cap-detail" "<url>"
    
Example:
    export RP_TOKEN="your_token_here"
    python3 analyze_rp_timings.py "https://ads-report-portal.staging.hulu.com/ui/#ad-apps-automation/launches/all/1527175"
    
    # Compare multiple launches (comma-separated, no spaces):
    python3 analyze_rp_timings.py "https://.../1527175,https://.../1527200,https://.../1527300"
    
    # Group by presence of 'frequency-cap-detail' key in payload:
    python3 analyze_rp_timings.py --key "frequency-cap-detail" "https://.../launches/all/1527175"

Environment variables:
    RP_TOKEN    - Required. Report Portal API token
    RP_API_BASE - Optional. API base URL (default: https://ads-report-portal.staging.hulu.com/api/v1/ad-apps-automation/log)

Options:
    --key KEY   - Group endpoints by presence/absence of KEY in request payload
                  Also filters out 4xx error responses

Opens an HTML report at http://localhost:9999
"""

import sys
import re
import json
import os
import argparse
import time
import urllib.request
import http.server
import socketserver
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from statistics import median as _median, quantiles as _quantiles
from zoneinfo import ZoneInfo
from typing import Optional


# ---------------------------------------------------------------------------
# Shared report CSS
# ---------------------------------------------------------------------------
# Single source of truth for the HTML reports' visual language. Used by all
# three generators (single-launch, 2-launch comparison, multi-launch tabbed).
#
# Style: flat Grafana-like dashboard -- monospace numbers/data, subtle 1px
# borders on solid dark panels, no gradients, 2px corners. The hero accent
# is Dokimos bronze (#CD7F32) instead of Grafana blue so the embedded report
# harmonises with the Dokimos Performance SPA shell that hosts it.
#
# Generator-specific bits (e.g. the dynamic `repeat(N, 1fr)` for the
# per-launch timings grid) are appended after this base block at template
# interpolation time.
REPORT_CSS_BASE = r"""
:root {
    --bg:           #0b0e14;
    --panel:        #161a21;
    --panel-soft:   #1d2128;
    --panel-hi:     #22272f;
    --border:       #21262e;
    --border-hi:    #2c313a;

    --fg:           #d8d9da;
    --fg-soft:      #9fa3a8;
    --fg-dim:       #7a808a;
    --fg-faint:     #4a4f57;

    --bronze:       #CD7F32;
    --bronze-lt:    #E6A45A;
    --bronze-dk:    #8B4F1A;

    --green:        #52c41a;
    --green-bg:     rgba(82, 196, 26, 0.14);
    --red:          #e02f44;
    --red-bg:       rgba(224, 47, 68, 0.14);
    --orange:       #fa8c16;

    --purple:       #8b5cf6;

    --font-body:    -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
    --font-mono:    "JetBrains Mono", "SF Mono", "Menlo", "Monaco", monospace;
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: var(--font-body);
    background: var(--bg);
    color: var(--fg);
    min-height: 100vh;
    padding: 16px;
    font-size: 13px;
    line-height: 1.5;
}
.container { max-width: 1600px; margin: 0 auto; }

/* ----- Page chrome ----- */
header {
    margin-bottom: 16px;
    padding: 12px 16px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-left: 3px solid var(--bronze);
    text-align: left;
}
h1 {
    font-family: var(--font-mono);
    font-size: 14px;
    font-weight: 700;
    color: var(--fg);
    letter-spacing: 0.04em;
    margin-bottom: 4px;
    text-transform: uppercase;
}
.meta {
    color: var(--fg-dim);
    font-family: var(--font-mono);
    font-size: 11px;
}
.meta span { margin-right: 18px; }
.meta strong { color: var(--fg-soft); font-weight: 600; }

/* ----- KPI strip ----- */
.stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 8px;
    margin-bottom: 16px;
}
.stat-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-left: 3px solid var(--bronze);
    padding: 10px 14px;
}
.stat-card h3 {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--fg-dim);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 4px;
    font-weight: 600;
}
.stat-card .value {
    font-family: var(--font-mono);
    font-size: 22px;
    font-weight: 700;
    color: var(--bronze);
}
.stat-card .label {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--fg-dim);
    margin-top: 2px;
}

/* ----- Tables ----- */
table {
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    border: 1px solid var(--border);
    font-size: 12px;
}
th {
    background: var(--panel-soft);
    padding: 8px 12px;
    text-align: left;
    font-family: var(--font-mono);
    font-weight: 600;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-soft);
    border-bottom: 1px solid var(--border-hi);
    border-right: 1px solid var(--border);
}
th:last-child { border-right: none; }
th.launch1, th.launch2 { color: var(--bronze); }
td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    border-right: 1px solid var(--border);
    vertical-align: middle;
}
td:last-child { border-right: none; }
tr:hover td { background: var(--panel-hi); }

/* ----- HTTP method badges ----- */
.method {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 2px;
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #fff;
    line-height: 1.4;
    flex-shrink: 0;
}
.method.get    { background: #23a85e; }
.method.post   { background: #326ce5; }
.method.put    { background: #d97706; }
.method.delete { background: #dc2626; }
.method.patch  { background: #7c3aed; }

/* ----- Endpoint URLs / numeric data ----- */
.endpoint, .endpoint-url {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--fg-soft);
    word-break: break-all;
}
.number {
    font-family: var(--font-mono);
    text-align: right;
    font-size: 12px;
    color: var(--fg);
}

/* ----- Better / worse / same badges ----- */
.diff, .timing-diff {
    font-family: var(--font-mono);
    font-weight: 600;
    font-size: 11px;
}
.diff.better, .timing-diff.better { color: var(--green); }
.diff.worse,  .timing-diff.worse  { color: var(--red);   }
.diff.same,   .timing-diff.same   { color: var(--fg-dim); }
.diff.na                          { color: var(--fg-faint); }
.timing-diff {
    display: inline-block;
    padding: 1px 5px;
    border-radius: 2px;
}
.timing-diff.better { background: var(--green-bg); }
.timing-diff.worse  { background: var(--red-bg);   }

.diff-pct {
    display: inline-block;
    margin-top: 6px;
    padding: 2px 8px;
    border-radius: 2px;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 600;
}
.diff-pct.better { background: var(--green-bg); color: var(--green); }
.diff-pct.worse  { background: var(--red-bg);   color: var(--red);   }
.diff-pct.same   { background: var(--panel-hi); color: var(--fg-dim); }

.min-val { color: var(--green); }
.max-val { color: var(--orange); }

/* ----- "key" badges (single-launch filter info) ----- */
.key-badge {
    display: inline-block;
    margin-left: 8px;
    padding: 1px 6px;
    border-radius: 2px;
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
.key-badge.with-key    { background: var(--green-bg); color: var(--green);   }
.key-badge.without-key { background: var(--panel-hi); color: var(--fg-dim); }

.filter-info {
    background: var(--panel);
    border: 1px solid var(--border);
    border-left: 3px solid var(--orange);
    padding: 8px 12px;
    margin-bottom: 12px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--orange);
}

/* ----- Tabs ----- */
.tabs-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 0;
    margin-bottom: 16px;
    border-bottom: 1px solid var(--border-hi);
}
.tab-btn {
    background: transparent;
    border: 1px solid transparent;
    border-bottom: none;
    color: var(--fg-soft);
    padding: 8px 14px;
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-right: 1px;
}
.tab-btn:hover { color: var(--fg); background: var(--panel); }
.tab-btn.active {
    color: var(--bronze);
    background: var(--panel);
    border-color: var(--border-hi);
    border-bottom-color: var(--panel);
    position: relative;
    top: 1px;
}
.tab-pane { display: none; }
.tab-pane.active { display: block; }
.tab-intro {
    margin-bottom: 12px;
    color: var(--fg-dim);
    font-family: var(--font-mono);
    font-size: 11px;
}

/* ----- Search bar ----- */
.search-container {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 12px;
}
.search-input {
    flex: 1;
    max-width: 480px;
    padding: 7px 12px;
    font-family: var(--font-mono);
    font-size: 12px;
    background: var(--panel);
    border: 1px solid var(--border-hi);
    border-radius: 2px;
    color: var(--fg);
    outline: none;
}
.search-input:focus { border-color: var(--bronze); }
.search-input::placeholder { color: var(--fg-faint); }
.search-hint { font-family: var(--font-mono); font-size: 11px; color: var(--fg-dim); }
.clear-filter {
    padding: 6px 12px;
    background: var(--red-bg);
    border: 1px solid var(--red);
    border-radius: 2px;
    color: var(--red);
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 11px;
    display: none;
}
.clear-filter.visible { display: block; }
.clear-filter:hover { filter: brightness(1.15); }

.filter-status {
    padding: 8px 12px;
    margin-bottom: 10px;
    background: var(--panel);
    border: 1px solid var(--border-hi);
    border-left: 3px solid var(--bronze);
    color: var(--bronze);
    font-family: var(--font-mono);
    font-size: 11px;
    display: none;
}
.filter-status.visible { display: block; }

/* ----- Launch info cards ----- */
.launches-row {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    flex-wrap: wrap;
}
.launch-info-card {
    background: var(--panel);
    border: 1px solid var(--border);
    padding: 10px 12px;
    flex: 1;
    min-width: 160px;
}
.launch-num {
    font-family: var(--font-mono);
    font-size: 14px;
    font-weight: 700;
    color: var(--bronze);
    margin-bottom: 4px;
}
.launch-id {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--fg-soft);
    margin-bottom: 4px;
    overflow: hidden;
    text-overflow: ellipsis;
}
.launch-stats { font-family: var(--font-mono); font-size: 10px; color: var(--fg-dim); }
.launch-position {
    display: inline-block;
    font-family: var(--font-mono);
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 1px 5px;
    border-radius: 2px;
    margin-bottom: 4px;
}
.launch-position.oldest { background: var(--panel-hi); color: var(--fg-soft); }
.launch-position.newest { background: var(--panel-hi); color: var(--bronze); }
.launch-position.best   { background: var(--green-bg); color: var(--green); }
.launch-position.worst  { background: var(--red-bg);   color: var(--red);   }

/* ----- Endpoint cards (collapsible) ----- */
.endpoint-card {
    background: var(--panel);
    border: 1px solid var(--border);
    margin-bottom: 8px;
    overflow: hidden;
    cursor: pointer;
}
.endpoint-card:hover { border-color: var(--border-hi); }
.endpoint-header {
    background: var(--panel-soft);
    padding: 8px 12px;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--border);
}
.expand-icon {
    margin-left: auto;
    color: var(--fg-dim);
    font-family: var(--font-mono);
    font-size: 11px;
    transition: transform 0.2s;
}
.expand-icon.expanded { transform: rotate(180deg); }

/* ----- Per-launch timings grid ----- */
.timings-grid {
    display: grid;
    gap: 1px;
    background: var(--border);
}
.launch-timing { background: var(--panel); padding: 8px 10px; text-align: center; }
.launch-timing.na { opacity: 0.4; }
.launch-label {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--bronze);
    font-weight: 700;
    letter-spacing: 0.04em;
    margin-bottom: 4px;
}
.timing-value {
    font-family: var(--font-mono);
    font-size: 14px;
    font-weight: 700;
    color: var(--fg);
    margin-bottom: 2px;
}
.timing-minmax {
    font-family: var(--font-mono);
    font-size: 9px;
    color: var(--fg-dim);
    margin-bottom: 2px;
    display: flex;
    justify-content: center;
    gap: 6px;
}
.timing-count {
    font-family: var(--font-mono);
    font-size: 9px;
    color: var(--fg-faint);
    margin-bottom: 3px;
}

/* ----- Trend chart wrapper ----- */
.trend-chart-wrapper {
    padding: 12px;
    background: var(--panel);
    border: 1px solid var(--border);
    margin-top: 8px;
}

/* ----- Datadog traces section ----- */
.dd-section {
    background: var(--panel-soft);
    border-top: 1px solid var(--border-hi);
    padding: 10px 12px;
}
.dd-header { display: flex; align-items: center; cursor: pointer; padding: 4px 0; }
.dd-expand-icon {
    color: var(--purple);
    margin-right: 8px;
    font-family: var(--font-mono);
    font-size: 11px;
    transition: transform 0.2s;
}
.dd-expand-icon.expanded { transform: rotate(90deg); }
.dd-title {
    color: var(--fg);
    font-family: var(--font-mono);
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 0.04em;
}
.dd-count { color: var(--fg-dim); font-family: var(--font-mono); font-size: 11px; margin-left: 8px; }
.dd-traces-list { display: none; margin-top: 10px; }
.dd-traces-list.expanded { display: block; }

.dd-launch-group {
    margin-bottom: 8px;
    border: 1px solid var(--border-hi);
    border-left: 3px solid var(--purple);
    background: var(--panel);
}
.dd-launch-label {
    color: var(--fg);
    font-family: var(--font-mono);
    font-weight: 600;
    font-size: 11px;
    padding: 8px 12px;
    background: var(--panel-soft);
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
}
.dd-launch-label:hover { background: var(--panel-hi); }
.dd-launch-icon {
    font-family: var(--font-mono);
    font-size: 10px;
    transition: transform 0.2s;
    color: var(--purple);
}
.dd-launch-icon.expanded { transform: rotate(90deg); }
.dd-launch-traces { display: none; padding: 8px; }
.dd-launch-traces.expanded { display: block; }

.dd-trace-row {
    padding: 8px 10px;
    background: var(--panel-soft);
    border-left: 2px solid var(--purple);
    margin-bottom: 6px;
    font-family: var(--font-mono);
    font-size: 11px;
}
.dd-trace-row.hidden { display: none !important; }
.dd-trace-main {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    margin-bottom: 4px;
}
.dd-trace-duration { font-weight: 700; color: var(--red); }
.dd-trace-status {
    padding: 1px 6px;
    border-radius: 2px;
    font-family: var(--font-mono);
    font-weight: 700;
    font-size: 10px;
}
.dd-trace-status.success { background: var(--green-bg); color: var(--green); }
.dd-trace-status.error   { background: var(--red-bg);   color: var(--red);   }
.dd-trace-resource {
    color: var(--fg-soft);
    font-family: var(--font-mono);
    font-size: 11px;
    flex: 1;
    min-width: 150px;
    word-break: break-all;
}
.dd-trace-time { color: var(--fg-dim); font-family: var(--font-mono); font-size: 10px; }
.dd-trace-link {
    color: var(--purple);
    text-decoration: none;
    font-family: var(--font-mono);
    font-size: 10px;
}
.dd-trace-link:hover { text-decoration: underline; }
.dd-trace-payload {
    grid-column: 1 / -1;
    margin-top: 4px;
    padding: 6px 8px;
    background: var(--bg);
    border: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--fg-dim);
    word-break: break-all;
    white-space: pre-wrap;
}
.dd-filter {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--fg-dim);
}
.dd-duration-filter {
    width: 60px;
    padding: 3px 6px;
    border: 1px solid var(--border-hi);
    background: var(--bg);
    color: var(--fg);
    font-family: var(--font-mono);
    font-size: 11px;
}

/* ----- Payload details (collapsible inside endpoint-card) ----- */
.payload-details {
    display: none;
    background: var(--bg);
    border-top: 1px solid var(--border-hi);
    padding: 12px;
}
.payload-details.expanded { display: block; }
.payload-section-header {
    color: var(--fg-dim);
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.04em;
    margin-bottom: 12px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.toggle-switch { display: flex; align-items: center; gap: 6px; cursor: pointer; }
.toggle-switch input { display: none; }
.toggle-slider {
    width: 30px;
    height: 16px;
    background: var(--panel-hi);
    border-radius: 8px;
    position: relative;
    transition: background 0.15s;
    border: 1px solid var(--border-hi);
}
.toggle-slider::after {
    content: '';
    position: absolute;
    width: 12px;
    height: 12px;
    background: var(--fg-dim);
    border-radius: 50%;
    top: 1px;
    left: 1px;
    transition: all 0.15s;
}
.toggle-switch input:checked + .toggle-slider { background: var(--green-bg); border-color: var(--green); }
.toggle-switch input:checked + .toggle-slider::after { left: 16px; background: var(--green); }
.toggle-label {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--fg-dim);
    letter-spacing: 0.04em;
}

.payload-row {
    margin-bottom: 8px;
    background: var(--panel);
    border: 1px solid var(--border);
}
.payload-header {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--fg-soft);
    padding: 6px 10px;
    background: var(--panel-soft);
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    display: flex;
    align-items: flex-start;
    gap: 6px;
}
.payload-header:hover { background: var(--panel-hi); }
.payload-expand-icon {
    color: var(--fg-dim);
    font-family: var(--font-mono);
    font-size: 10px;
    transition: transform 0.2s;
    flex-shrink: 0;
    margin-top: 2px;
}
.payload-expand-icon.expanded { transform: rotate(90deg); }
.payload-preview {
    flex: 1;
    min-width: 0;
    word-break: break-all;
    white-space: pre-wrap;
}
/* Inline timing summary that appears on the right of the payload header
   in the tab-1 "Slowest-run payloads" breakdown. Kept dim so it doesn't
   fight with the payload preview for attention; bronze accent matches
   the rest of the report's emphasis colour. */
.payload-inline-metrics {
    flex-shrink: 0;
    margin-left: 12px;
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--bronze);
    letter-spacing: 0.02em;
    white-space: nowrap;
}
.payload-full {
    display: none;
    margin: 8px;
    padding: 8px;
    background: var(--bg);
    border: 1px solid var(--border);
    overflow-x: auto;
}
.payload-full.expanded { display: block; }
.payload-full pre {
    margin: 0;
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--green);
    white-space: pre-wrap;
    word-break: break-all;
}

.payload-timings-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 1px;
    padding: 8px;
    background: var(--border);
}
.payload-timing { background: var(--panel); padding: 6px 8px; text-align: center; }
.payload-timing.na { opacity: 0.4; }
.payload-label {
    font-family: var(--font-mono);
    font-size: 9px;
    color: var(--fg-dim);
    margin-bottom: 2px;
    letter-spacing: 0.04em;
}
.payload-value {
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    color: var(--bronze);
    margin-bottom: 2px;
}
.payload-minmax { font-family: var(--font-mono); font-size: 9px; color: var(--fg-dim); margin-bottom: 2px; }
.payload-count  { font-family: var(--font-mono); font-size: 9px; color: var(--fg-faint); }

/* ----- Legend ----- */
.legend {
    display: flex;
    gap: 16px;
    margin-bottom: 12px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--fg-soft);
    flex-wrap: wrap;
}
.legend-item { display: flex; align-items: center; gap: 6px; }
.legend-dot { width: 10px; height: 10px; border-radius: 2px; }
.legend-dot.better { background: var(--green); }
.legend-dot.worse  { background: var(--red);   }
.legend-note { color: var(--fg-dim); }

/* ----- Loader overlay ----- */
.loader-overlay {
    position: fixed;
    inset: 0;
    background: rgba(11, 14, 20, 0.85);
    display: none;
    justify-content: center;
    align-items: center;
    z-index: 1000;
}
.loader-overlay.visible { display: flex; }
.loader { text-align: center; }
.loader-spinner {
    width: 40px;
    height: 40px;
    border: 2px solid var(--border-hi);
    border-top-color: var(--bronze);
    border-radius: 50%;
    animation: spin 0.9s linear infinite;
    margin: 0 auto;
}
.loader-text {
    margin-top: 10px;
    color: var(--fg);
    font-family: var(--font-mono);
    font-size: 12px;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ----- AES-256-GCM password gate ----- */
body.locked #protectedContent { display: none; }
.auth-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: var(--bg);
    z-index: 10000;
    align-items: center;
    justify-content: center;
}
body.locked .auth-overlay { display: flex; }
.auth-box {
    background: var(--panel);
    border: 1px solid var(--border);
    border-top: 2px solid var(--bronze);
    padding: 28px 32px;
    text-align: center;
    min-width: 320px;
    max-width: 420px;
}
.auth-icon {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--bronze);
    letter-spacing: 0.12em;
    font-weight: 700;
    margin-bottom: 6px;
    text-transform: uppercase;
}
.auth-box h2 {
    font-family: var(--font-mono);
    font-size: 16px;
    font-weight: 700;
    color: var(--fg);
    margin-bottom: 6px;
    background: none;
    -webkit-text-fill-color: var(--fg);
    -webkit-background-clip: initial;
}
.auth-box p {
    color: var(--fg-soft);
    font-size: 12px;
    margin-bottom: 16px;
}
.auth-input {
    width: 100%;
    padding: 9px 12px;
    font-family: var(--font-mono);
    font-size: 13px;
    background: var(--bg);
    border: 1px solid var(--border-hi);
    color: var(--fg);
    outline: none;
    margin-bottom: 10px;
}
.auth-input:focus { border-color: var(--bronze); }
.auth-submit {
    width: 100%;
    padding: 9px 14px;
    background: var(--bronze);
    border: 1px solid var(--bronze);
    color: var(--bg);
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.06em;
}
.auth-submit:hover { background: var(--bronze-lt); border-color: var(--bronze-lt); }
.auth-submit:disabled {
    background: var(--panel-hi);
    color: var(--fg-dim);
    cursor: progress;
    border-color: var(--border-hi);
}
.auth-spinner {
    display: none;
    width: 40px;
    height: 40px;
    border: 2px solid var(--border-hi);
    border-top-color: var(--bronze);
    border-radius: 50%;
    animation: spin 0.9s linear infinite;
    margin: 14px auto 6px;
}
.auth-decrypting-msg {
    display: none;
    color: var(--bronze);
    font-family: var(--font-mono);
    font-size: 11px;
    margin-top: 8px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
body.decrypting .auth-input,
body.decrypting .auth-submit,
body.decrypting .auth-error,
body.decrypting .auth-box p { display: none; }
body.decrypting .auth-spinner,
body.decrypting .auth-decrypting-msg { display: block; }
.auth-error {
    min-height: 1.2em;
    margin-top: 8px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--red);
}
.auth-meta {
    margin-top: 14px;
    font-family: var(--font-mono);
    font-size: 9px;
    color: var(--fg-faint);
    letter-spacing: 0.08em;
    text-transform: uppercase;
}

/* ----- Summary / intro blocks ----- */
.summary {
    background: var(--panel);
    border: 1px solid var(--border);
    border-left: 3px solid var(--bronze);
    padding: 10px 14px;
    margin-bottom: 12px;
    color: var(--fg-soft);
    font-family: var(--font-mono);
    font-size: 11px;
    text-align: left;
}

/* ----- Footer ----- */
footer {
    text-align: center;
    margin-top: 24px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
    color: var(--fg-faint);
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}

/* ----- Two-col grid override (used inside simple-card timing layouts) ----- */
.simple-card .timings-grid.two-col { grid-template-columns: 1fr 1fr; gap: 1px; }


/* ============================================================== */
/* report-css-v: 5 -- READABILITY PASS                            */
/* ============================================================== */
/*
   This sits at the bottom so it can override the base rules above
   without rewriting them. The version marker is matched by the
   SPA's backfill so existing reports auto-pick-up newer passes
   the next time the server boots.

   Goals:
   1. Bigger, scannable primary numbers (tabular-nums, larger fonts).
   2. Compressed launch-row at top: keep dates, drop the
      "2247 logs / 2800 requests" noise (it's already in the per-cell
      "calls" line inside the 'All N Launches' tab).
   3. Tab bar with an obvious active indicator and sticky scrolling.
   4. Reduced ALL-CAPS chrome -- ALL CAPS now reserved for tabs,
      column headers, and pill labels (not values).
   5. Brighter body text and softer dim tiers so hierarchy reads.
   6. Stronger delta badges (better/worse).
*/

/* Tabular-num all the things so digits align column-wise. */
.timing-value, .number, td.number, .stat-card .value,
.diff, .diff-pct, .timing-diff, .timing-minmax, .timing-count,
.launch-stats, .meta, .launch-num {
    font-variant-numeric: tabular-nums;
}

/* ----- Brighter, less harsh body text ----- */
:root {
    --fg:       #e8ebed;
    --fg-soft:  #b6bac0;
    --fg-dim:   #8a8f97;
    --fg-faint: #595e66;
}
body { font-size: 13px; line-height: 1.55; }

/* ----- Page header: real title, not a label ----- */
header {
    padding: 16px 20px;
    border-left: none;
    border-top: 3px solid var(--bronze);
}
h1 {
    font-size: 18px;
    text-transform: none;
    letter-spacing: 0;
    margin-bottom: 6px;
    color: var(--fg);
    font-weight: 700;
}
.meta { font-size: 11.5px; color: var(--fg-soft); }
.meta strong { color: var(--fg); }
.summary { color: var(--fg-soft); }

/* ----- Compressed launch info strip ----- */
.launches-row { gap: 4px; margin-bottom: 14px; }
.launch-info-card {
    flex: 0 0 auto;
    min-width: 0;
    padding: 6px 10px;
    background: var(--panel-soft);
    border: 1px solid var(--border);
}
.launch-position {
    font-size: 9px;
    padding: 0 5px;
    margin-bottom: 2px;
    background: transparent !important;
    color: var(--fg-dim) !important;
    letter-spacing: 0.08em;
    border: 0;
}
.launch-position.oldest { color: var(--fg-soft) !important; }
.launch-position.newest { color: var(--bronze) !important; }
.launch-position.best   { color: var(--green) !important; }
.launch-position.worst  { color: var(--red)   !important; }
.launch-num { font-size: 11.5px; color: var(--fg); font-weight: 600; margin: 0; }
/* The "2247 logs / 2800 requests" is identical across launches in practice
   and shows up again per-row in the tab content. Drop it to recover space. */
.launches-row .launch-info-card .launch-stats { display: none; }

/* ----- Comparing-N banner: just a hairline above tabs ----- */
.summary {
    background: transparent;
    border: 0;
    border-bottom: 1px solid var(--border);
    padding: 6px 0;
    margin-bottom: 10px;
    font-size: 12px;
    font-family: var(--font-body);
    text-transform: none;
    letter-spacing: 0;
}

/* ----- Filter bar ----- */
.search-container { margin-bottom: 14px; gap: 10px; }
.search-input {
    padding: 9px 14px;
    font-size: 13px;
    background: var(--panel);
    border: 1px solid var(--border-hi);
}
.search-input:focus {
    border-color: var(--bronze);
    box-shadow: 0 0 0 1px var(--bronze);
}
/* The "Press Enter to filter" hint is already in the placeholder. */
.search-hint { display: none; }

/* ----- Tabs: bold active state, sticky in-iframe ----- */
.tabs-nav {
    position: sticky;
    top: 0;
    z-index: 10;
    background: var(--bg);
    padding-left: 2px;
    border-bottom: 2px solid var(--border-hi);
    margin-bottom: 18px;
}
.tab-btn {
    padding: 11px 18px;
    border: 0;
    background: transparent;
    color: var(--fg-dim);
    font-size: 11.5px;
    margin: 0;
    position: relative;
    top: 0;
}
.tab-btn:hover { color: var(--fg); background: transparent; }
.tab-btn.active {
    color: var(--bronze);
    background: transparent;
    border: 0;
    top: 0;
    box-shadow: inset 0 -3px 0 var(--bronze);
}
.tab-intro { font-size: 12px; color: var(--fg-soft); margin-bottom: 14px; }

/* ----- Endpoint card: bigger header, real hover ----- */
.endpoint-card { margin-bottom: 12px; }
.endpoint-card:hover { border-color: var(--bronze-dk); }
.endpoint-header {
    padding: 10px 14px;
    gap: 10px;
    border-bottom: 1px solid var(--border-hi);
}
.endpoint-url { font-size: 12px; color: var(--fg); }
.method { padding: 3px 8px; font-size: 10.5px; }

/* ----- Per-launch cell (used in BOTH "All N Launches" cells and
         the 2-col Median/Slowest comparison cards) ----- */
.launch-timing { padding: 10px 12px; }
/* Compact form when many columns share the row (>=4 launches). */
.timings-grid:not(.two-col) .launch-timing { padding: 8px 6px; }

.launch-label {
    font-size: 10px;
    color: var(--fg-dim);
    font-weight: 600;
    letter-spacing: 0.04em;
    margin-bottom: 4px;
}

.timing-value {
    font-size: 18px;
    font-weight: 700;
    color: var(--fg);
    margin-bottom: 3px;
    line-height: 1.15;
}
/* In the wide 2-col Median-vs-Slowest / Fastest-vs-Slowest cards we
   have room for a hero number. */
.simple-card .timings-grid.two-col .timing-value {
    font-size: 28px;
    margin-bottom: 6px;
}

.timing-minmax {
    font-size: 10px;
    color: var(--fg-dim);
    gap: 8px;
    margin-bottom: 1px;
}
.timing-minmax .min-val { color: var(--fg-dim); }
.timing-minmax .max-val { color: var(--fg-dim); }
.timing-count { font-size: 10px; color: var(--fg-faint); }

/* In the 2-col cards the median side carries metadata, not a "min"
   value -- neutralize the green/orange so it reads as info. */
.simple-card .launch-position.oldest + .timing-value + .timing-minmax .min-val,
.simple-card .launch-position.oldest + .timing-value + .timing-minmax .max-val {
    color: var(--fg-soft);
}

/* ----- Delta badges: front-and-center ----- */
.diff-pct {
    margin-top: 8px;
    padding: 4px 10px;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.02em;
    border-radius: 3px;
}
.diff-pct.better { background: var(--green-bg); color: var(--green); border: 1px solid rgba(82,196,26,0.35); }
.diff-pct.worse  { background: var(--red-bg);   color: var(--red);   border: 1px solid rgba(224,47,68,0.35); }
.diff-pct.same   { background: var(--panel-hi); color: var(--fg-dim); }

/* In the dense "All N Launches" grid keep the badge readable but not huge. */
.timings-grid:not(.two-col) .diff-pct {
    margin-top: 6px;
    padding: 2px 6px;
    font-size: 11px;
}

/* ----- Stat strip ----- */
.stat-card .value { color: var(--fg); font-size: 24px; }
.stat-card { border-left-width: 3px; }
.stat-card h3 { color: var(--fg-soft); }

/* ----- Footer ----- */
footer {
    margin-top: 20px;
    padding: 10px 0;
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--fg-faint);
    text-align: center;
    letter-spacing: 0.06em;
    text-transform: uppercase;
}


/* ============================================================== */
/* v3 follow-ups (sit at the bottom so they win the cascade)      */
/* ============================================================== */
/*
   Driven by user feedback that even after v2 the report was still
   not as scannable as it should be. Biggest moves:

   1. Heatmap-style cell backgrounds in the dense "All N Launches"
      tab -- the eye picks up a row of red/green at a glance instead
      of having to read the small +/- badges.
   2. Sticky endpoint header inside long-scrolling tabs, so the URL
      / method always stay visible while you scan the row below.
   3. Compressed per-cell metadata -- the count line ("1340 calls")
      is identical across every cell in a row, so we hide it in the
      dense grid where it adds noise without info.
   4. Even tighter top launch strip (single line of monospace dates).
   5. Bigger primary number in 2-col cards, slightly smaller in the
      dense grid so the heatmap tint reads cleanly.
   6. Hide the "(Baseline = leftmost / oldest run. Newest = rightmost.)"
      clarification -- it's already encoded by the leftmost OLDEST
      and rightmost NEWEST chips at the top.
   7. Slightly more bottom-padding on the page so the last endpoint
      card doesn't crowd the viewport edge.
*/

/* ----- 1. Heatmap on per-launch cells ----- */
/* Uses :has() to look at the cell's child diff-pct class. Tints are
   intentionally subtle so they don't obscure the number; the badge
   stays as the primary signal but the cell-wide wash gives a
   row-of-thirteen visual that's instantly scannable. */
.timings-grid:not(.two-col) .launch-timing:has(.diff-pct.better) {
    background: linear-gradient(0deg, rgba(82, 196, 26, 0.10), rgba(82, 196, 26, 0.10)), var(--panel);
}
.timings-grid:not(.two-col) .launch-timing:has(.diff-pct.worse) {
    background: linear-gradient(0deg, rgba(224, 47, 68, 0.10), rgba(224, 47, 68, 0.10)), var(--panel);
}
/* Browsers without :has() support fall through to plain var(--panel). */

/* ----- 2. Endpoint header background ----- */
/* (sticky-positioning experiment removed -- it caused content shifts in
   tab-panes with virtualized layouts; keeping the visual hierarchy
   intact is more important than the pin-on-scroll affordance.) */

/* ----- 3. Compress per-cell metadata in the dense grid ----- */
.timings-grid:not(.two-col) .launch-timing { padding: 8px 6px; }
.timings-grid:not(.two-col) .launch-label {
    font-size: 9.5px;
    color: var(--fg-dim);
    margin-bottom: 3px;
}
.timings-grid:not(.two-col) .timing-value {
    font-size: 15px;
    color: var(--fg);
    margin-bottom: 2px;
}
.timings-grid:not(.two-col) .timing-minmax {
    font-size: 9px;
    margin-bottom: 1px;
    line-height: 1.2;
}
/* The per-cell "1340 calls" repeats unchanged across an entire row --
   it's noise inside a heatmap row. Hide in the dense grid only; the
   2-col cards still show it so the comparison view doesn't lose it. */
.timings-grid:not(.two-col) .timing-count { display: none; }

/* ----- 4. Even tighter launch chip strip ----- */
.launches-row {
    gap: 3px;
    margin-bottom: 12px;
    flex-wrap: nowrap;
    overflow-x: auto;
}
.launches-row .launch-info-card {
    padding: 4px 8px;
    background: var(--panel);
}
.launches-row .launch-num { font-size: 10.5px; font-weight: 500; }
.launches-row .launch-position {
    font-size: 8.5px;
    margin-bottom: 0;
    line-height: 1.4;
}

/* ----- 5. Tweak hero / dense number sizes ----- */
.simple-card .timings-grid.two-col .timing-value { font-size: 32px; }
/*
   Column titles in the 2-col compare cards need EQUAL visual weight so
   the reader can tell at a glance "this is MEDIAN, this is SLOWEST".
   We use brand bronze for the baseline and red for the worst run --
   both are bold and same-size, so the eye lands on both at once.
*/
.simple-card .timings-grid.two-col .launch-position {
    font-size: 12px !important;
    font-weight: 700;
    margin-bottom: 6px;
    padding: 2px 8px !important;
    background: var(--panel-hi) !important;
    color: var(--fg-soft) !important;
    border-radius: 2px;
    border: 1px solid var(--border) !important;
    letter-spacing: 0.06em;
}
.simple-card .timings-grid.two-col .launch-position.oldest,
.simple-card .timings-grid.two-col .launch-position.newest {
    color: var(--bronze) !important;
    border-color: var(--bronze-dk) !important;
    background: rgba(193, 137, 60, 0.10) !important;
}
.simple-card .timings-grid.two-col .launch-position.worst {
    color: var(--red) !important;
    border-color: rgba(224,47,68,0.45) !important;
    background: rgba(224,47,68,0.10) !important;
}
.simple-card .timings-grid.two-col .launch-position.best {
    color: var(--green) !important;
    border-color: rgba(82,196,26,0.45) !important;
    background: rgba(82,196,26,0.10) !important;
}
.simple-card .timings-grid.two-col .launch-label {
    font-size: 11px;
    color: var(--fg-soft);
    margin-bottom: 6px;
    font-family: var(--font-mono);
}

/* ----- 6. Drop the redundant legend clarification ----- */
.legend-note { display: none; }
.legend { gap: 14px; }
.legend-item { font-size: 11.5px; }

/* ----- 7. Cleaner spacing ----- */
body { padding: 16px 18px 32px; }
.container { max-width: none; }   /* let the report fill the iframe */
.endpoint-card { margin-bottom: 14px; }

/* Sub-card section labels inside the "All N Launches" tab. */
.payload-section-header {
    color: var(--fg-soft);
    font-size: 11.5px;
    letter-spacing: 0.04em;
    text-transform: none;
}

/* The diff-pct badge in the wide 2-col cards should also be bigger. */
.simple-card .timings-grid.two-col .diff-pct {
    margin-top: 12px;
    padding: 6px 14px;
    font-size: 14px;
}
"""


def _percentile(values, pct: float):
    """Return the `pct`-th percentile of `values` (0..100), or None if empty.

    Uses statistics.quantiles with the inclusive method when there are enough
    samples. Falls back to a manual interpolation for very small lists where
    quantiles() raises or is unreliable.
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    try:
        cutpoints = _quantiles(values, n=100, method='inclusive')
        # cutpoints[i] is the (i+1)th percentile.
        idx = max(0, min(98, int(round(pct)) - 1))
        return cutpoints[idx]
    except Exception:
        # Linear interpolation fallback for tiny samples.
        ordered = sorted(values)
        k = (len(ordered) - 1) * (pct / 100.0)
        lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
        frac = k - lo
        return ordered[lo] + (ordered[hi] - ordered[lo]) * frac

# ---------------------------------------------------------------------------
# Pagination, network and Datadog constants
# ---------------------------------------------------------------------------

_RP_LOG_PAGE_SIZE            = 300
_RP_HTTP_TIMEOUT_SECS        = 120
_RP_FETCH_RETRY_BACKOFF_SECS = 2
_DD_TRACE_BUFFER_MINUTES     = 5
_DD_TRACE_FETCH_LIMIT        = 200

TOKEN = os.environ.get("RP_TOKEN", "")
API_BASE = os.environ.get("RP_API_BASE", "https://ads-report-portal.staging.hulu.com/api/v1/ad-apps-automation/log")
PORT = 9999

# SECURITY-REVIEW: the report data (timings, payloads, URLs) is encrypted at
# generation time with AES-256-GCM under a key derived from REPORT_PASSWORD
# via PBKDF2-HMAC-SHA256 (300k iterations). Without the password the rendered
# HTML contains only a base64 ciphertext blob -- no DOM hack or DevTools poke
# can recover the plaintext. Brute-force resistance is bounded by the password
# strength, so use a non-trivial password. Empty REPORT_PASSWORD disables the
# gate (no encryption, content rendered in clear).
REPORT_PASSWORD = os.environ.get("REPORT_PASSWORD", "abcABC123(1)(1)")
_PBKDF2_ITERATIONS = 300_000


def _encrypt_report_payload(plaintext_bytes: bytes, password: str) -> dict:
    """AES-256-GCM encrypt `plaintext_bytes` with a key derived from
    `password` via PBKDF2-HMAC-SHA256. Returns a dict with base64-encoded
    salt / iv / ciphertext and the kdf iteration count, ready to embed in
    HTML. Salt and IV are freshly random on every call (never deterministic).
    """
    import base64
    import secrets
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    key = kdf.derive(password.encode("utf-8"))
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(iv, plaintext_bytes, associated_data=None)
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv":   base64.b64encode(iv).decode("ascii"),
        "ct":   base64.b64encode(ciphertext).decode("ascii"),
        "kdf_iter": _PBKDF2_ITERATIONS,
        "kdf": "PBKDF2-HMAC-SHA256",
        "alg": "AES-256-GCM",
        "v":   1,
    }

# Cache directory for logs
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")

# Datadog integration
DD_API_KEY = os.environ.get("DISNEY_DD_API_KEY", "")
DD_APP_KEY = os.environ.get("DISNEY_DD_APP_KEY", "")
DD_SERVICE = "ifp-service"


def fetch_datadog_traces(from_ts: int, to_ts: int, limit: int = 50, min_duration_s: float = 3.0, service_filter: str = None) -> list:
    """
    Fetch traces from Datadog APM for a given time range.
    
    Args:
        from_ts: Start timestamp in milliseconds
        to_ts: End timestamp in milliseconds
        limit: Max number of traces to return
        min_duration_s: Minimum duration in seconds (default 3s)
        service_filter: Optional service name filter (if None, searches all services)
    
    Returns:
        List of trace data
    """
    if not DD_API_KEY or not DD_APP_KEY:
        return []
    
    min_duration_ns = int(min_duration_s * 1_000_000_000)
    
    # Build query - optionally filter by service
    if service_filter:
        query = f"service:{service_filter} @duration:>{min_duration_ns}"
    else:
        query = f"@duration:>{min_duration_ns}"
    
    from_str = datetime.fromtimestamp(from_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_str = datetime.fromtimestamp(to_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    payload = {
        "data": {
            "type": "search_request",
            "attributes": {
                "filter": {
                    "query": query,
                    "from": from_str,
                    "to": to_str
                },
                "sort": "-duration",
                "page": {"limit": limit}
            }
        }
    }
    
    try:
        req = urllib.request.Request(
            "https://disneystreaming.datadoghq.com/api/v2/spans/events/search",
            data=json.dumps(payload).encode(),
            headers={
                "DD-API-KEY": DD_API_KEY,
                "DD-APPLICATION-KEY": DD_APP_KEY,
                "Content-Type": "application/json"
            },
            method="POST"
        )
        
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            return data.get('data', [])
    except Exception as e:
        print(f"  Warning: Datadog API error: {e}")
        return []


def get_datadog_trace_url(trace_id: str) -> str:
    """Generate Datadog UI URL for a trace."""
    return f"https://disneystreaming.datadoghq.com/apm/trace/{trace_id}"


def extract_launch_id(url: str) -> str:
    """
    Extract launch ID from Report Portal URL.
    
    Supported formats:
      - https://ads-report-portal.staging.hulu.com/ui/#ad-apps-automation/launches/all/1527175
      - https://ads-report-portal.staging.hulu.com/ui/#ad-apps-automation/launches/all/d77db9ee-24cc-40b1-8829-6a5c24f04abe
      - Any URL containing /launches/all/<id> or /launches/<id> (numeric or UUID)
      - Any URL containing launchId=<id>
      - Plain launch ID (numeric or UUID)
    """
    # Handle full Report Portal UI URLs with UUID: /launches/all/uuid
    match = re.search(r'launches/(?:all/)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', url, re.I)
    if match:
        return match.group(1)
    
    # Handle full Report Portal UI URLs with numeric ID: /launches/all/123
    match = re.search(r'launches/(?:all/)?(\d+)', url)
    if match:
        return match.group(1)
    
    # Handle API URLs with launchId parameter (UUID or numeric)
    match = re.search(r'launchId=([0-9a-f-]+)', url, re.I)
    if match:
        return match.group(1)
    
    # Handle plain launch ID (UUID format)
    uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    if re.match(uuid_pattern, url.strip(), re.I):
        return url.strip()
    
    # Handle plain numeric ID
    if url.strip().isdigit():
        return url.strip()
    
    raise ValueError(f"Could not extract launch ID from URL: {url}")


def resolve_launch_id(launch_id: str) -> dict:
    """Resolve launch ID and fetch launch info including start time.
    
    Returns dict with: numeric_id, uuid, start_time (epoch ms), start_time_str
    """
    api_base = API_BASE.rsplit('/log', 1)[0]  # Remove /log from the end
    
    # Check if it's a UUID
    uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    is_uuid = bool(re.match(uuid_pattern, launch_id, re.I))
    
    if is_uuid:
        url = f"{api_base}/launch/uuid/{launch_id}"
    else:
        url = f"{api_base}/launch/{launch_id}"
    
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        numeric_id = str(data['id'])
        start_time_raw = data.get('startTime', 0)
        la_tz = ZoneInfo("America/Los_Angeles")
        
        # Handle both ISO string and epoch ms formats
        if isinstance(start_time_raw, str):
            start_dt = datetime.fromisoformat(start_time_raw.replace('Z', '+00:00')).astimezone(la_tz)
            start_time = int(start_dt.timestamp() * 1000)
        else:
            start_time = int(start_time_raw)
            start_dt = datetime.fromtimestamp(start_time / 1000, tz=timezone.utc).astimezone(la_tz)
        
        start_time_str = start_dt.strftime("%m/%d %H:%M")
        
        if is_uuid:
            print(f"  Resolved UUID {launch_id} -> ID {numeric_id}, started {start_time_str}")
        else:
            print(f"  Launch {numeric_id}, started {start_time_str}")
        
        return {
            'numeric_id': numeric_id,
            'uuid': data.get('uuid', launch_id),
            'start_time': start_time,
            'start_time_str': start_time_str
        }


def get_cache_path(launch_id: str, with_responses: bool = False) -> str:
    """Get the cache file path for a launch ID.
    
    Files are named:
    - {launch_id}.json for logs without responses (Duration filter only)
    - {launch_id}_full.json for full logs with responses
    """
    suffix = "_full" if with_responses else ""
    return os.path.join(LOGS_DIR, f"{launch_id}{suffix}.json")


def load_logs_from_cache(launch_id: str, with_responses: bool = False) -> Optional[dict]:
    """Load logs from cache if available."""
    cache_path = get_cache_path(launch_id, with_responses)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)
                print(f"  Loaded {len(data.get('content', []))} logs from cache ({os.path.basename(cache_path)})")
                return data
        except Exception as e:
            print(f"  Warning: Failed to load cache: {e}")
    return None


def save_logs_to_cache(launch_id: str, logs_data: dict, with_responses: bool = False) -> None:
    """Save logs to cache file."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    cache_path = get_cache_path(launch_id, with_responses)
    try:
        with open(cache_path, 'w') as f:
            json.dump(logs_data, f)
        print(f"  Saved logs to cache: {cache_path}")
    except Exception as e:
        print(f"  Warning: Failed to save cache: {e}")


def fetch_logs_with_cache(launch_id: str, max_retries: int = 3, with_responses: bool = False) -> dict:
    """Fetch logs from cache or Report Portal API."""
    cached = load_logs_from_cache(launch_id, with_responses)
    if cached:
        return cached
    
    logs = fetch_logs(launch_id, max_retries, with_responses)
    save_logs_to_cache(launch_id, logs, with_responses)
    return logs


def get_dd_cache_path(launch_id: str) -> str:
    """Get the Datadog cache file path for a launch ID."""
    return os.path.join(LOGS_DIR, f"{launch_id}_dd.json")


def load_dd_traces_from_cache(launch_id: str) -> Optional[list]:
    """Load Datadog traces from cache if available."""
    cache_path = get_dd_cache_path(launch_id)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)
                print(f"    Loaded {len(data)} DD traces from cache ({os.path.basename(cache_path)})")
                return data
        except Exception as e:
            print(f"    Warning: Failed to load DD cache: {e}")
    return None


def save_dd_traces_to_cache(launch_id: str, traces: list) -> None:
    """Save Datadog traces to cache file."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    cache_path = get_dd_cache_path(launch_id)
    try:
        with open(cache_path, 'w') as f:
            json.dump(traces, f)
        print(f"    Saved {len(traces)} DD traces to cache: {os.path.basename(cache_path)}")
    except Exception as e:
        print(f"    Warning: Failed to save DD cache: {e}")


def fetch_dd_traces_with_cache(launch_id: str, from_ts: int, to_ts: int, limit: int = 200, min_duration_s: float = 3.0) -> list:
    """Fetch Datadog traces from cache or API."""
    cached = load_dd_traces_from_cache(launch_id)
    if cached is not None:
        return cached
    
    traces = fetch_datadog_traces(from_ts, to_ts, limit=limit, min_duration_s=min_duration_s, service_filter=None)
    save_dd_traces_to_cache(launch_id, traces)
    return traces


def fetch_logs(launch_id: str, max_retries: int = 3, with_responses: bool = False) -> dict:
    """Fetch logs from Report Portal API with retry logic and pagination."""
    if with_responses:
        base_url = f"{API_BASE}?filter.eq.launchId={launch_id}"
    else:
        base_url = f"{API_BASE}?filter.eq.launchId={launch_id}&filter.cnt.message=Duration"
    
    all_content = []
    page = 1
    page_size = _RP_LOG_PAGE_SIZE
    total_pages = 1
    total_elements = 0
    
    while page <= total_pages:
        url = f"{base_url}&page.size={page_size}&page.page={page}"
        
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url)
                req.add_header("Authorization", f"Bearer {TOKEN}")
                
                with urllib.request.urlopen(req, timeout=_RP_HTTP_TIMEOUT_SECS) as response:
                    data = json.loads(response.read().decode())
                    all_content.extend(data.get('content', []))
                    total_pages = data.get('page', {}).get('totalPages', 1)
                    total_elements = data.get('page', {}).get('totalElements', 0)
                    if page == 1:
                        print(f"  Found {total_elements} logs ({total_pages} pages)")
                    else:
                        print(f"  Page {page}/{total_pages}...", end='\r')
                    break
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"  Retry {attempt + 1}/{max_retries} after error: {e}")
                    time.sleep(_RP_FETCH_RETRY_BACKOFF_SECS)
                else:
                    raise
        
        page += 1
    
    print(f"  Fetched {len(all_content)} logs")
    return {'content': all_content, 'page': {'totalElements': len(all_content)}}


def analyze_timings(logs_data: dict, payload_key: str = None) -> dict:
    """
    Extract endpoint timings from logs.
    
    If payload_key is specified:
    - Groups endpoints by URL + presence of key in payload
    - Filters out 4xx error responses
    - Returns dict with keys like "POST http://... [with key]" or "POST http://... [without key]"
    """
    if payload_key:
        return _analyze_timings_with_key(logs_data, payload_key)
    
    endpoint_timings = defaultdict(list)
    
    for log in logs_data.get('content', []):
        message = log.get('message', '')
        
        # Try REQUEST: format first (most common)
        if message.startswith('REQUEST:'):
            method_match = re.search(r'Method:\s*(POST|GET|PUT|DELETE|PATCH)', message)
            url_match = re.search(r'URL:\s*(\S+)', message)
            duration_match = re.search(r'Duration:\s*([0-9.]+)s', message)
            
            if method_match and url_match and duration_match:
                method = method_match.group(1)
                url = url_match.group(1)
                duration = float(duration_match.group(1))
                endpoint = f'{method} {url}'
                endpoint_timings[endpoint].append(duration)
            continue
        
        # Fallback: numbered format "1. POST http://..."
        lines = message.split('\n')
        for i, line in enumerate(lines):
            endpoint_match = re.match(r'^\d+\.\s+(POST|GET|PUT|DELETE|PATCH)\s+(.+)$', line.strip())
            if endpoint_match:
                method = endpoint_match.group(1)
                url = endpoint_match.group(2).strip()
                endpoint = f'{method} {url}'
                
                if i + 1 < len(lines):
                    duration_match = re.search(r'Duration:\s+([0-9.]+)s', lines[i + 1])
                    if duration_match:
                        duration = float(duration_match.group(1))
                        endpoint_timings[endpoint].append(duration)
    
    return endpoint_timings


def _analyze_timings_with_key(logs_data: dict, payload_key: str) -> dict:
    """
    Analyze timings with payload key grouping and 4xx filtering.
    
    Groups by: endpoint URL + whether payload contains the specified key.
    Filters out: requests that resulted in 4xx status codes.
    """
    endpoint_timings = defaultdict(list)
    
    # Group logs by itemId to correlate REQUEST/RESPONSE
    logs_by_item = defaultdict(list)
    for log in logs_data.get('content', []):
        item_id = log.get('itemId')
        if item_id:
            logs_by_item[item_id].append(log)
    
    # Process each item's logs
    for item_id, logs in logs_by_item.items():
        # Find REQUEST logs with Duration
        for log in logs:
            message = log.get('message', '')
            
            if not message.startswith('REQUEST:'):
                continue
            
            # Parse REQUEST log
            method_match = re.search(r'Method:\s*(POST|GET|PUT|DELETE|PATCH)', message)
            url_match = re.search(r'URL:\s*(\S+)', message)
            payload_match = re.search(r'Payload:\s*(\{.*\})', message, re.DOTALL)
            duration_match = re.search(r'Duration:\s*([0-9.]+)s', message)
            
            if not (method_match and url_match and duration_match):
                continue
            
            method = method_match.group(1)
            url = url_match.group(1)
            duration = float(duration_match.group(1))
            
            # Check for key in payload
            has_key = False
            if payload_match:
                try:
                    payload = json.loads(payload_match.group(1))
                    has_key = _check_key_in_payload(payload, payload_key)
                except json.JSONDecodeError:
                    pass
            
            # Find corresponding RESPONSE to check status
            response_status = None
            for resp_log in logs:
                resp_msg = resp_log.get('message', '')
                if resp_msg.startswith('RESPONSE:'):
                    status_match = re.search(r'Status:\s*(\d+)', resp_msg)
                    if status_match:
                        response_status = int(status_match.group(1))
                        break
            
            # Filter out 4xx errors
            if response_status and 400 <= response_status < 500:
                continue
            
            # Create endpoint key with key presence indicator
            key_indicator = f" [with {payload_key}]" if has_key else f" [without {payload_key}]"
            endpoint = f'{method} {url}{key_indicator}'
            endpoint_timings[endpoint].append(duration)
    
    return endpoint_timings


def _check_key_in_payload(payload: dict, key: str) -> bool:
    """Check if key exists in payload (supports nested keys with dot notation)."""
    if '.' in key:
        parts = key.split('.', 1)
        if parts[0] in payload and isinstance(payload[parts[0]], dict):
            return _check_key_in_payload(payload[parts[0]], parts[1])
        return False
    return key in payload


def _normalize_payload(payload: dict) -> str:
    """Create a normalized string representation of payload for grouping."""
    if not payload:
        return "(empty)"
    try:
        # Sort keys and create a compact representation
        return json.dumps(payload, sort_keys=True, separators=(',', ':'))
    except Exception:
        return str(payload)


def analyze_timings_detailed(logs_data: dict) -> dict:
    """
    Extract endpoint timings with detailed payload breakdown.
    
    Returns dict with structure:
    {
        'endpoint': {
            'durations': [list of all durations],
            'by_payload': {
                'payload_str': [list of durations for this payload]
            },
            'slow_requests': [list of {timestamp, duration, payload} for requests > 3s]
        }
    }
    """
    endpoint_data = defaultdict(lambda: {'durations': [], 'by_payload': defaultdict(list), 'slow_requests': []})
    
    for log in logs_data.get('content', []):
        message = log.get('message', '')
        log_time = log.get('time', '')  # ISO format timestamp from Report Portal
        
        # Try REQUEST: format first (most common)
        if message.startswith('REQUEST:'):
            method_match = re.search(r'Method:\s*(POST|GET|PUT|DELETE|PATCH)', message)
            url_match = re.search(r'URL:\s*(\S+)', message)
            duration_match = re.search(r'Duration:\s*([0-9.]+)s', message)
            payload_match = re.search(r'Payload:\s*(\{.*\})', message, re.DOTALL)
            
            if method_match and url_match and duration_match:
                method = method_match.group(1)
                url = url_match.group(1)
                duration = float(duration_match.group(1))
                endpoint = f'{method} {url}'
                
                # Extract and normalize payload
                payload_str = "(no payload)"
                if payload_match:
                    try:
                        payload = json.loads(payload_match.group(1))
                        payload_str = _normalize_payload(payload)
                    except json.JSONDecodeError:
                        payload_str = "(invalid json)"
                
                endpoint_data[endpoint]['durations'].append(duration)
                endpoint_data[endpoint]['by_payload'][payload_str].append(duration)
                
                # Track slow requests (> 3s) with their timestamps for Datadog lookup
                if duration > 3.0 and log_time:
                    endpoint_data[endpoint]['slow_requests'].append({
                        'timestamp': log_time,
                        'duration': duration,
                        'payload': payload_str[:200]
                    })
            continue
        
        # Fallback: numbered format "1. POST http://..."
        lines = message.split('\n')
        for i, line in enumerate(lines):
            endpoint_match = re.match(r'^\d+\.\s+(POST|GET|PUT|DELETE|PATCH)\s+(.+)$', line.strip())
            if endpoint_match:
                method = endpoint_match.group(1)
                url = endpoint_match.group(2).strip()
                endpoint = f'{method} {url}'
                
                if i + 1 < len(lines):
                    duration_match = re.search(r'Duration:\s+([0-9.]+)s', lines[i + 1])
                    if duration_match:
                        duration = float(duration_match.group(1))
                        endpoint_data[endpoint]['durations'].append(duration)
                        endpoint_data[endpoint]['by_payload']["(no payload)"].append(duration)
                        
                        # Track slow requests
                        if duration > 3.0 and log_time:
                            endpoint_data[endpoint]['slow_requests'].append({
                                'timestamp': log_time,
                                'duration': duration,
                                'payload': "(no payload)"
                            })
    
    return endpoint_data


def generate_html(launch_id: str, logs_data: dict, endpoint_timings: dict, payload_key: str = None, report_password: Optional[str] = None) -> str:
    """Generate HTML report.

    When ``report_password`` is non-empty (or the module-level
    ``REPORT_PASSWORD`` env-var fallback is set), the body content is
    encrypted with AES-256-GCM and the rendered HTML carries only the
    auth overlay until the user enters the password. The single-launch
    gate is a simpler version of the one used in
    :func:`generate_multi_comparison_html` -- the protected bundle only
    needs the HTML string (no JS state to re-hydrate).
    """
    total_logs = logs_data.get('page', {}).get('totalElements', 0)
    la_tz = ZoneInfo("America/Los_Angeles")
    _now_la = datetime.now(la_tz)
    # Server-formatted fallback (used as the static text inside <time>) plus
    # an unambiguous ISO 8601 string with timezone offset. The in-report JS
    # rewrites the visible text to the viewer's local timezone using the
    # `datetime` attribute -- so opening a PST-generated report from EST
    # shows the timestamp in EST.
    generated_at = _now_la.strftime("%Y-%m-%d %H:%M:%S")
    generated_at_iso = _now_la.isoformat(timespec="seconds")
    
    rows = []
    for endpoint in sorted(endpoint_timings.keys()):
        durations = endpoint_timings[endpoint]
        avg_duration = sum(durations) / len(durations)
        min_duration = min(durations)
        max_duration = max(durations)
        count = len(durations)
        
        method = endpoint.split()[0]
        method_class = method.lower()
        
        # Handle key indicator in endpoint name
        endpoint_url = endpoint.split(' ', 1)[1]
        key_badge = ""
        if payload_key:
            if f"[with {payload_key}]" in endpoint_url:
                key_badge = f'<span class="key-badge with-key">with {payload_key}</span>'
                endpoint_url = endpoint_url.replace(f" [with {payload_key}]", "")
            elif f"[without {payload_key}]" in endpoint_url:
                key_badge = f'<span class="key-badge without-key">without {payload_key}</span>'
                endpoint_url = endpoint_url.replace(f" [without {payload_key}]", "")
        
        rows.append(f"""
            <tr>
                <td><span class="method {method_class}">{method}</span></td>
                <td class="endpoint">{endpoint_url}{key_badge}</td>
                <td class="number">{count}</td>
                <td class="number">{avg_duration:.3f}s</td>
                <td class="number">{min_duration:.3f}s</td>
                <td class="number">{max_duration:.3f}s</td>
            </tr>
        """)
    
    filter_info_html = (
        f'<div class="filter-info">Grouped by presence of "<strong>{payload_key}</strong>" '
        f'in request payload | 4xx errors excluded</div>'
        if payload_key else ''
    )
    rows_html = (
        ''.join(rows)
        if rows else
        '<tr><td colspan="6" style="text-align:center;padding:40px;">No timing data found</td></tr>'
    )
    protected_html = f"""
        {filter_info_html}

        <div class="stats">
            <div class="stat-card">
                <h3>Total Logs Processed</h3>
                <div class="value">{total_logs}</div>
            </div>
            <div class="stat-card">
                <h3>Unique Endpoints</h3>
                <div class="value">{len(endpoint_timings)}</div>
            </div>
            <div class="stat-card">
                <h3>Total Requests</h3>
                <div class="value">{sum(len(v) for v in endpoint_timings.values())}</div>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th>Method</th>
                    <th>Endpoint</th>
                    <th style="text-align: right;">Count</th>
                    <th style="text-align: right;">Average</th>
                    <th style="text-align: right;">Min</th>
                    <th style="text-align: right;">Max</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    """

    # ----- AES-GCM gate ----- (mirrors generate_multi_comparison_html)
    # The caller can supply an explicit ``report_password``; if not, we fall
    # back to the module-level REPORT_PASSWORD env-var. An empty string at
    # either layer disables the gate entirely.
    effective_password = report_password if report_password is not None else REPORT_PASSWORD
    if effective_password:
        bundle = {"html": protected_html}
        enc = _encrypt_report_payload(
            json.dumps(bundle, separators=(",", ":")).encode("utf-8"),
            effective_password,
        )
        encrypted_payload_json = json.dumps(enc)
        protected_html_rendered = ''
        body_locked_class = ' class="locked"'
        auth_overlay_html = '''
    <div class="auth-overlay" id="authOverlay">
        <div class="auth-box">
            <div class="auth-icon">[ Locked ]</div>
            <h2>Protected report</h2>
            <p>Enter the password to decrypt the data.</p>
            <input type="password" id="authPasswordInput" class="auth-input"
                   placeholder="password" autocomplete="off" autofocus>
            <button type="button" class="auth-submit" id="authSubmit" onclick="checkAuthPassword()">Unlock</button>
            <div class="auth-error" id="authError"></div>
            <div class="auth-spinner"></div>
            <div class="auth-decrypting-msg">Decrypting&hellip;</div>
            <div class="auth-meta">AES-256-GCM &middot; PBKDF2-HMAC-SHA256 (300k iter)</div>
        </div>
    </div>
'''
    else:
        encrypted_payload_json = 'null'
        protected_html_rendered = protected_html
        body_locked_class = ''
        auth_overlay_html = ''

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Endpoint Response Time Analysis - Launch {launch_id}</title>
    <style>{REPORT_CSS_BASE}</style>
</head>
<body{body_locked_class}>
    {auth_overlay_html}
    <div class="container">
        <header>
            <h1>Endpoint Response Time Analysis</h1>
            <div class="meta">
                <span>Launch ID: <strong>{launch_id}</strong></span>
                <span>Generated: <time datetime="{generated_at_iso}" data-localize="datetime">{generated_at}</time></span>
            </div>
        </header>

        <div id="protectedContent">{protected_html_rendered}</div>

        <footer>
            Report Portal Timing Analysis Tool
        </footer>
    </div>

    <script>
        // ---------- AES-GCM password gate (single-launch) ----------
        // Simpler than the multi-launch gate: there's no JS state to
        // re-hydrate, so we just drop the decrypted HTML into the slot
        // and clear the locked class. Implementation mirrors the
        // multi-launch one so both reports unlock the same way.
        const _AUTH_ENCRYPTED_PAYLOAD = {encrypted_payload_json};
        const _AUTH_PAYLOAD_KEY = _AUTH_ENCRYPTED_PAYLOAD
            ? 'rp_report_payload_v1_' + (_AUTH_ENCRYPTED_PAYLOAD.ct || '').slice(0, 24)
            : null;

        function _b64ToBytes(b64) {{
            const bin = atob(b64);
            const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            return arr;
        }}

        async function _deriveAesKey(password, saltBytes, iters) {{
            const enc = new TextEncoder();
            const baseKey = await crypto.subtle.importKey(
                'raw', enc.encode(password), 'PBKDF2', false, ['deriveKey']);
            return crypto.subtle.deriveKey(
                {{ name: 'PBKDF2', salt: saltBytes, iterations: iters, hash: 'SHA-256' }},
                baseKey,
                {{ name: 'AES-GCM', length: 256 }},
                false,
                ['decrypt']
            );
        }}

        async function _decryptPayload(password) {{
            const p = _AUTH_ENCRYPTED_PAYLOAD;
            if (!p) throw new Error('No encrypted payload');
            const salt = _b64ToBytes(p.salt);
            const iv   = _b64ToBytes(p.iv);
            const ct   = _b64ToBytes(p.ct);
            const key  = await _deriveAesKey(password, salt, p.kdf_iter);
            const plain = await crypto.subtle.decrypt({{ name: 'AES-GCM', iv: iv }}, key, ct);
            return new TextDecoder().decode(plain);
        }}

        // Re-render any <time data-localize="datetime"> elements in `root`
        // (defaults to the document) using the viewer's local timezone.
        // The element's `datetime` attribute is the unambiguous ISO 8601
        // string from the server; the visible text gets replaced via
        // Intl.DateTimeFormat using the browser's default locale + zone.
        function _localizeReportTimes(root) {{
            const scope = root || document;
            const els = scope.querySelectorAll('time[data-localize="datetime"]');
            if (!els.length) return;
            for (const el of els) {{
                if (el.dataset.localized === '1') continue;
                const iso = el.getAttribute('datetime');
                if (!iso) continue;
                const d = new Date(iso);
                if (isNaN(d.getTime())) continue;
                try {{
                    const fmt = new Intl.DateTimeFormat(undefined, {{
                        year: 'numeric', month: '2-digit', day: '2-digit',
                        hour: '2-digit', minute: '2-digit', second: '2-digit',
                        hour12: false,
                        timeZoneName: 'short'
                    }});
                    el.textContent = fmt.format(d);
                    el.dataset.localized = '1';
                    if (!el.title) el.title = 'Report generated at ' + iso;
                }} catch (_e) {{}}
            }}
        }}

        function _applyDecryptedPayload(plaintextJson) {{
            const obj = JSON.parse(plaintextJson);
            const slot = document.getElementById('protectedContent');
            if (slot) slot.innerHTML = obj.html || '';
            _localizeReportTimes(slot || document);
        }}

        async function checkAuthPassword() {{
            const input  = document.getElementById('authPasswordInput');
            const err    = document.getElementById('authError');
            const submit = document.getElementById('authSubmit');
            if (!input || !_AUTH_ENCRYPTED_PAYLOAD) return;
            const value = input.value;
            if (!value) return;

            err.textContent = '';
            if (submit) {{ submit.textContent = 'Decrypting…'; submit.disabled = true; }}
            document.body.classList.add('decrypting');
            // Force one paint cycle so the spinner appears before the
            // CPU-bound JSON.parse / innerHTML hits the main thread.
            await new Promise(function(r) {{ requestAnimationFrame(r); }});
            try {{
                const json = await _decryptPayload(value);
                _applyDecryptedPayload(json);
                document.body.classList.remove('locked');
                input.value = '';
            }} catch (e) {{
                err.textContent = 'Wrong password.';
                input.select();
            }} finally {{
                document.body.classList.remove('decrypting');
                if (submit) {{ submit.textContent = 'Unlock'; submit.disabled = false; }}
            }}
        }}

        window.addEventListener('DOMContentLoaded', function() {{
            // Localize any <time> markers in the static chrome (the meta
            // line outside the encrypted payload).
            _localizeReportTimes(document);
            const input = document.getElementById('authPasswordInput');
            if (input) {{
                input.addEventListener('keydown', function(e) {{
                    if (e.key === 'Enter') {{ e.preventDefault(); checkAuthPassword(); }}
                }});
                if (document.body.classList.contains('locked')) {{
                    setTimeout(function() {{ input.focus(); }}, 50);
                }}
            }}
        }});
    </script>
</body>
</html>"""
    return html


def generate_comparison_html(launch1_id: str, launch2_id: str, 
                              logs1: dict, logs2: dict,
                              timings1: dict, timings2: dict) -> str:
    """Generate HTML comparison report for two launches."""
    la_tz = ZoneInfo("America/Los_Angeles")
    _now_la = datetime.now(la_tz)
    generated_at = _now_la.strftime("%Y-%m-%d %H:%M:%S")
    generated_at_iso = _now_la.isoformat(timespec="seconds")
    total_logs1 = logs1.get('page', {}).get('totalElements', 0)
    total_logs2 = logs2.get('page', {}).get('totalElements', 0)
    
    all_endpoints = sorted(set(timings1.keys()) | set(timings2.keys()))
    
    rows = []
    for endpoint in all_endpoints:
        d1 = timings1.get(endpoint, [])
        d2 = timings2.get(endpoint, [])
        
        avg1 = sum(d1) / len(d1) if d1 else None
        avg2 = sum(d2) / len(d2) if d2 else None
        count1 = len(d1)
        count2 = len(d2)
        
        method = endpoint.split()[0]
        method_class = method.lower()
        url = endpoint.split(' ', 1)[1]
        
        # Calculate difference
        if avg1 is not None and avg2 is not None:
            diff = avg2 - avg1
            diff_pct = (diff / avg1 * 100) if avg1 > 0 else 0
            if diff > 0:
                diff_class = "worse"
                diff_text = f"+{diff:.3f}s (+{diff_pct:.1f}%)"
            elif diff < 0:
                diff_class = "better"
                diff_text = f"{diff:.3f}s ({diff_pct:.1f}%)"
            else:
                diff_class = "same"
                diff_text = "0s (0%)"
        else:
            diff_class = "na"
            diff_text = "N/A"
        
        avg1_str = f"{avg1:.3f}s" if avg1 is not None else "-"
        avg2_str = f"{avg2:.3f}s" if avg2 is not None else "-"
        
        rows.append(f"""
            <tr>
                <td><span class="method {method_class}">{method}</span></td>
                <td class="endpoint">{url}</td>
                <td class="number">{count1 or '-'}</td>
                <td class="number">{avg1_str}</td>
                <td class="number">{count2 or '-'}</td>
                <td class="number">{avg2_str}</td>
                <td class="number diff {diff_class}">{diff_text}</td>
            </tr>
        """)
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Timing Comparison - {launch1_id} vs {launch2_id}</title>
    <style>{REPORT_CSS_BASE}</style>
</head>
<body>
    <div class="container">
        <div class="meta">
            <span>Launch 1: <strong>{launch1_id}</strong></span>
            <span>vs</span>
            <span>Launch 2: <strong>{launch2_id}</strong></span>
            <span>|</span>
            <span>Generated: <time datetime="{generated_at_iso}" data-localize="datetime">{generated_at}</time></span>
        </div>
        
        <div class="stats">
            <div class="stat-card">
                <h3>Launch 1 Logs</h3>
                <div class="value">{total_logs1}</div>
                <div class="label">ID: {launch1_id}</div>
            </div>
            <div class="stat-card">
                <h3>Launch 2 Logs</h3>
                <div class="value">{total_logs2}</div>
                <div class="label">ID: {launch2_id}</div>
            </div>
            <div class="stat-card">
                <h3>Unique Endpoints</h3>
                <div class="value">{len(all_endpoints)}</div>
            </div>
        </div>
        
        <div class="legend">
            <div class="legend-item"><span class="legend-dot better"></span> Faster in Launch 2</div>
            <div class="legend-item"><span class="legend-dot worse"></span> Slower in Launch 2</div>
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>Method</th>
                    <th>Endpoint</th>
                    <th class="launch1" style="text-align: right;">Count 1</th>
                    <th class="launch1" style="text-align: right;">Avg 1</th>
                    <th class="launch2" style="text-align: right;">Count 2</th>
                    <th class="launch2" style="text-align: right;">Avg 2</th>
                    <th style="text-align: right;">Difference</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows) if rows else '<tr><td colspan="7" style="text-align:center;padding:40px;">No timing data found</td></tr>'}
            </tbody>
        </table>
        
        <footer>
            Report Portal Timing Comparison Tool
        </footer>
    </div>
</body>
</html>"""
    return html


def print_comparison(launch1_id: str, launch2_id: str,
                     logs1: dict, logs2: dict,
                     timings1: dict, timings2: dict):
    """Print comparison report to console."""
    total1 = logs1.get('page', {}).get('totalElements', 0)
    total2 = logs2.get('page', {}).get('totalElements', 0)
    
    print(f'Launch 1 ({launch1_id}): {total1} logs')
    print(f'Launch 2 ({launch2_id}): {total2} logs\n')
    
    print('=' * 100)
    print('TIMING COMPARISON')
    print('=' * 100)
    print()
    
    all_endpoints = sorted(set(timings1.keys()) | set(timings2.keys()))
    
    for endpoint in all_endpoints:
        d1 = timings1.get(endpoint, [])
        d2 = timings2.get(endpoint, [])
        
        avg1 = sum(d1) / len(d1) if d1 else None
        avg2 = sum(d2) / len(d2) if d2 else None
        
        print(f'Endpoint: {endpoint}')
        print(f'  Launch 1: {f"{avg1:.3f}s" if avg1 else "N/A"} ({len(d1)} calls)')
        print(f'  Launch 2: {f"{avg2:.3f}s" if avg2 else "N/A"} ({len(d2)} calls)')
        
        if avg1 and avg2:
            diff = avg2 - avg1
            diff_pct = (diff / avg1 * 100) if avg1 > 0 else 0
            indicator = "SLOWER" if diff > 0 else "FASTER" if diff < 0 else "SAME"
            print(f'  Diff: {diff:+.3f}s ({diff_pct:+.1f}%) - {indicator}')
        print()
    
    print('=' * 100)
    print(f'Total endpoints compared: {len(all_endpoints)}')
    print('=' * 100)


def print_report(launch_id: str, logs_data: dict, endpoint_timings: dict, payload_key: str = None):
    """Print the timing analysis report to console."""
    total_logs = logs_data.get('page', {}).get('totalElements', 0)
    print(f'Processing {total_logs} logs...\n')
    
    print('=' * 80)
    print('ENDPOINT RESPONSE TIME ANALYSIS')
    print(f'Launch ID: {launch_id}')
    if payload_key:
        print(f'Grouped by: presence of "{payload_key}" in payload')
        print('Filter: 4xx errors excluded')
    print('=' * 80)
    print()
    
    if not endpoint_timings:
        print('No timing data found in logs.')
        return
    
    for endpoint in sorted(endpoint_timings.keys()):
        durations = endpoint_timings[endpoint]
        avg_duration = sum(durations) / len(durations)
        min_duration = min(durations)
        max_duration = max(durations)
        count = len(durations)
        
        print(f'Endpoint: {endpoint}')
        print(f'  Count: {count}')
        print(f'  Average: {avg_duration:.3f}s')
        print(f'  Min: {min_duration:.3f}s')
        print(f'  Max: {max_duration:.3f}s')
        print()
    
    print('=' * 80)
    print(f'Total unique endpoints: {len(endpoint_timings)}')
    print('=' * 80)


def print_multi_comparison(launch_labels: list, logs_list: list, timings_list: list):
    """Print multi-launch comparison report to console."""
    n = len(launch_labels)
    
    for label, logs in zip(launch_labels, logs_list):
        total = logs.get('page', {}).get('totalElements', 0)
        print(f'{label}: {total} logs')
    print()
    
    print('=' * 100)
    print(f'TIMING COMPARISON ({n} LAUNCHES) - Sorted oldest to newest')
    print('=' * 100)
    print()
    
    all_endpoints = set()
    for timings in timings_list:
        all_endpoints.update(timings.keys())
    all_endpoints = sorted(all_endpoints)
    
    for endpoint in all_endpoints:
        print(f'Endpoint: {endpoint}')
        for label, timings in zip(launch_labels, timings_list):
            durations = timings.get(endpoint, [])
            if durations:
                avg = sum(durations) / len(durations)
                min_d = min(durations)
                max_d = max(durations)
                print(f'  {label}: avg={avg:.3f}s  min={min_d:.3f}s  max={max_d:.3f}s  ({len(durations)} calls)')
            else:
                print(f'  {label}: N/A')
        print()
    
    print('=' * 100)
    print(f'Total endpoints compared: {len(all_endpoints)}')
    print('=' * 100)


def generate_multi_comparison_html(launch_labels: list, logs_list: list, timings_list: list, detailed_timings_list: list = None, launch_timestamps: list = None, launch_ids: list = None, report_password: Optional[str] = None) -> str:
    """Generate HTML comparison report for multiple launches.

    When ``len(launch_labels) >= 4`` the report renders as four tabs:
        1. Median (across all runs) vs single slowest run
        2. Fastest run vs slowest run
        3. Per-endpoint median / avg trend chart across all runs
        4. The original full N-launch side-by-side comparison

    For 2-3 launches the report keeps the original flat layout.
    """
    la_tz = ZoneInfo("America/Los_Angeles")
    _now_la = datetime.now(la_tz)
    generated_at = _now_la.strftime("%Y-%m-%d %H:%M:%S")
    generated_at_iso = _now_la.isoformat(timespec="seconds")
    n = len(launch_labels)
    
    # Fetch Datadog traces for slow requests if API keys are available
    dd_enabled = bool(DD_API_KEY and DD_APP_KEY)
    # Structure: endpoint -> launch_idx -> list of traces
    dd_traces_by_endpoint_launch = defaultdict(lambda: defaultdict(list))
    all_dd_traces = []  # Store all traces for filtering
    
    if dd_enabled and detailed_timings_list:
        print("Fetching Datadog traces for slow requests (> 3s)...")
        
        # Find the time range covering all slow requests and collect slow request info per launch
        all_timestamps = []
        slow_requests_by_launch = defaultdict(list)  # launch_idx -> list of {endpoint, timestamp, duration, payload}
        
        for launch_idx, detailed in enumerate(detailed_timings_list):
            for endpoint, data in detailed.items():
                for slow_req in data.get('slow_requests', []):
                    if slow_req.get('timestamp'):
                        try:
                            ts = datetime.fromisoformat(slow_req['timestamp'].replace('Z', '+00:00'))
                            all_timestamps.append(ts)
                            slow_requests_by_launch[launch_idx].append({
                                'endpoint': endpoint,
                                'timestamp': ts,
                                'duration': slow_req['duration'],
                                'payload': slow_req.get('payload', '')
                            })
                        except Exception:
                            pass
        
        # Calculate time ranges for each launch
        launch_time_ranges = {}  # launch_idx -> (min_time, max_time)
        for launch_idx, slow_reqs in slow_requests_by_launch.items():
            if slow_reqs:
                times = [sr['timestamp'] for sr in slow_reqs]
                launch_time_ranges[launch_idx] = (min(times), max(times))
        
        # Print launch time ranges for debugging (in LA time)
        la_tz = ZoneInfo("America/Los_Angeles")
        for idx, (start, end) in sorted(launch_time_ranges.items()):
            start_la = start.astimezone(la_tz)
            end_la = end.astimezone(la_tz)
            print(f"  Launch {idx+1} time range: {start_la.strftime('%m/%d %H:%M')} to {end_la.strftime('%m/%d %H:%M')}")
        
        # Query Datadog SEPARATELY for each launch's time range
        resources_found = set()
        services_found = set()
        all_endpoints = set().union(*[set(t.keys()) for t in timings_list])
        
        for launch_idx, (range_start, range_end) in sorted(launch_time_ranges.items()):
            # Add buffer around the launch time range
            buffer = timedelta(minutes=_DD_TRACE_BUFFER_MINUTES)
            from_ts = int((range_start - buffer).timestamp() * 1000)
            to_ts = int((range_end + buffer).timestamp() * 1000)
            
            # Get launch ID for caching
            launch_id_for_cache = launch_ids[launch_idx] if launch_ids else str(launch_idx + 1)
            
            print(f"  Querying Datadog for Launch {launch_idx+1}...")
            dd_traces = fetch_dd_traces_with_cache(launch_id_for_cache, from_ts, to_ts, limit=_DD_TRACE_FETCH_LIMIT, min_duration_s=3.0)
            print(f"    Found {len(dd_traces)} traces for Launch {launch_idx+1}")
            
            for trace in dd_traces:
                attrs = trace.get('attributes', {})
                resource = attrs.get('resource_name', '')
                service = attrs.get('service', '')
                trace_ts_str = attrs.get('start_timestamp', '')
                duration_ns = attrs.get('custom', {}).get('duration', 0)
                
                if resource:
                    resources_found.add(resource)
                if service:
                    services_found.add(service)
                
                if trace_ts_str and resource:
                    try:
                        trace_time = datetime.fromisoformat(trace_ts_str.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        continue
                    
                    trace_info = {
                        'trace_id': attrs.get('trace_id', ''),
                        'resource': resource,
                        'service': service,
                        'timestamp': trace_ts_str,
                        'trace_time': trace_time,
                        'duration_s': duration_ns / 1_000_000_000,
                        'status': attrs.get('custom', {}).get('http.status_code', 200)
                    }
                    all_dd_traces.append(trace_info)
                    
                    # Extract path from resource for matching
                    resource_path = resource.split(' ', 1)[-1] if ' ' in resource else resource
                    
                    # Match to endpoints
                    for endpoint in all_endpoints:
                        # Extract path from endpoint URL for matching
                        endpoint_url = endpoint.split(' ', 1)[-1] if ' ' in endpoint else endpoint
                        try:
                            from urllib.parse import urlparse
                            endpoint_path = urlparse(endpoint_url).path
                        except Exception:
                            endpoint_path = endpoint_url
                        
                        # Match if resource path matches endpoint path
                        if resource_path == endpoint_path or endpoint_path.endswith(resource_path) or resource_path in endpoint:
                            # Find matching payload from slow requests for this launch
                            payload = ''
                            for slow_req in slow_requests_by_launch.get(launch_idx, []):
                                if slow_req['endpoint'] == endpoint:
                                    payload = slow_req.get('payload', '')
                                    break
                            
                            trace_with_payload = trace_info.copy()
                            trace_with_payload['payload'] = payload
                            dd_traces_by_endpoint_launch[endpoint][launch_idx].append(trace_with_payload)
        
        # Print debug info
        print(f"  Services found: {', '.join(sorted(services_found)[:10])}")
        print(f"  Sample resources: {', '.join(list(resources_found)[:5])}")
        
        for ep, launch_traces in dd_traces_by_endpoint_launch.items():
            total = sum(len(t) for t in launch_traces.values())
            if total:
                # Show breakdown by launch
                breakdown = [f"L{idx+1}:{len(traces)}" for idx, traces in sorted(launch_traces.items())]
                print(f"  {ep[:55]}... -> {total} traces ({', '.join(breakdown)})")
    
    # Collect all endpoints
    all_endpoints = set()
    for timings in timings_list:
        all_endpoints.update(timings.keys())
    all_endpoints = sorted(all_endpoints)

    # Aggregate per-endpoint stats across all launches. Powers the
    # median-vs-slowest, fastest-vs-slowest and trend tabs. We use the
    # median-of-each-launch (not avg) to pick the fastest / slowest run
    # because median is robust to a single freak slow call inside a launch.
    endpoint_aggregate_stats = {}
    for endpoint in all_endpoints:
        all_durations = []
        per_launch = []
        for i, timings in enumerate(timings_list):
            durations = timings.get(endpoint, [])
            all_durations.extend(durations)
            if durations:
                per_launch.append({
                    'launch_idx': i,
                    'launch_label': launch_labels[i],
                    'avg':    sum(durations) / len(durations),
                    'median': _median(durations),
                    'p95':    _percentile(durations, 95),
                    'min':    min(durations),
                    'max':    max(durations),
                    'count':  len(durations),
                })
            else:
                per_launch.append({
                    'launch_idx': i,
                    'launch_label': launch_labels[i],
                    'avg': None, 'median': None, 'p95': None,
                    'min': None, 'max': None, 'count': 0,
                })

        populated = [pl for pl in per_launch if pl['median'] is not None]
        if not populated or not all_durations:
            continue

        fastest_pl = min(populated, key=lambda x: x['median'])
        slowest_pl = max(populated, key=lambda x: x['median'])

        endpoint_aggregate_stats[endpoint] = {
            'all_count':           len(all_durations),
            'median_across_all':   _median(all_durations),
            'avg_across_all':      sum(all_durations) / len(all_durations),
            'min_across_all':      min(all_durations),
            'max_across_all':      max(all_durations),
            'per_launch':          per_launch,
            'fastest':             fastest_pl,
            'slowest':             slowest_pl,
        }

    # Calculate stats for each launch
    launch_stats = []
    for i, (label, logs, timings) in enumerate(zip(launch_labels, logs_list, timings_list)):
        total_logs = logs.get('page', {}).get('totalElements', 0)
        total_requests = sum(len(v) for v in timings.values())
        launch_stats.append({
            'id': label,
            'label': label,
            'total_logs': total_logs,
            'total_requests': total_requests,
            'num': i + 1
        })
    
    # Build endpoint rows - card-based layout for each endpoint
    endpoint_cards = []
    endpoint_idx = 0
    for endpoint in all_endpoints:
        method = endpoint.split()[0]
        method_class = method.lower()
        url = endpoint.split(' ', 1)[1] if ' ' in endpoint else endpoint
        
        # Get timing data for each launch
        launch_data = []
        avgs = []
        for i, (label, timings) in enumerate(zip(launch_labels, timings_list)):
            durations = timings.get(endpoint, [])
            if durations:
                avg = sum(durations) / len(durations)
                avgs.append(avg)
                launch_data.append({
                    'num': i + 1,
                    'label': label,
                    'avg': avg,
                    'count': len(durations),
                    'min': min(durations),
                    'max': max(durations)
                })
            else:
                avgs.append(None)
                launch_data.append({'num': i + 1, 'label': label, 'avg': None, 'count': 0})
        
        # Calculate baseline (first launch with data) for comparison
        baseline_avg = next((a for a in avgs if a is not None), None)
        
        timing_blocks = []
        for i, ld in enumerate(launch_data):
            timing_id = f"timing-{endpoint_idx}-{i}"
            if ld['avg'] is not None:
                diff_class = ""
                diff_text = ""
                if baseline_avg and i > 0 and avgs[0] is not None:
                    diff = ld['avg'] - avgs[0]
                    diff_pct = (diff / avgs[0] * 100) if avgs[0] > 0 else 0
                    if diff < -0.001:
                        diff_class = "better"
                        diff_text = f"{diff_pct:.1f}%"
                    elif diff > 0.001:
                        diff_class = "worse"
                        diff_text = f"+{diff_pct:.1f}%"
                    else:
                        diff_class = "same"
                        diff_text = "0%"
                
                timing_blocks.append(f'''
                    <div class="launch-timing" id="{timing_id}">
                        <div class="launch-label">{ld['label']}</div>
                        <div class="timing-value" id="{timing_id}-avg">{ld['avg']:.3f}s</div>
                        <div class="timing-minmax">
                            <span class="min-val" id="{timing_id}-min">min: {ld['min']:.3f}s</span>
                            <span class="max-val" id="{timing_id}-max">max: {ld['max']:.3f}s</span>
                        </div>
                        <div class="timing-count" id="{timing_id}-count">{ld['count']} calls</div>
                        <div class="timing-diff {diff_class}" id="{timing_id}-diff">{diff_text}</div>
                    </div>
                ''')
            else:
                timing_blocks.append(f'''
                    <div class="launch-timing na" id="{timing_id}">
                        <div class="launch-label">{ld['label']}</div>
                        <div class="timing-value" id="{timing_id}-avg">N/A</div>
                    </div>
                ''')
        
        # Build payload breakdown section if detailed data is available
        payload_details_html = ""
        if detailed_timings_list:
            # Collect all unique payloads across all launches for this endpoint
            all_payloads = set()
            for detailed in detailed_timings_list:
                if endpoint in detailed:
                    all_payloads.update(detailed[endpoint]['by_payload'].keys())
            
            if all_payloads:
                payload_rows = []
                payload_row_idx = 0
                for payload_str in sorted(all_payloads):
                    # Truncate long payloads for display
                    display_payload = payload_str[:100] + '...' if len(payload_str) > 100 else payload_str
                    
                    # Prettify JSON for full view
                    try:
                        payload_obj = json.loads(payload_str)
                        full_payload = json.dumps(payload_obj, indent=2, sort_keys=True)
                    except Exception:
                        full_payload = payload_str
                    
                    # Escape for HTML
                    full_payload_escaped = full_payload.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
                    display_payload_escaped = display_payload.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    
                    payload_timing_blocks = []
                    payload_avgs = []
                    for i, (label, detailed) in enumerate(zip(launch_labels, detailed_timings_list)):
                        durations = []
                        if endpoint in detailed:
                            durations = detailed[endpoint]['by_payload'].get(payload_str, [])
                        
                        if durations:
                            avg = sum(durations) / len(durations)
                            payload_avgs.append(avg)
                            payload_timing_blocks.append(f'''
                                <div class="payload-timing">
                                    <div class="payload-label">{label}</div>
                                    <div class="payload-value">{avg:.3f}s</div>
                                    <div class="payload-minmax">
                                        <span class="min-val">min: {min(durations):.3f}s</span>
                                        <span class="max-val">max: {max(durations):.3f}s</span>
                                    </div>
                                    <div class="payload-count">{len(durations)} calls</div>
                                </div>
                            ''')
                        else:
                            payload_avgs.append(None)
                            payload_timing_blocks.append(f'''
                                <div class="payload-timing na">
                                    <div class="payload-label">{label}</div>
                                    <div class="payload-value">N/A</div>
                                </div>
                            ''')
                    
                    payload_id = f"payload-{endpoint_idx}-{payload_row_idx}"
                    payload_rows.append(f'''
                        <div class="payload-row">
                            <div class="payload-header" onclick="togglePayload('{payload_id}'); event.stopPropagation();">
                                <span class="payload-expand-icon" id="picon-{payload_id}">▶</span>
                                <span class="payload-preview" id="preview-{payload_id}">{display_payload_escaped}</span>
                            </div>
                            <div class="payload-full" id="full-{payload_id}"><pre>{full_payload_escaped}</pre></div>
                            <div class="payload-timings-grid">
                                {''.join(payload_timing_blocks)}
                            </div>
                        </div>
                    ''')
                    payload_row_idx += 1
                
                payload_details_html = f'''
                    <div class="payload-details" id="details-{endpoint_idx}" onclick="event.stopPropagation();">
                        <div class="payload-section-header">
                            <span>Breakdown by Unique Payload ({len(all_payloads)} variants)</span>
                            <label class="toggle-switch" onclick="event.stopPropagation();">
                                <input type="checkbox" onchange="toggleAllPayloads({endpoint_idx}, this.checked)">
                                <span class="toggle-slider"></span>
                                <span class="toggle-label">Expand all</span>
                            </label>
                        </div>
                        {''.join(payload_rows)}
                    </div>
                '''
        
        # Build Datadog traces section for this endpoint - grouped by launch
        dd_section_html = ""
        endpoint_key = f'{method} {url}'
        launch_traces = dd_traces_by_endpoint_launch.get(endpoint_key, {})
        total_traces = sum(len(t) for t in launch_traces.values())
        
        if total_traces > 0:
            la_tz = ZoneInfo("America/Los_Angeles")
            launch_sections = []
            
            for launch_idx, traces in sorted(launch_traces.items()):
                if not traces:
                    continue
                
                # Get launch label with index number for clarity
                base_label = launch_labels[launch_idx] if launch_idx < len(launch_labels) else f'Launch {launch_idx+1}'
                launch_label = f"Launch {launch_idx + 1}: {base_label}"
                trace_rows = []
                
                for trace in sorted(traces, key=lambda t: t['duration_s'], reverse=True):
                    try:
                        dt = datetime.fromisoformat(trace['timestamp'].replace('Z', '+00:00')).astimezone(la_tz)
                        time_str = dt.strftime("%m/%d %H:%M:%S")
                    except (ValueError, TypeError):
                        time_str = trace['timestamp'][:19]
                    
                    trace_url = get_datadog_trace_url(trace['trace_id'])
                    status_class = 'success' if 200 <= trace['status'] < 400 else 'error'
                    payload_display = trace.get('payload', '')[:150] if trace.get('payload') else '(no payload)'
                    payload_escaped = payload_display.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    resource_display = trace.get('resource', '')[:60]
                    
                    trace_rows.append(f'''
                        <div class="dd-trace-row" data-duration="{trace['duration_s']:.2f}">
                            <div class="dd-trace-main">
                                <span class="dd-trace-duration">{trace['duration_s']:.2f}s</span>
                                <span class="dd-trace-status {status_class}">{trace['status']}</span>
                                <span class="dd-trace-resource">{resource_display}</span>
                                <span class="dd-trace-time">{time_str}</span>
                                <a href="{trace_url}" target="_blank" class="dd-trace-link" onclick="event.stopPropagation();">View in Datadog →</a>
                            </div>
                            <div class="dd-trace-payload">Payload: {payload_escaped}</div>
                        </div>
                    ''')
                
                launch_group_id = f"dd-launch-{endpoint_idx}-{launch_idx}"
                launch_sections.append(f'''
                    <div class="dd-launch-group" id="{launch_group_id}">
                        <div class="dd-launch-label" onclick="toggleLaunchGroup('{launch_group_id}'); event.stopPropagation();">
                            <span class="dd-launch-icon" id="icon-{launch_group_id}">▶</span>
                            {launch_label} ({len(traces)} traces)
                        </div>
                        <div class="dd-launch-traces" id="traces-{launch_group_id}">
                            {''.join(trace_rows)}
                        </div>
                    </div>
                ''')
            
            url_short = url[-50:] if len(url) > 50 else url
            dd_section_html = f'''
                <div class="dd-section" id="dd-section-{endpoint_idx}" onclick="event.stopPropagation();">
                    <div class="dd-header" onclick="toggleDdSection({endpoint_idx}); event.stopPropagation();">
                        <span class="dd-expand-icon" id="dd-icon-{endpoint_idx}">▶</span>
                        <span class="dd-title">🐕 Datadog Traces for <strong>{method} ...{url_short}</strong></span>
                        <span class="dd-count" id="dd-count-{endpoint_idx}">({total_traces} total)</span>
                        <div class="dd-filter" onclick="event.stopPropagation();">
                            <label>Min:</label>
                            <input type="number" step="0.5" value="3" min="0" class="dd-duration-filter" 
                                   oninput="filterDdTraces({endpoint_idx}, this.value)" 
                                   onclick="event.stopPropagation();">
                            <span>s</span>
                        </div>
                    </div>
                    <div class="dd-traces-list" id="dd-traces-{endpoint_idx}">
                        {''.join(launch_sections)}
                    </div>
                </div>
            '''
        
        endpoint_cards.append(f'''
            <div class="endpoint-card" onclick="toggleDetails({endpoint_idx})">
                <div class="endpoint-header">
                    <span class="method {method_class}">{method}</span>
                    <span class="endpoint-url">{url}</span>
                    <span class="expand-icon" id="icon-{endpoint_idx}">▼</span>
                </div>
                <div class="timings-grid">
                    {''.join(timing_blocks)}
                </div>
                {dd_section_html}
                {payload_details_html}
            </div>
        ''')
        endpoint_idx += 1
    
    # Build data structure for JavaScript filtering
    js_data = {
        'labels': launch_labels,
        'endpoints': {}
    }
    
    if detailed_timings_list:
        for ep_idx, endpoint in enumerate(all_endpoints):
            js_data['endpoints'][endpoint] = {
                'idx': ep_idx,
                'by_payload': {}
            }
            for i, detailed in enumerate(detailed_timings_list):
                if endpoint in detailed:
                    for payload_str, durations in detailed[endpoint]['by_payload'].items():
                        if payload_str not in js_data['endpoints'][endpoint]['by_payload']:
                            js_data['endpoints'][endpoint]['by_payload'][payload_str] = {}
                        js_data['endpoints'][endpoint]['by_payload'][payload_str][i] = durations
    
    js_data_json = json.dumps(js_data)
    
    # Build launch info cards.
    # Launches are already sorted oldest -> newest (left -> right). Annotate the
    # leftmost as "oldest (baseline)" and the rightmost as "newest" so the column
    # roles are unambiguous when there are 2+ launches.
    launch_info_cards = []
    _n_launches = len(launch_stats)
    for _idx, stat in enumerate(launch_stats):
        if _n_launches >= 2 and _idx == 0:
            _position = '<div class="launch-position oldest">oldest (baseline)</div>'
        elif _n_launches >= 2 and _idx == _n_launches - 1:
            _position = '<div class="launch-position newest">newest</div>'
        else:
            _position = ''
        launch_info_cards.append(f'''
            <div class="launch-info-card">
                {_position}
                <div class="launch-num">{stat['label']}</div>
                <div class="launch-stats">{stat['total_logs']} logs / {stat['total_requests']} requests</div>
            </div>
        ''')

    # -------- Tabbed views (only when N >= 4 launches) --------
    # Tab 1: median across all runs vs the single slowest run, per endpoint.
    # Tab 2: fastest run vs slowest run, per endpoint.
    # Tab 3: line chart of per-iter median / avg per endpoint.
    # Tab 4: existing full N-launch comparison.
    show_tabs = n >= 4

    def _fmt_secs(v):
        return f"{v:.3f}s" if v is not None else "N/A"

    def _diff_pct_html(slower, faster):
        if faster is None or slower is None or faster <= 0:
            return ''
        pct = (slower - faster) / faster * 100
        if pct >= 1:
            return f'<div class="diff-pct worse">+{pct:.1f}% slower</div>'
        if pct <= -1:
            return f'<div class="diff-pct better">{pct:.1f}% faster</div>'
        return f'<div class="diff-pct same">{pct:.1f}%</div>'

    tab1_cards = []  # Median vs Slowest
    tab2_cards = []  # Fastest vs Slowest
    trend_chart_blocks = []
    trend_chart_data = {}  # canvas_id -> {labels, median, avg, overall_median}

    for ep_idx_t, endpoint in enumerate(all_endpoints):
        if endpoint not in endpoint_aggregate_stats:
            continue
        agg = endpoint_aggregate_stats[endpoint]
        method_t = endpoint.split()[0]
        method_class_t = method_t.lower()
        url_t = endpoint.split(' ', 1)[1] if ' ' in endpoint else endpoint
        median_all = agg['median_across_all']
        slowest = agg['slowest']
        fastest = agg['fastest']

        # Slowest-run payload breakdown: for each payload variant that was
        # actually used in the slowest launch, show avg/min/max/calls and
        # let the user expand to see the full JSON. This points the reader
        # at the specific request bodies that caused the worst run.
        tab1_slow_payloads_html = ""
        slow_ep_id = f"slow-{ep_idx_t}"
        if detailed_timings_list:
            slow_idx = slowest['launch_idx']
            if 0 <= slow_idx < len(detailed_timings_list):
                slow_detailed = detailed_timings_list[slow_idx]
                slow_by_payload = (slow_detailed.get(endpoint, {}) or {}).get('by_payload', {})
                if slow_by_payload:
                    payload_rows = []
                    # Sort by avg duration desc so the slowest payloads bubble
                    # to the top -- those are the ones to investigate first.
                    sorted_payloads = sorted(
                        slow_by_payload.items(),
                        key=lambda kv: (sum(kv[1]) / len(kv[1])) if kv[1] else 0.0,
                        reverse=True,
                    )
                    for row_idx, (payload_str, durations) in enumerate(sorted_payloads):
                        if not durations:
                            continue
                        avg_d = sum(durations) / len(durations)
                        min_d = min(durations)
                        max_d = max(durations)
                        cnt   = len(durations)
                        display_payload = (payload_str[:140] + '...') if len(payload_str) > 140 else payload_str
                        try:
                            payload_obj = json.loads(payload_str)
                            full_payload = json.dumps(payload_obj, indent=2, sort_keys=True)
                        except Exception:
                            full_payload = payload_str
                        # SECURITY-REVIEW: payloads are echoed straight from
                        # RP into the rendered HTML -- escape both the preview
                        # text and the full JSON block so they can't break out
                        # of the surrounding tags.
                        full_payload_esc    = (full_payload
                                               .replace('&', '&amp;')
                                               .replace('<', '&lt;')
                                               .replace('>', '&gt;')
                                               .replace('"', '&quot;'))
                        display_payload_esc = (display_payload
                                               .replace('&', '&amp;')
                                               .replace('<', '&lt;')
                                               .replace('>', '&gt;'))
                        slow_payload_id = f"slow-payload-{ep_idx_t}-{row_idx}"
                        payload_rows.append(f'''
                            <div class="payload-row">
                                <div class="payload-header" onclick="togglePayload('{slow_payload_id}'); event.stopPropagation();">
                                    <span class="payload-expand-icon" id="picon-{slow_payload_id}">▶</span>
                                    <span class="payload-preview" id="preview-{slow_payload_id}">{display_payload_esc}</span>
                                    <span class="payload-inline-metrics">{avg_d:.3f}s avg &middot; {min_d:.3f}s min &middot; {max_d:.3f}s max &middot; {cnt} call{'s' if cnt != 1 else ''}</span>
                                </div>
                                <div class="payload-full" id="full-{slow_payload_id}"><pre>{full_payload_esc}</pre></div>
                            </div>
                        ''')
                    if payload_rows:
                        # Collapsed by default. The endpoint card is wired
                        # to toggleDetails(slow_ep_id) below so clicking
                        # the endpoint expands this list. Per-payload JSON
                        # inside stays individually collapsible via the
                        # row click + "Expand all" toggle.
                        tab1_slow_payloads_html = f'''
                            <div class="payload-details" id="details-{slow_ep_id}" onclick="event.stopPropagation();">
                                <div class="payload-section-header">
                                    <span>Slowest-run payloads ({len(payload_rows)} variant{'s' if len(payload_rows) != 1 else ''} &middot; iter {slow_idx + 1})</span>
                                    <label class="toggle-switch" onclick="event.stopPropagation();">
                                        <input type="checkbox" onchange="toggleAllPayloads('{slow_ep_id}', this.checked)">
                                        <span class="toggle-slider"></span>
                                        <span class="toggle-label">Expand all</span>
                                    </label>
                                </div>
                                {''.join(payload_rows)}
                            </div>
                        '''

        # Endpoint card is only clickable-to-expand when it actually has
        # slowest-run payload variants to show. The caret in the header
        # rotates via toggleDetails() to indicate state.
        if tab1_slow_payloads_html:
            card_click_attrs   = f' onclick="toggleDetails(\'{slow_ep_id}\')" style="cursor: pointer;"'
            header_caret_html  = f'<span class="payload-expand-icon" id="icon-{slow_ep_id}">▶</span>'
        else:
            card_click_attrs   = ''
            header_caret_html  = ''

        tab1_cards.append(f'''
            <div class="endpoint-card simple-card"{card_click_attrs}>
                <div class="endpoint-header">
                    {header_caret_html}
                    <span class="method {method_class_t}">{method_t}</span>
                    <span class="endpoint-url">{url_t}</span>
                </div>
                <div class="timings-grid two-col">
                    <div class="launch-timing">
                        <div class="launch-position oldest">MEDIAN (all {n} runs)</div>
                        <div class="timing-value">{_fmt_secs(median_all)}</div>
                        <div class="timing-minmax">
                            <span class="min-val">across {agg['all_count']} calls</span>
                            <span class="max-val">avg: {_fmt_secs(agg['avg_across_all'])}</span>
                        </div>
                        <div class="timing-count">global min: {_fmt_secs(agg['min_across_all'])} &middot; max: {_fmt_secs(agg['max_across_all'])}</div>
                    </div>
                    <div class="launch-timing">
                        <div class="launch-position worst">SLOWEST RUN</div>
                        <div class="launch-label">iter {slowest['launch_idx']+1} &mdash; {slowest['launch_label']}</div>
                        <div class="timing-value">{_fmt_secs(slowest['median'])}</div>
                        <div class="timing-minmax">
                            <span class="min-val">min: {_fmt_secs(slowest['min'])}</span>
                            <span class="max-val">max: {_fmt_secs(slowest['max'])}</span>
                        </div>
                        <div class="timing-count">{slowest['count']} calls &middot; avg: {_fmt_secs(slowest['avg'])}</div>
                        {_diff_pct_html(slowest['median'], median_all)}
                    </div>
                </div>
                {tab1_slow_payloads_html}
            </div>
        ''')

        tab2_cards.append(f'''
            <div class="endpoint-card simple-card">
                <div class="endpoint-header">
                    <span class="method {method_class_t}">{method_t}</span>
                    <span class="endpoint-url">{url_t}</span>
                </div>
                <div class="timings-grid two-col">
                    <div class="launch-timing">
                        <div class="launch-position best">FASTEST RUN</div>
                        <div class="launch-label">iter {fastest['launch_idx']+1} &mdash; {fastest['launch_label']}</div>
                        <div class="timing-value">{_fmt_secs(fastest['median'])}</div>
                        <div class="timing-minmax">
                            <span class="min-val">min: {_fmt_secs(fastest['min'])}</span>
                            <span class="max-val">max: {_fmt_secs(fastest['max'])}</span>
                        </div>
                        <div class="timing-count">{fastest['count']} calls &middot; avg: {_fmt_secs(fastest['avg'])}</div>
                    </div>
                    <div class="launch-timing">
                        <div class="launch-position worst">SLOWEST RUN</div>
                        <div class="launch-label">iter {slowest['launch_idx']+1} &mdash; {slowest['launch_label']}</div>
                        <div class="timing-value">{_fmt_secs(slowest['median'])}</div>
                        <div class="timing-minmax">
                            <span class="min-val">min: {_fmt_secs(slowest['min'])}</span>
                            <span class="max-val">max: {_fmt_secs(slowest['max'])}</span>
                        </div>
                        <div class="timing-count">{slowest['count']} calls &middot; avg: {_fmt_secs(slowest['avg'])}</div>
                        {_diff_pct_html(slowest['median'], fastest['median'])}
                    </div>
                </div>
            </div>
        ''')

        canvas_id = f"trend-chart-{ep_idx_t}"
        trend_chart_data[canvas_id] = {
            'labels': [f"iter {pl['launch_idx']+1}" for pl in agg['per_launch']],
            'max':    [pl['max']    for pl in agg['per_launch']],
            'p95':    [pl['p95']    for pl in agg['per_launch']],
            'overall_max':    agg['max_across_all'],
        }
        trend_chart_blocks.append(f'''
            <div class="endpoint-card">
                <div class="endpoint-header">
                    <span class="method {method_class_t}">{method_t}</span>
                    <span class="endpoint-url">{url_t}</span>
                </div>
                <div class="trend-chart-wrapper">
                    <canvas id="{canvas_id}" height="120"></canvas>
                </div>
            </div>
        ''')

    trend_chart_data_json = json.dumps(trend_chart_data)

    _endpoint_cards_joined = (
        ''.join(endpoint_cards)
        if endpoint_cards
        else '<div class="endpoint-card"><div class="endpoint-header">No timing data found</div></div>'
    )

    if show_tabs:
        tabs_nav_html = f'''
        <div class="tabs-nav">
            <button class="tab-btn active" data-tab="0" onclick="switchTab(0)">Median vs Slowest</button>
            <button class="tab-btn"        data-tab="1" onclick="switchTab(1)">Fastest vs Slowest</button>
            <button class="tab-btn"        data-tab="2" onclick="switchTab(2)">Per-endpoint Trend</button>
            <button class="tab-btn"        data-tab="3" onclick="switchTab(3)">All {n} Launches</button>
        </div>
        '''
        tab_panes_html = f'''
        <div class="tab-pane active" data-tab-pane="0">
            <div class="tab-intro">Median across all {n} runs vs the single slowest run (slowest = launch with highest per-endpoint median).</div>
            {''.join(tab1_cards)}
        </div>
        <div class="tab-pane" data-tab-pane="1">
            <div class="tab-intro">Per-endpoint fastest vs slowest single run, ranked by per-endpoint median.</div>
            {''.join(tab2_cards)}
        </div>
        <div class="tab-pane" data-tab-pane="2">
            <div class="tab-intro">Per-endpoint tail latency across all {n} iterations: max (slowest single call) and p95 per run. Overall max is shown as a dotted reference.</div>
            {''.join(trend_chart_blocks)}
        </div>
        <div class="tab-pane" data-tab-pane="3">
            <div class="tab-intro">Full comparison across all {n} launches (original layout).</div>
            {_endpoint_cards_joined}
        </div>
        '''
    else:
        tabs_nav_html  = ""
        tab_panes_html = _endpoint_cards_joined

    # ----- Bundle all sensitive content so it can be encrypted as a unit -----
    # Anything that reveals timings, payloads, URLs, or even iteration counts
    # goes inside `protected_html`; the static page chrome (footer, auth
    # overlay) stays outside so the locked report renders cleanly.
    #
    # Layout: the summary line + tab nav (and tab panes) come first so the
    # viewer lands directly on the comparison UI. Secondary metadata (search
    # input, launch chip row, legend, "Generated" timestamp) sits below the
    # tabs so it doesn't bury the primary content.
    protected_html = f'''
            <div class="summary">
                Comparing {len(all_endpoints)} unique endpoints across {n} launches
            </div>

            {tabs_nav_html}

            <div class="meta">{n} Launches | Generated: <time datetime="{generated_at_iso}" data-localize="datetime">{generated_at}</time></div>

            <div class="search-container">
                <input type="text" class="search-input" id="searchInput" placeholder="Filter by payload content (press Enter to apply)">
                <button class="clear-filter" id="clearFilter" onclick="clearFilter()">Clear Filter</button>
                <span class="search-hint">Press Enter to filter</span>
            </div>

            <div class="filter-status" id="filterStatus"></div>

            <div class="launches-row">
                {''.join(launch_info_cards)}
            </div>

            <div class="legend">
                <div class="legend-item"><span class="legend-dot better"></span> Faster than the oldest run</div>
                <div class="legend-item"><span class="legend-dot worse"></span> Slower than the oldest run</div>
                <div class="legend-note">(Baseline = leftmost / oldest run. Newest = rightmost.)</div>
            </div>

            {tab_panes_html}
    '''

    # ----- AES-GCM gate -----
    # The caller can supply an explicit ``report_password`` (e.g. a fresh
    # per-report password generated by the SPA backend); if not, we fall back
    # to the module-level REPORT_PASSWORD (set via env var, defaults to the
    # legacy global). An empty string at either layer disables the gate
    # entirely.
    #
    # When a password is in effect, we encrypt the protected_html + js_data +
    # trend_chart_data bundle into a single base64 blob. The rendered HTML
    # carries ONLY ciphertext for these. Without the password, no DOM removal
    # / CSS filter / JS poke can recover the plaintext.
    effective_password = report_password if report_password is not None else REPORT_PASSWORD
    if effective_password:
        bundle = {
            "html":           protected_html,
            "reportData":     js_data,
            "trendChartData": trend_chart_data,
        }
        enc = _encrypt_report_payload(
            json.dumps(bundle, separators=(",", ":")).encode("utf-8"),
            effective_password,
        )
        encrypted_payload_json = json.dumps(enc)
        # Wipe the cleartext variables that flow into the f-string so they
        # never appear in the rendered HTML.
        protected_html_rendered = ''
        js_data_json = 'null'
        trend_chart_data_json = 'null'
        body_locked_class = ' class="locked"'
        auth_overlay_html = '''
    <div class="auth-overlay" id="authOverlay">
        <div class="auth-box">
            <div class="auth-icon">[ Locked ]</div>
            <h2>Protected report</h2>
            <p>Enter the password to decrypt the data.</p>
            <input type="password" id="authPasswordInput" class="auth-input"
                   placeholder="password" autocomplete="off" autofocus>
            <button type="button" class="auth-submit" id="authSubmit" onclick="checkAuthPassword()">Unlock</button>
            <div class="auth-error" id="authError"></div>
            <div class="auth-spinner"></div>
            <div class="auth-decrypting-msg">Decrypting&hellip;</div>
            <div class="auth-meta">AES-256-GCM &middot; PBKDF2-HMAC-SHA256 (300k iter)</div>
        </div>
    </div>
'''
    else:
        encrypted_payload_json = 'null'
        protected_html_rendered = protected_html
        body_locked_class = ''
        auth_overlay_html = ''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Timing Comparison - {n} Launches</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        {REPORT_CSS_BASE}
        /* Multi-launch needs N columns in the timings grid -- emit it
           dynamically here because CSS can't put a var() inside repeat()'s
           first arg. */
        .timings-grid {{ grid-template-columns: repeat({n}, 1fr); }}
    </style>
</head>
<body{body_locked_class}>
    {auth_overlay_html}
    <div class="loader-overlay" id="loaderOverlay">
        <div class="loader">
            <div class="loader-spinner"></div>
            <div class="loader-text">Filtering data...</div>
        </div>
    </div>
    
    <div class="container">
        <div id="protectedContent">{protected_html_rendered}</div>

        <footer>
            Report Portal Timing Comparison Tool
        </footer>
    </div>
    
    <script>
        let reportData = {js_data_json};
        let trendChartData = {trend_chart_data_json};
        let currentFilter = '';

        // ---------- AES-GCM password gate ----------
        // When the report is encrypted, _AUTH_ENCRYPTED_PAYLOAD holds the
        // base64'd salt / iv / ciphertext + kdf params. The plaintext payload
        // (HTML chrome + reportData + trendChartData) only ever exists after
        // a successful decrypt() with the user-supplied password.
        const _AUTH_ENCRYPTED_PAYLOAD = {encrypted_payload_json};
        // Tie the session cache key to the ciphertext prefix so a regenerated
        // report (new salt/iv/ct) invalidates the cache automatically.
        const _AUTH_PAYLOAD_KEY = _AUTH_ENCRYPTED_PAYLOAD
            ? 'rp_report_payload_v1_' + (_AUTH_ENCRYPTED_PAYLOAD.ct || '').slice(0, 24)
            : null;

        function _b64ToBytes(b64) {{
            const bin = atob(b64);
            const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            return arr;
        }}

        async function _deriveAesKey(password, saltBytes, iters) {{
            const enc = new TextEncoder();
            const baseKey = await crypto.subtle.importKey(
                'raw', enc.encode(password), 'PBKDF2', false, ['deriveKey']);
            return crypto.subtle.deriveKey(
                {{ name: 'PBKDF2', salt: saltBytes, iterations: iters, hash: 'SHA-256' }},
                baseKey,
                {{ name: 'AES-GCM', length: 256 }},
                false,
                ['decrypt']
            );
        }}

        async function _decryptPayload(password) {{
            const p = _AUTH_ENCRYPTED_PAYLOAD;
            if (!p) throw new Error('No encrypted payload');
            const salt = _b64ToBytes(p.salt);
            const iv   = _b64ToBytes(p.iv);
            const ct   = _b64ToBytes(p.ct);
            const key  = await _deriveAesKey(password, salt, p.kdf_iter);
            const plain = await crypto.subtle.decrypt({{ name: 'AES-GCM', iv: iv }}, key, ct);
            return new TextDecoder().decode(plain);
        }}

        function _bindSearchInput() {{
            const searchInput = document.getElementById('searchInput');
            if (searchInput) {{
                searchInput.addEventListener('keypress', function(e) {{
                    if (e.key === 'Enter') applyFilter(this.value);
                }});
            }}
        }}

        // Re-render any <time data-localize="datetime"> elements inside ``root``
        // (defaults to the whole document) so the viewer always sees the
        // generated-at timestamp in their OWN local timezone. The element's
        // `datetime` attribute holds the unambiguous ISO 8601 string (with
        // offset) that the report was generated at; we feed that through
        // Intl.DateTimeFormat using the browser's default locale and zone.
        function _localizeReportTimes(root) {{
            const scope = root || document;
            const els = scope.querySelectorAll('time[data-localize="datetime"]');
            if (!els.length) return;
            for (const el of els) {{
                if (el.dataset.localized === '1') continue;
                const iso = el.getAttribute('datetime');
                if (!iso) continue;
                const d = new Date(iso);
                if (isNaN(d.getTime())) continue;
                try {{
                    const fmt = new Intl.DateTimeFormat(undefined, {{
                        year: 'numeric', month: '2-digit', day: '2-digit',
                        hour: '2-digit', minute: '2-digit', second: '2-digit',
                        hour12: false,
                        timeZoneName: 'short'
                    }});
                    el.textContent = fmt.format(d);
                    el.dataset.localized = '1';
                    if (!el.title) el.title = 'Report generated at ' + iso;
                }} catch (_e) {{}}
            }}
        }}

        function _applyDecryptedPayload(plaintextJson) {{
            const obj = JSON.parse(plaintextJson);
            reportData = obj.reportData || {{}};
            trendChartData = obj.trendChartData || {{}};
            const slot = document.getElementById('protectedContent');
            if (slot) slot.innerHTML = obj.html || '';
            _bindSearchInput();
            // The decrypted HTML may contain <time data-localize="datetime">
            // elements; re-localize them now that they're in the DOM.
            _localizeReportTimes(slot || document);
            // Chart.js canvases were just (re)created in the freshly injected
            // HTML; clear the init flag so they get drawn the next time the
            // user opens the trend tab.
            _trendChartsInited = false;
        }}

        async function checkAuthPassword() {{
            const input  = document.getElementById('authPasswordInput');
            const err    = document.getElementById('authError');
            const submit = document.getElementById('authSubmit');
            if (!input || !_AUTH_ENCRYPTED_PAYLOAD) return;
            const value = input.value;
            if (!value) return;

            err.textContent = '';
            if (submit) {{ submit.textContent = 'Decrypting…'; submit.disabled = true; }}
            document.body.classList.add('decrypting');
            // Force one paint cycle so the spinner appears before the
            // CPU-bound JSON.parse / innerHTML hits the main thread.
            await new Promise(function(r) {{ requestAnimationFrame(r); }});
            try {{
                const json = await _decryptPayload(value);
                _applyDecryptedPayload(json);
                document.body.classList.remove('locked');
                input.value = '';
            }} catch (e) {{
                // AES-GCM auth-tag failure looks like an exception here. We
                // intentionally do not differentiate "wrong password" from
                // "corrupt payload" to keep the error message minimal.
                err.textContent = 'Wrong password.';
                input.select();
            }} finally {{
                document.body.classList.remove('decrypting');
                if (submit) {{ submit.textContent = 'Unlock'; submit.disabled = false; }}
            }}
        }}

        window.addEventListener('DOMContentLoaded', function() {{
            // If no encryption is configured, the protected slot already has
            // the rendered HTML; we still need to wire up the search input.
            if (!_AUTH_ENCRYPTED_PAYLOAD) {{
                _bindSearchInput();
            }}
            // Always localize any <time> markers already in the static
            // chrome (the H1/meta strip outside the encrypted payload).
            _localizeReportTimes(document);

            const input = document.getElementById('authPasswordInput');
            if (input) {{
                input.addEventListener('keydown', function(e) {{
                    if (e.key === 'Enter') {{
                        e.preventDefault();
                        checkAuthPassword();
                    }}
                }});
                if (document.body.classList.contains('locked')) {{
                    setTimeout(function() {{ input.focus(); }}, 50);
                }}
            }}
        }});

        function switchTab(idx) {{
            document.querySelectorAll('.tab-btn').forEach(function(b) {{
                b.classList.toggle('active', b.getAttribute('data-tab') == idx);
            }});
            document.querySelectorAll('.tab-pane').forEach(function(p) {{
                p.classList.toggle('active', p.getAttribute('data-tab-pane') == idx);
            }});
            // Charts on tab 2 must be initialized lazily so canvas elements are
            // visible (Chart.js mis-measures hidden canvases).
            if (idx == 2) initTrendCharts();
        }}

        let _trendChartsInited = false;
        function initTrendCharts() {{
            if (_trendChartsInited) return;
            if (typeof Chart === 'undefined') return; // Chart.js failed to load (offline?)
            _trendChartsInited = true;
            Object.keys(trendChartData).forEach(function(canvasId) {{
                const d = trendChartData[canvasId];
                const canvas = document.getElementById(canvasId);
                if (!canvas) return;
                const overallMax = d.overall_max;
                const overall_max_line = d.labels.map(function() {{ return overallMax; }});
                new Chart(canvas, {{
                    type: 'line',
                    data: {{
                        labels: d.labels,
                        datasets: [
                            {{
                                label: 'Max per run (slowest single call)',
                                data: d.max,
                                borderColor: '#f44336',
                                backgroundColor: 'rgba(244,67,54,0.12)',
                                tension: 0.25,
                                pointRadius: 4,
                                fill: false,
                                spanGaps: true,
                            }},
                            {{
                                label: 'p95 per run',
                                data: d.p95,
                                borderColor: '#ffb74d',
                                backgroundColor: 'rgba(255,183,77,0.10)',
                                borderDash: [4, 4],
                                tension: 0.25,
                                pointRadius: 3,
                                fill: false,
                                spanGaps: true,
                            }},
                            {{
                                label: 'Overall max (all runs)',
                                data: overall_max_line,
                                borderColor: '#888',
                                borderDash: [2, 6],
                                pointRadius: 0,
                                fill: false,
                            }},
                        ]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: {{ mode: 'index', intersect: false }},
                        plugins: {{
                            legend: {{ labels: {{ color: '#ccc' }} }},
                            tooltip: {{ callbacks: {{
                                label: function(ctx) {{
                                    const v = ctx.parsed.y;
                                    return ctx.dataset.label + ': ' + (v == null ? 'N/A' : v.toFixed(3) + 's');
                                }}
                            }} }},
                        }},
                        scales: {{
                            y: {{
                                beginAtZero: true,
                                ticks: {{ color: '#888', callback: function(v) {{ return v + 's'; }} }},
                                grid:  {{ color: 'rgba(255,255,255,0.05)' }},
                                title: {{ display: true, text: 'duration (s)', color: '#888' }},
                            }},
                            x: {{
                                ticks: {{ color: '#888' }},
                                grid:  {{ color: 'rgba(255,255,255,0.05)' }},
                            }},
                        }},
                    }}
                }});
            }});
        }}

        function toggleDetails(idx) {{
            const details = document.getElementById('details-' + idx);
            const icon = document.getElementById('icon-' + idx);

            if (details) {{
                details.classList.toggle('expanded');
                if (icon) icon.classList.toggle('expanded');
            }}
        }}
        
        function toggleDdSection(idx) {{
            const traces = document.getElementById('dd-traces-' + idx);
            const icon = document.getElementById('dd-icon-' + idx);
            
            if (traces) {{
                traces.classList.toggle('expanded');
                icon.classList.toggle('expanded');
            }}
        }}
        
        function toggleLaunchGroup(groupId) {{
            const traces = document.getElementById('traces-' + groupId);
            const icon = document.getElementById('icon-' + groupId);
            
            if (traces) {{
                traces.classList.toggle('expanded');
                icon.classList.toggle('expanded');
            }}
        }}
        
        function filterDdTraces(idx, minDuration) {{
            const container = document.getElementById('dd-traces-' + idx);
            if (!container) return;
            
            const minDur = parseFloat(minDuration) || 0;
            const rows = container.querySelectorAll('.dd-trace-row');
            let visibleCount = 0;
            
            rows.forEach(row => {{
                const duration = parseFloat(row.getAttribute('data-duration')) || 0;
                if (duration >= minDur) {{
                    row.style.display = '';
                    row.classList.remove('hidden');
                    visibleCount++;
                }} else {{
                    row.style.display = 'none';
                    row.classList.add('hidden');
                }}
            }});
            
            // Update counts in launch group labels
            const launchGroups = container.querySelectorAll('.dd-launch-group');
            launchGroups.forEach(group => {{
                const groupTraces = group.querySelector('.dd-launch-traces');
                if (!groupTraces) return;
                const groupRows = groupTraces.querySelectorAll('.dd-trace-row');
                let groupVisible = 0;
                groupRows.forEach(r => {{
                    if (!r.classList.contains('hidden')) groupVisible++;
                }});
                const label = group.querySelector('.dd-launch-label');
                if (label) {{
                    // Get the icon element and preserve it
                    const icon = label.querySelector('.dd-launch-icon');
                    const iconHtml = icon ? icon.outerHTML : '';
                    const textContent = label.textContent.split('(')[0].trim();
                    // Remove icon text from base name
                    const baseName = textContent.replace('▶', '').replace('▼', '').trim();
                    label.innerHTML = iconHtml + ' ' + baseName + ' (' + groupVisible + ' traces)';
                }}
            }});
            
            // Update count in header
            const countEl = document.getElementById('dd-count-' + idx);
            if (countEl) {{
                countEl.textContent = '(' + visibleCount + ' shown, >= ' + minDur + 's)';
            }}
        }}
        
        function togglePayload(payloadId) {{
            const full = document.getElementById('full-' + payloadId);
            const preview = document.getElementById('preview-' + payloadId);
            const icon = document.getElementById('picon-' + payloadId);
            
            if (full) {{
                full.classList.toggle('expanded');
                icon.classList.toggle('expanded');
            }}
        }}
        
        function toggleAllPayloads(endpointIdx, expand) {{
            const details = document.getElementById('details-' + endpointIdx);
            if (!details) return;
            
            const payloadFulls = details.querySelectorAll('.payload-full');
            const payloadIcons = details.querySelectorAll('.payload-expand-icon');
            
            payloadFulls.forEach(el => {{
                if (expand) {{
                    el.classList.add('expanded');
                }} else {{
                    el.classList.remove('expanded');
                }}
            }});
            
            payloadIcons.forEach(el => {{
                if (expand) {{
                    el.classList.add('expanded');
                }} else {{
                    el.classList.remove('expanded');
                }}
            }});
        }}
        
        function showLoader() {{
            document.getElementById('loaderOverlay').classList.add('visible');
        }}
        
        function hideLoader() {{
            document.getElementById('loaderOverlay').classList.remove('visible');
        }}
        
        function applyFilter(filterText) {{
            showLoader();
            // Normalize filter: remove spaces around colons and commas for JSON matching
            currentFilter = filterText.toLowerCase().trim();
            const normalizedFilter = currentFilter.replace(/\\s*:\\s*/g, ':').replace(/\\s*,\\s*/g, ',');
            
            setTimeout(() => {{
                const numLaunches = reportData.labels.length;
                let totalMatched = 0;
                
                for (const [endpoint, data] of Object.entries(reportData.endpoints)) {{
                    const epIdx = data.idx;
                    const launchDurations = {{}};
                    
                    for (let i = 0; i < numLaunches; i++) {{
                        launchDurations[i] = [];
                    }}
                    
                    for (const [payload, launchData] of Object.entries(data.by_payload)) {{
                        // Try both original and normalized filter
                        const payloadLower = payload.toLowerCase();
                        const matches = currentFilter === '' || 
                            payloadLower.includes(currentFilter) || 
                            payloadLower.includes(normalizedFilter);
                        if (matches) {{
                            for (const [launchIdx, durations] of Object.entries(launchData)) {{
                                launchDurations[launchIdx] = launchDurations[launchIdx].concat(durations);
                                totalMatched += durations.length;
                            }}
                        }}
                    }}
                    
                    // Calculate baseline for diff
                    let baselineAvg = null;
                    for (let i = 0; i < numLaunches; i++) {{
                        if (launchDurations[i].length > 0) {{
                            baselineAvg = launchDurations[i].reduce((a, b) => a + b, 0) / launchDurations[i].length;
                            break;
                        }}
                    }}
                    
                    // Update each launch's timing display
                    for (let i = 0; i < numLaunches; i++) {{
                        const timingId = `timing-${{epIdx}}-${{i}}`;
                        const durations = launchDurations[i];
                        
                        const avgEl = document.getElementById(`${{timingId}}-avg`);
                        const minEl = document.getElementById(`${{timingId}}-min`);
                        const maxEl = document.getElementById(`${{timingId}}-max`);
                        const countEl = document.getElementById(`${{timingId}}-count`);
                        const diffEl = document.getElementById(`${{timingId}}-diff`);
                        const containerEl = document.getElementById(timingId);
                        
                        if (durations.length > 0) {{
                            const avg = durations.reduce((a, b) => a + b, 0) / durations.length;
                            const min = Math.min(...durations);
                            const max = Math.max(...durations);
                            
                            if (avgEl) avgEl.textContent = avg.toFixed(3) + 's';
                            if (minEl) minEl.textContent = 'min: ' + min.toFixed(3) + 's';
                            if (maxEl) maxEl.textContent = 'max: ' + max.toFixed(3) + 's';
                            if (countEl) countEl.textContent = durations.length + ' calls';
                            
                            if (diffEl && baselineAvg && i > 0) {{
                                const diff = avg - baselineAvg;
                                const diffPct = (diff / baselineAvg * 100);
                                diffEl.className = 'timing-diff';
                                if (diff < -0.001) {{
                                    diffEl.classList.add('better');
                                    diffEl.textContent = diffPct.toFixed(1) + '%';
                                }} else if (diff > 0.001) {{
                                    diffEl.classList.add('worse');
                                    diffEl.textContent = '+' + diffPct.toFixed(1) + '%';
                                }} else {{
                                    diffEl.classList.add('same');
                                    diffEl.textContent = '0%';
                                }}
                            }} else if (diffEl && i === 0) {{
                                diffEl.textContent = '';
                            }}
                            
                            if (containerEl) {{
                                containerEl.classList.remove('na');
                            }}
                        }} else {{
                            if (avgEl) avgEl.textContent = 'N/A';
                            if (minEl) minEl.textContent = '';
                            if (maxEl) maxEl.textContent = '';
                            if (countEl) countEl.textContent = '';
                            if (diffEl) diffEl.textContent = '';
                            if (containerEl) {{
                                containerEl.classList.add('na');
                            }}
                        }}
                    }}
                }}
                
                // Update filter status
                const filterStatus = document.getElementById('filterStatus');
                const clearBtn = document.getElementById('clearFilter');
                
                if (currentFilter !== '') {{
                    filterStatus.textContent = `Filtered by "${{currentFilter}}" - ${{totalMatched}} matching requests`;
                    filterStatus.classList.add('visible');
                    clearBtn.classList.add('visible');
                }} else {{
                    filterStatus.classList.remove('visible');
                    clearBtn.classList.remove('visible');
                }}
                
                hideLoader();
            }}, 50);
        }}
        
        function clearFilter() {{
            const inp = document.getElementById('searchInput');
            if (inp) inp.value = '';
            applyFilter('');
        }}

        // The search input is now (re)bound by _bindSearchInput() after the
        // protected HTML is rendered (whether at page load when unprotected,
        // or after AES-GCM decrypt when protected). Top-level binding was
        // removed because the element doesn't exist before unlock.
    </script>
</body>
</html>'''
    return html


def serve_html(html_content: str):
    """Serve HTML content on HTTP server."""
    
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(html_content.encode())
        
        def log_message(self, format, *args):
            pass
    
    class ReuseAddrTCPServer(socketserver.TCPServer):
        allow_reuse_address = True
    
    with ReuseAddrTCPServer(("", PORT), Handler) as httpd:
        print(f"\n{'='*60}")
        print(f"  HTML Report available at: http://localhost:{PORT}")
        print("  Press Ctrl+C to stop the server")
        print(f"{'='*60}\n")
        httpd.serve_forever()


def generate_report_for_urls(urls, payload_key=None, log=print, report_password=None):
    """Run the same pipeline as the CLI but return the rendered HTML instead
    of serving it. Used by the SPA backend so it can persist the report to
    disk and serve it later. Mirrors the dispatch in ``main()`` exactly.

    Args:
        urls: list of Report Portal launch URLs (1 to 20).
        payload_key: optional payload key to group endpoints by.
        log: callable accepting one string -- progress messages. Defaults to
             ``print``; the SPA passes a logger that writes to a job log.
        report_password: optional per-call password used to AES-256-GCM
             encrypt the report payload. ``None`` falls back to the module-
             level ``REPORT_PASSWORD`` (env var, defaults to the legacy
             global). An empty string at either layer disables the gate.
             The SPA backend passes a fresh random password per report so
             every saved report has its own decryption secret.

    Returns:
        Tuple ``(html, metadata)`` where ``metadata`` is a dict describing
        the report (launch ids, labels, endpoint count, etc.) for caller
        bookkeeping.
    """
    if not urls:
        raise ValueError("at least one Report Portal URL is required")
    if len(urls) > 20:
        raise ValueError("Maximum 20 URLs supported")
    if not TOKEN:
        raise RuntimeError("RP_TOKEN environment variable is required")

    if len(urls) == 1:
        rp_url = urls[0]
        launch_id = extract_launch_id(rp_url)
        launch_info = resolve_launch_id(launch_id)
        numeric_id = launch_info['numeric_id']
        log(f"Fetching logs for launch ID: {numeric_id}...")

        logs_data = fetch_logs_with_cache(numeric_id, with_responses=bool(payload_key))
        endpoint_timings = analyze_timings(logs_data, payload_key=payload_key)
        html = generate_html(
            launch_id, logs_data, endpoint_timings,
            payload_key=payload_key,
            report_password=report_password,
        )
        meta = {
            "mode":         "single",
            "num_launches": 1,
            "launch_ids":   [numeric_id],
            "labels":       [launch_info.get('start_time_str', numeric_id)],
            "endpoints":    len(endpoint_timings),
        }
        return html, meta

    def _resolve(i_url):
        i, url = i_url
        launch_id = extract_launch_id(url)
        log(f"Fetching info for launch {i+1} (ID: {launch_id})...")
        launch_info = resolve_launch_id(launch_id)
        return {
            'url': url,
            'original_id': launch_id,
            'numeric_id': launch_info['numeric_id'],
            'start_time': launch_info['start_time'],
            'start_time_str': launch_info['start_time_str'],
        }

    with ThreadPoolExecutor(max_workers=8) as executor:
        launches = list(executor.map(_resolve, enumerate(urls)))
    launches.sort(key=lambda x: x['start_time'])

    launch_labels = [launch['start_time_str'] for launch in launches]

    def _fetch_and_analyze(launch):
        log(f"Fetching logs for {launch['start_time_str']} (ID: {launch['numeric_id']})...")
        logs    = fetch_logs_with_cache(launch['numeric_id'], with_responses=bool(payload_key))
        timings = analyze_timings(logs, payload_key=payload_key)
        detail  = analyze_timings_detailed(logs)
        return logs, timings, detail

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(_fetch_and_analyze, launches))

    logs_list             = [r[0] for r in results]
    timings_list          = [r[1] for r in results]
    detailed_timings_list = [r[2] for r in results]

    launch_ids = [launch['numeric_id'] for launch in launches]
    html = generate_multi_comparison_html(
        launch_labels, logs_list, timings_list, detailed_timings_list,
        launch_ids=launch_ids,
        report_password=report_password,
    )
    all_eps = set()
    for t in timings_list:
        all_eps.update(t.keys())
    meta = {
        "mode":         "multi",
        "num_launches": len(launches),
        "launch_ids":   launch_ids,
        "labels":       launch_labels,
        "endpoints":    len(all_eps),
    }
    return html, meta


def main():
    parser = argparse.ArgumentParser(
        description='Analyze endpoint response times from Report Portal logs.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "https://.../launches/all/1527175"
  %(prog)s "https://.../1527175,https://.../1527200"
  %(prog)s "https://.../1527175,https://.../1527200,https://.../1527300"
  %(prog)s --key "frequency-cap-detail" "https://.../launches/all/1527175"
        """
    )
    parser.add_argument('urls', help='Report Portal URL(s) - comma-separated for comparison (max 20)')
    parser.add_argument('--key', '-k', dest='payload_key', 
                        help='Group by presence of this key in request payload (also filters 4xx errors)')
    
    args = parser.parse_args()
    
    # Parse comma-separated URLs
    urls = [u.strip() for u in args.urls.split(',') if u.strip()]
    
    if len(urls) > 20:
        parser.error("Maximum 20 URLs supported")
    
    if not TOKEN:
        print("Error: RP_TOKEN environment variable is required", file=sys.stderr)
        print("  export RP_TOKEN='your_token_here'", file=sys.stderr)
        sys.exit(1)
    
    payload_key = args.payload_key
    
    try:
        if len(urls) == 1:
            # Single launch analysis
            rp_url = urls[0]
            launch_id = extract_launch_id(rp_url)
            display_id = launch_id
            launch_info = resolve_launch_id(launch_id)
            numeric_id = launch_info['numeric_id']
            print(f"Fetching logs for launch ID: {numeric_id}...")
            
            logs_data = fetch_logs_with_cache(numeric_id, with_responses=bool(payload_key))
            endpoint_timings = analyze_timings(logs_data, payload_key=payload_key)
            
            if payload_key:
                print(f"  Grouping by presence of '{payload_key}' in payload")
                print("  Filtering out 4xx error responses")
            
            print_report(display_id, logs_data, endpoint_timings, payload_key=payload_key)
            
            html = generate_html(display_id, logs_data, endpoint_timings, payload_key=payload_key)
            serve_html(html)
        else:
            # Multi-launch comparison mode (2-20 launches; tabbed view at >= 4)
            # Collect launch info with timestamps
            launches = []
            for i, url in enumerate(urls):
                launch_id = extract_launch_id(url)
                print(f"Fetching info for launch {i+1} (ID: {launch_id})...")
                launch_info = resolve_launch_id(launch_id)
                launches.append({
                    'url': url,
                    'original_id': launch_id,
                    'numeric_id': launch_info['numeric_id'],
                    'start_time': launch_info['start_time'],
                    'start_time_str': launch_info['start_time_str']
                })
            
            # Sort by start_time (oldest first, left to right)
            launches.sort(key=lambda x: x['start_time'])
            
            print("\nLaunches sorted by date (oldest to newest):")
            for launch in launches:
                print(f"  {launch['start_time_str']} - ID: {launch['numeric_id']}")
            print()
            
            # Fetch logs for each sorted launch
            launch_labels = []
            logs_list = []
            timings_list = []
            detailed_timings_list = []
            
            for launch in launches:
                launch_labels.append(launch['start_time_str'])
                print(f"Fetching logs for {launch['start_time_str']} (ID: {launch['numeric_id']})...")
                logs = fetch_logs_with_cache(launch['numeric_id'], with_responses=bool(payload_key))
                timings = analyze_timings(logs, payload_key=payload_key)
                detailed = analyze_timings_detailed(logs)
                
                logs_list.append(logs)
                timings_list.append(timings)
                detailed_timings_list.append(detailed)
            
            if payload_key:
                print(f"  Grouping by presence of '{payload_key}' in payload")
                print("  Filtering out 4xx error responses")
            
            print_multi_comparison(launch_labels, logs_list, timings_list)
            
            # Extract launch IDs for Datadog caching
            launch_ids = [launch['numeric_id'] for launch in launches]
            
            html = generate_multi_comparison_html(launch_labels, logs_list, timings_list, detailed_timings_list, launch_ids=launch_ids)
            serve_html(html)
        
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error fetching logs: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
