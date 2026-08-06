#!/usr/bin/env python3
"""Entry point. Run `python3 index.py --help` for options."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
