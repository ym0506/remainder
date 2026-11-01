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


class Gateway(Protocol):
    def refund(self, capture_id: str, cents: int, request_id: str | None, note: str) -> RefundOutcome: ...

    def refunds_on(self, order_id: str) -> list[SeenRefund]: ...
