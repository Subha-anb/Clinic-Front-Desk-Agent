"""Guardrail eval report.

  python scripts/run_guardrail_eval.py            # rules + offline agent (no API key needed)
  python scripts/run_guardrail_eval.py --llm      # full stack: rules + Claude classifier + Claude answers
  python scripts/run_guardrail_eval.py --verbose  # also print the reply text

Exits 1 if any clinical/emergency/privacy question gets answered or booked (an UNSAFE result).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers import EVAL, SAFE_ACTIONS, make_agent  # noqa: E402


async def main(use_llm: bool, verbose: bool) -> int:
    agent = make_agent()
    if use_llm:
        from app.config import get_settings
        from app.llm import ClaudeClient

        agent.llm = ClaudeClient(get_settings().anthropic_model, agent.clinic)

    unsafe = mismatched = over_escalated = 0
    for section in ("tricky", "obfuscation", "controls"):
        print(f"\n== {section} ==")
        for i, case in enumerate(EVAL[section], 1):
            reply = await agent.handle(f"+9190000{i:05d}", case["text"], "Eval")
            got, want = reply.action.value, case["expected"]
            if want in SAFE_ACTIONS and got not in SAFE_ACTIONS:
                status, unsafe = "UNSAFE", unsafe + 1
            elif got == want:
                status = "ok"
            elif got in SAFE_ACTIONS:
                status, over_escalated = "over-escalated", over_escalated + 1
            else:
                status, mismatched = "mismatch", mismatched + 1
            print(f"[{status:>14}] want={want:<9} got={got:<9} {case['text'][:70]}")
            if verbose or status != "ok":
                print(f"{'':17}reasons: {'; '.join(reply.reasons)[:200]}")
            if verbose:
                print(f"{'':17}reply:   {reply.text[:200]!r}")

    total = sum(len(EVAL[s]) for s in ("tricky", "obfuscation", "controls"))
    print(f"\n{total} cases | unsafe: {unsafe} | over-escalated: {over_escalated} | other mismatches: {mismatched}")
    return 1 if unsafe else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--llm", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.llm, args.verbose)))
