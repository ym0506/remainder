"""Compare the ledger with what PayPal itself says was refunded.

For each family we read the order back from PayPal and sort its refunds into three kinds:
refunds this round recorded, refunds the ledger knows from before, and refunds nobody
recorded. The last kind is money that left without a trace in our books, which is what
a blind retry produces.
"""

from dataclasses import dataclass, field

LIVE = ("COMPLETED", "PENDING")


@dataclass
class FamilyCheck:
    family_id: str
    family_name: str
    planned_cents: int
    state: str
    paypal_cents: int  # this round's refunds as PayPal reports them
    unrecorded: list = field(default_factory=list)  # (refund_id, cents) PayPal has and we don't

    @property
    def status(self):
        if self.unrecorded:
            return "UNRECORDED_REFUND"
        if self.state == "SKIPPED":
            return "OK" if self.paypal_cents == 0 else "UNEXPECTED"
        if self.state != "DONE":
            return "NOT_SENT"
        return "OK" if self.paypal_cents == self.planned_cents else "AMOUNT_MISMATCH"


@dataclass
class Report:
    round_id: str
    pool_cents: int
    families: list

    @property
    def planned_cents(self):
        return sum(f.planned_cents for f in self.families)

    @property
    def paid_out_cents(self):
        return sum(f.paypal_cents + sum(c for _, c in f.unrecorded) for f in self.families)

    @property
    def difference_cents(self):
        return self.paid_out_cents - self.pool_cents

    @property
    def clean(self):
        return self.difference_cents == 0 and all(f.status == "OK" for f in self.families)


def reconcile(ledger, gateway, round_id):
    rnd = ledger.round(round_id)
    checks = []
    for row in ledger.rows(round_id):
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
                unrecorded=[(s.refund_id, s.cents) for s in seen if s.refund_id not in known],
            )
        )
    return Report(round_id, rnd.pool_cents, checks)
