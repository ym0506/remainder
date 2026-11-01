"""Read a finished refund round back from PayPal and save the result as evidence.

    .venv/bin/python -m evals.export_run data/<season>.db [data/<comparison season>.db ...]

Writes evidence/sandbox-run-<timestamp>.json: per round, the reconciliation against PayPal
and every saved PayPal answer. No credentials are written.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from remainder.ledger import Ledger
from remainder.paypal import PayPalGateway
from remainder.reconcile import reconcile


def main(paths):
    gateway = PayPalGateway.from_env()
    runs = []
    for path in paths:
        ledger = Ledger(path)
        round_id = ledger.latest_round()
        rnd = ledger.round(round_id)
        report = reconcile(ledger, gateway, round_id)
        runs.append(
            {
                "season_file": Path(path).name,
                "round_id": round_id,
                "use_request_ids": rnd.use_request_ids,
                "rules": json.loads(rnd.rules_json),
                "pool_cents": report.pool_cents,
                "kept_cents": report.kept_cents,
                "to_refund_cents": report.to_refund_cents,
                "paid_out_cents": report.paid_out_cents,
                "difference_cents": report.difference_cents,
                "clean": report.clean,
                "families": [
                    {
                        "family": f.family_name,
                        "planned_cents": f.planned_cents,
                        "paypal_cents": f.paypal_cents,
                        "unrecorded": f.unrecorded,
                        "status": f.status,
                    }
                    for f in report.families
                ],
                "rows": [
                    {"family": r.family_name, "state": r.state, "refund_id": r.paypal_refund_id, "detail": r.detail}
                    for r in ledger.rows(round_id)
                ],
                "attempts": ledger.attempts(round_id),
            }
        )
        print(Path(path).name, "request IDs" if rnd.use_request_ids else "no request IDs",
              "| to refund", report.to_refund_cents, "| PayPal refunded", report.paid_out_cents,
              "| difference", report.difference_cents, "| clean", report.clean)
        ledger.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = Path("evidence") / f"sandbox-run-{stamp}.json"
    out.write_text(json.dumps({"recorded_at": stamp, "environment": "PayPal sandbox", "runs": runs}, indent=1) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
