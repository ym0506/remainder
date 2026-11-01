"""Send an approved refund round to PayPal, one family at a time, resumable after a crash.

Every family's refund carries a PayPal-Request-Id fixed when the plan was made. If the
process dies after PayPal made a refund but before we wrote it down, the row is still
SENDING; the next run sends it again with the same ID and PayPal hands back the refund it
already made (HTTP 200) instead of making a second one. Measured in the sandbox on
2026-11-01: same ID twice gave one refund, no ID twice gave two.

use_request_ids=False exists only for the side-by-side demo of what goes wrong without it.
"""

import os
import sys

CRASH_EXIT = 70


class NotApproved(RuntimeError):
    pass


def _die():
    os._exit(CRASH_EXIT)


def run(ledger, gateway, round_id, crash_after_send=None, die=_die, log=print):
    rnd = ledger.round(round_id)
    if not rnd.approved_at:
        raise NotApproved(f"round {round_id} has not been approved")

    sent = 0
    for row in ledger.rows(round_id, states=("PLANNED", "SENDING")):
        ledger.mark_sending(round_id, row.family_id)
        request_id = row.request_id if rnd.use_request_ids else None
        outcome = gateway.refund(row.capture_id, row.share_cents, request_id, row.note)
        sent += 1
        if crash_after_send == sent:
            log(f"crash: stopping after PayPal answered for {row.family_name}, before writing it down")
            die()
            return sent
        ledger.record(round_id, row.family_id, request_id, outcome)
        log(f"{row.family_name}: HTTP {outcome.http_status} {outcome.refund_id or outcome.error}")
    return sent


def main(argv=None):
    import argparse

    from .ledger import Ledger
    from .paypal import PayPalGateway

    parser = argparse.ArgumentParser(description="Send an approved refund round to the PayPal sandbox.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--round", required=True)
    parser.add_argument("--crash-after-send", type=int, default=None)
    args = parser.parse_args(argv)

    ledger = Ledger(args.db)
    gateway = PayPalGateway.from_env()
    run(ledger, gateway, args.round, crash_after_send=args.crash_after_send, log=lambda m: print(m, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
