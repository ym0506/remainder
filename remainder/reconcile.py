"""Compare the ledger with what PayPal itself says was refunded.

For each family we read the order back from PayPal and sort its refunds into refunds this
round recorded, refunds the ledger knows from before, and refunds made since this round
started that nobody recorded. The last kind is money that left without a trace in our
books, which is what a blind retry produces. On a row that is still SENDING, such a refund
is ours and simply not written down yet, so it is reported as in flight.
"""

from dataclasses import dataclass, field
from datetime import datetime

LIVE = ("COMPLETED", "PENDING")


def _after(create_time, started):
    if not create_time:
        return True
    return datetime.fromisoformat(create_time) >= datetime.fromisoformat(started)


@dataclass
class FamilyCheck:
    family_id: str
    family_name: str
    planned_cents: int
    state: str
    paypal_cents: int  # this round's recorded refunds, as PayPal reports them
    unrecorded: list = field(default_factory=list)  # (refund_id, cents) PayPal has and we don't

    @property
    def status(self):
        if self.unrecorded:
            return "IN_FLIGHT" if self.state == "SENDING" else "UNRECORDED_REFUND"
        if self.state == "SKIPPED":
            return "OK" if self.paypal_cents == 0 else "UNEXPECTED"
        if self.state != "DONE":
            return "NOT_SENT"
        return "OK" if self.paypal_cents == self.planned_cents else "AMOUNT_MISMATCH"


@dataclass
class Report:
    round_id: str
    pool_cents: int
    kept_cents: int  # shares families asked the club to keep; never sent
    families: list

    @property
    def to_refund_cents(self):
        return self.pool_cents - self.kept_cents

    @property
    def planned_cents(self):
        return sum(f.planned_cents for f in self.families)

    @property
    def paid_out_cents(self):
        return sum(f.paypal_cents + sum(c for _, c in f.unrecorded) for f in self.families)

    @property
    def difference_cents(self):
        return self.paid_out_cents - self.to_refund_cents

    @property
    def clean(self):
        return self.difference_cents == 0 and all(f.status == "OK" for f in self.families)


def reconcile(ledger, gateway, round_id):
    rnd = ledger.round(round_id)
    rows = ledger.rows(round_id)
    checks = []
    for row in rows:
        seen = [s for s in gateway.refunds_on(row.order_id) if s.status in LIVE]
        mine = ledger.refund_ids_for(round_id, row.family_id)
        known = ledger.known_refund_ids(row.capture_id)
        checks.append(
            FamilyCheck(
                family_id=row.family_id,
                family_name=row.family_name,
                planned_cents=row.share_cents,
                state=row.state,
                paypal_cents=sum(s.cents for s in seen if s.refund_id in mine),
                unrecorded=[
                    (s.refund_id, s.cents)
                    for s in seen
                    if s.refund_id not in known and _after(s.create_time, rnd.created_at)
                ],
            )
        )
    return Report(round_id, rnd.pool_cents, sum(r.kept_cents for r in rows), checks)
