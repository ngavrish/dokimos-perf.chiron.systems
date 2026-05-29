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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Network and retry constants
# ---------------------------------------------------------------------------
_RP_HTTP_TIMEOUT_SECS          = 120
_RP_FETCH_RETRY_BACKOFF_SECS   = 2

TOKEN = os.environ.get("RP_TOKEN", "")
API_BASE = os.environ.get("RP_API_BASE", "https://ads-report-portal.staging.hulu.com/api/v1/ad-apps-automation/log")
PORT = 9999


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


def fetch_logs(launch_id: str, max_retries: int = 3, with_responses: bool = False) -> dict:
    """Fetch logs from Report Portal API with retry logic and pagination."""
    if with_responses:
        base_url = f"{API_BASE}?filter.eq.launchId={launch_id}"
    else:
        base_url = f"{API_BASE}?filter.eq.launchId={launch_id}&filter.cnt.message=Duration"
    
    all_content = []
    page = 1
    page_size = 2000
    total_pages = 1
    
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
                    if page == 0:
                        print(f"  Found {data['page']['totalElements']} logs ({total_pages} pages)")
                    break
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"  Retry {attempt + 1}/{max_retries} after error: {e}")
                    time.sleep(_RP_FETCH_RETRY_BACKOFF_SECS)
                else:
                    raise
        
        page += 1
    
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
            }
        }
    }
    """
    endpoint_data = defaultdict(lambda: {'durations': [], 'by_payload': defaultdict(list)})
    
    for log in logs_data.get('content', []):
        message = log.get('message', '')
        
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
    
    return endpoint_data


def generate_html(launch_id: str, logs_data: dict, endpoint_timings: dict, payload_key: str = None) -> str:
    """Generate HTML report."""
    total_logs = logs_data.get('page', {}).get('totalElements', 0)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
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
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Endpoint Response Time Analysis - Launch {launch_id}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            padding: 40px 20px;
            color: #e4e4e4;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        header {{
            text-align: center;
            margin-bottom: 40px;
        }}
        h1 {{
            font-size: 2.5rem;
            font-weight: 600;
            background: linear-gradient(90deg, #00d4ff, #7b2cbf);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }}
        .meta {{
            color: #888;
            font-size: 0.95rem;
        }}
        .meta span {{
            margin: 0 15px;
        }}
        .stats {{
            display: flex;
            gap: 20px;
            margin-bottom: 30px;
            flex-wrap: wrap;
        }}
        .stat-card {{
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 20px 30px;
            flex: 1;
            min-width: 200px;
        }}
        .stat-card h3 {{
            font-size: 0.85rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }}
        .stat-card .value {{
            font-size: 2rem;
            font-weight: 700;
            color: #00d4ff;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 12px;
            overflow: hidden;
        }}
        th {{
            background: rgba(0, 212, 255, 0.1);
            padding: 16px 20px;
            text-align: left;
            font-weight: 600;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #00d4ff;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }}
        td {{
            padding: 14px 20px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        }}
        tr:hover {{
            background: rgba(255, 255, 255, 0.05);
        }}
        .method {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
        }}
        .method.get {{ background: #2e7d32; color: #fff; }}
        .method.post {{ background: #1565c0; color: #fff; }}
        .method.put {{ background: #ef6c00; color: #fff; }}
        .method.delete {{ background: #c62828; color: #fff; }}
        .method.patch {{ background: #6a1b9a; color: #fff; }}
        .endpoint {{
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.85rem;
            color: #aaa;
            word-break: break-all;
        }}
        .number {{
            font-family: 'Monaco', 'Menlo', monospace;
            text-align: right;
            font-size: 0.9rem;
        }}
        .key-badge {{
            display: inline-block;
            margin-left: 10px;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .key-badge.with-key {{
            background: #2e7d32;
            color: #fff;
        }}
        .key-badge.without-key {{
            background: #5d4037;
            color: #fff;
        }}
        .filter-info {{
            background: rgba(255, 193, 7, 0.1);
            border: 1px solid rgba(255, 193, 7, 0.3);
            border-radius: 8px;
            padding: 12px 20px;
            margin-bottom: 20px;
            font-size: 0.9rem;
            color: #ffc107;
        }}
        footer {{
            text-align: center;
            margin-top: 40px;
            color: #666;
            font-size: 0.85rem;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Endpoint Response Time Analysis</h1>
            <div class="meta">
                <span>Launch ID: <strong>{launch_id}</strong></span>
                <span>Generated: {generated_at}</span>
            </div>
        </header>
        
        {f'<div class="filter-info">Grouped by presence of "<strong>{payload_key}</strong>" in request payload | 4xx errors excluded</div>' if payload_key else ''}
        
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
                {''.join(rows) if rows else '<tr><td colspan="6" style="text-align:center;padding:40px;">No timing data found</td></tr>'}
            </tbody>
        </table>
        
        <footer>
            Report Portal Timing Analysis Tool
        </footer>
    </div>
</body>
</html>"""
    return html


