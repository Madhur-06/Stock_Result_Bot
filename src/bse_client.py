"""BSE corporate-filings API client."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Primary all-category endpoint. Verified live (Jun 2026) to return full rows
# (NEWSID/HEADLINE/NEWSSUB/CATEGORYNAME/ATTACHMENTNAME/…) for strCat="-1".
ANN_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
# Fallback used only when the primary returns a degenerate response (a bare
# "No Record Found!" string or the {"Table":[{"Column1":1}]} sentinel). BSE has
# been observed to flip which of these two endpoints is healthy, so we try the
# other rather than silently returning nothing.
FALLBACK_ANN_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
PDF_BASE_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_ROOT_URL = "https://www.bseindia.com/"
BSE_HOME_URL = "https://www.bseindia.com/corporates/ann.html"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
# Phrases that mark a genuine quarterly/annual RESULTS disclosure. Deliberately
# tighter than the bare word "financial" (which also appears in "financial year"
# on dividend/AGM/annual-report rows). All-category fetching (strCat="-1") pulls
# in many non-result rows, so the detector must require real results language.
RESULT_PHRASES = ("financial result", "audited financial", "unaudited financial")
PDF_MAGIC = b"%PDF-"

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return a process-wide Session pre-warmed with BSE cookies."""
    global _session
    if _session is not None:
        return _session
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    try:
        # Visit the BSE root then the announcements page to seed cookies — without
        # these, PDF downloads on www.bseindia.com return an HTML stub.
        s.get(BSE_ROOT_URL, timeout=REQUEST_TIMEOUT)
        s.get(BSE_HOME_URL, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Cookie warm-up call to BSE failed: %s — continuing anyway", exc)
    _session = s
    return s


def reset_session() -> None:
    """Drop the cached Session so the next request re-warms cookies from scratch."""
    global _session
    _session = None
    logger.info("BSE session reset — cookies will be re-warmed on next request")


def _request_with_retry(method: str, url: str, **kwargs: Any) -> requests.Response:
    """HTTP request with exponential backoff; re-warm cookies on 403/429."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        session = _get_session()
        try:
            resp = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if resp.status_code in (401, 403, 429):
                # Anti-bot/cookie gate or rate limit — back off and re-warm.
                wait = 2 ** attempt
                logger.warning(
                    "BSE %s returned %d (attempt %d/%d) — re-warming, retry in %ds",
                    url, resp.status_code, attempt, MAX_RETRIES, wait,
                )
                reset_session()
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                raise requests.HTTPError(f"server {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.RequestException,) as exc:
            last_exc = exc
            # A non-auth 4xx (e.g. 404 for an expired AttachLive PDF) will never
            # succeed on retry — fail fast instead of burning the backoff budget.
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
    raise requests.HTTPError(f"BSE request to {url} exhausted retries (auth/rate gate)")


def _extract_table(resp: requests.Response) -> list[dict[str, Any]] | None:
    """Return the 'Table' rows, or None if the response is unusable/degenerate.

    A legitimately empty day is ``{"Table": []}`` (a list) and returns ``[]`` —
    NOT None — so we don't pointlessly hit the fallback endpoint on quiet ticks.
    None is reserved for a bare-string body ("No Record Found!") or the
    ``{"Table":[{"Column1":1}]}`` sentinel BSE sometimes emits.
    """
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    table = data.get("Table")
    if not isinstance(table, list):
        return None
    if len(table) == 1 and "Column1" in table[0] and "NEWSID" not in table[0]:
        return None  # degenerate sentinel row
    return table


def fetch_announcements(scrip_code: str, days_back: int = 2) -> list[dict[str, Any]]:
    """Return ALL-category announcement rows for a scrip over the last days_back.

    Fetches strCat="-1" (every category) — not just "Result" — so the
    board-meeting-OUTCOME row, where the results PDF first appears (often minutes
    before the dedicated "Result" filing), is included. is_quarterly_result then
    filters the non-result rows that all-category fetching pulls in.
    """
    today = datetime.now().date()
    start = today - timedelta(days=days_back)
    params = {
        "pageno": 1,
        "strCat": "-1",
        "strPrevDate": start.strftime("%Y%m%d"),
        "strToDate": today.strftime("%Y%m%d"),
        "strScrip": str(scrip_code),
        "strSearch": "P",
        "strType": "C",
        "subcategory": "-1",
    }
    logger.info("Fetching BSE announcements scrip=%s range=%s..%s (all categories)",
                scrip_code, params["strPrevDate"], params["strToDate"])
    resp = _request_with_retry("GET", ANN_API_URL, params=params, headers=DEFAULT_HEADERS)
    table = _extract_table(resp)
    if table is None:
        logger.warning("BSE primary endpoint gave an unusable response for scrip %s "
                       "— retrying via AnnGetData", scrip_code)
        resp = _request_with_retry("GET", FALLBACK_ANN_API_URL, params=params,
                                   headers=DEFAULT_HEADERS)
        table = _extract_table(resp)
    if table is None:
        logger.error("BSE returned no usable announcements for scrip %s", scrip_code)
        return []
    logger.info("BSE returned %d announcements for scrip %s", len(table), scrip_code)
    return table


def is_quarterly_result(announcement: dict[str, Any]) -> bool:
    """True only for a genuine quarterly/annual RESULTS disclosure.

    Tight by design because strCat="-1" pulls every category. Two classes of row
    REFERENCE results in their body text but are NOT a results filing, so they are
    excluded explicitly:
      - trading-window closures (category "Insider Trading / SAST"; their notice
        says "...48 hours after the financial results...");
      - board-meeting INTIMATIONS (the pre-meeting notice "to consider and approve
        the financial results") — firing on these carries no numbers AND would
        wrongly satisfy the per-company-per-day dedupe, suppressing the real
        OUTCOME row that follows.
    """
    category = str(announcement.get("CATEGORYNAME", "")).lower()
    subject = " ".join(
        str(announcement.get(k, "")) for k in ("HEADLINE", "NEWSSUB", "SUBCATNAME", "MORE")
    ).lower()
    if "insider" in category or "trading window" in subject:
        return False
    if "intimation" in subject and "outcome" not in subject:
        return False
    return any(p in subject for p in RESULT_PHRASES)


def is_board_meeting_outcome(announcement: dict[str, Any]) -> bool:
    """True for a generic 'Outcome of Board Meeting' row, regardless of results wording.

    Widened candidate for HOT (results-day) stocks only: a board-meeting outcome
    can carry the results in its attachment even when the headline omits explicit
    results language (some companies post a bare 'Outcome of Board Meeting'). The
    poller's PDF content gate then decides whether it is really a result, so this
    stays permissive. Pre-meeting intimations and trading-window rows are excluded.
    """
    category = str(announcement.get("CATEGORYNAME", "")).lower()
    subject = " ".join(
        str(announcement.get(k, "")) for k in ("HEADLINE", "NEWSSUB", "SUBCATNAME", "MORE")
    ).lower()
    if "insider" in category or "trading window" in subject:
        return False
    if "intimation" in subject and "outcome" not in subject:
        return False
    return "board meeting" in category or "outcome of board meeting" in subject


def download_pdf(attachment_name: str, save_dir: Path) -> Path:
    """Download a BSE attachment PDF and return the saved local path."""
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    url = PDF_BASE_URL + attachment_name
    dest = save_dir / attachment_name
    logger.info("Downloading PDF %s", url)
    resp = _request_with_retry("GET", url, stream=True)

    # BSE sometimes returns a small HTML stub when cookies/session are missing.
    # Detect that by sniffing the PDF magic prefix instead of writing garbage.
    head = resp.raw.read(8, decode_content=True) if hasattr(resp, "raw") else b""
    if not head.startswith(PDF_MAGIC):
        ctype = resp.headers.get("Content-Type", "?")
        raise ValueError(
            f"BSE returned non-PDF content for {attachment_name} "
            f"(content-type={ctype}, prefix={head!r})"
        )

    with dest.open("wb") as fh:
        fh.write(head)
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                fh.write(chunk)
    logger.info("Saved PDF to %s (%d bytes)", dest, dest.stat().st_size)
    return dest


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Standalone smoke test: fetch announcements for Reliance over the last 14 days
    rows = fetch_announcements("500325", days_back=14)
    print(f"Total announcements: {len(rows)}")
    for row in rows[:5]:
        headline = row.get("HEADLINE") or row.get("NEWSSUB") or "(no headline)"
        print(f"- {row.get('NEWS_DT', '?')} | {row.get('NEWSID', '?')} | "
              f"result={is_quarterly_result(row)} | {headline[:120]}")
