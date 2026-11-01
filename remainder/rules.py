"""Refund rules read from an organizer's notice, and the checks that keep them honest.

The AI fills in Rules from free text. Nothing here trusts it. Every value must come with a
quote that is one or more whole sentences of the notice, copied word for word; amounts must
appear in their own quote as money; excluded families must be named in theirs. Failures
block the plan. Questions from the AI, and money in the notice the rules don't use, need a
person to confirm them before the plan can be made.
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
        pool_cents = to_cents(data["pool_amount"])
        fee_cents = to_cents(fee) if fee not in (None, "") else 0
        if pool_cents < 0 or fee_cents < 0:
            raise ValueError("amounts cannot be negative")
        return cls(
            pool_cents=pool_cents,
            pool_quote=data.get("pool_quote", ""),
            fee_per_player_cents=fee_cents,
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
    confirmable: bool = False  # a person may confirm it and go on; otherwise it must be fixed


@dataclass(frozen=True)
class PlanLine:
    family_id: str
    share_cents: int
    weight_cents: int
    exact: str  # exact share in cents as a fraction, for the screen
    extra_cent: bool
    capped: bool
    excluded: str = ""  # reason, if excluded
    kept_cents: int = 0  # share the family asked the club to keep instead of refunding
    note: str = field(default="", compare=False)


_SPACES = re.compile(r"\s+")
# Money as written in a notice: "$1,000.00", "$15", "15.00". Bare counts like "10" are not money.
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?|\b\d[\d,]*\.\d{2}\b")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_FOLD = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u00a0": " "})


def _norm(text):
    return _SPACES.sub(" ", text.translate(_FOLD)).strip().lower()


def _money_in(text):
    found = []
    for token in _MONEY.findall(text):
        try:
            found.append(to_cents(token.replace(" ", "")))
        except ValueError:
            pass
    return found


def _sentences(notice):
    found = []
    for line in notice.splitlines():
        found.extend(_norm(s).rstrip(".!?") for s in _SENTENCE_BREAK.split(line.strip()) if s.strip())
    return found


def _whole_sentences(quote, sentences, longest=3):
    """True if the quote is one sentence of the notice, or a few consecutive ones."""
    q = _norm(quote).rstrip(".!?")
    for i in range(len(sentences)):
        joined = ""
        for j in range(i, min(i + longest, len(sentences))):
            joined = f"{joined} {sentences[j]}".strip()
            if joined == q:
                return True
    return False


def _names(name, text):
    return re.search(rf"(?<![\w'-]){re.escape(_norm(name))}(?![\w'-])", _norm(text)) is not None


def check(rules, notice, payments):
    """Problems a person has to fix or confirm before the plan can be made."""
    problems = []
    sentences = _sentences(notice)

    def quoted(field_name, quote):
        if not quote.strip():
            problems.append(Problem(field_name, "no quote from the notice"))
            return False
        if _norm(quote) not in _norm(notice):
            problems.append(Problem(field_name, f'quote not found in the notice: "{quote}"'))
            return False
        if not _whole_sentences(quote, sentences):
            problems.append(Problem(field_name, f'quote is only part of a sentence: "{quote}"'))
            return False
        return True

    if rules.pool_cents <= 0:
        problems.append(Problem("pool", "the amount to return must be more than zero"))
    if quoted("pool", rules.pool_quote) and rules.pool_cents not in _money_in(rules.pool_quote):
        problems.append(Problem("pool", f"{display(rules.pool_cents)} does not appear in its quote"))

    if rules.fee_per_player_cents:
        if quoted("non_refundable", rules.fee_quote) and rules.fee_per_player_cents not in _money_in(rules.fee_quote):
            problems.append(Problem("non_refundable", f"{display(rules.fee_per_player_cents)} does not appear in its quote"))

    for e in rules.exclusions:
        label = f"excluded:{e.family}"
        if quoted(label, e.quote) and not _names(e.family, e.quote):
            problems.append(Problem(label, f'"{e.family}" is not named in its quote'))
        matches = [p for p in payments if _norm(p.family_name) == _norm(e.family)]
        if len(matches) != 1:
            problems.append(Problem(label, f'"{e.family}" matches {len(matches)} families on the roster'))

    used = {rules.pool_cents, rules.fee_per_player_cents}
    for cents in sorted(set(_money_in(notice)) - used):
        problems.append(
            Problem("amount", f"The notice also mentions {display(cents)}, which these rules don't use.", confirmable=True)
        )

    for q in rules.questions:
        problems.append(Problem("question", q, confirmable=True))

    return problems


def open_problems(problems, confirmed):
    """Problems still in the way after a person confirmed some of the confirmable ones."""
    return [p for p in problems if not (p.confirmable and p.message in confirmed)]


def plan(rules, payments, title, keep=()):
    """Turn rules into one refund line per family. Raises AllocationError if it can't add up.

    Families in `keep` asked the club to keep their share: it is worked out the same way and
    recorded as kept, so refunds plus kept shares still add up to the pool.
    """
    keep = {_norm(name) for name in keep}
    excluded = {_norm(e.family): e.reason or "excluded by the notice" for e in rules.exclusions}
    claims, weights, reasons = [], {}, {}
    for p in payments:
        reason = excluded.get(_norm(p.family_name), "")
        weight = 0 if reason else max(p.paid_cents - rules.fee_per_player_cents * p.players, 0)
        weights[p.family_id], reasons[p.family_id] = weight, reason
        # Ties go to the earlier payment time, then roster order (times are kept to the second).
        claims.append(Claim(p.family_id, weight, p.paid_cents - p.refunded_before_cents, f"{p.paid_at}|{p.family_id}"))

    names = {p.family_id: p.family_name for p in payments}
    lines = []
    for share in allocate(rules.pool_cents, claims):
        weight = weights[share.key]
        kept = share.cents if _norm(names[share.key]) in keep else 0
        note = ""
        if share.cents and not kept:
            note = (
                f"{title}. Your share of the {display(rules.pool_cents)} being returned, "
                f"in proportion to the {display(weight)} counted from your registration payment."
            )
        lines.append(
            PlanLine(
                family_id=share.key,
                share_cents=share.cents - kept,
                weight_cents=weight,
                exact=f"{share.exact.numerator}/{share.exact.denominator}" if share.exact.denominator != 1 else str(share.exact.numerator),
                extra_cent=share.extra_cent,
                capped=share.capped,
                excluded=reasons[share.key],
                kept_cents=kept,
                note=note,
            )
        )
    return lines