def generate_comparison_html(launch1_id: str, launch2_id: str, 
                              logs1: dict, logs2: dict,
                              timings1: dict, timings2: dict) -> str:
    """Generate HTML comparison report for two launches."""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            padding: 40px 20px;
            color: #e4e4e4;
        }}
        .container {{ max-width: 1600px; margin: 0 auto; }}
        header {{ text-align: center; margin-bottom: 40px; }}
        h1 {{
            font-size: 2.5rem;
            font-weight: 600;
            background: linear-gradient(90deg, #00d4ff, #7b2cbf);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }}
        .meta {{ color: #888; font-size: 0.95rem; }}
        .meta span {{ margin: 0 15px; }}
        .stats {{ display: flex; gap: 20px; margin-bottom: 30px; flex-wrap: wrap; }}
        .stat-card {{
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 20px 30px;
            flex: 1;
            min-width: 200px;
        }}
        .stat-card h3 {{
            font-size: 0.85rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }}
        .stat-card .value {{ font-size: 2rem; font-weight: 700; color: #00d4ff; }}
        .stat-card .label {{ font-size: 0.8rem; color: #666; margin-top: 4px; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 12px;
            overflow: hidden;
        }}
        th {{
            background: rgba(0, 212, 255, 0.1);
            padding: 16px 12px;
            text-align: left;
            font-weight: 600;
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #00d4ff;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }}
        th.launch1 {{ background: rgba(0, 150, 255, 0.15); }}
        th.launch2 {{ background: rgba(150, 0, 255, 0.15); }}
        td {{ padding: 12px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }}
        tr:hover {{ background: rgba(255, 255, 255, 0.05); }}
        .method {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
        }}
        .method.get {{ background: #2e7d32; color: #fff; }}
        .method.post {{ background: #1565c0; color: #fff; }}
        .method.put {{ background: #ef6c00; color: #fff; }}
        .method.delete {{ background: #c62828; color: #fff; }}
        .method.patch {{ background: #6a1b9a; color: #fff; }}
        .endpoint {{
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.8rem;
            color: #aaa;
            word-break: break-all;
            max-width: 400px;
        }}
        .number {{
            font-family: 'Monaco', 'Menlo', monospace;
            text-align: right;
            font-size: 0.85rem;
        }}
        .diff {{ font-weight: 600; }}
        .diff.better {{ color: #4caf50; }}
        .diff.worse {{ color: #f44336; }}
        .diff.same {{ color: #888; }}
        .diff.na {{ color: #666; }}
        footer {{ text-align: center; margin-top: 40px; color: #666; font-size: 0.85rem; }}
        .legend {{
            display: flex;
            gap: 30px;
            justify-content: center;
            margin-bottom: 20px;
            font-size: 0.85rem;
        }}
        .legend-item {{ display: flex; align-items: center; gap: 8px; }}
        .legend-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
        .legend-dot.better {{ background: #4caf50; }}
        .legend-dot.worse {{ background: #f44336; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Timing Comparison</h1>
            <div class="meta">
                <span>Launch 1: <strong>{launch1_id}</strong></span>
                <span>vs</span>
                <span>Launch 2: <strong>{launch2_id}</strong></span>
                <span>|</span>
                <span>Generated: {generated_at}</span>
            </div>
        </header>
        
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


def generate_multi_comparison_html(launch_labels: list, logs_list: list, timings_list: list, detailed_timings_list: list = None) -> str:
    """Generate HTML comparison report for multiple launches (2-4)."""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = len(launch_labels)
    
    # Collect all endpoints
    all_endpoints = set()
    for timings in timings_list:
        all_endpoints.update(timings.keys())
    all_endpoints = sorted(all_endpoints)
    
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
    
    # Build launch info cards
    launch_info_cards = []
    for stat in launch_stats:
        launch_info_cards.append(f'''
            <div class="launch-info-card">
                <div class="launch-num">{stat['label']}</div>
                <div class="launch-stats">{stat['total_logs']} logs / {stat['total_requests']} requests</div>
            </div>
        ''')
    
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Timing Comparison - {n} Launches</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            padding: 30px 20px;
            color: #e4e4e4;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        header {{ text-align: center; margin-bottom: 30px; }}
        h1 {{
            font-size: 2rem;
            font-weight: 600;
            background: linear-gradient(90deg, #00d4ff, #7b2cbf);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 8px;
        }}
        .meta {{ color: #888; font-size: 0.9rem; }}
        
        .search-container {{
            margin-bottom: 20px;
            display: flex;
            gap: 10px;
            align-items: center;
            justify-content: center;
        }}
        .search-input {{
            width: 400px;
            padding: 12px 16px;
            font-size: 1rem;
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.15);
            border-radius: 8px;
            color: #fff;
            outline: none;
            transition: border-color 0.2s, background 0.2s;
        }}
        .search-input:focus {{
            border-color: #00d4ff;
            background: rgba(255, 255, 255, 0.12);
        }}
        .search-input::placeholder {{
            color: #666;
        }}
        .search-hint {{
            font-size: 0.8rem;
            color: #666;
        }}
        .clear-filter {{
            padding: 10px 16px;
            background: rgba(244, 67, 54, 0.2);
            border: 1px solid rgba(244, 67, 54, 0.4);
            border-radius: 8px;
            color: #f44336;
            cursor: pointer;
            font-size: 0.9rem;
            display: none;
        }}
        .clear-filter.visible {{
            display: block;
        }}
        .clear-filter:hover {{
            background: rgba(244, 67, 54, 0.3);
        }}
        .filter-status {{
            text-align: center;
            padding: 10px;
            margin-bottom: 15px;
            background: rgba(0, 212, 255, 0.1);
            border: 1px solid rgba(0, 212, 255, 0.3);
            border-radius: 8px;
            color: #00d4ff;
            display: none;
        }}
        .filter-status.visible {{
            display: block;
        }}
        
        .loader-overlay {{
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 1000;
        }}
        .loader-overlay.visible {{
            display: flex;
        }}
        .loader {{
            text-align: center;
        }}
        .loader-spinner {{
            width: 50px;
            height: 50px;
            border: 4px solid rgba(255, 255, 255, 0.2);
            border-top-color: #00d4ff;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }}
        .loader-text {{
            margin-top: 15px;
            color: #fff;
            font-size: 1.1rem;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
        
        .launches-row {{
            display: flex;
            gap: 15px;
            margin-bottom: 25px;
            flex-wrap: wrap;
        }}
        .launch-info-card {{
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 10px;
            padding: 15px;
            flex: 1;
            min-width: 200px;
        }}
        .launch-num {{
            font-size: 1.5rem;
            font-weight: 700;
            color: #00d4ff;
            margin-bottom: 5px;
        }}
        .launch-id {{
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.75rem;
            color: #aaa;
            margin-bottom: 5px;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .launch-stats {{ font-size: 0.8rem; color: #666; }}
        
        .legend {{
            display: flex;
            gap: 25px;
            justify-content: center;
            margin-bottom: 20px;
            font-size: 0.85rem;
            flex-wrap: wrap;
        }}
        .legend-item {{ display: flex; align-items: center; gap: 6px; }}
        .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
        .legend-dot.better {{ background: #4caf50; }}
        .legend-dot.worse {{ background: #f44336; }}
        .legend-note {{ color: #888; font-style: italic; }}
        
        .endpoint-card {{
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            margin-bottom: 15px;
            overflow: hidden;
            cursor: pointer;
            transition: border-color 0.2s;
        }}
        .endpoint-card:hover {{
            border-color: rgba(255, 255, 255, 0.2);
        }}
        .endpoint-header {{
            background: rgba(0, 0, 0, 0.2);
            padding: 12px 15px;
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }}
        .method {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 0.7rem;
            font-weight: 700;
            text-transform: uppercase;
            flex-shrink: 0;
        }}
        .method.get {{ background: #2e7d32; color: #fff; }}
        .method.post {{ background: #1565c0; color: #fff; }}
        .method.put {{ background: #ef6c00; color: #fff; }}
        .method.delete {{ background: #c62828; color: #fff; }}
        .method.patch {{ background: #6a1b9a; color: #fff; }}
        .endpoint-url {{
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.8rem;
            color: #aaa;
            word-break: break-all;
        }}
        
        .timings-grid {{
            display: grid;
            grid-template-columns: repeat({n}, 1fr);
            gap: 1px;
            background: rgba(255, 255, 255, 0.05);
        }}
        .launch-timing {{
            background: #1a1a2e;
            padding: 12px;
            text-align: center;
        }}
        .launch-timing.na {{ opacity: 0.5; }}
        .launch-label {{
            font-size: 0.75rem;
            color: #00d4ff;
            font-weight: 600;
            margin-bottom: 5px;
        }}
        .timing-value {{
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 3px;
        }}
        .timing-minmax {{
            font-size: 0.65rem;
            color: #777;
            margin-bottom: 3px;
            display: flex;
            justify-content: center;
            gap: 8px;
        }}
        .min-val {{ color: #4caf50; }}
        .max-val {{ color: #ff9800; }}
        .timing-count {{
            font-size: 0.7rem;
            color: #888;
            margin-bottom: 3px;
        }}
        .timing-diff {{
            font-size: 0.8rem;
            font-weight: 600;
            padding: 2px 6px;
            border-radius: 3px;
            display: inline-block;
        }}
        .timing-diff.better {{ color: #4caf50; background: rgba(76, 175, 80, 0.15); }}
        .timing-diff.worse {{ color: #f44336; background: rgba(244, 67, 54, 0.15); }}
        .timing-diff.same {{ color: #888; }}
        
        .expand-icon {{
            margin-left: auto;
            color: #666;
            font-size: 0.8rem;
            transition: transform 0.3s;
        }}
        .expand-icon.expanded {{
            transform: rotate(180deg);
        }}
        
        .payload-details {{
            display: none;
            background: rgba(0, 0, 0, 0.15);
            border-top: 1px solid rgba(255, 255, 255, 0.08);
            padding: 15px;
        }}
        .payload-details.expanded {{
            display: block;
        }}
        .payload-section-header {{
            color: #888;
            font-size: 0.85rem;
            margin-bottom: 15px;
            padding-bottom: 8px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .toggle-switch {{
            display: flex;
            align-items: center;
            gap: 8px;
            cursor: pointer;
        }}
        .toggle-switch input {{
            display: none;
        }}
        .toggle-slider {{
            width: 36px;
            height: 20px;
            background: rgba(255, 255, 255, 0.15);
            border-radius: 10px;
            position: relative;
            transition: background 0.2s;
        }}
        .toggle-slider::after {{
            content: '';
            position: absolute;
            width: 16px;
            height: 16px;
            background: #888;
            border-radius: 50%;
            top: 2px;
            left: 2px;
            transition: all 0.2s;
        }}
        .toggle-switch input:checked + .toggle-slider {{
            background: rgba(76, 175, 80, 0.4);
        }}
        .toggle-switch input:checked + .toggle-slider::after {{
            left: 18px;
            background: #4caf50;
        }}
        .toggle-label {{
            font-size: 0.75rem;
            color: #888;
        }}
        .payload-row {{
            margin-bottom: 12px;
            padding: 10px;
            background: rgba(255, 255, 255, 0.02);
            border-radius: 6px;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }}
        .payload-header {{
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.7rem;
            color: #aaa;
            margin-bottom: 10px;
            padding: 8px 10px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 4px;
            cursor: pointer;
            display: flex;
            align-items: flex-start;
            gap: 8px;
            transition: background 0.2s;
        }}
        .payload-header:hover {{
            background: rgba(0, 0, 0, 0.35);
        }}
        .payload-expand-icon {{
            color: #666;
            font-size: 0.65rem;
            transition: transform 0.2s;
            flex-shrink: 0;
            margin-top: 2px;
        }}
        .payload-expand-icon.expanded {{
            transform: rotate(90deg);
        }}
        .payload-preview {{
            word-break: break-all;
            white-space: pre-wrap;
        }}
        .payload-full {{
            display: none;
            margin-bottom: 10px;
            padding: 10px;
            background: rgba(0, 0, 0, 0.3);
            border-radius: 4px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            overflow-x: auto;
        }}
        .payload-full.expanded {{
            display: block;
        }}
        .payload-full pre {{
            margin: 0;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.75rem;
            color: #8bc34a;
            white-space: pre-wrap;
            word-break: break-all;
        }}
        .payload-timings-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 8px;
        }}
        .payload-timing {{
            background: rgba(255, 255, 255, 0.03);
            border-radius: 6px;
            padding: 8px;
            text-align: center;
        }}
        .payload-timing.na {{
            opacity: 0.5;
        }}
        .payload-label {{
            font-size: 0.7rem;
            color: #888;
            margin-bottom: 4px;
        }}
        .payload-value {{
            font-size: 1rem;
            font-weight: bold;
            color: #4fc3f7;
            margin-bottom: 4px;
        }}
        .payload-minmax {{
            font-size: 0.6rem;
            color: #777;
            margin-bottom: 4px;
        }}
        .payload-minmax .min-val {{ color: #4caf50; }}
        .payload-minmax .max-val {{ color: #ff9800; }}
        .payload-count {{
            font-size: 0.65rem;
            color: #666;
        }}
        
        footer {{
            text-align: center;
            margin-top: 30px;
            color: #666;
            font-size: 0.85rem;
        }}
        
        .summary {{
            background: rgba(255, 255, 255, 0.03);
            border-radius: 10px;
            padding: 15px;
            margin-bottom: 20px;
            text-align: center;
            color: #888;
        }}
    </style>
</head>
<body>
    <div class="loader-overlay" id="loaderOverlay">
        <div class="loader">
            <div class="loader-spinner"></div>
            <div class="loader-text">Filtering data...</div>
        </div>
    </div>
    
    <div class="container">
        <header>
            <h1>Timing Comparison</h1>
            <div class="meta">{n} Launches | Generated: {generated_at}</div>
        </header>
        
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
            <div class="legend-item"><span class="legend-dot better"></span> Faster than oldest</div>
            <div class="legend-item"><span class="legend-dot worse"></span> Slower than L1</div>
            <div class="legend-note">(Comparison baseline: Launch 1)</div>
        </div>
        
        <div class="summary">
            Comparing {len(all_endpoints)} unique endpoints across {n} launches
        </div>
        
        {''.join(endpoint_cards) if endpoint_cards else '<div class="endpoint-card"><div class="endpoint-header">No timing data found</div></div>'}
        
        <footer>
            Report Portal Timing Comparison Tool
        </footer>
    </div>
    
    <script>
        const reportData = {js_data_json};
        let currentFilter = '';
        
        function toggleDetails(idx) {{
            const details = document.getElementById('details-' + idx);
            const icon = document.getElementById('icon-' + idx);
            
            if (details) {{
                details.classList.toggle('expanded');
                icon.classList.toggle('expanded');
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
            document.getElementById('searchInput').value = '';
            applyFilter('');
        }}
        
        document.getElementById('searchInput').addEventListener('keypress', function(e) {{
            if (e.key === 'Enter') {{
                applyFilter(this.value);
            }}
        }});
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
    parser.add_argument('urls', help='Report Portal URL(s) - comma-separated for comparison (max 4)')
    parser.add_argument('--key', '-k', dest='payload_key', 
                        help='Group by presence of this key in request payload (also filters 4xx errors)')
    
    args = parser.parse_args()
    
    # Parse comma-separated URLs
    urls = [u.strip() for u in args.urls.split(',') if u.strip()]
    
    if len(urls) > 4:
        parser.error("Maximum 4 URLs supported")
    
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
            
            logs_data = fetch_logs(numeric_id, with_responses=bool(payload_key))
            endpoint_timings = analyze_timings(logs_data, payload_key=payload_key)
            
            if payload_key:
                print(f"  Grouping by presence of '{payload_key}' in payload")
                print("  Filtering out 4xx error responses")
            
            print_report(display_id, logs_data, endpoint_timings, payload_key=payload_key)
            
            html = generate_html(display_id, logs_data, endpoint_timings, payload_key=payload_key)
            serve_html(html)
        else:
            # Multi-launch comparison mode (2-4 launches)
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
                logs = fetch_logs(launch['numeric_id'], with_responses=bool(payload_key))
                timings = analyze_timings(logs, payload_key=payload_key)
                detailed = analyze_timings_detailed(logs)
                
                logs_list.append(logs)
                timings_list.append(timings)
                detailed_timings_list.append(detailed)
            
            if payload_key:
                print(f"  Grouping by presence of '{payload_key}' in payload")
                print("  Filtering out 4xx error responses")
            
            print_multi_comparison(launch_labels, logs_list, timings_list)
            
            html = generate_multi_comparison_html(launch_labels, logs_list, timings_list, detailed_timings_list)
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
