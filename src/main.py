"""Orchestrate the daily BSE-results scan: fetch → extract → compare → notify."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from src import bse_client, pdf_extractor, storage
from src.ai_comparator import generate_comparison_report
from src.telegram_sender import send_document, send_message

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
PDF_DIR = DATA_DIR / "pdfs"

WATCHLIST_PATH = CONFIG_DIR / "watchlist.json"
ESTIMATES_PATH = CONFIG_DIR / "estimates.json"

DEFAULT_DAYS_BACK = 2

logger = logging.getLogger("bse_bot")


def _configure_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"bot_{datetime.now().strftime('%Y-%m-%d')}.log"
    fmt = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)


def _format_estimates(entry: dict[str, Any] | None) -> str:
    if not entry:
        return ""
    lines = [f"Quarter: {entry.get('quarter', 'N/A')}"]
    for key, label in (
        ("revenue_cr", "Revenue (cr)"),
        ("ebitda_cr", "EBITDA (cr)"),
        ("pat_cr", "PAT (cr)"),
        ("eps", "EPS"),
    ):
        if key in entry and entry[key] is not None:
            lines.append(f"{label}: {entry[key]}")
    if entry.get("notes"):
        lines.append(f"Notes: {entry['notes']}")
    return "\n".join(lines)


def _process_stock(
    stock: dict[str, Any],
    estimates: dict[str, Any],
    days_back: int,
    dry_run: bool,
) -> dict[str, int]:
    """Process a single watchlist stock; returns counters for the run summary."""
    counters = {"new_filings": 0, "reports_sent": 0}
    name = stock.get("name") or stock.get("nse_symbol") or stock.get("scrip", "?")
    scrip = str(stock.get("scrip", "")).strip()
    if not scrip:
        logger.warning("Skipping %s — no scrip code", name)
        return counters

    announcements = bse_client.fetch_announcements(scrip, days_back=days_back)
    results = [a for a in announcements if bse_client.is_quarterly_result(a)]
    logger.info("%s: %d announcements, %d look like results", name, len(announcements), len(results))

    for ann in results:
        filing_id = str(ann.get("NEWSID") or ann.get("NEWS_ID") or "")
        if not filing_id:
            logger.warning("%s: announcement missing NEWSID, skipping", name)
            continue
        if storage.is_seen(filing_id):
            logger.info("%s: filing %s already seen — skipping", name, filing_id)
            continue

        attachment = ann.get("ATTACHMENTNAME") or ann.get("ATTACHMENT_NAME") or ""
        if not attachment:
            logger.warning("%s: filing %s has no attachment, marking seen", name, filing_id)
            storage.mark_seen(filing_id)
            continue

        counters["new_filings"] += 1
        try:
            pdf_path = bse_client.download_pdf(attachment, PDF_DIR)
        except Exception as exc:  # noqa: BLE001 — network can throw many things
            logger.exception("%s: PDF download failed for %s: %s", name, attachment, exc)
            continue

        text = pdf_extractor.extract_text(pdf_path)
        if not text:
            logger.warning("%s: no text extracted from %s — skipping AI step", name, pdf_path.name)
            continue

        estimates_text = _format_estimates(estimates.get(name))
        report = generate_comparison_report(name, text, estimates_text)
        if not report:
            logger.error("%s: empty report — not sending", name)
            continue

        message = f"[{name} — BSE result]\n{report}"
        if dry_run:
            logger.info("%s: DRY-RUN — would send Telegram message:\n%s", name, message)
            print("\n" + "=" * 60)
            print(message)
            print("=" * 60 + "\n")
            storage.mark_seen(filing_id)
            counters["reports_sent"] += 1
            continue

        if send_message(message):
            send_document(pdf_path, caption=f"{name} — source filing ({pdf_path.name})")
            storage.mark_seen(filing_id)
            counters["reports_sent"] += 1
            logger.info("%s: report + PDF sent and filing %s marked seen", name, filing_id)
        else:
            logger.error("%s: Telegram send failed for filing %s — NOT marking seen",
                         name, filing_id)

    return counters


def run(days_back: int = DEFAULT_DAYS_BACK, dry_run: bool = False) -> dict[str, int]:
    """Run a single scan across all watchlist stocks."""
    _configure_logging()
    logger.info("=== BSE bot run start (dry_run=%s, days_back=%d) ===", dry_run, days_back)

    watchlist = storage.load_json(WATCHLIST_PATH, {"stocks": []}).get("stocks", [])
    estimates = storage.load_json(ESTIMATES_PATH, {})
    logger.info("Loaded %d stocks, %d estimate entries", len(watchlist), len(estimates))

    totals = {"stocks_checked": 0, "new_filings": 0, "reports_sent": 0, "errors": 0}
    for stock in watchlist:
        totals["stocks_checked"] += 1
        try:
            c = _process_stock(stock, estimates, days_back=days_back, dry_run=dry_run)
            totals["new_filings"] += c["new_filings"]
            totals["reports_sent"] += c["reports_sent"]
        except Exception as exc:  # noqa: BLE001 — keep loop alive on any per-stock failure
            totals["errors"] += 1
            logger.exception("Unhandled error processing %s: %s", stock.get("name", "?"), exc)

    summary = (
        f"Run summary: {totals['stocks_checked']} stocks checked | "
        f"{totals['new_filings']} new filings | "
        f"{totals['reports_sent']} reports sent | "
        f"{totals['errors']} errors"
    )
    logger.info(summary)
    print(summary)
    return totals


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BSE quarterly-results AI comparison bot")
    p.add_argument("--dry-run", action="store_true",
                   help="Print reports instead of sending Telegram messages")
    p.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK,
                   help="Lookback window in days for BSE announcements")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    run(days_back=args.days_back, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
