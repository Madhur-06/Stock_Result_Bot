"""Send notifications via the Telegram Bot API."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MSG_CHARS = 4000  # Telegram limit is 4096; leave headroom for the prefix.
MAX_CAPTION_CHARS = 1000  # Telegram caption limit is 1024.
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024  # Telegram bot upload cap.
RATE_LIMIT_SLEEP_SEC = 1
REQUEST_TIMEOUT = 60
# Document uploads can be many MB; a results PDF on a slow uplink needs more than
# the 60s message timeout (a 10MB filing timed out at 60s in practice).
DOCUMENT_TIMEOUT = 180


def _chunks(text: str, size: int) -> list[str]:
    """Split text into <=size-char pieces, preferring line breaks."""
    parts: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def send_message(text: str) -> bool:
    """Send text via Telegram. Returns True if every chunk was delivered."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing from .env")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    pieces = _chunks(text, MAX_MSG_CHARS) if len(text) > MAX_MSG_CHARS else [text]
    ok = True
    for i, piece in enumerate(pieces, 1):
        payload = {
            "chat_id": chat_id,
            "text": piece,
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
            body = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
            if resp.status_code == 200 and body.get("ok"):
                logger.info("Telegram chunk %d/%d OK (message_id=%s)",
                            i, len(pieces), body.get("result", {}).get("message_id"))
            else:
                ok = False
                logger.error("Telegram chunk %d/%d failed (status=%s): %s",
                             i, len(pieces), resp.status_code, (resp.text or "")[:300])
        except requests.RequestException as exc:
            ok = False
            logger.error("Telegram request error on chunk %d/%d: %s", i, len(pieces), exc)
        time.sleep(RATE_LIMIT_SLEEP_SEC)
    return ok


def send_document(file_path: Path, caption: str = "") -> bool:
    """Upload a file (e.g. the BSE PDF) to Telegram with an optional caption."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing from .env")
        return False
    file_path = Path(file_path)
    if not file_path.exists():
        logger.error("send_document: %s does not exist", file_path)
        return False
    size = file_path.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        logger.error("send_document: %s is %d bytes (>50MB Telegram limit) — skipping",
                     file_path, size)
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendDocument"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:MAX_CAPTION_CHARS]
    try:
        with file_path.open("rb") as fh:
            files = {"document": (file_path.name, fh, "application/pdf")}
            resp = requests.post(url, data=data, files=files, timeout=DOCUMENT_TIMEOUT)
        body = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
        if resp.status_code == 200 and body.get("ok"):
            logger.info("Telegram document sent: %s (%d bytes)", file_path.name, size)
            return True
        logger.error("Telegram sendDocument failed (status=%s): %s",
                     resp.status_code, (resp.text or "")[:300])
        return False
    except requests.RequestException as exc:
        logger.error("Telegram sendDocument request error: %s", exc)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = send_message("Bot test message")
    print(f"send_message returned: {result}")
