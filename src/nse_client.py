"""NSE corporate-announcements API client.

Mirrors bse_client.py: a process-wide Session pre-warmed with NSE cookies, a
retrying request helper that re-warms on 401/403/429 (NSE expires cookies fast
and gates bare requests behind them), plus fetch/filter/download helpers.

NSE quirks vs BSE:
- The announcements endpoint returns a BARE JSON LIST (not wrapped in "Table").
- attchmntFile is already a FULL URL on nsearchives.nseindia.com, so download_pdf
  takes the URL directly rather than building it from an attachment name.
- The unique per-row id is seq_id; the dissemination timestamp is exchdisstime
  ("30-Jun-2026 12:52:56").
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

ANN_API_URL = "https://www.nseindia.com/api/corporate-announcements"
BOARD_MEETINGS_URL = "https://www.nseindia.com/api/corporate-board-meetings"
NSE_HOME_URL = "https://www.nseindia.com"
NSE_FILINGS_URL = (
    "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
)
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# The JSON API wants an XHR-style Accept and a Referer pointing at the filings page.
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": NSE_FILINGS_URL,
    "X-Requested-With": "XMLHttpRequest",
}
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
# Phrases that mark a genuine RESULTS filing. Deliberately tight: a bare
# "outcome of board meeting" also covers AGM-convening / dividend board meetings,
# so we require explicit results language in the subject/attachment text instead.
RESULT_PHRASES = ("financial result", "audited financial", "unaudited financial")
PDF_MAGIC = b"%PDF-"

_session: requests.Session | None = None


def _warm_session() -> requests.Session:
    """Build a fresh Session and seed NSE cookies via the homepage + filings page."""
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    try:
        # NSE sets its anti-bot cookies on these HTML pages; the JSON API 401/403s
        # without them. Two hops mirror a real browser landing on the filings page.
        s.get(NSE_HOME_URL, timeout=REQUEST_TIMEOUT)
        s.get(NSE_FILINGS_URL, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Cookie warm-up call to NSE failed: %s — continuing anyway", exc)
    return s


def _get_session() -> requests.Session:
    """Return the process-wide warmed Session, building it on first use."""
    global _session
    if _session is None:
        _session = _warm_session()
    return _session


def reset_session() -> None:
    """Drop the cached Session so the next request re-warms cookies from scratch."""
    global _session
    _session = None
    logger.info("NSE session reset — cookies will be re-warmed on next request")


def _request_with_retry(method: str, url: str, **kwargs: Any) -> requests.Response:
    """HTTP request with exponential backoff; re-warm cookies on 401/403/429."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        session = _get_session()
        try:
            resp = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if resp.status_code in (401, 403, 429):
                # Cookie/anti-bot gate or rate limit — back off and re-warm.
                wait = 2 ** attempt
                logger.warning(
                    "NSE %s returned %d (attempt %d/%d) — re-warming, retry in %ds",
                    url, resp.status_code, attempt, MAX_RETRIES, wait,
                )
                reset_session()
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                raise requests.HTTPError(f"server {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            # A non-auth 4xx will never succeed on retry — fail fast.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500 and status not in (401, 403, 429):
                logger.warning("Request to %s failed with client error %d — not retrying",
                               url, status)
                break
            wait = 2 ** (attempt - 1)
            logger.warning("Request to %s failed (attempt %d/%d): %s — retry in %ds",
                           url, attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
    if last_exc is not None:
        raise last_exc
    raise requests.HTTPError(f"NSE request to {url} exhausted retries (auth/rate gate)")


def fetch_announcements(symbol: str, days_back: int = 2) -> list[dict[str, Any]]:
    """Return recent equity announcements NSE has for a trading symbol.

    Bounded to the last ``days_back`` days via the API's from_date/to_date
    (dd-mm-yyyy) params — without them the endpoint returns the symbol's FULL
    history (~1700 rows), which is wasteful to fetch and parse every poll tick.
    """
    today = datetime.now().date()
    frm = (today - timedelta(days=days_back)).strftime("%d-%m-%Y")
    to = today.strftime("%d-%m-%Y")
    params = {
        "index": "equities",
        "symbol": str(symbol).strip().upper(),
        "from_date": frm,
        "to_date": to,
    }
    logger.info("Fetching NSE announcements symbol=%s range=%s..%s",
                params["symbol"], frm, to)
    resp = _request_with_retry("GET", ANN_API_URL, params=params, headers=API_HEADERS)
    try:
        data = resp.json()
    except ValueError as exc:
        logger.error("NSE returned non-JSON for symbol %s: %s", symbol, exc)
        return []
    # Endpoint returns a bare list; tolerate a wrapped {"data": [...]} just in case.
    if isinstance(data, dict):
        data = data.get("data") or data.get("rows") or []
    rows = data if isinstance(data, list) else []
    logger.info("NSE returned %d announcements for symbol %s", len(rows), symbol)
    return rows


def is_quarterly_result(announcement: dict[str, Any]) -> bool:
    """True only for a genuine RESULTS filing.

    Mirrors the BSE detector: require explicit results language and exclude rows
    that merely reference results — trading-window closures and pre-meeting
    board-meeting INTIMATIONS (the latter would also wrongly satisfy the per-day
    dedupe and suppress the real outcome).
    """
    blob = " ".join(
        str(announcement.get(k, "")) for k in ("desc", "attchmntText", "sm_name")
    ).lower()
    if "trading window" in blob:
        return False
    if "intimation" in blob and "outcome" not in blob:
        return False
    return any(p in blob for p in RESULT_PHRASES)


def is_board_meeting_outcome(announcement: dict[str, Any]) -> bool:
    """True for a generic board-meeting-outcome row, regardless of results wording.

    Widened candidate for HOT (results-day) stocks only; mirrors the BSE helper.
    The poller's PDF content gate decides whether the attachment is really a
    result, so this stays permissive. Intimations / trading-window rows excluded.
    """
    blob = " ".join(
        str(announcement.get(k, "")) for k in ("desc", "attchmntText", "sm_name")
    ).lower()
    if "trading window" in blob:
        return False
    if "intimation" in blob and "outcome" not in blob:
        return False
    return "board meeting" in blob and "outcome" in blob


def fetch_board_meetings(symbol: str) -> list[dict[str, Any]]:
    """Return NSE's board-meeting intimations for a symbol (SEBI Reg 29 notices).

    A board meeting is a company-level event intimated to BOTH exchanges, so this
    NSE feed gives the authoritative meeting date regardless of where the eventual
    results filing is caught. Each row carries bm_date (e.g. '29-May-2026') and
    bm_purpose (e.g. 'Financial Results/Dividend').
    """
    params = {"index": "equities", "symbol": str(symbol).strip().upper()}
    logger.info("Fetching NSE board meetings symbol=%s", params["symbol"])
    resp = _request_with_retry("GET", BOARD_MEETINGS_URL, params=params, headers=API_HEADERS)
    try:
        data = resp.json()
    except ValueError as exc:
        logger.error("NSE board-meetings non-JSON for %s: %s", symbol, exc)
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("rows") or []
    return data if isinstance(data, list) else []


def board_meeting_date(row: dict[str, Any]) -> "date | None":
    """Parse a board-meeting row's meeting date ('29-May-2026') to a date, or None."""
    raw = str(row.get("bm_date") or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d-%b-%Y").date()
    except ValueError:
        return None


def is_results_board_meeting(row: dict[str, Any]) -> bool:
    """True if a board-meeting row is convened to consider financial results."""
    blob = " ".join(str(row.get(k, "")) for k in ("bm_purpose", "bm_desc")).lower()
    return any(p in blob for p in RESULT_PHRASES)


def download_pdf(file_url: str, save_dir: Path) -> Path:
    """Download an NSE attachment PDF (full URL) and return the saved local path."""
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlsplit(file_url).path).name or "nse_filing.pdf"
    dest = save_dir / filename
    logger.info("Downloading NSE PDF %s", file_url)
    # nsearchives serves the file but still wants the browser-ish headers/cookies.
    resp = _request_with_retry("GET", file_url, stream=True,
                               headers={"Referer": NSE_FILINGS_URL})

    head = resp.raw.read(8, decode_content=True) if hasattr(resp, "raw") else b""
    if not head.startswith(PDF_MAGIC):
        ctype = resp.headers.get("Content-Type", "?")
        raise ValueError(
            f"NSE returned non-PDF content for {file_url} "
            f"(content-type={ctype}, prefix={head!r})"
        )

    with dest.open("wb") as fh:
        fh.write(head)
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                fh.write(chunk)
    logger.info("Saved NSE PDF to %s (%d bytes)", dest, dest.stat().st_size)
    return dest


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Standalone smoke test: list recent announcements for Asian Paints.
    rows = fetch_announcements("ASIANPAINT")
    print(f"Total announcements: {len(rows)}")
    for row in rows[:5]:
        print(f"- {row.get('exchdisstime', '?')} | {row.get('seq_id', '?')} | "
              f"result={is_quarterly_result(row)} | {str(row.get('desc',''))[:80]}")
