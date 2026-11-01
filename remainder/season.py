"""Create the demo season's registration payments and record them in a fresh ledger."""

from concurrent.futures import ThreadPoolExecutor

from . import demo
from .rules import Payment


def seed(ledger, pay, refund_in_full, families=demo.FAMILIES, refunded_before=demo.REFUNDED_BEFORE, workers=1):
    """pay(label, cents) -> (order_id, capture_id, paid_at); refund_in_full(capture_id, cents) -> refund_id.

    Payments may be created in parallel; they are written to the ledger in family order.
    """
    jobs = [(f"f{n:02d}", name, fees) for n, (name, fees) in enumerate(families, 1)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        made = list(pool.map(lambda job: pay(f"{job[0]}-{job[1]}", sum(job[2])), jobs))
    for (family_id, name, fees), (order_id, capture_id, paid_at) in zip(jobs, made):
        ledger.add_payment(Payment(family_id, name, len(fees), sum(fees), 0, order_id, capture_id, paid_at))
        if name in refunded_before:
            ledger.add_prior_refund(capture_id, refund_in_full(capture_id, sum(fees)), sum(fees))
    return ledger.payments()
