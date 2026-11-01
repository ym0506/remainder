"""In-memory stand-in for the PayPal refund API.

It copies what the sandbox actually did on 2026-11-01 (spike/results/spike-20261101-141816.json
for the first four; the last is how the sender found Jensen's refund in
evidence/sandbox-run-20261101T0633Z.json):
  - same PayPal-Request-Id, same capture: HTTP 200 and the refund already made
  - no PayPal-Request-Id: a new refund every time
  - a used PayPal-Request-Id on another capture: HTTP 400 DUPLICATE_REQUEST_ID
  - more than what is left on the capture: HTTP 422 REFUND_AMOUNT_EXCEEDED
  - a refund's custom_id comes back when the order is read
"""

import itertools
from datetime import datetime, timezone

from remainder.gateway import RefundOutcome, SeenRefund


class FakePayPal:
    def __init__(self):
        self._ids = itertools.count(1)
        self.captures = {}  # capture_id -> {"order_id", "cents", "refunds": [SeenRefund]}
        self._by_key = {}  # request_id -> (capture_id, SeenRefund)
        self.fail_next = {}  # capture_id -> RefundOutcome to return once, before acting
        self.drop_answer_once = set()  # capture_ids whose next refund is made but the answer is lost
        self.calls = 0

    def add_capture(self, order_id, capture_id, cents):
        self.captures[capture_id] = {"order_id": order_id, "cents": cents, "refunds": []}

    def refund_now(self, capture_id, cents, custom_id=None):
        refund = SeenRefund(
            f"R{next(self._ids):04d}", cents, "COMPLETED", custom_id, datetime.now(timezone.utc).isoformat()
        )
        self.captures[capture_id]["refunds"].append(refund)
        return refund

    def refund(self, capture_id, cents, request_id, note, custom_id):
        self.calls += 1
        if capture_id in self.fail_next:
            return self.fail_next.pop(capture_id)
        if request_id in self._by_key:
            used_on, refund = self._by_key[request_id]
            if used_on != capture_id:
                return RefundOutcome(400, error="DUPLICATE_REQUEST_ID")
            return RefundOutcome(200, refund.refund_id, refund.status)
        capture = self.captures[capture_id]
        left = capture["cents"] - sum(r.cents for r in capture["refunds"])
        if cents > left:
            return RefundOutcome(422, error="REFUND_AMOUNT_EXCEEDED")
        refund = self.refund_now(capture_id, cents, custom_id)
        if request_id:
            self._by_key[request_id] = (capture_id, refund)
        if capture_id in self.drop_answer_once:
            self.drop_answer_once.discard(capture_id)
            return RefundOutcome(None, error="timed out")
        return RefundOutcome(201, refund.refund_id, refund.status)

    def refunds_on(self, order_id):
        for capture in self.captures.values():
            if capture["order_id"] == order_id:
                return list(capture["refunds"])
        return []
