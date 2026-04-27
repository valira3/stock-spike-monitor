"""Module entry point so `python -m backtest.replay` works.

`python -m backtest` (no submodule) defers to backtest.replay.main.
"""
from .replay import main
import sys

if __name__ == "__main__":
    sys.exit(main())
