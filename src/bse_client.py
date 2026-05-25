"""BSE corporate-filings API client."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

ANN_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
PDF_BASE_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
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
RESULT_KEYWORDS = ("result", "financial")
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
        # Visit the BSE announcements page to seed cookies — without these,
        # PDF downloads on www.bseindia.com return an HTML stub.
        s.get(BSE_HOME_URL, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Cookie warm-up call to BSE failed: %s — continuing anyway", exc)
    _session = s
    return s


def _request_with_retry(method: str, url: str, **kwargs: Any) -> requests.Response:
    """HTTP request with exponential backoff for transient failures."""
    session = _get_session()
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"server {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.RequestException,) as exc:
            last_exc = exc
            wait = 2 ** (attempt - 1)
            logger.warning("Request to %s failed (attempt %d/%d): %s — retry in %ds",
                           url, attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def fetch_announcements(scrip_code: str, days_back: int = 2) -> list[dict[str, Any]]:
    """Return the 'Table' array of result-category announcements from BSE."""
    today = datetime.now().date()
    start = today - timedelta(days=days_back)
    params = {
        "pageno": 1,
        "strCat": "Result",
        "strPrevDate": start.strftime("%Y%m%d"),
        "strToDate": today.strftime("%Y%m%d"),
        "strScrip": str(scrip_code),
        "strSearch": "P",
        "strType": "C",
        "subcategory": "-1",
    }
    logger.info("Fetching BSE announcements scrip=%s range=%s..%s",
                scrip_code, params["strPrevDate"], params["strToDate"])
    resp = _request_with_retry("GET", ANN_API_URL, params=params, headers=DEFAULT_HEADERS)
    try:
        data = resp.json()
    except ValueError as exc:
        logger.error("BSE returned non-JSON for scrip %s: %s", scrip_code, exc)
        return []
    table = data.get("Table") or []
    logger.info("BSE returned %d announcements for scrip %s", len(table), scrip_code)
    return table


def is_quarterly_result(announcement: dict[str, Any]) -> bool:
    """True if the announcement headline mentions results/financials."""
    headline = " ".join(
        str(announcement.get(k, "")) for k in ("HEADLINE", "NEWSSUB", "SUBCATNAME", "MORE")
    ).lower()
    return any(kw in headline for kw in RESULT_KEYWORDS)


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
