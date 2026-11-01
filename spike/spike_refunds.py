"""Sandbox spike for Remainder.

Answers five questions against the PayPal sandbox before any product code is written:

  E1  Can a card order be created and captured from the server, with no buyer approval?
  E2  The same partial refund sent twice with one PayPal-Request-Id: how many refunds exist?
  E3  The same partial refund sent twice with no PayPal-Request-Id: how many refunds exist?
  E4  A PayPal-Request-Id already used on capture A, reused for a refund on capture C: what comes back?
  E5  A refund one cent larger than what is left on the capture: is it rejected?

Credentials are read from ~/.config/paypal-remainder/.env (PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET).
Every request and response is written to results/spike-<timestamp>.json, without credentials.
Only the sandbox host is ever called.
"""

import argparse
import base64
import json
import ssl
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib import error, parse, request

SANDBOX = "https://api-m.sandbox.paypal.com"
ENV_FILE = Path.home() / ".config" / "paypal-remainder" / ".env"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Published sandbox test card (developer.paypal.com/sandbox-testing/card-testing).
TEST_CARD = {
    "name": "Spike Family",
    "number": "4012888888881881",
    "expiry": "2030-12",
    "security_code": "123",
}

# Full fee, sibling discount, scholarship: three different amounts, like the demo season.
SEASON_FEES_CENTS = [12000, 10800, 6000]

# python.org builds for macOS ship without a CA bundle until "Install Certificates" is run.
# Fall back to the bundle macOS keeps at /etc/ssl/cert.pem instead of changing the install.
MACOS_CA_BUNDLE = Path("/etc/ssl/cert.pem")


def tls_context():
    context = ssl.create_default_context()
    if context.cert_store_stats()["x509_ca"] == 0 and MACOS_CA_BUNDLE.exists():
        context.load_verify_locations(cafile=str(MACOS_CA_BUNDLE))
    return context


def money(cents):
    if cents < 0:
        raise ValueError(f"negative amount: {cents}")
    return f"{cents // 100}.{cents % 100:02d}"


def cents_of(value):
    cents = Decimal(value) * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"more than two decimals: {value}")
    return int(cents)


def parse_env(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def capture_id_of(order):
    for unit in order.get("purchase_units", []):
        for capture in unit.get("payments", {}).get("captures", []):
            return capture["id"]
    return None


def refunds_of(order):
    found = []
    for unit in order.get("purchase_units", []):
        found.extend(unit.get("payments", {}).get("refunds", []))
    return found


def error_name(payload):
    details = payload.get("details") or []
    if details and details[0].get("issue"):
        return details[0]["issue"]
    return payload.get("name", "")


class Sandbox:
    def __init__(self, client_id, secret, log):
        self.client_id = client_id
        self.secret = secret
        self.log = log
        self.token = None
        self.tls = tls_context()

    def _send(self, method, path, body=None, headers=None, form=None):
        all_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if form is not None:
            data = parse.urlencode(form).encode()
            all_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            all_headers["Content-Type"] = "application/json"
        req = request.Request(SANDBOX + path, data=data, method=method, headers=all_headers)
        try:
            with request.urlopen(req, timeout=60, context=self.tls) as resp:
                status, raw = resp.status, resp.read()
        except error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        return status, (json.loads(raw) if raw else {})

    def authenticate(self):
        basic = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
        status, payload = self._send(
            "POST",
            "/v1/oauth2/token",
            headers={"Authorization": "Basic " + basic},
            form={"grant_type": "client_credentials"},
        )
        if status != 200:
            sys.exit(f"token request failed: HTTP {status} {payload.get('error', '')}")
        self.token = payload["access_token"]

    def call(self, step, method, path, body=None, request_id=None):
        headers = {"Authorization": "Bearer " + self.token, "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        status, payload = self._send(method, path, body, headers)
        self.log.append(
            {
                "step": step,
                "method": method,
                "path": path,
                "request_id": request_id,
                "request": body,
                "status": status,
                "response": payload,
            }
        )
        return status, payload


def create_paid_order(api, label, cents):
    body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {"reference_id": label, "amount": {"currency_code": "USD", "value": money(cents)}}
        ],
        "payment_source": {"card": TEST_CARD},
    }
    status, order = api.call(f"E1 create {label}", "POST", "/v2/checkout/orders", body, str(uuid.uuid4()))
    if status not in (200, 201):
        return None
    if order.get("status") == "APPROVED":
        status, order = api.call(
            f"E1 capture {label}", "POST", f"/v2/checkout/orders/{order['id']}/capture", {}, str(uuid.uuid4())
        )
    capture_id = capture_id_of(order)
    if order.get("status") != "COMPLETED" or not capture_id:
        return None
    return {"label": label, "cents": cents, "order_id": order["id"], "capture_id": capture_id}


