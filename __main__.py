"""Entry point for ``python -m rp_perf_report``.

Dispatches between two modes:

* No URL arguments (or ``--spa``)  -> start the SPA at ``http://localhost:9999``.
* One or more URL arguments        -> legacy single-shot CLI (back-compat).
"""

import sys

from .analyzer import main as cli_main
from .spa import serve_spa


def _wants_spa(argv: list) -> bool:
    """SPA mode when no positional URL is provided, or ``--spa`` is passed."""
    if "--spa" in argv:
        return True
    # Strip program name and known optional flags. If anything is left it's
    # a positional URL, i.e. legacy CLI mode.
    leftover = []
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--key", "-k"):
            skip_next = True
            continue
        if arg.startswith("--key=") or arg.startswith("-k="):
            continue
        if arg in ("-h", "--help"):
            return False
        leftover.append(arg)
    return not leftover


def main() -> None:
    if _wants_spa(sys.argv):
        # Drop any --spa flag so unrelated argparse-style parsers downstream
        # don't choke if they ever inspect argv.
        sys.argv = [a for a in sys.argv if a != "--spa"]
        serve_spa()
        return
    cli_main()


if __name__ == "__main__":
    main()
