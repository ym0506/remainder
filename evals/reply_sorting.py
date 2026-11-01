"""Repeat Claude's sorting of the demo replies and keep every raw answer.

    .venv/bin/python -m evals.reply_sorting --runs 10

Writes evidence/reply-sorting-<date>.json.
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from remainder import demo, replies

EXPECTED = {
    "Chen": "keep_share",
    "Fischer": "question",
    "Haddad": "change_destination",
    "Larsen": "outside_rules",
    "Petrov": "question",
    "Usman": "no_action",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    sharing = [name for name, _ in demo.FAMILIES if name not in demo.REFUNDED_BEFORE]
    runs = []
    for n in range(1, args.runs + 1):
        results, error = replies.read_with_claude(demo.REPLIES, demo.NOTICE)
        problems = replies.check(results, demo.REPLIES, demo.NOTICE, sharing) if results else []
        runs.append(
            {
                "run": n,
                "error": error,
                "results": [replies.to_json(s) for s in results] if results else None,
                "problems": [{"field": p.field, "message": p.message, "confirmable": p.confirmable} for p in problems],
            }
        )
        print(n, error or [(s.family, s.kind) for s in results], flush=True)

    kinds = Counter()
    for r in runs:
        for s in r["results"] or []:
            kinds[(s["family"], s["kind"])] += 1
    summary = {
        "runs": args.runs,
        "errors": sum(1 for r in runs if r["error"]),
        "matches_expected": {f: kinds[(f, k)] for f, k in EXPECTED.items()},
        "fischer_answered_from_notice": sum(
            1 for r in runs for s in r["results"] or [] if s["family"] == "Fischer" and s["notice_quote"]
        ),
        "blocking_problems": sum(1 for r in runs for p in r["problems"] if not p["confirmable"]),
        "kinds": {f"{f}:{k}": c for (f, k), c in sorted(kinds.items())},
    }
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = Path("evidence") / f"reply-sorting-{stamp[:10]}.json"
    out.write_text(json.dumps({"recorded_at": stamp, "model": replies.MODEL, "summary": summary, "runs": runs}, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
