"""JSON-file-backed persistence helpers."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEEN_FILINGS_PATH = PROJECT_ROOT / "data" / "seen_filings.json"


def load_json(path: Path, default: Any) -> Any:
    """Load JSON from path, returning default if missing or unreadable."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load %s: %s — using default", path, exc)
        return default


def save_json(path: Path, data: Any) -> None:
    """Write data to path as pretty-printed JSON (atomic-ish via tmp file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _load_seen() -> list[str]:
    data = load_json(SEEN_FILINGS_PATH, [])
    return [str(x) for x in data] if isinstance(data, list) else []


def is_seen(filing_id: str) -> bool:
    """Return True if filing_id was already processed."""
    return str(filing_id) in _load_seen()


def mark_seen(filing_id: str) -> None:
    """Persist filing_id into seen_filings.json (deduped)."""
    seen = _load_seen()
    fid = str(filing_id)
    if fid not in seen:
        seen.append(fid)
        save_json(SEEN_FILINGS_PATH, seen)
