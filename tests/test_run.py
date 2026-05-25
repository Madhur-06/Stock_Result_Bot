"""Smoke test: invoke main() in dry-run mode."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable when running this file directly.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.main import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main(["--dry-run", "--days-back", "14"]))
