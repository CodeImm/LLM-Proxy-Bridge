"""LLM Bridge — entry point.

    python bridge.py                 # interactive profile choice
    python bridge.py --profile 2     # or: -p gemini
    python bridge.py --help

The implementation lives in the ``llm_bridge`` package next to this file.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.stderr.write("LLM Bridge требует Python 3.11+ (сейчас {}.{})\n".format(*sys.version_info[:2]))
    sys.exit(1)

_REQUIRED = {"fastapi", "uvicorn", "httpx", "rich", "dotenv", "starlette"}

try:
    from llm_bridge.cli import main
except ModuleNotFoundError as exc:
    if exc.name and exc.name.split(".")[0] in _REQUIRED:
        sys.stderr.write(
            f"Не установлена зависимость: {exc.name}\nУстановите: pip install -r requirements.txt\n"
        )
        sys.exit(1)
    raise

if __name__ == "__main__":
    sys.exit(main())
