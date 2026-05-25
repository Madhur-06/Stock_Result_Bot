"""Use OpenAI to compare actual quarterly results vs. expectations."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

OPENAI_MODEL = "gpt-4o"
MAX_TOKENS = 1500
RESULT_TEXT_CHAR_CAP = 12000

SYSTEM_PROMPT = (
    "You are an equity-research assistant summarizing Indian-listed company quarterly "
    "results for a personal Telegram digest. Output rules (STRICT):\n"
    "- Plain text only. NO markdown, NO asterisks, NO headers, NO code fences.\n"
    "- Under 1500 characters total.\n"
    "- First line is the verdict: BEAT / MISS / IN-LINE (single word).\n"
    "- Then concise lines comparing Revenue, EBITDA (and margin), Net Profit. For each: "
    "actual vs estimate (when given), YoY%, QoQ if obvious.\n"
    "- Then a short 'Surprises / concerns:' section (2-4 bullet-style lines using '- ').\n"
    "- End with one-line 'Takeaway:'.\n"
    "- Use emojis sparingly — only 📈 (positive), 📉 (negative), ⚠️ (concern).\n"
    "- All figures in INR crore unless the filing uses another unit (call it out).\n"
    "- If estimates are not provided, skip the comparison and produce a clean summary "
    "of actuals with YoY/QoQ deltas instead."
)


def _build_user_prompt(company_name: str, result_text: str, estimates_text: str) -> str:
    result_text = (result_text or "").strip()[:RESULT_TEXT_CHAR_CAP]
    estimates_block = (estimates_text or "").strip()
    if not estimates_block:
        estimates_block = "(no analyst estimates provided — produce an actuals-only summary)"
    return (
        f"Company: {company_name}\n\n"
        f"=== Analyst / market estimates ===\n{estimates_block}\n\n"
        f"=== Filed result (extracted from BSE PDF) ===\n{result_text}\n\n"
        "Write the Telegram digest now, following the system rules exactly."
    )


def generate_comparison_report(
    company_name: str, result_text: str, estimates_text: str
) -> str:
    """Call OpenAI and return the Telegram-ready comparison report."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY missing — cannot generate report")
        return ""
    client = OpenAI(api_key=api_key)
    user_prompt = _build_user_prompt(company_name, result_text, estimates_text)
    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
    except OpenAIError as exc:
        logger.error("OpenAI API error for %s: %s", company_name, exc)
        return ""

    choice = completion.choices[0]
    text = (choice.message.content or "").strip()
    logger.info("Generated report for %s (%d chars, finish=%s)",
                company_name, len(text), getattr(choice, "finish_reason", "?"))
    return text


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sample_result = (
        "Reliance Industries Limited\n"
        "Q2 FY26 Consolidated Results\n"
        "Revenue from operations: Rs. 252,300 crore (YoY +8.2%, QoQ +3.1%)\n"
        "EBITDA: Rs. 44,800 crore, margin 17.8% (YoY +6.4%)\n"
        "Profit After Tax: Rs. 18,150 crore (YoY +12.4%, QoQ +5.8%)\n"
        "EPS: Rs. 26.8\n"
        "Net debt reduced by Rs. 5,400 crore vs Q1.\n"
        "Jio ARPU: Rs. 198 (vs Rs. 191 QoQ). Retail revenue up 11% YoY."
    )
    sample_estimates = (
        "Quarter: Q2 FY26\n"
        "Revenue (cr): 245000\n"
        "EBITDA (cr): 42500\n"
        "PAT (cr): 17200\n"
        "EPS: 25.4\n"
        "Notes: Street expects margin around 17%, ARPU above 195."
    )
    report = generate_comparison_report("Reliance Industries", sample_result, sample_estimates)
    print("--- REPORT ---")
    print(report or "(empty — check API key / errors above)")
