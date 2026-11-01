# Remainder: fair-share refunds for groups

When a season or event is cut short, the people who paid are owed part of their money back, and each is owed a different amount. Remainder reads the organizer's notice and the families' replies, works out every share to the cent, and sends each PayPal refund once, even if the sending process is killed partway through. It then reads the refunds back from PayPal to show the books match.

PayPal sandbox only. The league, families and amounts in the demo are made up.

## The problem

A volunteer treasurer who collected 24 registrations through PayPal now has to send up to 24 different partial refunds.

- Eventbrite can refund all orders at once, but for partial amounts its help says: "If you need to partially refund orders, issue refunds individually." For events paid through PayPal, those refunds are made from the organizer's PayPal account. ([Eventbrite help](https://www.eventbrite.com/help/en-us/articles/680082/refund-and-cancel-all-orders/))
- Splitting a fixed amount in proportion leaves stray cents. Rounding each share on its own can return a cent more or less than the pool.
- A refund request whose answer is lost may still have gone through. PayPal's docs: "when you omit the PayPal-Request-Id header from a request, PayPal duplicates the request." ([idempotency](https://developer.paypal.com/api/rest/reference/idempotency/)) PayPal blocks a refund larger than what is left on a payment, but not a second partial refund that still fits.

## What it does

1. **Season.** 24 registration payments in the PayPal sandbox (Orders v2, created with a sandbox test card). One spare season is kept ready so a visitor doesn't wait for them.
2. **Notice.** Claude returns what the treasurer's notice states: the pool, a per-player amount left out of each share, and excluded families, each with the sentence it came from. Code checks that every quote is one or more whole sentences of the notice, that each amount appears in its quote written as money, and that excluded families are named in theirs. Money in the notice that the rules don't use, and any question Claude raises, must be confirmed by the treasurer. Claude never computes anyone's refund.
3. **Replies.** Six families answered the notice. Claude sorts each reply: keep our share for the club, a question (answered from the notice when it can be), send our refund somewhere else, a demand the notice doesn't cover, or nothing to do. Only "keep our share" changes money, and the treasurer can turn it off. A request to send a refund to another account changes nothing: a PayPal refund can only go back to the payment it came from, and nothing here takes an account, email or card.
4. **Plan.** Amounts are integer cents; exact shares are fractions. Each family gets the whole cents of its exact share, and the cents left over go one each to the largest fractions, ties to the earlier payment time and then roster order (largest remainder method). No share may exceed what is still refundable on that payment. A kept share is worked out the same way and recorded as kept. Refunds plus kept shares must equal the pool or the plan is not saved. One season has one plan per notice, so the same refunds can't be planned twice.
5. **Send.** Each refund carries a PayPal-Request-Id fixed when the plan is made, and the same value as its `custom_id`. A row is marked SENDING and committed before PayPal is called. If the sending process dies, the next run first looks for a refund with that `custom_id` on the order and records it if PayPal has it; only if PayPal has nothing does it send again, with the same request ID. Unclear answers (no answer, 5xx, 408, 409, 425, 429) leave the row SENDING; any other 4xx, such as `REFUND_AMOUNT_EXCEEDED`, is held with PayPal's reason and not retried automatically. One sender per round, enforced with a file lock.
6. **Check.** Reads every order back from PayPal and compares its refunds with the ledger: refunds this round recorded, refunds known from before, and refunds made since the round started that nobody recorded.

A seventh step on the page repeats the run on a fresh season without request IDs, for comparison.

## Measured in the PayPal sandbox (2026-11-01)

Same 24 families, same notice, replies and plan in both runs; the sending process was killed right after PayPal answered refund #9 and then restarted.

