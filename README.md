# Remainder

When a season or an event is cut short, the people who paid are owed part of their money back, and rarely the same amount each. Remainder reads the organizer's notice, works out every payer's share to the cent, and sends each PayPal refund exactly once, even if the server dies halfway through. It then reads the refunds back from PayPal to prove the books match.

PayPal sandbox only. The league, families and amounts in the demo are made up.

## The problem

A volunteer treasurer who collected 24 registrations through PayPal now has to send 24 different partial refunds. The tools stop short of that:

- Eventbrite, which processes some events through PayPal: "If you need to partially refund orders, issue refunds individually." ([help article](https://www.eventbrite.com/help/en-us/articles/680082/refund-and-cancel-all-orders/))
- Splitting a fixed amount in proportion leaves stray cents. Rounding each share on its own can return a cent more or less than the money there is.
- A refund request that times out may still have gone through. PayPal's docs: "when you omit the PayPal-Request-Id header from a request, PayPal duplicates the request." ([idempotency](https://developer.paypal.com/api/rest/reference/idempotency/)) PayPal blocks refunds larger than what's left on a payment, but not a second partial refund that still fits.

## What it does

1. **Season.** Creates the 24 registration payments in the PayPal sandbox (Orders v2, sandbox test card) and records each capture.
2. **Notice.** Claude reads the treasurer's notice and returns only what it states: the amount being returned, a per-player amount left out of each share, and families excluded, each with the exact sentence it came from. The code checks that every quote appears word for word in the notice and that every amount appears in its quote. Anything that fails, and any question Claude raises, blocks the plan until a person resolves it. Claude never computes anyone's refund.
3. **Plan.** Integer cents and exact fractions. Each family gets the whole cents of its exact share; the cents left over go one each to the largest fractions, ties to whoever paid first (largest remainder method). No share may exceed what is still refundable on that payment. The plan must add up to the pool or it is not saved.
4. **Send.** Every refund carries a PayPal-Request-Id derived from the round and the capture, fixed when the plan is made. A row is marked SENDING and committed before PayPal is called. After a crash, SENDING rows are sent again with the same ID, and PayPal returns the refund it already made (HTTP 200) instead of making another. Refusals (for example a disputed capture) are held with PayPal's reason and never retried blindly.
5. **Check.** Reads every order back from PayPal and sorts its refunds into ones this round recorded, ones the ledger knew from before, and ones nobody recorded. Shows what PayPal actually refunded against the pool.

## Measured in the PayPal sandbox (2026-11-01)

| Run | Result |
|---|---|
| Same partial refund sent twice with one PayPal-Request-Id | first HTTP 201, second HTTP 200 with the same refund ID; one refund on the capture |
| Same partial refund sent twice with no PayPal-Request-Id | two refunds, both COMPLETED |
| A used PayPal-Request-Id sent for a different capture | HTTP 400 `DUPLICATE_REQUEST_ID`, no refund |
| Refund one cent above what is left | HTTP 422 `REFUND_AMOUNT_EXCEEDED` |
| Demo season, 23 refunds, process killed after refund #9 was made, then resumed | PayPal refunded $1,000.00 of $1,000.00; difference $0.00; 24 of 24 families match; refund #9 came back as a replay |
| Same season and plan without request IDs, same kill, plain retry | PayPal refunded $1,016.67; one family refunded twice; the second refund appears in no record of ours |

Raw logs: `spike/results/spike-20261101-141816.json` and `evidence/sandbox-run-2026-11-01.json`.

Reading the demo notice with Claude Haiku 5.5, ten runs in a row: the same pool, per-player amount and exclusion every time, all ten passing the quote checks. An earlier prompt that did not tell the model how the refund would be computed raised questions on all ten runs instead (about whether the uniform fee applied to every player, and where an excluded family's share goes). Two deliberately unclear notices, one with no amount and one excluding "families who paid after the deadline" without naming them, were blocked in all three runs each.

## How the pieces fit

```
notice ──Claude──▶ rules + quotes ──checks──▶ plan (integer cents) ──approve──▶ runner ──PayPal Payments v2──▶ refunds
                                                     │                              │
                                                SQLite ledger ◀──────────────────────┘
                                                     │
                                         reconcile ◀── PayPal Orders v2 (read back)
```

- `remainder/allocate.py` largest remainder with caps
- `remainder/rules.py` rules, quote checks, plan lines
- `remainder/ledger.py` payments, rounds, plan rows, every PayPal call
- `remainder/runner.py` resumable sender (runs as its own process so it can be killed)
- `remainder/reconcile.py` ledger against PayPal
- `remainder/paypal.py` PayPal Server SDK calls; `paypal_request_id` is passed on each refund
- `remainder/notice.py` Claude structured output

## Run it

Python 3.11+, a PayPal sandbox REST app linked to a **US** sandbox business account (card payments are not offered for every country), and optionally an Anthropic API key.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... ANTHROPIC_API_KEY=...
.venv/bin/python -m uvicorn app:app --port 8765
```

Open http://localhost:8765 and go through the six steps. Without an Anthropic key the demo notice still works: the app shows a saved result of a real Claude run on that exact notice (`remainder/demo_reading.json`) and says so on screen. Editing the notice needs a key.

Tests (no network): `.venv/bin/python -m unittest discover -s tests -t .`

The fake PayPal used in the tests copies the behavior measured in the sandbox above.

## Limits

- USD and one payment per family. No multi-currency, no split payments.
- The demo creates payments with a sandbox test card from the server; real registrations would come in through PayPal Checkout.
- PayPal fees on refunds are not modeled. Who absorbs them is a policy choice the notice would have to state.
- One SQLite file per demo visitor. Fine for a demo, not for many organizers.

## Built with

- PayPal Orders v2 and Payments v2 through the [PayPal Server SDK for Python](https://github.com/paypal/PayPal-Python-Server-SDK) (generated by APIMatic)
- Claude Haiku 5.5 (Anthropic API, structured output) for reading notices
- FastAPI, SQLite, plain HTML and JavaScript

MIT License.
