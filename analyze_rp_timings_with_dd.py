#!/usr/bin/env python3
"""Backwards-compatible shim.

The real implementation lives in the ``rp_perf_report`` package -- this file
is preserved so muscle-memory invocations of the old script name keep
working unchanged.

Equivalent invocations:

    python3 ~/work/rp_perf_report/analyze_rp_timings_with_dd.py "<urls>"
    cd ~/work && python3 -m rp_perf_report "<urls>"

See ``README.md`` for full docs (env vars, AES-256-GCM gate, multi-launch
tabbed comparison, etc.).
"""
import os
import sys


def _run() -> None:
    # This file lives *inside* the rp_perf_report package directory. To
    # import the package by its top-level name we add the package's parent
    # directory (~/work, or wherever the package was placed) to sys.path.
    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    _parent = os.path.dirname(_pkg_dir)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)

    from rp_perf_report.analyzer import main as _main
    _main()


if __name__ == "__main__":
    _run()
