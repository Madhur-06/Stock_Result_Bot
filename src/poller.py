"""Long-running dual-exchange poller for quarterly-results filings.

Turns the one-shot scanner in main.py into a continuously running process that
watches BSE and NSE *concurrently* and delivers the Telegram report within
~10-15s of an exchange disseminating a result.

Architecture (detection decoupled from processing):

    BSE poll thread ┐                              ┌─ worker thread(s)
                    ├─ detect → download PDF ──────→ Queue ─→ extract → OpenAI → Telegram
    NSE poll thread ┘   (fast path, per exchange)            (slow ~5s path, off the loop)

A slow OpenAI call can never delay detection of the next filing because the
detection threads only download the PDF (1-3s) and hand a Job to the queue.

Dedupe is three-layered so we send exactly one report per company per day and a
restart never re-sends:
  1. in-memory per-exchange "seen rows" (row id) — don't re-handle a row each tick;
     NOT persisted, so a crash before send never permanently loses an unsent filing.
  2. in-memory company "claim" (name|YYYYMMDD, under a lock) — the first exchange to
     spot a company's result claims it; the other exchange's duplicate is dropped.
  3. persistent company "seen" via storage.mark_seen(name|YYYYMMDD), written only
     AFTER a successful Telegram send — survives restarts.

Cadence: stocks whose watchlist entry has expected_results_date == today (IST) are
tight-polled (~2.5s); everything else gets the ~10s baseline. Errors are isolated
per stock/exchange and never kill the loop; 403/429 backs off and re-warms.
"""
from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src import bse_client, nse_client, pdf_extractor, storage
from src.ai_comparator import generate_comparison_report
from src.main import (
    DATA_DIR,
    ESTIMATES_PATH,
    PDF_DIR,
    WATCHLIST_PATH,
    _configure_logging,
    _format_estimates,
)
from src.telegram_sender import send_document, send_message

logger = logging.getLogger("bse_bot.poller")

POLLER_CONFIG_PATH = WATCHLIST_PATH.parent / "poller.json"

DEFAULTS = {
    "timezone": "Asia/Kolkata",
    "market_open": "09:00",
    "market_close": "16:30",
    "weekdays_only": True,
    "baseline_interval_sec": 10.0,
    "tight_interval_sec": 2.5,
    "off_hours_check_sec": 60.0,
    "worker_threads": 1,
    "enable_bse": True,
    "enable_nse": True,
    "ignore_market_hours": False,
    # Freshness gate: only act on a filing disseminated recently. 0 means
    # "today's IST date only" (the NSE API returns full history, so without this
    # the poller would fire reports for LAST quarter's results on startup). A
    # positive value switches to a rolling age window of that many minutes —
    # useful for replaying a recent real filing in a test.
    "max_filing_age_min": 0,
    # Auto-hot: once per day (and on startup) check each stock's NSE board-meeting
    # intimations; if a results board meeting is scheduled for today, tight-poll it
    # automatically — no manual expected_results_date needed.
    "auto_hot": True,
    "hot_refresh_check_sec": 300.0,
}

_SHUTDOWN = object()  # sentinel pushed onto the queue to stop workers


# --------------------------------------------------------------------------- #
# Job + shared state
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    """A detected filing whose PDF is already downloaded, awaiting AI + send."""
    company: str
    company_key: str          # name|YYYYMMDD — the cross-exchange dedupe key
    exchange: str             # "BSE" / "NSE"
    row_id: str
    pdf_path: Path
    headline: str
    detected: datetime
    disseminated: datetime | None
    needs_content_check: bool = False  # widened HOT-stock board-outcome candidate


