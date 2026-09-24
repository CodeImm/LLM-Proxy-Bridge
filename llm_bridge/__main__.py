"""``python -m llm_bridge`` — same as ``python bridge.py``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
