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
# Structured [TABLE] blocks are more verbose than flattened text (empty cells
# preserve column alignment), so the top-N pages can run ~15-20k chars. This cap
# must fit both pages; ~30k chars is ~8k input tokens, trivial for gpt-4o.
RESULT_TEXT_CHAR_CAP = 30000

SYSTEM_PROMPT = (
    "You are an equity-research analyst writing a personal Telegram digest of an "
    "Indian-listed company's quarterly results. The filing below was extracted from "
    "a BSE PDF. Each page may contain a '[TABLE]' block where cells are separated by "
    "' | ' with columns preserved (one results row per line), followed by a '[TEXT]' "
    "block of the flattened page for context (date headers, units line, notes). "
    "READ THE NUMBERS FROM THE '[TABLE]' BLOCK — its columns are reliable; use '[TEXT]' "
    "only for surrounding context. If no '[TABLE]' block is present, fall back to the "
    "flattened text and identify period columns by matching the 'Quarter Ended' date "
    "headers, not by left-to-right order.\n"
    "\n"
    "OUTPUT FORMAT (STRICT)\n"
    "- Plain text only. NO markdown, NO asterisks, NO headers, NO code fences.\n"
    "- Under 1500 characters total.\n"
    "- Line 1: 'VERDICT: <X>' where X is your clear one-word opinion on the quarter in "
    "CAPS — STRONG, WEAK, or MIXED. When analyst estimates are provided, append the "
    "vs-estimate call in parentheses, e.g. 'VERDICT: STRONG (BEAT vs est)' or "
    "'VERDICT: WEAK (MISS vs est)'. ALWAYS give the STRONG/WEAK/MIXED opinion, with or "
    "without estimates — never omit line 1.\n"
    "- Line 2: 'Quarter: Q{1-4} FY{YY} (INR crore)'.\n"
    "- Then concise lines for Revenue, EBITDA (with margin %), PAT, EPS. For each: "
    "actual, vs-estimate delta (when estimates are given), YoY%, QoQ% (only if "
    "computable from a prior-quarter column).\n"
    "- Then 'Surprises / concerns:' with 2-4 lines starting with '- '.\n"
    "- End with: 'Takeaway: <one sentence>'.\n"
    "- Emojis ONLY: 📈 (>=2% positive), 📉 (>=2% negative), ⚠️ (genuine concern). Sparingly.\n"
    "\n"
    "WHICH NUMBERS TO READ\n"
    "1. If BOTH standalone and consolidated statements appear, USE THE CONSOLIDATED — "
    "that is what analyst estimates and the market price in.\n"
    "2. Result tables have 4-6 period columns: current quarter, prior quarter (QoQ), "
    "year-ago quarter (YoY), 9-months, prior 9-months, full year. Identify the "
    "CURRENT-QUARTER column by matching the latest 'Quarter Ended' date in the header "
    "and read actuals ONLY from that column.\n"
    "3. Line-item mapping (use the EXACT label from the statement):\n"
    "   - Revenue = 'Revenue from operations' (NOT 'Total income', which includes other income)\n"
    "   - EBITDA   = printed line if present; else compute = (Profit before tax) + "
    "(Finance costs) + (Depreciation & amortisation), excluding exceptional items\n"
    "   - PAT     = 'Profit for the period' / 'Net profit attributable to owners of "
    "the parent' (consolidated) or 'Profit after tax' (standalone). AFTER tax, AFTER "
    "minority interest.\n"
    "   - EPS     = Basic EPS for the quarter (NOT diluted, NOT YTD, NOT annualised). "
    "If only diluted is given, use diluted and say so.\n"
    "4. Numbers in PARENTHESES are NEGATIVE: '(1,234)' means -1234. Losses must be signed.\n"
    "5. Units: report in INR crore. If filing is in lakh, divide by 100; if million, by 10. "
    "If unit conversion was needed, note it in parentheses on line 2 (e.g. '(INR crore; "
    "filing in lakh)').\n"
    "\n"
    "PERCENTAGES\n"
    "- yoy_pct = (current - year_ago) / |year_ago| * 100\n"
    "- qoq_pct = (current - prior_quarter) / |prior_quarter| * 100\n"
    "- ebitda margin = EBITDA / Revenue * 100\n"
    "- vs-estimate = (actual - estimate) / |estimate| * 100\n"
    "- Round to one decimal. If the base is zero or negative, write 'N/A' instead.\n"
    "\n"
    "VERDICT (line 1) — ALWAYS REQUIRED\n"
    "Opinion word (STRONG / WEAK / MIXED), judged on the actuals:\n"
    "- STRONG: Revenue AND PAT both grow YoY (typically >=10%) with stable-or-expanding "
    "EBITDA margin and no material red flag.\n"
    "- WEAK: Revenue OR PAT declines YoY, OR EBITDA margin contracts materially "
    "(>=150 bps), OR a loss / large one-off-driven profit, OR a serious concern.\n"
    "- MIXED: growth and margins point in different directions, or signals are unclear.\n"
    "Base the opinion on UNDERLYING performance — exclude one-off / exceptional gains "
    "when judging the trend (call them out under Surprises instead).\n"
    "vs-estimate call (only when estimates are given; append in parentheses):\n"
    "- BEAT: BOTH Revenue AND PAT are >=2% above their estimates.\n"
    "- MISS: EITHER Revenue OR PAT is >=2% below its estimate.\n"
    "- IN-LINE: otherwise.\n"
    "When no estimates are provided, give ONLY the STRONG/WEAK/MIXED word (no vs-est "
    "parenthetical) and still report YoY/QoQ deltas in the body.\n"
    "\n"
    "SURPRISES / CONCERNS (2-4 items, each <=120 chars)\n"
    "Material and specific only. Priority order:\n"
    "- Margin moves >=100 bps with the driver (e.g. 'EBITDA margin -180 bps YoY to "
    "22.4% on RM cost inflation')\n"
    "- Exceptional items / one-offs / deferred-tax effects WITH the rupee amount\n"
    "- Segment standouts: best/worst segment with one number\n"
    "- Guidance changes, order-book updates, dividends/buybacks (with amount/ratio)\n"
    "- Net debt / cash moves >=10% with the absolute change\n"
    "AVOID: restating the headline numbers already in the digest, generic phrases "
    "('strong performance', 'healthy growth'), forward-looking opinions that are not "
    "in the filing.\n"
    "\n"
    "TAKEAWAY (<=140 chars, ONE sentence)\n"
    "Does this quarter validate or break the investment case? Anchor it to ONE concrete "
    "driver from the filing. NOT a restatement of the verdict.\n"
    "\n"
    "GENERAL\n"
    "- Read numbers EXACTLY as printed; do not round or smooth.\n"
    "- If a value is missing from the filing, write 'N/A' — never fabricate.\n"
    "- 'quarter' label: derive from the latest Quarter Ended date (e.g. 31.03.2026 -> "
    "Q4 FY26)."
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
        "Write the Telegram digest now. Reminders:\n"
        "1) Use the CONSOLIDATED statement if both standalone and consolidated are present.\n"
        "2) Read actuals ONLY from the current-quarter column (latest 'Quarter Ended' date).\n"
        "3) Revenue = 'Revenue from operations', NOT 'Total income'.\n"
        "4) Numbers in parentheses are NEGATIVE.\n"
        "5) Convert to INR crore if filing is in lakh/million.\n"
        "6) ALWAYS start with line 1 'VERDICT: STRONG/WEAK/MIXED' (append the BEAT/MISS/"
        "IN-LINE call in parentheses only if estimates were provided)."
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
