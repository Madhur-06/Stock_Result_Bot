"""Extract text from BSE-result PDFs using pdfplumber."""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 5
_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    """Collapse runs of whitespace and excess blank lines."""
    text = text.replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def extract_text(pdf_path: Path, max_pages: int = DEFAULT_MAX_PAGES) -> str:
    """Return cleaned text from the first max_pages of a PDF. Empty on failure."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.warning("PDF not found: %s", pdf_path)
        return ""
    chunks: list[str] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            for idx, page in enumerate(pdf.pages[:max_pages]):
                try:
                    txt = page.extract_text() or ""
                except (ValueError, AttributeError) as exc:
                    logger.warning("Page %d extract failed in %s: %s", idx + 1, pdf_path.name, exc)
                    txt = ""
                if txt:
                    chunks.append(txt)
            logger.info("Extracted %d/%d pages from %s", min(max_pages, page_count),
                        page_count, pdf_path.name)
    except (OSError, ValueError, PdfminerException) as exc:
        logger.warning("Failed to open PDF %s: %s (likely non-PDF response or scanned image)",
                       pdf_path, exc)
        return ""

    cleaned = _clean("\n\n".join(chunks))
    if not cleaned:
        logger.warning("Empty text from %s — possibly scanned/image-only PDF", pdf_path.name)
    return cleaned


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python -m src.pdf_extractor <path-to-pdf>")
        sys.exit(1)
    out = extract_text(Path(sys.argv[1]))
    print(f"--- extracted {len(out)} chars ---")
    print(out[:2000])
