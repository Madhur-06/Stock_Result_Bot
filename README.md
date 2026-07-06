# BSE / NSE Quarterly-Results Alert Bot

A lightweight Python automation that monitors the **Bombay Stock Exchange (BSE)**
and **National Stock Exchange (NSE)** for quarterly-results filings from a custom
watchlist, extracts the reported figures from the filed PDF, uses **OpenAI** to
compare them against your stored expectations, and delivers a concise verdict
(**STRONG / WEAK / MIXED**, with an optional **BEAT / MISS / IN-LINE** call
against estimates) to your personal **Telegram**.

Designed for personal, single-user operation — no database, no containers, no web
service. State lives in local JSON files and the process runs from a laptop, a
cron entry, or a small VM.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Environment variables](#environment-variables)
  - [Watchlist](#watchlist-configwatchlistjson)
  - [Estimates](#estimates-configestimatesjson)
  - [Poller settings](#poller-settings-configpollerjson)
- [Usage](#usage)
  - [One-shot scan](#one-shot-scan)
  - [Live poll mode](#live-poll-mode)
  - [Module smoke tests](#module-smoke-tests)
- [How it works](#how-it-works)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [Project structure](#project-structure)
- [Design notes & limitations](#design-notes--limitations)

---

## Features

- **Dual-exchange coverage** — watches BSE and NSE concurrently; a company that
  files to both exchanges produces exactly one report.
- **Low latency** — the live poller delivers a report within seconds of an
  exchange disseminating a result. Detection is decoupled from processing, so a
  slow OpenAI call never delays spotting the next filing.
- **Automatic results-day detection** — reads SEBI Reg. 29 board-meeting
  intimations and tightens the polling cadence for any stock holding a results
  meeting that day. No manual scheduling required.
- **AI-generated digest** — an equity-research-style summary of revenue, EBITDA
  (with margin), PAT, and EPS, including YoY / QoQ deltas and vs-estimate calls.
- **Source PDF attached** — the original filing is forwarded alongside the digest.
- **Resilient by design** — per-stock error isolation, exponential backoff with
  automatic cookie re-warming on anti-bot gates, and restart-safe deduplication.

---

## Architecture

The live poller separates fast detection from slow processing:

```
 BSE poll thread ┐                                    ┌─ worker thread(s)
                 ├─ detect → download PDF ──→ Queue ──→ extract → OpenAI → Telegram
 NSE poll thread ┘   (fast path, per exchange)          (slow path, off the loop)
```

- **Producer threads** (`ExchangePoller`, one per exchange) poll the watchlist,
  identify new results rows, download the PDF, and enqueue a job.
- **Consumer threads** (`Worker`) drain the queue: extract text, call OpenAI, and
  send the Telegram report.
- **Hot-set refresher** (`HotSetRefresher`) recomputes the results-day watchlist
  on startup and once per day.

Because the producer only performs the fast download before handing off, a
multi-second AI call on one filing cannot stall detection of the next.

---

## Requirements

- Python **3.12+** (uses `zoneinfo`; `tzdata` is bundled for Windows)
- An **OpenAI API key**
- A **Telegram bot token** and **chat ID**

---

## Installation

```bash
git clone https://github.com/Madhur-06/Stock_Result_Bot

python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env   # then edit .env with your credentials
```

---

## Configuration

### Environment variables

Copy `.env.example` to `.env` and populate the three values (no quotes, no
trailing spaces):

```dotenv
OPENAI_API_KEY=sk-...
TELEGRAM_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=123456789
```

**Obtaining a Telegram token and chat ID**

1. In Telegram, open a chat with **@BotFather** and send `/newbot`. Follow the
   prompts to receive an HTTP token — set it as `TELEGRAM_TOKEN`.
2. Open a chat with your new bot and send it any message (e.g. `/start`).
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser.
4. Locate `"chat":{"id":<NNNNN>,...}` in the JSON — that number is your
   `TELEGRAM_CHAT_ID`.

### Watchlist (`config/watchlist.json`)

Each entry requires a `name` and at least one exchange identifier. `scrip` is the
BSE 6-digit code; `nse_symbol` is the NSE trading symbol.

```json
{
  "stocks": [
    {
      "name": "Reliance Industries",
      "scrip": "500325",
      "nse_symbol": "RELIANCE"
    }
  ]
}
```

**Finding a BSE scrip code:** open the company page on
[bseindia.com](https://www.bseindia.com/); the URL contains a 6-digit number,
e.g. `.../500325/` → scrip code `500325`.

**Manual results-day override** — force a stock into tight-polling for a specific
date (useful for BSE-only stocks the auto-hot detector cannot see, since it keys
off the NSE board-meeting feed):

```json
{
  "name": "NMDC Ltd",
  "scrip": "526371",
  "nse_symbol": "NMDC",
  "expected_results_date": "2026-07-15"
}
```

### Estimates (`config/estimates.json`)

Optional. Keyed by the exact `name` from the watchlist. When present, the digest
includes vs-estimate deltas and a BEAT/MISS/IN-LINE call; when absent, the bot
produces an actuals-only summary.

```json
{
  "Reliance Industries": {
    "quarter": "Q2 FY26",
    "revenue_cr": 245000,
    "ebitda_cr": 42500,
    "pat_cr": 17200,
    "eps": 25.4,
    "notes": "Street expects margin around 17%, ARPU above 195"
  }
}
```

### Poller settings (`config/poller.json`)

| Key | Default | Description |
|-----|---------|-------------|
| `timezone` | `Asia/Kolkata` | IANA timezone for market hours and freshness checks. |
| `market_open` / `market_close` | `09:00` / `16:30` | Active polling window. |
| `weekdays_only` | `true` | Skip Saturdays and Sundays. |
| `baseline_interval_sec` | `10.0` | Poll cadence for normal stocks. |
| `tight_interval_sec` | `2.5` | Poll cadence for results-day ("hot") stocks. |
| `off_hours_check_sec` | `60.0` | Idle re-check interval outside market hours. |
| `worker_threads` | `1` | Number of processing workers. |
| `enable_bse` / `enable_nse` | `true` | Per-exchange enable flags. |
| `auto_hot` | `true` | Auto-detect results-day stocks via NSE board meetings. |
| `hot_refresh_check_sec` | `300.0` | How often the hot-set refresher re-evaluates the day. |
| `max_filing_age_min` | `0` | Freshness gate: `0` = today only; `N` = last `N` minutes. |

Most keys can be overridden per-run via CLI flags (see below).

---

## Usage

### One-shot scan

A single sweep over the watchlist, then exit — intended for cron.

```bash
python -m src.main                 # run and send reports
python -m src.main --dry-run       # print reports instead of sending
python -m src.main --days-back 14  # widen the BSE lookback window
```

### Live poll mode

A long-running process that watches both exchanges continuously.

```bash
python -m src.main --poll                       # via main.py
python -m src.poller                            # or run the poller directly
python -m src.poller --dry-run                  # print instead of sending
python -m src.poller --dry-run --ignore-market-hours   # poll off-hours (testing)
python -m src.poller --no-nse                   # BSE only
```

**Poller CLI flags**

| Flag | Effect |
|------|--------|
| `--dry-run` | Print reports instead of sending them. |
| `--baseline <sec>` | Override the baseline poll interval. |
| `--tight <sec>` | Override the results-day poll interval. |
| `--no-bse` / `--no-nse` | Disable an exchange. |
| `--no-auto-hot` | Disable NSE board-meeting auto-detection. |
| `--ignore-market-hours` | Poll regardless of time or day (testing). |
| `--max-age-min <N>` | Act on filings up to `N` minutes old instead of today only. |

The poller idles outside market hours and acts only on filings disseminated
**today** — NSE's API returns full history, so this freshness gate prevents it
firing last quarter's results on startup. Use `--max-age-min` to replay a recent
real filing during testing.

### Module smoke tests

Each module can be run standalone to verify a single stage of the pipeline:

```bash
python -m src.bse_client              # fetch recent BSE announcements
python -m src.nse_client              # fetch recent NSE announcements
python -m src.pdf_extractor <file>    # score pages and print extracted text
python -m src.telegram_sender         # send a test message
python -m src.ai_comparator           # run the prompt on sample data
```

---

## How it works

**Detection.** For each stock the poller fetches all announcement categories and
applies a tight results filter that requires explicit results language, excluding
pre-meeting intimations and trading-window notices. On results day a stock is
additionally admitted on a bare board-meeting-outcome row, whose PDF is then
gated by a content check that rejects non-results attachments.

**Results-day cadence.** A stock is tight-polled when either its watchlist entry
carries an `expected_results_date` matching today, or the auto-hot detector finds
a results board meeting scheduled for today in the NSE Reg. 29 feed. The active
hot set is logged on startup and at each daily refresh.

**Deduplication.** Three layers guarantee exactly one report per company per day,
and that a restart never re-sends:

1. In-memory per-exchange row tracking (avoids re-handling a row each tick).
2. An in-memory company claim (`name|YYYYMMDD`) under a lock — the first exchange
   to spot a result claims the company; the other's duplicate is dropped.
3. A persistent company marker written **only after** a successful Telegram send,
   so it survives restarts.

**Latency.** Each detection logs the dissemination timestamp and the end-to-end
latency (disseminated → Telegram sent) so throughput can be verified.

---

## Deployment

**Cron (one-shot mode, macOS / Linux).** Runs every 10 minutes during Indian
market and post-market hours, Monday–Friday:

```cron
*/10 15-20 * * 1-5 cd /path/to/bse-bot && /usr/bin/python3 -m src.main >> logs/cron.log 2>&1
```

Adjust the Python path and hour range for your server's timezone.

**Long-running (poll mode).** Run `python -m src.poller` under a process manager
(`systemd`, `supervisor`, `tmux`, or Task Scheduler on Windows) so it restarts on
failure and survives logout. The poller self-manages market hours and idles
off-hours.

---

## Troubleshooting

| Symptom | Cause & resolution |
|---------|--------------------|
| **BSE 403** | The BSE JSON API rejects requests without a browser `User-Agent` and the `bseindia.com` `Referer`. Both are set in `src/bse_client.py`; preserve them if you swap the HTTP layer. |
| **NSE 401 / 403** | NSE gates its JSON API behind short-lived anti-bot cookies. The client warms them via the homepage and filings page and re-warms automatically on 401/403/429. A persistent 403 usually means the warm-up pages were themselves blocked — retry shortly. |
| **Empty extracted text** | Some filings are scanned images, not text PDFs. Extraction returns empty, a warning is logged, and the AI step is skipped. There is no OCR fallback by design. |
| **"BSE returned non-PDF content"** | BSE's `AttachLive` directory only serves recent filings; older ones return an HTML shell. The client detects this via the `%PDF-` magic prefix. Same-week filings work fine. |
| **Telegram message not received** | Ensure you have messaged the bot at least once (e.g. `/start`) before fetching `getUpdates` — the chat ID appears only after the bot has seen a message from you. Confirm the token in `.env` has no quotes or trailing spaces. |
| **Re-sending the same filing** | One-shot mode keys on the BSE `NEWSID`; poll mode keys on `name\|YYYYMMDD` (written only after a successful send). Delete the relevant entry in `data/seen_filings.json` — or the whole file — to force a re-run. |
| **API costs** | OpenAI is called once per new filing per stock. Budget accordingly as the watchlist grows. |

---

## Project structure

```
bse-bot/
├── config/
│   ├── watchlist.json      # stocks to track
│   ├── estimates.json      # per-stock analyst estimates (optional)
│   └── poller.json         # poller runtime settings
├── data/
│   ├── pdfs/               # downloaded filings (gitignored)
│   └── seen_filings.json   # dedupe state (gitignored)
├── logs/                   # daily run logs (gitignored)
├── src/
│   ├── main.py             # entry point; one-shot scan + --poll dispatch
│   ├── poller.py           # long-running dual-exchange poller
│   ├── bse_client.py       # BSE announcements API client
│   ├── nse_client.py       # NSE announcements & board-meetings API client
│   ├── pdf_extractor.py    # page scoring + structured table extraction
│   ├── ai_comparator.py    # OpenAI comparison-report generation
│   ├── telegram_sender.py  # Telegram Bot API delivery
│   └── storage.py          # JSON-file persistence helpers
├── tests/
│   └── test_run.py         # dry-run smoke test
├── requirements.txt
└── .env.example
```

---

## Design notes & limitations

- **Single-user, personal scale.** State is stored in local JSON files; there is
  no database, queue broker, or multi-tenant support.
- **No OCR.** Scanned image-only filings cannot be parsed and are skipped.
- **Text-PDF availability.** BSE serves only recent filings from `AttachLive`;
  keep the lookback window small so cron runs hit live attachments.
- **AI cost scales with the watchlist.** One OpenAI call is made per new filing.
- **Auto-hot depends on NSE data.** Results-day auto-detection reads the NSE
  board-meeting feed; BSE-only stocks require a manual `expected_results_date`.
