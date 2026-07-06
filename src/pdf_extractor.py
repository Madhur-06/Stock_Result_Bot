"""Extract text from BSE-result PDFs, ranking pages and returning the top N.

For a typical BSE filing, the headline P&L table is rarely on page 1: cover
letters, board-meeting intimations and auditor reports come first. We extract
text per page, score each page by (a) P&L-statement keywords + numeric density
and (b) penalty for cover-letter / auditor / board-notice phrasing, then return
the concatenated text of the top N pages — separated with `=== Page N ===`
markers so the LLM sees them as discrete pages.

Fallback: if every page scores <=0 (typical for scanned image-only PDFs where
text extraction returns empty), return the first N pages so the pipeline can
still try.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 3
MAX_NEG_PENALTY = 18   # cap so one stray match can't disqualify a true P&L page
MAX_NUM_BONUS = 12     # at ~48 numeric tokens; real P&Ls easily exceed this

_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


# ---------- Scoring patterns ----------

_POSITIVE_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    # The statement title is the single strongest signal.
    (re.compile(r"statement\s+of\s+(?:standalone|consolidated|unaudited|audited)?\s*"
                r"(?:financial\s+)?results?", re.I), 6),
    # Period header — repeats across column headers in a results table.
    (re.compile(r"\bquarter\s+(?:and\s+(?:year|half[- ]?year)\s+)?ended\b", re.I), 3),
    # Quarter-end dates: 31.03.YYYY (Mar), 30.06 (Jun), 30.09 (Sep), 31.12 (Dec).
    (re.compile(r"\b(?:31[./-]03|30[./-]06|30[./-]09|31[./-]12)[./-](?:20)?\d{2}\b"), 2),
    # Core P&L line items.
    (re.compile(r"revenue\s+from\s+operations", re.I), 3),
    (re.compile(r"\btotal\s+income\b", re.I), 2),
    (re.compile(r"\btotal\s+expenses?\b", re.I), 2),
    (re.compile(r"profit\s*(?:/\s*\(?loss\)?)?\s+(?:before|after|for\s+the\s+(?:period|quarter|year))",
                re.I), 2),
    (re.compile(r"\bfinance\s+costs?\b", re.I), 2),
    (re.compile(r"depreciation(?:\s*,?\s+(?:amortisation|amortization))?"
                r"(?:\s+(?:and|&)\s+(?:impairment|amortisation|amortization))?\s*expenses?",
                re.I), 2),
    (re.compile(r"\btax\s+expenses?\b", re.I), 2),
    (re.compile(r"earnings?\s+per\s+(?:equity\s+)?share", re.I), 3),
    (re.compile(r"\bother\s+(?:income|comprehensive\s+income)\b", re.I), 1),
    (re.compile(r"\bexceptional\s+items?\b", re.I), 1),
    (re.compile(r"cost\s+of\s+materials?\s+consumed", re.I), 1),
    (re.compile(r"employee\s+benefits?\s+expenses?", re.I), 1),
    # Currency-unit declaration above results tables.
    (re.compile(r"\(\s*(?:rs\.?|inr|₹)\s*in\s+(?:lakhs?|crores?|millions?)\s*\)", re.I), 3),
    (re.compile(r"(?:rs\.?|inr|₹)\s*(?:in\s+)?(?:lakhs?|crores?|millions?)\b", re.I), 1),
    # Audit qualifier in P&L column headers ("Unaudited" / "Audited").
    (re.compile(r"\b(?:un)?audited\b", re.I), 1),
]

_NEGATIVE_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    # Cover-letter / submission language.
    (re.compile(r"\b(?:dear|to,?)\s+(?:sir|madam|sirs)\b", re.I), 6),
    (re.compile(r"please\s+find\s+(?:enclosed|attached)", re.I), 6),
    (re.compile(r"yours\s+(?:faithfully|sincerely|truly)", re.I), 6),
    # Stock-exchange submission addresses.
    (re.compile(r"bse\s+limited[\s,]+(?:p\.?\s*j\.|phiroze|dalal)", re.I), 5),
    (re.compile(r"national\s+stock\s+exchange\s+of\s+india", re.I), 5),
    (re.compile(r"listing\s+(?:department|compliance|regulations?)", re.I), 3),
    # Auditor report.
    (re.compile(r"independent\s+auditor[''']?s?\s+(?:report|review\s+report)", re.I), 10),
    (re.compile(r"we\s+have\s+(?:audited|reviewed)\s+the\s+accompanying", re.I), 6),
    (re.compile(r"\bin\s+our\s+opinion\b", re.I), 4),
    (re.compile(r"basis\s+for\s+(?:qualified\s+)?opinion", re.I), 6),
    (re.compile(r"chartered\s+accountants?", re.I), 2),
    (re.compile(r"firm\s+(?:registration|reg\.?)\s+(?:no\.|number)", re.I), 3),
    # Board / dividend intimations.
    (re.compile(r"(?:outcome|intimation)\s+of\s+(?:the\s+)?board\s+meeting", re.I), 6),
    # Notes / MD&A.
    (re.compile(r"notes\s+to\s+the\s+(?:financial\s+)?(?:results|statements)", re.I), 5),
    (re.compile(r"management\s+discussion\s+and\s+analysis", re.I), 8),
]

# Number-like tokens: thousand-separated, Indian lakh notation, plain 4+ digit ints,
# decimals, and parenthesised negatives.
_NUMERIC_RE = re.compile(
    r"\b\d{1,3}(?:,\d{2,3})+(?:\.\d+)?\b"   # 12,345 / 1,23,456 / 12,345.67
    r"|\b\d{4,}(?:\.\d+)?\b"                 # 45000 / 45000.50
    r"|\(\d[\d,]*(?:\.\d+)?\)"               # (1,234) negative
)


def _score_page(text: str) -> tuple[int, int, int, int]:
    """Return (positive, negative_capped, numeric_bonus, total)."""
    if not text:
        return 0, 0, 0, 0
    pos = sum(w * len(p.findall(text)) for p, w in _POSITIVE_PATTERNS)
    neg = sum(w * len(p.findall(text)) for p, w in _NEGATIVE_PATTERNS)
    neg_capped = min(neg, MAX_NEG_PENALTY)
    num_bonus = min(len(_NUMERIC_RE.findall(text)) // 4, MAX_NUM_BONUS)
    return pos, neg_capped, num_bonus, pos - neg_capped + num_bonus


# Content gate: does the extracted text actually contain a P&L / results statement?
# Used by the poller to reject non-results PDFs (e.g. a change-in-management board
# outcome) that a HOT stock's widened candidate filter admitted on headline alone.
_RESULTS_CONTENT_RE = re.compile(
    r"revenue\s+from\s+operations"
    r"|profit\s*(?:/\s*\(?loss\)?)?\s+(?:before|after|for\s+the\s+(?:period|quarter|year))"
    r"|earnings?\s+per\s+(?:equity\s+)?share"
    r"|statement\s+of\s+(?:standalone|consolidated|unaudited|audited)?\s*"
    r"(?:financial\s+)?results?",
    re.I,
)


def looks_like_results(text: str) -> bool:
    """Heuristic gate: True if extracted filing text contains a results/P&L statement."""
    return bool(text) and bool(_RESULTS_CONTENT_RE.search(text))


# ---------- Cleaning ----------

def _clean(text: str) -> str:
    """Collapse runs of whitespace and excess blank lines."""
    text = text.replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


# ---------- Structured-table rendering ----------
#
# page.extract_text() flattens a results table into a single space-separated
# line, destroying the column boundaries the LLM needs to align each number
# with its 'Quarter Ended' period. page.extract_tables() keeps the row/column
# grid, which we render as a pipe-delimited block so columns survive.
#
# Cost note: this runs ONLY on the already-selected top-N pages (reusing pages
# pdfplumber has already opened), never on the whole document — so it adds a
# fraction of a second, not a second full extraction pass.

# Two detection strategies. "lines" works when the table has ruled borders;
# "text" clusters on character x/y positions and is what actually separates the
# period columns on borderless BSE result tables (where "lines" collapses all
# line items into a few mega-rows). We render both and keep whichever genuinely
# splits the numbers into columns — see _column_quality.
_TEXT_TABLE_SETTINGS = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "snap_tolerance": 4,
}

# A cell that is purely a number: 2,963.20 / 11,949.73 / (126.54) / -50.05
_NUM_CELL_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")


def _render_one(tables) -> tuple[str, int]:
    """Render a strategy's tables to text; return (text, column_quality)."""
    rendered: list[str] = []
    quality = 0
    for tbl in tables or []:
        rows: list[str] = []
        for row in tbl:
            cells = [_WS_RE.sub(" ", (c or "").replace("\n", " ")).strip() for c in row]
            if not any(cells):  # skip fully blank rows
                continue
            rows.append(" | ".join(cells))
            # A well-separated results row has its period values in distinct
            # cells; count rows with >=3 pure-number cells as the quality signal.
            if sum(1 for c in cells if _NUM_CELL_RE.match(c)) >= 3:
                quality += 1
        # A real results table has a header plus several line items; skip stray
        # 1-row "tables" that table detection sometimes hallucinates.
        if len(rows) >= 2:
            rendered.append("\n".join(rows))
    return "\n\n".join(rendered), quality


