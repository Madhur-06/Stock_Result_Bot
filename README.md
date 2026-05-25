# BSE Quarterly Results AI Comparison Bot

A small Python automation that watches BSE (Bombay Stock Exchange) for quarterly
result filings from a custom stock watchlist, uses OpenAI to compare the filed
numbers against your stored expectations, and pushes a concise verdict
(BEAT / MISS / IN-LINE) to your personal Telegram via the free Bot API.
Personal-use, laptop-cron grade — no databases, no Docker, no web service.

## Setup

```bash
git clone <this repo>
cd bse-bot
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env   # then edit .env and fill in the three values
```

Edit `config/watchlist.json` to list the stocks you want to track. Each entry
needs at minimum a `name` and a `scrip` (BSE 6-digit code). `nse_symbol` is
informational.

## Getting a BSE scrip code

1. Go to <https://www.bseindia.com/> and search for the company.
2. Open its page; the URL contains a 6-digit number, e.g.
   `/stock-share-price/.../500325/` → scrip code is `500325` (Reliance).
3. Add it to `config/watchlist.json`.

## Getting a Telegram bot token + chat id

1. In Telegram, search for **@BotFather** and start a chat.
2. Send `/newbot`. Pick a display name and a unique username ending in `bot`.
   BotFather replies with an HTTP token like `123456789:ABCdef...`. Put that
   in `TELEGRAM_TOKEN` in `.env`.
3. Search for the bot you just created and send it any message (e.g. `/start`)
   so it has a chat with you.
4. Open this URL in a browser, replacing `<TOKEN>` with your bot token:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Find `"chat":{"id":NNNNN, ...}` in the JSON response. That number is your
   chat id — put it in `TELEGRAM_CHAT_ID` in `.env`.

You will receive every report as a normal Telegram message on the device
where you signed into the bot's chat.

## Maintaining estimates.json

Before a stock's expected result date, add or update its entry in
`config/estimates.json`. The key must exactly match the `name` field in
`watchlist.json`. Example:

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

If a stock has no estimates entry, the bot still produces an actuals-only
summary instead of a vs.-estimate comparison.

## Running

Standalone smoke tests for each module:

```bash
python -m src.bse_client       # fetches recent announcements for Reliance
python -m src.pdf_extractor data/pdfs/<file>.pdf
python -m src.telegram_sender  # sends "Bot test message"
python -m src.ai_comparator    # runs the prompt with hard-coded sample data
```

Full pipeline:

```bash
python -m src.main --dry-run            # print reports instead of sending
python -m src.main                      # the real thing
python -m src.main --days-back 14       # widen the lookback window
```

## Cron (Mac / Linux)

Runs every 10 minutes during Indian market & post-market hours, Mon–Fri:

```cron
*/10 15-20 * * 1-5 cd /path/to/bse-bot && /usr/bin/python3 -m src.main >> logs/cron.log 2>&1
```

(Adjust the Python path and the hour range to your timezone.)

## Troubleshooting

- **403 from BSE.** The BSE JSON API rejects requests without a real
  `User-Agent` and the `https://www.bseindia.com/` Referer header. Both are
  already set in `src/bse_client.py`; if you swap in a new HTTP layer keep
  them.
- **Empty extracted text.** Some filings are scanned images, not text PDFs.
  pdfplumber will return an empty string and the bot logs a warning and
  skips the AI step. There is no OCR fallback by design.
- **BSE returned non-PDF content.** BSE's `AttachLive` directory only
  serves recent filings (roughly the last few weeks). For older filings the
  server returns its SPA-shell HTML page instead. The bot detects this via
  the `%PDF-` magic prefix and logs a warning. Day-of and same-week
  filings work fine — keep the default 2-day lookback for cron.
- **Telegram message not received.** Make sure you sent at least one
  message to your bot (e.g. `/start`) before fetching `getUpdates` — the
  chat id only appears after the bot has seen a message from you. If
  `getUpdates` returns `{"ok":true,"result":[]}`, send `/start` and refresh
  it. Also confirm the token in `.env` has no quotes or trailing spaces.
- **Re-sending the same filing.** Each filing's BSE `NEWSID` is stored in
  `data/seen_filings.json`. Delete the entry (or the whole file) to force a
  re-run.
- **API costs.** The bot calls OpenAI once per new filing per stock; budget
  for that if you grow the watchlist.
