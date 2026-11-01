"""Claude reads an organizer's notice and returns refund rules, each with its quote.

The model never computes anyone's amount. It only says what the notice says: how much is
being returned, what is not refundable per player, who is excluded and why, and what it
could not decide. rules.check() then verifies every quote against the notice.
"""

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import anthropic
from pydantic import BaseModel

from .paypal import load_env_file
from .rules import Rules, _norm

MODEL = os.environ.get("REMAINDER_MODEL", "claude-haiku-5-5")
SAVED_READING = Path(__file__).with_name("demo_reading.json")

SYSTEM = """You read notices that volunteer organizers send when part of the money people paid is being returned.
Extract only what the notice states. Do not calculate anyone's refund.

How the refund will be computed, by code, from what you extract:
the whole pool is shared among the families that are not excluded, in proportion to what each paid minus the per-player amount for each of its players. An excluded family's share goes to the others. The per-player amount applies to every player on the roster.

- pool_amount: the total being returned to payers, as digits with two decimals (for example 1000.00).
- pool_quote: the sentence that states that total, including the amount as written.
- non_refundable_per_player: an amount per player that is left out of each family's share, or null if the notice names none.
- excluded_families: families the notice says get nothing from this refund, with the reason the notice gives.
- Every quote must be one whole sentence of the notice, copied word for word from its first word to its last, with no changes. Use two or three consecutive sentences only if one is not enough.
- questions: only for something the notice leaves open or states in a way that conflicts with the computation above. If the notice and the computation agree, leave questions empty.

The roster below lists the family names on record, so you can spell exclusions the way the roster does."""


class ExcludedFamily(BaseModel):
    family: str
    reason: str
    quote: str


class NoticeReading(BaseModel):
    pool_amount: str
    pool_quote: str
    non_refundable_per_player: str | None
    non_refundable_quote: str | None
    excluded_families: list[ExcludedFamily]
    questions: list[str]


@dataclass(frozen=True)
class Reading:
    rules: Rules | None
    source: str  # "claude", or "saved" for the stored demo reading
    model: str
    error: str = ""


def _client():
    key = os.environ.get("ANTHROPIC_API_KEY") or load_env_file().get("ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=key) if key else None


def read_with_claude(notice, family_names, client=None):
    client = client or _client()
    if client is None:
        return Reading(None, "claude", MODEL, "no Anthropic API key configured")
    roster = "\n".join(f"- {name}" for name in family_names)
    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": f"Roster:\n{roster}\n\nNotice:\n{notice}"}],
            output_format=NoticeReading,
        )
    except anthropic.RateLimitError:
        return Reading(None, "claude", MODEL, "Claude rate limit reached; try again in a minute")
    except anthropic.APIStatusError as exc:
        return Reading(None, "claude", MODEL, f"Claude API error {exc.status_code}")
    except anthropic.APIConnectionError:
        return Reading(None, "claude", MODEL, "could not reach the Claude API")
    if response.stop_reason == "refusal":
        return Reading(None, "claude", MODEL, "Claude declined to read this notice")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        return Reading(None, "claude", MODEL, "Claude's answer was cut off")
    data = response.parsed_output.model_dump()
    if not data["pool_amount"].strip():
        data["pool_amount"] = "0.00"
        if not data["questions"]:
            data["questions"] = ["The notice does not say how much is being returned."]
    try:
        return Reading(Rules.from_json(data), "claude", MODEL)
    except ValueError as exc:
        return Reading(None, "claude", MODEL, f"Claude returned an amount that is not money: {exc}")


def saved_reading_for(notice):
    """The stored result of a real Claude run on the demo notice, so the demo works without a key."""
    if not SAVED_READING.exists():
        return None
    saved = json.loads(SAVED_READING.read_text())
    if _norm(saved["notice"]) != _norm(notice):
        return None
    return Reading(Rules.from_json(saved["reading"]), "saved", saved["model"])


def read(notice, family_names, client=None):
    saved = saved_reading_for(notice)
    if (client or _client()) is None and saved:
        return saved
    reading = read_with_claude(notice, family_names, client)
    if reading.rules is None and saved:
        return saved
    return reading


def save_demo_reading(notice, family_names):
    reading = read_with_claude(notice, family_names)
    if reading.rules is None:
        raise RuntimeError(reading.error)
    SAVED_READING.write_text(
        json.dumps(
            {"model": reading.model, "date": date.today().isoformat(), "notice": notice, "reading": reading.rules.to_json()},
            indent=2,
        )
        + "\n"
    )
    return reading