def refund(api, step, payment, cents, request_id):
    body = {
        "amount": {"currency_code": "USD", "value": money(cents)},
        "note_to_payer": "Remainder sandbox spike",
    }
    return api.call(step, "POST", f"/v2/payments/captures/{payment['capture_id']}/refund", body, request_id)


def refunds_on(api, step, payment):
    status, order = api.call(step, "GET", f"/v2/checkout/orders/{payment['order_id']}")
    if status != 200:
        return None
    return [
        {"id": r.get("id"), "value": r.get("amount", {}).get("value"), "status": r.get("status")}
        for r in refunds_of(order)
    ]


def still_refundable(payment, refunds):
    taken = sum(cents_of(r["value"]) for r in refunds if r["status"] not in ("FAILED", "CANCELLED"))
    return payment["cents"] - taken


def run(api, payments_wanted):
    findings = {}

    paid = []
    for i in range(payments_wanted):
        result = create_paid_order(api, f"family-{i + 1:02d}", SEASON_FEES_CENTS[i % len(SEASON_FEES_CENTS)])
        if result:
            paid.append(result)
    findings["E1"] = {"captured": len(paid), "asked": payments_wanted}
    if len(paid) < 3:
        return findings
    a, b, c = paid[:3]

    shared_key = str(uuid.uuid4())
    s1, r1 = refund(api, "E2 first", a, 1000, shared_key)
    s2, r2 = refund(api, "E2 repeat, same key", a, 1000, shared_key)
    on_a = refunds_on(api, "E2 check", a)
    findings["E2"] = {
        "http": [s1, s2],
        "refund_ids": [r1.get("id"), r2.get("id")],
        "refunds_on_capture": on_a,
    }

    s3, r3 = refund(api, "E3 first, no key", b, 500, None)
    s4, r4 = refund(api, "E3 repeat, no key", b, 500, None)
    findings["E3"] = {
        "http": [s3, s4],
        "refund_ids": [r3.get("id"), r4.get("id")],
        "refunds_on_capture": refunds_on(api, "E3 check", b),
    }

    s5, r5 = refund(api, "E4 key from E2, other capture", c, 300, shared_key)
    findings["E4"] = {
        "http": s5,
        "returned_refund_id": r5.get("id"),
        "same_as_E2_refund": r5.get("id") is not None and r5.get("id") == r1.get("id"),
        "error": error_name(r5) if s5 >= 400 else "",
        "refunds_on_capture": refunds_on(api, "E4 check", c),
    }

    if on_a is not None:
        over = still_refundable(a, on_a) + 1
        s6, r6 = refund(api, "E5 one cent over", a, over, None)
        findings["E5"] = {"asked": money(over), "http": s6, "error": error_name(r6) if s6 >= 400 else ""}

    return findings


def summary_lines(findings):
    lines = []
    e1 = findings.get("E1", {})
    lines.append(f"E1 card payments captured without buyer approval: {e1.get('captured')}/{e1.get('asked')}")
    for key, expect in (("E2", "1 expected"), ("E3", "2 expected per PayPal docs")):
        if key in findings:
            refunds = findings[key]["refunds_on_capture"]
            count = "unknown" if refunds is None else len(refunds)
            lines.append(f"{key} refunds on the capture after two sends: {count} ({expect})")
    if "E4" in findings:
        e4 = findings["E4"]
        lines.append(
            f"E4 reused key on another capture: HTTP {e4['http']}, same refund as E2: {e4['same_as_E2_refund']}, "
            f"refunds on that capture: {e4['refunds_on_capture']}, error: {e4['error'] or '-'}"
        )
    if "E5" in findings:
        e5 = findings["E5"]
        lines.append(f"E5 refund of {e5['asked']} (one cent over): HTTP {e5['http']}, error: {e5['error'] or '-'}")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--payments", type=int, default=3, help="card payments to create in E1 (stop rule needs 10)")
    args = parser.parse_args()
    if args.payments < 3:
        parser.error("--payments must be at least 3")
    if not ENV_FILE.exists():
        sys.exit(f"missing {ENV_FILE}: add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox app)")

    env = parse_env(ENV_FILE.read_text())
    missing = [k for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET") if not env.get(k)]
    if missing:
        sys.exit(f"{ENV_FILE} is missing: {', '.join(missing)}")

    log = []
    api = Sandbox(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"], log)
    api.authenticate()
    findings = run(api, args.payments)

    RESULTS_DIR.mkdir(exist_ok=True)
    run_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"spike-{run_at}.json"
    out.write_text(json.dumps({"run_at": run_at, "findings": findings, "log": log}, indent=2))

    for line in summary_lines(findings):
        print(line)
    print(f"full log: {out}")
    return 0 if findings.get("E1", {}).get("captured", 0) >= 3 else 1


if __name__ == "__main__":
    sys.exit(main())
