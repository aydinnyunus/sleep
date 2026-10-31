#!/usr/bin/env python3
"""Entry point shim so the repo can be run without installation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sleepkit.cli import main  # noqa: E402

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # `sleepctl status | head` closes the pipe early. A traceback for that is
        # noise in a tool whose output is meant to be piped and grepped.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:
            pass
        raise SystemExit(0)
