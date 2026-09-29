#!/usr/bin/env python3
"""Start the web UI under debugpy for IDE "Attach to DAP" debugging.

Use this when the IDE cannot launch its Python DAP adapter directly:

    .venv/bin/python debug_web.py

Then attach the IDE debugger to 127.0.0.1:5678.
"""
from __future__ import annotations

import argparse
import os
import sys


ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MediaHarvest Web with debugpy attached.")
    parser.add_argument("--debug-host", default="127.0.0.1")
    parser.add_argument("--debug-port", type=int, default=5678)
    parser.add_argument("--no-wait-debugger", action="store_true")
    args, web_args = parser.parse_known_args(argv)

    try:
        import debugpy
    except ImportError:
        print("debugpy is not installed. Run: .venv/bin/python -m pip install -e '.[dev]'", file=sys.stderr)
        return 1

    debugpy.listen((args.debug_host, args.debug_port))
    print(f"debugpy is listening on {args.debug_host}:{args.debug_port}")
    if not args.no_wait_debugger:
        print("Waiting for IDE debugger to attach...")
        debugpy.wait_for_client()
        print("Debugger attached.")

    from mediaharvest.web import main as web_main

    if not web_args:
        web_args = ["--no-browser"]
    return int(web_main(web_args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