def _render_tables(page) -> str:
    """Return the page's best-separated tables as pipe-delimited rows, or ''."""
    try:
        lines_text, lines_q = _render_one(page.extract_tables())
        text_text, text_q = _render_one(page.extract_tables(_TEXT_TABLE_SETTINGS))
    except Exception as exc:  # noqa: BLE001 — table detection can throw on odd geometry
        logger.warning("Table extraction failed on a page: %s", exc)
        return ""
    # Prefer whichever strategy actually split numbers into columns. On a tie
    # (or when neither separates numbers) keep the ruled-line rendering.
    return text_text if text_q > lines_q else lines_text


# ---------- Public entry point ----------

def _select_pages(scores: list[int], page_count: int, top_n: int, name: str) -> list[int]:
    """Pick the top-N page indices to keep (doc order), with scanned-PDF fallback."""
    if all(s <= 0 for s in scores):
        chosen = list(range(min(top_n, page_count)))
        logger.warning(
            "All pages scored <=0 in %s — likely scanned PDF or empty extraction. "
            "Falling back to first %d pages (1-based: %s)",
            name, len(chosen), [i + 1 for i in chosen],
        )
        return chosen
    # Sort indices by (-score, index): higher score wins; ties keep doc order.
    ranked = sorted(range(page_count), key=lambda i: (-scores[i], i))
    chosen = sorted(ranked[:top_n])
    logger.info(
        "Scores=%s for %s — selected top %d (1-based pages: %s)",
        scores, name, top_n, [i + 1 for i in chosen],
    )
    return chosen


