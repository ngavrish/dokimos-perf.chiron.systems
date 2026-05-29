"""rp_perf_report -- Report Portal performance analysis & comparison server.

Pull behavex / API-test launch logs from Report Portal, aggregate per-endpoint
response times, optionally correlate with Datadog APM traces, and serve a
self-contained HTML report (locally encrypted with AES-256-GCM when a password
is configured).

See README.md for usage. The CLI entrypoint is `rp_perf_report.analyzer.main`;
this module is runnable as `python -m rp_perf_report <urls>`.
"""

from .analyzer import (  # noqa: F401  (re-exported for programmatic use)
    main,
    generate_html,
    generate_comparison_html,
    generate_multi_comparison_html,
    generate_report_for_urls,
    analyze_timings,
    analyze_timings_detailed,
    fetch_logs_with_cache,
    resolve_launch_id,
    serve_html,
)
from .spa import (  # noqa: F401
    serve_spa,
    sprint_window_for,
    sprint_dir_name,
)

__all__ = [
    "main",
    "generate_html",
    "generate_comparison_html",
    "generate_multi_comparison_html",
    "generate_report_for_urls",
    "analyze_timings",
    "analyze_timings_detailed",
    "fetch_logs_with_cache",
    "resolve_launch_id",
    "serve_html",
    "serve_spa",
    "sprint_window_for",
    "sprint_dir_name",
]
