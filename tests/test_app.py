"""HTTP-level checks for the parts of app.py that guard money. No network: the PayPal
gateway is never called here and Claude is unavailable."""

import json
import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ.update(
    REMAINDER_DATA=_tmp.name,
    REMAINDER_SPARE_SEASONS="0",
    PAYPAL_CLIENT_ID="test",
    PAYPAL_CLIENT_SECRET="test",
    ANTHROPIC_API_KEY="",
)

from fastapi.testclient import TestClient  # noqa: E402

import app as webapp  # noqa: E402
from remainder import demo, notice, rules, season  # noqa: E402
from remainder.ledger import Ledger  # noqa: E402
from tests.fake_paypal import FakePayPal  # noqa: E402


def make_season(season_id):
    fake = FakePayPal()
    ledger = Ledger(str(Path(_tmp.name) / f"{season_id}.db"))
    clock = iter(range(100))

    def pay(label, cents):
        fake.add_capture(f"O-{label}", f"C-{label}", cents)
        return f"O-{label}", f"C-{label}", f"2027-02-01T10:{next(clock):02d}:00Z"

    season.seed(ledger, pay, lambda c, cents: fake.refund_now(c, cents).refund_id)
    ledger.set_meta("seeded", "1")
    reading = notice.saved_reading_for(demo.NOTICE)
    ledger.set_meta("reading", json.dumps({"notice": demo.NOTICE, "rules": reading.rules.to_json(), "source": "saved", "model": reading.model}))
    ledger.close()


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        notice._client = lambda: None  # Claude unavailable: saved readings only
        cls.client = TestClient(webapp.app)

    def test_written_reply_must_come_from_a_family_on_the_roster(self):
        make_season("s-roster1")
        res = self.client.post("/api/inbox", json={"season_id": "s-roster1", "family": "Nobody", "text": "Hello."})
        self.assertEqual(res.status_code, 422)

    def test_written_replies_are_capped_and_reset_the_sorting(self):
        make_season("s-cap1")
        for i in range(webapp.MAX_ADDED_REPLIES):
            res = self.client.post("/api/inbox", json={"season_id": "s-cap1", "family": "Chen", "text": f"Note {i}."})
            self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["inbox"]), len(demo.REPLIES) + webapp.MAX_ADDED_REPLIES)
        res = self.client.post("/api/inbox", json={"season_id": "s-cap1", "family": "Chen", "text": "One more."})
        self.assertEqual(res.status_code, 429)
        # The saved demo sorting no longer matches this inbox, and Claude is unavailable.
        res = self.client.post("/api/replies", json={"season_id": "s-cap1"})
        self.assertEqual(res.status_code, 503)

    def test_a_visitor_season_always_uses_request_ids(self):
        make_season("s-ids1")
        self.assertEqual(self.client.post("/api/replies", json={"season_id": "s-ids1"}).status_code, 200)
        petrov = [p["message"] for p in self.client.get("/api/me", cookies={"season": "s-ids1"}).json()["replies"]["problems"]]
        res = self.client.post(
            "/api/plan", json={"season_id": "s-ids1", "confirmed": petrov, "use_request_ids": False}
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["use_request_ids"])
        self.assertEqual(res.json()["left_over"], "$0.00")

    def test_nothing_can_be_reread_after_approval(self):
        make_season("s-appr1")
        self.client.post("/api/replies", json={"season_id": "s-appr1"})
        ledger = Ledger(str(Path(_tmp.name) / "s-appr1.db"))
        r = rules.Rules.from_json(json.loads(ledger.get_meta("reading"))["rules"])
        lines = rules.plan(r, ledger.payments(), demo.TITLE)
        round_id = ledger.create_round(demo.TITLE, demo.NOTICE, r.to_json(), r.pool_cents, lines)
        ledger.approve(round_id)
        ledger.close()
        self.assertEqual(self.client.post("/api/read", json={"season_id": "s-appr1", "notice": demo.NOTICE}).status_code, 409)
        res = self.client.post("/api/inbox", json={"season_id": "s-appr1", "family": "Chen", "text": "Hi."})
        self.assertEqual(res.status_code, 409)


if __name__ == "__main__":
    unittest.main()
