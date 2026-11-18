"""Development launcher: python run.py [--account-window ...]"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from ai_usage_board.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
