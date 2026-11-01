"""Claude sorts the families' replies to the notice; code decides what each one may change.

Only one kind of reply changes money: a family asking the club to keep its share. That
share is still worked out, then kept instead of refunded. A request to send the refund
somewhere else is never carried out: a PayPal refund can only go back to the payment it
came from, and nothing here takes an account, email or card. Questions the notice does
not answer wait for the treasurer. Every reading must quote the reply word for word, and
any answer taken from the notice must quote the notice word for word.
"""

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

from .notice import MODEL, _client
from .rules import Problem, _norm, _sentences, _whole_sentences

SAVED = Path(__file__).with_name("demo_replies_reading.json")

SYSTEM = """You sort replies that families sent after an organizer's refund notice. For each reply, choose one kind:

- keep_share: the family asks the club to keep the money it would get back (for a fund, a donation, next season).
- question: the family asks something about the refund. If a sentence of the notice already answers it, copy that sentence into notice_quote; otherwise set notice_quote to null.
- change_destination: the family asks for the refund to go to a different account, email address, card or person.
- outside_rules: the family asks for more, less or different money than the notice gives, without asking a question.
- no_action: anything else, such as thanks.

quote: one whole sentence of the reply, copied word for word, that shows the kind.
summary: one short plain sentence saying what the family wants.
Never work out amounts. Return one result per reply, with its reply_id."""

KINDS = ("keep_share", "question", "change_destination", "outside_rules", "no_action")


class ReplyResult(BaseModel):
    reply_id: int
    kind: Literal["keep_share", "question", "change_destination", "outside_rules", "no_action"]
    quote: str
    summary: str
    notice_quote: str | None


class RepliesReading(BaseModel):
    results: list[ReplyResult]


@dataclass(frozen=True)
class Sorted:
    reply_id: int
    family: str
    text: str
    kind: str
    quote: str
    summary: str
    notice_quote: str | None


def read_with_claude(replies, notice, client=None):
    """replies: list of (family, text). Returns (list of Sorted, error)."""
    client = client or _client()
    if client is None:
        return None, "no Anthropic API key configured"
    listing = "\n\n".join(f"reply_id {i}, from the {family} family:\n{text}" for i, (family, text) in enumerate(replies))
    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": f"Notice:\n{notice}\n\nReplies:\n\n{listing}"}],
            output_format=RepliesReading,
        )
    except anthropic.RateLimitError:
        return None, "Claude rate limit reached; try again in a minute"
    except anthropic.APIStatusError as exc:
        return None, f"Claude API error {exc.status_code}"
    except anthropic.APIConnectionError:
        return None, "could not reach the Claude API"
    if response.stop_reason == "refusal":
        return None, "Claude declined to read these replies"
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        return None, "Claude's answer was cut off"
    out = []
    for r in response.parsed_output.results:
        if 0 <= r.reply_id < len(replies):
            family, text = replies[r.reply_id]
            out.append(Sorted(r.reply_id, family, text, r.kind, r.quote, r.summary, r.notice_quote))
    return out, ""


def check(sorted_replies, replies, notice, plan_families):
    """Problems to fix or confirm. plan_families: names that get a share in this round."""
    problems = []
    seen = {s.reply_id for s in sorted_replies}
    if len(seen) != len(sorted_replies):
        problems.append(Problem("replies", "Claude sorted the same reply more than once; sort them again"))
    for i, (family, _) in enumerate(replies):
        if i not in seen:
            problems.append(Problem(f"reply:{family}", "Claude did not sort this reply"))
    notice_sentences = _sentences(notice)
    sharing = {_norm(n) for n in plan_families}
    for s in sorted_replies:
        label = f"reply:{s.family}"
        if not _whole_sentences(s.quote, _sentences(s.text)):
            problems.append(Problem(label, f'quote is not a whole sentence of the reply: "{s.quote}"'))
        if s.notice_quote and not _whole_sentences(s.notice_quote, notice_sentences):
            problems.append(Problem(label, f'answer is not a whole sentence of the notice: "{s.notice_quote}"'))
        if s.kind == "keep_share" and _norm(s.family) not in sharing:
            problems.append(Problem(label, f"the {s.family} family has no share in this round to keep"))
        if s.kind == "question" and not s.notice_quote:
            problems.append(Problem(label, f"{s.family}: {s.summary}", confirmable=True))
    return problems


def keep_list(sorted_replies):
    return [s.family for s in sorted_replies if s.kind == "keep_share"]


def to_json(s):
    return {
        "reply_id": s.reply_id,
        "family": s.family,
        "text": s.text,
        "kind": s.kind,
        "quote": s.quote,
        "summary": s.summary,
        "notice_quote": s.notice_quote,
    }


def from_json(d):
    return Sorted(d["reply_id"], d["family"], d["text"], d["kind"], d["quote"], d["summary"], d.get("notice_quote"))


def saved_for(replies, notice):
    if not SAVED.exists():
        return None
    saved = json.loads(SAVED.read_text())
    if saved["replies"] != [list(r) for r in replies] or _norm(saved["notice"]) != _norm(notice):
        return None
    return [from_json(d) for d in saved["results"]], saved["model"]


def save_demo(replies, notice):
    results, error = read_with_claude(replies, notice)
    if results is None:
        raise RuntimeError(error)
    SAVED.write_text(
        json.dumps(
            {
                "model": MODEL,
                "date": date.today().isoformat(),
                "notice": notice,
                "replies": [list(r) for r in replies],
                "results": [to_json(s) for s in results],
            },
            indent=2,
        )
        + "\n"
    )
    return results


