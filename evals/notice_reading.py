"""Repeat Claude's reading of notices and keep every raw answer.

Cases: the demo notice, two notices that leave out something the plan needs, and the demo
notice read with the earlier system prompt that did not describe the computation. Each run
records the model's output and what rules.check() made of it.

    .venv/bin/python -m evals.notice_reading --runs 10

Writes evidence/notice-reading-<date>.json. Costs a few cents with Claude Haiku 5.5.
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from remainder import demo, notice, rules
from remainder.rules import Payment

# The system prompt before 2026-11-01 15:00 KST, kept to reproduce the comparison.
EARLIER_SYSTEM = """You read notices that volunteer organizers send when part of the money people paid is being returned.
Extract only what the notice states. Do not calculate anyone's refund.

- pool_amount: the total being returned to payers, as digits with two decimals (for example 1000.00).
- non_refundable_per_player: an amount per player that is kept back and not counted toward a family's share, or null if the notice names none.
- excluded_families: families the notice says get nothing from this refund, with the reason the notice gives.
- Every quote must be copied word for word from the notice, one sentence or clause, with no changes.
- If the notice leaves open something that changes who gets how much, add a short question to questions instead of guessing. Do not ask about anything the notice already answers.

The roster below lists the family names on record, so you can spell exclusions the way the roster does."""

NO_AMOUNT = demo.NOTICE.replace(
    "The city has refunded $1,000.00 of our field permit", "The city has refunded part of our field permit"
)
UNNAMED_EXCLUSION = demo.NOTICE.replace(
    "The Okafor family already received a full refund when they moved away in March, so they are not included.",
    "Families who paid after the February deadline are not included.",
)

CASES = [
    ("demo notice", demo.NOTICE, None),
    ("no amount stated", NO_AMOUNT, None),
    ("exclusion without names", UNNAMED_EXCLUSION, None),
    ("demo notice, earlier prompt", demo.NOTICE, EARLIER_SYSTEM),
]


def roster():
    return [
        Payment(f"f{i:02d}", name, len(fees), sum(fees), 0, "order", f"capture-{i}", f"t{i:02d}")
        for i, (name, fees) in enumerate(demo.FAMILIES, 1)
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()

    payments = roster()
    names = [p.family_name for p in payments]
    results = []
    for label, text, system in CASES:
        original = notice.SYSTEM
        if system:
            notice.SYSTEM = system
        try:
            for n in range(1, args.runs + 1):
                reading = notice.read_with_claude(text, names)
                problems = rules.check(reading.rules, text, payments) if reading.rules else []
                results.append(
                    {
                        "case": label,
                        "run": n,
                        "model": reading.model,
                        "error": reading.error,
                        "rules": reading.rules.to_json() if reading.rules else None,
                        "problems": [{"field": p.field, "message": p.message} for p in problems],
                        "outcome": "error" if reading.error else ("blocked" if problems else "passed"),
                    }
                )
                print(label, n, results[-1]["outcome"], flush=True)
        finally:
            notice.SYSTEM = original

    summary = {}
    for label, _, _ in CASES:
        rows = [r for r in results if r["case"] == label]
        summary[label] = {
            "outcomes": dict(Counter(r["outcome"] for r in rows)),
            "distinct_readings": len({json.dumps(r["rules"], sort_keys=True) for r in rows if r["rules"]}),
            "distinct_values": len(
                {
                    (r["rules"]["pool_amount"], r["rules"]["non_refundable_per_player"],
                     tuple(e["family"] for e in r["rules"]["excluded_families"]))
                    for r in rows if r["rules"]
                }
            ),
        }
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = Path("evidence") / f"notice-reading-{stamp[:10]}.json"
    out.write_text(json.dumps({"recorded_at": stamp, "summary": summary, "runs": results}, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
