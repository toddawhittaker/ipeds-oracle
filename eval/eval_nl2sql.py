"""NL→SQL regression harness.

Runs the full agent against questions with known-good answers (drawn from
SCHEMA.md §8 + README). Use it to (a) sanity-check the pipeline and (b) gate
model swaps. Requires OPENROUTER_API_KEY in the environment/.env.

    .venv/bin/python eval/eval_nl2sql.py
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.llm import run_agent


def _nums(text: str) -> set[int]:
    """All integers appearing in text, ignoring thousands separators."""
    return {int(m.replace(",", "")) for m in re.findall(r"\d[\d,]*", text)}


def contains_number(target: int, tol: float = 0.0):
    def check(res):
        if tol == 0:
            return target in _nums(res.answer)
        return any(abs(n - target) <= tol * target for n in _nums(res.answer))
    return check


# (question, human-readable expectation, check function)
CASES = [
    (
        "How many bachelor's degrees in Computer Science (CIP 11.0701) did "
        "California public universities award in the most recent year?",
        "≈ 7,679",
        contains_number(7679, tol=0.02),
    ),
    (
        "What was the national total of associate's degrees awarded in the most "
        "recent year, across all programs?",
        "≈ 1.0M (0.9–1.1M)",
        lambda res: any(900_000 <= n <= 1_100_000 for n in _nums(res.answer)),
    ),
    (
        "How many collection years of data are in the database?",
        "5 years (2021–2025)",
        contains_number(5),
    ),
]


async def main() -> int:
    s = get_settings()
    if not s.openrouter_api_key:
        print("SKIP: OPENROUTER_API_KEY not set — cannot run the LLM eval.")
        print("      Set it in .env, then re-run.")
        return 0

    passed = 0
    for q, expect, check in CASES:
        print(f"\nQ: {q}\n   expect: {expect}")
        res = await run_agent(q)
        if res.error:
            print(f"   ✗ ERROR: {res.error}")
            continue
        ok = check(res)
        passed += ok
        tag = "✓" if ok else "✗"
        print(f"   {tag} model={res.model_used} escalated={res.escalated} "
              f"iters={res.iterations} tokens={res.total_tokens}")
        print("   SQL: " + (res.sql_log[-1][:120] if res.sql_log else "(none)"))
        print("   answer: " + res.answer[:300].replace("\n", " "))

    print(f"\n{passed}/{len(CASES)} cases passed")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