@dataclass
class SharedState:
    """Cross-thread coordination: the work queue and company-level dedupe."""
    cfg: dict[str, Any]
    estimates: dict[str, Any]
    dry_run: bool
    work_q: "queue.Queue[Job | object]" = field(default_factory=queue.Queue)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _claimed: set[str] = field(default_factory=set)
    _done: set[str] = field(default_factory=set)  # in-memory done (so dry-run dedupes too)
    _hot_lock: threading.Lock = field(default_factory=threading.Lock)
    _hot_names: set[str] = field(default_factory=set)  # auto-detected results-day stocks
    hot_date: str = ""  # IST YYYYMMDD the hot set is valid for ("" = never computed)

    def try_claim(self, company_key: str) -> bool:
        """Atomically claim a company for today. False if already done or claimed."""
        with self._lock:
            if (company_key in self._claimed or company_key in self._done
                    or storage.is_seen(company_key)):
                return False
            self._claimed.add(company_key)
            return True

    def release(self, company_key: str) -> None:
        """Release an in-memory claim so the filing can be retried."""
        with self._lock:
            self._claimed.discard(company_key)

    def mark_done(self, company_key: str) -> None:
        """Mark the company done after a successful send.

        Always tracked in memory (cross-exchange dedupe within this run); only
        persisted to disk in live mode, so a --dry-run never pollutes the real
        seen store and suppresses a genuine filing later.
        """
        with self._lock:
            self._done.add(company_key)
            self._claimed.discard(company_key)
        if not self.dry_run:
            storage.mark_seen(company_key)

    def is_hot_name(self, name: str) -> bool:
        """True if the auto-hot detector flagged this stock for today."""
        with self._hot_lock:
            return name in self._hot_names

    def set_hot(self, names: set[str], date_str: str) -> None:
        """Replace the auto-hot set (called once per day by the refresher)."""
        with self._hot_lock:
            self._hot_names = set(names)
            self.hot_date = date_str


# --------------------------------------------------------------------------- #
# Per-exchange adapters — normalise BSE vs NSE row shapes
# --------------------------------------------------------------------------- #

@dataclass
class ExchangeAdapter:
    name: str
    stock_key: str                                  # watchlist field with the id
    fetch: Callable[[str], list[dict[str, Any]]]
    is_result: Callable[[dict[str, Any]], bool]
    row_id: Callable[[dict[str, Any]], str]
    disseminated: Callable[[dict[str, Any]], datetime | None]
    headline: Callable[[dict[str, Any]], str]
    download: Callable[[dict[str, Any], Path], Path]
    is_board_outcome: Callable[[dict[str, Any]], bool]


def _parse_bse_dt(row: dict[str, Any], tz: ZoneInfo) -> datetime | None:
    # BSE: ISO-ish naive "2026-05-29T21:31:44.82" in DissemDT/NEWS_DT.
    raw = str(row.get("DissemDT") or row.get("NEWS_DT") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=tz)
    except ValueError:
        return None