| Run | Planned refunds | PayPal refunded | Difference | Families paid twice |
|---|---|---|---|---|
| Remainder: request ID and lookup per refund | $961.11 to 22 families ($38.89 kept at the Chen family's request) | $961.11 | $0.00 | 0 |
| Fresh season, no request IDs, plain retry | $961.11 | $977.78 | +$16.67 | 1 (Ito; the refund made before the kill is in no record of ours) |

In the first run the restarted sender found refund #9 (Jensen) on the order and did not send it again. Log: `evidence/sandbox-run-20261101T0633Z.json`.

Before writing the product, the refund API itself (`spike/results/spike-20261101-141816.json`):

| Request | Result |
|---|---|
| Same partial refund twice, one PayPal-Request-Id | HTTP 201, then 200 with the same refund ID; one refund |
| Same partial refund twice, no PayPal-Request-Id | two refunds, both COMPLETED |
| A used PayPal-Request-Id on another capture | HTTP 400 `DUPLICATE_REQUEST_ID`, no refund |
| One cent more than what is left | HTTP 422 `REFUND_AMOUNT_EXCEEDED` |

Claude Haiku 5.5, ten runs per case (`evidence/notice-reading-2026-11-01.json`, `evidence/reply-sorting-2026-11-01.json`):

- Demo notice: 10 of 10 readings passed every check, all ten identical.
- Notice with the amount removed, and notice excluding "families who paid after the February deadline" without names: blocked 10 of 10 each.
- Demo notice with the earlier prompt, which did not describe the computation or require whole-sentence quotes: blocked 10 of 10 (9 runs raised questions, such as whether the uniform fee applies to every player; all 10 quoted only part of a sentence somewhere).
- Six replies, ten runs: all 60 sorted as expected. The Fischer question was answered from the notice in 10 of 10; one run quoted part of a notice sentence for it and was blocked by the check.

## How the pieces fit

```
notice ─Claude─▶ rules + quotes ─checks─┐
replies ─Claude─▶ sorted + quotes ─checks─┴▶ plan (integer cents) ─approve─▶ sender ─PayPal Payments v2─▶ refunds
                                                   │                           │
                                              SQLite ledger ◀──────────────────┘
                                                   │
                                       check ◀── PayPal Orders v2 (read back)
```

- `remainder/allocate.py` largest remainder with caps
- `remainder/rules.py` notice rules, quote checks, plan lines
- `remainder/replies.py` reply sorting and what each kind may change
- `remainder/ledger.py` payments, rounds, plan rows, saved PayPal answers
- `remainder/runner.py` sender, its own process so it can be killed
- `remainder/reconcile.py` ledger against PayPal
- `remainder/paypal.py` PayPal Server SDK calls
- `remainder/notice.py` Claude structured output for the notice
- `evals/` the repeat runs above; `evals/export_run.py` writes a finished round to `evidence/`

## Run it

Python 3.11+, a PayPal sandbox REST app linked to a **US** sandbox business account (card payments aren't offered for every country), and optionally an Anthropic API key.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... ANTHROPIC_API_KEY=...
.venv/bin/python -m uvicorn app:app --port 8765
```

Open http://localhost:8765 and follow the steps. With a key, Claude reads live, up to a daily cap. Without a key, or past the cap, the demo notice and replies fall back to saved results of real Claude runs on that exact text (`remainder/demo_reading.json`, `remainder/demo_replies_reading.json`) and the page says so. Editing the notice needs a key.

Tests, no network: `.venv/bin/python -m unittest discover -s tests -t .` (42 tests). The fake PayPal in `tests/` copies the behavior measured above.

### Deploy

`render.yaml` is a Render Blueprint: one Python web service. Render asks for `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` and `ANTHROPIC_API_KEY` when the Blueprint is created. On the free plan the service sleeps after 15 idle minutes and its disk is wiped on restart, which only means a visitor starts a new season.

## Limits

- USD and one payment per family.
- The demo creates payments with a sandbox test card from the server; real registrations would come in through PayPal Checkout.
- PayPal fees on refunds are not modeled. Who absorbs them is a policy the notice would have to state.
- The reply inbox is the fixed demo set.
- One SQLite file per visitor, and the season ID in a cookie is its only key. Daily caps are counted on local disk, so a restart on the free plan resets them.

## Built with

- PayPal Orders v2 and Payments v2 through the [PayPal Server SDK for Python](https://github.com/paypal/PayPal-Python-Server-SDK), which is generated by APIMatic. Its refund call takes `paypal_request_id` per call; typed error details become the reasons on held rows; the SDK's own retries are off (`max_retries=0`) so every retry goes through the lookup first.
- Claude Haiku 5.5 through the Anthropic API, structured output, for reading the notice and sorting replies.
- FastAPI, SQLite, plain HTML and JavaScript.

MIT License.
