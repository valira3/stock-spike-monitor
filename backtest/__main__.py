"""Module entry point for `python -m backtest`.

v5.14.0: the legacy SHADOW_CONFIGS replay was retired. The full
v5.11.x backtest engine still lives at backtest.replay_v511_full and
remains the recommended entry point.
"""

from .replay_v511_full import main
import sys

if __name__ == "__main__":
    sys.exit(main())