def _parse_nse_dt(row: dict[str, Any], tz: ZoneInfo) -> datetime | None:
    # NSE: naive "30-Jun-2026 12:52:56" in exchdisstime (fall back to an_dt).
    raw = str(row.get("exchdisstime") or row.get("an_dt") or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d-%b-%Y %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None


def _build_adapters(tz: ZoneInfo, cfg: dict[str, Any]) -> list[ExchangeAdapter]:
    adapters: list[ExchangeAdapter] = []
    if cfg["enable_bse"]:
        adapters.append(ExchangeAdapter(
            name="BSE",
            stock_key="scrip",
            fetch=lambda code: bse_client.fetch_announcements(code, days_back=1),
            is_result=bse_client.is_quarterly_result,
            row_id=lambda r: str(r.get("NEWSID") or r.get("NEWS_ID") or ""),
            disseminated=lambda r: _parse_bse_dt(r, tz),
            headline=lambda r: str(r.get("HEADLINE") or r.get("NEWSSUB") or ""),
            download=lambda r, d: bse_client.download_pdf(
                str(r.get("ATTACHMENTNAME") or r.get("ATTACHMENT_NAME") or ""), d),
            is_board_outcome=bse_client.is_board_meeting_outcome,
        ))
    if cfg["enable_nse"]:
        adapters.append(ExchangeAdapter(
            name="NSE",
            stock_key="nse_symbol",
            fetch=nse_client.fetch_announcements,
            is_result=nse_client.is_quarterly_result,
            row_id=lambda r: str(r.get("seq_id") or ""),
            disseminated=lambda r: _parse_nse_dt(r, tz),
            headline=lambda r: str(r.get("desc") or r.get("attchmntText") or ""),
            download=lambda r, d: nse_client.download_pdf(str(r.get("attchmntFile") or ""), d),
            is_board_outcome=nse_client.is_board_meeting_outcome,
        ))
    return adapters


# --------------------------------------------------------------------------- #
# Market hours
# --------------------------------------------------------------------------- #

def _parse_hhmm(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


def _within_market_hours(cfg: dict[str, Any], tz: ZoneInfo) -> bool:
    if cfg["ignore_market_hours"]:
        return True
    now = datetime.now(tz)
    if cfg["weekdays_only"] and now.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return _parse_hhmm(cfg["market_open"]) <= now.time() <= _parse_hhmm(cfg["market_close"])


def _today_ist(tz: ZoneInfo) -> str:
    return datetime.now(tz).strftime("%Y%m%d")


def _stock_name(stock: dict[str, Any]) -> str:
    """Canonical display/dedupe name for a watchlist entry (matches main.py)."""
    return (stock.get("name") or stock.get("nse_symbol")
            or stock.get("scrip") or "?")


def _is_fresh(disseminated: datetime | None, tz: ZoneInfo, cfg: dict[str, Any]) -> bool:
    """True if a filing is recent enough to act on (guards against stale history).

    Unparseable timestamps are treated as NOT fresh: both exchanges supply a
    reliable dissemination time, so a missing one is more likely a junk row than
    a real new result — better to skip than to fire on last quarter's filing.
    """
    if disseminated is None:
        return False
    age_min = cfg.get("max_filing_age_min", 0) or 0
    now = datetime.now(tz)
    if age_min > 0:
        return (now - disseminated).total_seconds() <= age_min * 60
    return disseminated.date() == now.date()


# --------------------------------------------------------------------------- #
# Producer: one ExchangePoller per exchange
# --------------------------------------------------------------------------- #

class ExchangePoller(threading.Thread):
    """Polls one exchange across the watchlist; downloads + enqueues new results."""

    def __init__(self, adapter: ExchangeAdapter, watchlist: list[dict[str, Any]],
                 shared: SharedState, tz: ZoneInfo, stop: threading.Event) -> None:
        super().__init__(name=f"poller-{adapter.name}", daemon=True)
        self.adapter = adapter
        self.watchlist = watchlist
        self.shared = shared
        self.tz = tz
        self.stop = stop
        self.cfg = shared.cfg
        self._seen_rows: set[str] = set()        # row ids handled this process
        self._next_due: dict[str, float] = {}    # stock-id -> monotonic due time

    # -- cadence helpers ----------------------------------------------------- #

    def _is_hot(self, stock: dict[str, Any]) -> bool:
        """True if today is this stock's results day → tight-poll it.

        Two independent triggers: a manual ``expected_results_date`` override in
        watchlist.json, OR the auto-hot detector (NSE board-meeting intimation for
        today). Either one is enough.
        """
        want = str(stock.get("expected_results_date") or "").strip()
        if want and want.replace("-", "") == _today_ist(self.tz):  # YYYY-MM-DD or YYYYMMDD
            return True
        return self.shared.is_hot_name(_stock_name(stock))

    def _interval(self, stock: dict[str, Any]) -> float:
        return (self.cfg["tight_interval_sec"] if self._is_hot(stock)
                else self.cfg["baseline_interval_sec"])

    # -- main loop ----------------------------------------------------------- #

    def run(self) -> None:
        logger.info("[%s] poller started (%d stocks)", self.adapter.name, len(self.watchlist))
        while not self.stop.is_set():
            if not _within_market_hours(self.cfg, self.tz):
                self.stop.wait(self.cfg["off_hours_check_sec"])
                continue
            now = time.monotonic()
            for stock in self.watchlist:
                if self.stop.is_set():
                    break
                sid = str(stock.get(self.adapter.stock_key) or "")
                if not sid:
                    continue
                if self._next_due.get(sid, 0.0) > now:
                    continue
                try:
                    self._poll_stock(stock, sid)
                except Exception:  # noqa: BLE001 — one stock must never kill the loop
                    logger.exception("[%s] unhandled error polling %s",
                                     self.adapter.name, stock.get("name", sid))
                self._next_due[sid] = time.monotonic() + self._interval(stock)
            self._sleep_until_next_due()
        logger.info("[%s] poller stopped", self.adapter.name)

    def _sleep_until_next_due(self) -> None:
        if not self._next_due:
            self.stop.wait(1.0)
            return
        gap = max(0.2, min(self._next_due.values()) - time.monotonic())
        self.stop.wait(min(gap, 1.0))  # cap so shutdown / market-close stays responsive

    # -- per-stock detection ------------------------------------------------- #

    def _poll_stock(self, stock: dict[str, Any], sid: str) -> None:
        name = _stock_name(stock)
        hot = self._is_hot(stock)
        rows = self.adapter.fetch(sid)
        # Strict results (explicit results wording in the headline) always qualify.
        # For a HOT stock we ALSO admit a bare board-meeting-outcome row: its
        # attachment may carry the results even when the headline omits results
        # language. Such loose candidates are flagged for the worker's PDF content
        # gate, which rejects non-results PDFs (e.g. a change-in-management
        # outcome). Non-hot stocks keep the tight headline-only filter.
        candidates: list[tuple[dict[str, Any], bool]] = []
        for r in rows:
            if self.adapter.is_result(r):
                candidates.append((r, False))
            elif hot and self.adapter.is_board_outcome(r):
                candidates.append((r, True))
        if not candidates:
            return
        for row, needs_check in candidates:
            if self.stop.is_set():
                return
            rid = self.adapter.row_id(row)
            tagged = f"{self.adapter.name}:{rid}"
            if not rid or tagged in self._seen_rows:
                continue

            disseminated = self.adapter.disseminated(row)
            if not _is_fresh(disseminated, self.tz, self.cfg):
                # Stale historical filing (e.g. last quarter) — never act on it,
                # and remember it so we don't re-evaluate it every tick.
                self._seen_rows.add(tagged)
                continue

            company_key = f"{name}|{_today_ist(self.tz)}"
            if not self.shared.try_claim(company_key):
                # Already delivered today or claimed by the other exchange — drop.
                self._seen_rows.add(tagged)
                continue

            detected = datetime.now(self.tz)
            attachment = self.adapter.headline(row)

            # Fast path: download the PDF immediately, then hand off to a worker.
            try:
                pdf_path = self.adapter.download(row, PDF_DIR)
            except Exception as exc:  # noqa: BLE001 — network/HTML-stub can throw
                # A 4xx (e.g. expired-PDF 404) is expected — log cleanly; only
                # dump a stack trace for genuinely unexpected failures.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None:
                    logger.error("[%s] %s: PDF download failed: HTTP %s",
                                 self.adapter.name, name, status)
                else:
                    logger.exception("[%s] %s: PDF download failed", self.adapter.name, name)
                self.shared.release(company_key)  # let it retry next tick (rid unseen)
                continue

            self._seen_rows.add(tagged)
            lag = ((detected - disseminated).total_seconds()
                   if disseminated else None)
            logger.info(
                "[%s] DETECTED %s%s | disseminated=%s detect-lag=%s | %s",
                self.adapter.name, name,
                " (board-outcome, hot — content-gated)" if needs_check else "",
                disseminated.isoformat() if disseminated else "?",
                f"{lag:.1f}s" if lag is not None else "?",
                attachment[:80],
            )
            self.shared.work_q.put(Job(
                company=name, company_key=company_key, exchange=self.adapter.name,
                row_id=rid, pdf_path=pdf_path, headline=attachment,
                detected=detected, disseminated=disseminated,
                needs_content_check=needs_check,
            ))


# --------------------------------------------------------------------------- #
# Consumer: worker drains the queue (extract → OpenAI → Telegram)
# --------------------------------------------------------------------------- #

class Worker(threading.Thread):
    def __init__(self, shared: SharedState, tz: ZoneInfo, idx: int) -> None:
        super().__init__(name=f"worker-{idx}", daemon=True)
        self.shared = shared
        self.tz = tz

    def run(self) -> None:
        while True:
            item = self.shared.work_q.get()
            try:
                if item is _SHUTDOWN:
                    return
                assert isinstance(item, Job)
                self._process(item)
            except Exception:  # noqa: BLE001 — a bad job must never kill the worker
                logger.exception("worker: unhandled error processing job")
            finally:
                self.shared.work_q.task_done()

    def _process(self, job: Job) -> None:
        text = pdf_extractor.extract_text(job.pdf_path)
        if not text:
            logger.warning("%s [%s]: no text extracted from %s — releasing claim",
                           job.company, job.exchange, job.pdf_path.name)
            self.shared.release(job.company_key)  # other exchange may still deliver
            return

        if job.needs_content_check and not pdf_extractor.looks_like_results(text):
            # A HOT stock's widened board-outcome candidate whose PDF has no P&L /
            # results statement (e.g. change-in-management). Not a result — skip it,
            # but release the claim so a genuine results filing later today can
            # still be processed. The row is already in _seen_rows, so it won't
            # re-trigger a download.
            logger.info("%s [%s]: board-outcome PDF has no results statement — "
                        "not a result, skipping", job.company, job.exchange)
            self.shared.release(job.company_key)
            return

        estimates_text = _format_estimates(self.shared.estimates.get(job.company))
        report = generate_comparison_report(job.company, text, estimates_text)
        if not report:
            logger.error("%s [%s]: empty AI report — releasing claim",
                         job.company, job.exchange)
            self.shared.release(job.company_key)
            return

        message = f"[{job.company} — {job.exchange} result]\n{report}"
        if self.shared.dry_run:
            self.shared.mark_done(job.company_key)
            self._log_latency(job, "DRY-RUN")
            print("\n" + "=" * 60)
            print(message)
            print("=" * 60 + "\n")
            return

        if send_message(message):
            send_document(job.pdf_path,
                          caption=f"{job.company} — {job.exchange} source filing "
                                  f"({job.pdf_path.name})")
            self.shared.mark_done(job.company_key)
            self._log_latency(job, "SENT")
        else:
            logger.error("%s [%s]: Telegram send failed — releasing claim (NOT marked done)",
                         job.company, job.exchange)
            self.shared.release(job.company_key)

    def _log_latency(self, job: Job, tag: str) -> None:
        sent = datetime.now(self.tz)
        if job.disseminated:
            e2e = (sent - job.disseminated).total_seconds()
            logger.info("%s %s [%s] | disseminated=%s → %s in %.1fs (end-to-end)",
                        tag, job.company, job.exchange,
                        job.disseminated.isoformat(), tag.lower(), e2e)
        else:
            since_detect = (sent - job.detected).total_seconds()
            logger.info("%s %s [%s] | (no disseminated ts) processed %.1fs after detect",
                        tag, job.company, job.exchange, since_detect)


# --------------------------------------------------------------------------- #
# Auto-hot detector: tight-poll stocks holding a results board meeting today
# --------------------------------------------------------------------------- #

def _compute_hot_set(watchlist: list[dict[str, Any]], tz: ZoneInfo) -> set[str]:
    """Return the names of stocks whose results board meeting is scheduled today.

    Source is NSE's board-meeting feed (Reg 29 intimations, filed 1-2 days ahead),
    keyed by nse_symbol — a board meeting is a company event intimated to both
    exchanges, so this date is authoritative regardless of which exchange later
    carries the filing. One stock failing must not abort the others.
    """
    today = datetime.now(tz).date()
    hot: set[str] = set()
    for stock in watchlist:
        sym = str(stock.get("nse_symbol") or "").strip()
        if not sym:
            continue  # BSE-only stock — relies on manual expected_results_date
        try:
            meetings = nse_client.fetch_board_meetings(sym)
        except Exception:  # noqa: BLE001 — one symbol must not break the sweep
            logger.exception("auto-hot: board-meeting fetch failed for %s", sym)
            continue
        for m in meetings:
            if (nse_client.board_meeting_date(m) == today
                    and nse_client.is_results_board_meeting(m)):
                hot.add(_stock_name(stock))
                break
    return hot


class HotSetRefresher(threading.Thread):
    """Recomputes the auto-hot set on startup and once per IST day."""

    def __init__(self, watchlist: list[dict[str, Any]], shared: SharedState,
                 tz: ZoneInfo, stop: threading.Event) -> None:
        super().__init__(name="hot-refresher", daemon=True)
        self.watchlist = watchlist
        self.shared = shared
        self.tz = tz
        self.stop = stop

    def run(self) -> None:
        check = float(self.shared.cfg.get("hot_refresh_check_sec", 300.0))
        while not self.stop.is_set():
            today = _today_ist(self.tz)
            if self.shared.hot_date != today:  # startup ("") or a new day
                try:
                    hot = _compute_hot_set(self.watchlist, self.tz)
                    self.shared.set_hot(hot, today)
                    logger.info("Auto-hot set for %s: %s (tight-poll %ss)",
                                today, sorted(hot) or "(none)",
                                self.shared.cfg["tight_interval_sec"])
                except Exception:  # noqa: BLE001 — never let the refresher die
                    logger.exception("auto-hot: refresh failed — keeping previous set")
                    self.shared.set_hot(set(), today)  # avoid hammering on a bad day
            self.stop.wait(check)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _load_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(storage.load_json(POLLER_CONFIG_PATH, {}) or {})
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


def run_forever(dry_run: bool = False, overrides: dict[str, Any] | None = None) -> int:
    """Start the dual poller and run until Ctrl+C."""
    _configure_logging()
    cfg = _load_config(overrides)
    tz = ZoneInfo(cfg["timezone"])
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    watchlist = storage.load_json(WATCHLIST_PATH, {"stocks": []}).get("stocks", [])
    estimates = storage.load_json(ESTIMATES_PATH, {})
    shared = SharedState(cfg=cfg, estimates=estimates, dry_run=dry_run)

    adapters = _build_adapters(tz, cfg)
    if not adapters:
        logger.error("Both exchanges disabled — nothing to poll. Exiting.")
        return 1

    logger.info("=== Poller starting (dry_run=%s) exchanges=%s stocks=%d "
                "baseline=%ss tight=%ss auto_hot=%s hours=%s-%s %s ===",
                dry_run, [a.name for a in adapters], len(watchlist),
                cfg["baseline_interval_sec"], cfg["tight_interval_sec"],
                cfg.get("auto_hot", True),
                cfg["market_open"], cfg["market_close"], cfg["timezone"])

    stop = threading.Event()
    workers = [Worker(shared, tz, i) for i in range(max(1, int(cfg["worker_threads"])))]
    pollers = [ExchangePoller(a, watchlist, shared, tz, stop) for a in adapters]
    refresher = (HotSetRefresher(watchlist, shared, tz, stop)
                 if cfg.get("auto_hot", True) else None)
    for w in workers:
        w.start()
    if refresher is not None:
        refresher.start()  # computes the initial hot set before stocks go tight
    for p in pollers:
        p.start()

    try:
        while any(p.is_alive() for p in pollers):
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Ctrl+C received — shutting down…")
    finally:
        stop.set()
        for p in pollers:
            p.join(timeout=5)
        if refresher is not None:
            refresher.join(timeout=5)
        for _ in workers:
            shared.work_q.put(_SHUTDOWN)
        for w in workers:
            w.join(timeout=10)
    logger.info("=== Poller stopped ===")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dual BSE+NSE quarterly-results poller")
    p.add_argument("--dry-run", action="store_true",
                   help="Print reports instead of sending Telegram messages")
    p.add_argument("--baseline", type=float, dest="baseline_interval_sec",
                   help="Baseline poll interval in seconds (default 10)")
    p.add_argument("--tight", type=float, dest="tight_interval_sec",
                   help="Tight poll interval for hot stocks in seconds (default 2.5)")
    p.add_argument("--no-bse", action="store_true", help="Disable BSE polling")
    p.add_argument("--no-nse", action="store_true", help="Disable NSE polling")
    p.add_argument("--no-auto-hot", action="store_true",
                   help="Disable NSE board-meeting auto-hot detection "
                        "(rely only on manual expected_results_date)")
    p.add_argument("--ignore-market-hours", action="store_true",
                   help="Poll regardless of time/day (for testing)")
    p.add_argument("--max-age-min", type=float, dest="max_filing_age_min",
                   help="Act on filings up to N minutes old instead of today-only "
                        "(for replaying a recent real filing in a test)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    overrides: dict[str, Any] = {
        "baseline_interval_sec": args.baseline_interval_sec,
        "tight_interval_sec": args.tight_interval_sec,
        "max_filing_age_min": args.max_filing_age_min,
    }
    if args.no_bse:
        overrides["enable_bse"] = False
    if args.no_nse:
        overrides["enable_nse"] = False
    if args.no_auto_hot:
        overrides["auto_hot"] = False
    if args.ignore_market_hours:
        overrides["ignore_market_hours"] = True
    return run_forever(dry_run=args.dry_run, overrides=overrides)


if __name__ == "__main__":
    sys.exit(main())
