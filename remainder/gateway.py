"""What the runner and reconciler need from PayPal, and nothing more."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RefundOutcome:
    http_status: int | None  # None: no answer came back (timeout, dropped connection)
    refund_id: str | None = None
    refund_status: str | None = None
    error: str = ""  # PayPal issue name, or the transport error


@dataclass(frozen=True)
class SeenRefund:
    refund_id: str
    cents: int
    status: str
    custom_id: str | None = None  # we put the row's request ID here when sending
    create_time: str | None = None


class Gateway(Protocol):
    def refund(self, capture_id: str, cents: int, request_id: str | None, note: str, custom_id: str) -> RefundOutcome: ...

    def refunds_on(self, order_id: str) -> list[SeenRefund]: ...


def find_refund(gateway, order_id, custom_id):
    """The refund PayPal already holds for this row, if any."""
    for seen in gateway.refunds_on(order_id):
        if seen.custom_id == custom_id:
            return seen
    return None
