"""Send an approved refund round to PayPal, one family at a time, resumable after a crash.

Every family's refund carries a PayPal-Request-Id fixed when the plan was made, and the same
value as the refund's custom_id. A row is marked SENDING and committed before PayPal is
called. If the process dies after PayPal made the refund but before we wrote it down, the
row is still SENDING; the next run first looks for a refund with that custom_id on the
order and records it if PayPal has it. Only if PayPal has nothing does it send again, with
the same request ID, so a refund that lands late is still not made twice.

Measured in the sandbox on 2026-11-01: the same request ID twice gave one refund, no request
ID twice gave two. PayPal keeps request IDs for a limited time (45 days per the SDK docs);
the lookup does not depend on that.

use_request_ids=False exists only for the side-by-side demo of what goes wrong without it:
no request ID, no lookup, a plain retry.
"""

import fcntl
import os
import sys

from .gateway import RefundOutcome, find_refund

CRASH_EXIT = 70
BUSY_EXIT = 75


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
        if rnd.use_request_ids and row.state == "SENDING":
            seen = find_refund(gateway, row.order_id, row.request_id)
            if seen:
                ledger.record(round_id, row.family_id, row.request_id, RefundOutcome(200, seen.refund_id, seen.status), found=True)
                log(f"{row.family_name}: PayPal already has {seen.refund_id}; not sent again")
                continue
        if not ledger.mark_sending(round_id, row.family_id):
            continue
        request_id = row.request_id if rnd.use_request_ids else None
        outcome = gateway.refund(row.capture_id, row.share_cents, request_id, row.note, row.request_id)
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

    # One sender per round, across processes and server restarts.
    lock = open(f"{args.db}.{args.round}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another process is already sending this round", flush=True)
        return BUSY_EXIT

    ledger = Ledger(args.db)
    gateway = PayPalGateway.from_env()
    run(ledger, gateway, args.round, crash_after_send=args.crash_after_send, log=lambda m: print(m, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
