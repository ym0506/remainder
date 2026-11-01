"""Refund rules read from an organizer's notice, and the checks that keep them honest.

The AI fills in Rules from free text. Nothing here trusts it: every value must come with a
quote, every quote must appear word for word in the notice, and the value must appear in
its own quote. Anything that fails goes back to the organizer before money moves.
"""

import re
from dataclasses import dataclass, field

from .allocate import Claim, allocate
from .money import display, to_cents, to_str


@dataclass(frozen=True)
class Exclusion:
    family: str
    reason: str
    quote: str


@dataclass(frozen=True)
class Rules:
    pool_cents: int
    pool_quote: str
    fee_per_player_cents: int = 0
    fee_quote: str = ""
    exclusions: tuple = ()
    questions: tuple = ()

    @classmethod
    def from_json(cls, data):
        fee = data.get("non_refundable_per_player")
        return cls(
            pool_cents=to_cents(data["pool_amount"]),
            pool_quote=data.get("pool_quote", ""),
            fee_per_player_cents=to_cents(fee) if fee not in (None, "") else 0,
            fee_quote=data.get("non_refundable_quote") or "",
            exclusions=tuple(
                Exclusion(e["family"], e.get("reason", ""), e.get("quote", ""))
                for e in data.get("excluded_families", [])
            ),
            questions=tuple(data.get("questions", [])),
        )

    def to_json(self):
        return {
            "pool_amount": to_str(self.pool_cents),
            "pool_quote": self.pool_quote,
            "non_refundable_per_player": to_str(self.fee_per_player_cents) if self.fee_per_player_cents else None,
            "non_refundable_quote": self.fee_quote or None,
            "excluded_families": [{"family": e.family, "reason": e.reason, "quote": e.quote} for e in self.exclusions],
            "questions": list(self.questions),
        }


@dataclass(frozen=True)
class Payment:
    family_id: str
    family_name: str
    players: int
    paid_cents: int
    refunded_before_cents: int
    order_id: str
    capture_id: str
    paid_at: str


@dataclass(frozen=True)
class Problem:
    field: str
    message: str


@dataclass(frozen=True)
class PlanLine:
    family_id: str
    share_cents: int
    weight_cents: int
    exact: str  # exact share in cents as a fraction, for the screen
    extra_cent: bool
    capped: bool
    excluded: str = ""  # reason, if excluded
    note: str = field(default="", compare=False)


_SPACES = re.compile(r"\s+")
_AMOUNT = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")
_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def _norm(text):
    return _SPACES.sub(" ", text.translate(_FOLD)).strip().lower()


def _amounts_in(text):
    found = set()
    for token in _AMOUNT.findall(text):
        try:
            found.add(to_cents(token))
        except ValueError:
            pass
    return found


def check(rules, notice, payments):
    """Everything that must be fixed by a person before the plan can be approved."""
    problems = []
    source = _norm(notice)

    def quoted(field_name, quote):
        if not quote.strip():
            problems.append(Problem(field_name, "no quote from the notice"))
            return False
        if _norm(quote) not in source:
            problems.append(Problem(field_name, f'quote not found in the notice: "{quote}"'))
            return False
        return True

    if rules.pool_cents <= 0:
        problems.append(Problem("pool", "the amount to return must be more than zero"))
    if quoted("pool", rules.pool_quote) and rules.pool_cents not in _amounts_in(rules.pool_quote):
        problems.append(Problem("pool", f"{display(rules.pool_cents)} does not appear in its quote"))

    if rules.fee_per_player_cents:
        if quoted("non_refundable", rules.fee_quote) and rules.fee_per_player_cents not in _amounts_in(rules.fee_quote):
            problems.append(Problem("non_refundable", f"{display(rules.fee_per_player_cents)} does not appear in its quote"))

    names = {}
    for p in payments:
        names.setdefault(_norm(p.family_name), []).append(p)
    for e in rules.exclusions:
        label = f"excluded:{e.family}"
        if quoted(label, e.quote) and _norm(e.family) not in _norm(e.quote):
            problems.append(Problem(label, f'"{e.family}" does not appear in its quote'))
        matches = names.get(_norm(e.family), [])
        if len(matches) != 1:
            problems.append(Problem(label, f'"{e.family}" matches {len(matches)} families on the roster'))

    for q in rules.questions:
        problems.append(Problem("question", q))

    return problems


def plan(rules, payments, title):
    """Turn rules into one refund line per family. Raises AllocationError if it can't add up."""
    excluded = {_norm(e.family): e.reason or "excluded by the notice" for e in rules.exclusions}
    claims, weights, reasons = [], {}, {}
    for p in payments:
        reason = excluded.get(_norm(p.family_name), "")
        weight = 0 if reason else max(p.paid_cents - rules.fee_per_player_cents * p.players, 0)
        weights[p.family_id], reasons[p.family_id] = weight, reason
        claims.append(Claim(p.family_id, weight, p.paid_cents - p.refunded_before_cents, f"{p.paid_at}|{p.family_id}"))

    lines = []
    for share in allocate(rules.pool_cents, claims):
        weight = weights[share.key]
        note = ""
        if share.cents:
            note = (
                f"{title}. Your share of the {display(rules.pool_cents)} being returned, "
                f"in proportion to the {display(weight)} you paid toward registration."
            )
        lines.append(
            PlanLine(
                family_id=share.key,
                share_cents=share.cents,
                weight_cents=weight,
                exact=f"{share.exact.numerator}/{share.exact.denominator}" if share.exact.denominator != 1 else str(share.exact.numerator),
                extra_cent=share.extra_cent,
                capped=share.capped,
                excluded=reasons[share.key],
                note=note,
            )
        )
    return lines
