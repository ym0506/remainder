"""Split a pool of cents in proportion to weights, so the parts add up to the pool exactly.

Largest remainder method: everyone gets the floor of their exact share, then the cents
that are left go one each to the largest fractional parts. Ties go to the earlier
tie_key (we use payment time, so whoever paid first is first in line).

Each share is also capped at what can still be refunded on that payment. If the exact
share would exceed the cap, that family gets the cap and the rest is shared again among
everyone else.
"""

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Claim:
    key: str
    weight: int  # cents the share is proportional to
    cap: int  # most that may be refunded, in cents
    tie_key: str


@dataclass(frozen=True)
class Share:
    key: str
    cents: int
    exact: Fraction  # the share before rounding; equals cents when capped
    extra_cent: bool  # True if this share got one of the leftover cents
    capped: bool


class AllocationError(ValueError):
    pass


def allocate(pool, claims):
    if pool < 0:
        raise AllocationError("pool cannot be negative")
    if any(c.weight < 0 or c.cap < 0 for c in claims):
        raise AllocationError("weights and caps cannot be negative")
    if len({c.key for c in claims}) != len(claims):
        raise AllocationError("duplicate claim key")

    active = [c for c in claims if c.weight > 0]
    if pool > 0 and not active:
        raise AllocationError("nobody to refund: every weight is zero")
    if pool > sum(c.cap for c in active):
        raise AllocationError("pool is larger than what can still be refunded")

    shares = {c.key: Share(c.key, 0, Fraction(0), False, False) for c in claims}
    remaining = pool
    exact = {}
    while active:
        total_weight = sum(c.weight for c in active)
        exact = {c.key: Fraction(remaining * c.weight, total_weight) for c in active}
        over = [c for c in active if exact[c.key] > c.cap]
        if not over:
            break
        for c in over:
            shares[c.key] = Share(c.key, c.cap, Fraction(c.cap), False, True)
            remaining -= c.cap
        active = [c for c in active if exact[c.key] <= c.cap]

    floors = {c.key: exact[c.key].numerator // exact[c.key].denominator for c in active}
    leftover = remaining - sum(floors.values())
    order = sorted(active, key=lambda c: (-(exact[c.key] - floors[c.key]), c.tie_key, c.key))
    lucky = {c.key for c in order[:leftover]}
    for c in active:
        bump = c.key in lucky
        shares[c.key] = Share(c.key, floors[c.key] + bump, exact[c.key], bump, False)

    result = [shares[c.key] for c in claims]
    assert sum(s.cents for s in result) == pool
    return result