def extract_text(pdf_path: Path, top_n: int = DEFAULT_TOP_N) -> str:
    """Score every page, return the top N pages with both structured tables and text.

    Each selected page is emitted under a `=== Page N ===` marker. When a results
    table is detected on the page it is rendered as a pipe-delimited `[TABLE]`
    block (columns preserved); the flattened `[TEXT]` of the page follows for
    context (date headers, units line, notes). Tables are extracted only for the
    chosen pages, so the extra cost is negligible. Empty string on open failure.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.warning("PDF not found: %s", pdf_path)
        return ""

    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            page_texts: list[str] = []
            for idx, page in enumerate(pdf.pages):
                try:
                    txt = page.extract_text() or ""
                except (ValueError, AttributeError) as exc:
                    logger.warning("Page %d extract failed in %s: %s",
                                   idx + 1, pdf_path.name, exc)
                    txt = ""
                page_texts.append(txt)

            scores = [_score_page(t)[3] for t in page_texts]
            chosen = _select_pages(scores, page_count, top_n, pdf_path.name)

            # Render tables only for the winning pages (cheap; pages already open).
            chunks: list[str] = []
            tables_found = 0
            for i in chosen:
                page_body = _clean(page_texts[i])
                table_block = _render_tables(pdf.pages[i])
                if not page_body and not table_block:
                    continue
                parts = [f"=== Page {i + 1} ==="]
                if table_block:
                    tables_found += 1
                    parts.append("[TABLE]\n" + table_block)
                if page_body:
                    parts.append("[TEXT]\n" + page_body)
                chunks.append("\n".join(parts))
    except (OSError, ValueError, PdfminerException) as exc:
        logger.warning("Failed to open PDF %s: %s (likely non-PDF or scanned image)",
                       pdf_path, exc)
        return ""

    out = "\n\n".join(chunks).strip()
    if not out:
        logger.warning("Empty text from top %d pages of %s — possibly scanned PDF",
                       len(chosen), pdf_path.name)
    else:
        logger.info("Returning %d chars from %d pages of %s (%d with structured tables)",
                    len(out), len(chunks), pdf_path.name, tables_found)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python -m src.pdf_extractor <path-to-pdf>")
        sys.exit(1)
    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"Not found: {pdf_path}")
        sys.exit(1)

    # Per-page score breakdown for debugging.
    try:
        with pdfplumber.open(pdf_path) as pdf:
            print(f"\n--- Score breakdown for {pdf_path.name} ({len(pdf.pages)} pages) ---")
            print(f"{'page':>4} {'pos':>5} {'neg':>5} {'num':>5} {'total':>6}")
            for idx, page in enumerate(pdf.pages):
                txt = page.extract_text() or ""
                pos, neg, num, total = _score_page(txt)
                print(f"{idx + 1:>4} {pos:>5} -{neg:>4} +{num:>4} {total:>6}")
    except (OSError, ValueError, PdfminerException) as exc:
        print(f"Failed to open PDF: {exc}")
        sys.exit(1)

    print()
    out = extract_text(pdf_path)
    print(f"--- selected text ({len(out)} chars) ---")
    print(out[:2000])
