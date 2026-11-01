import itertools
import tempfile
import unittest
from pathlib import Path

from remainder import demo, rules, season
from remainder.gateway import RefundOutcome
from remainder.ledger import Ledger
from remainder.money import to_cents
from remainder.reconcile import reconcile
from remainder.runner import NotApproved, run
from tests.fake_paypal import FakePayPal

# What a correct reading of demo.NOTICE looks like.
DEMO_RULES = {
    "pool_amount": "1000.00",
    "pool_quote": "The city has refunded $1,000.00 of our field permit, and on Thursday the board voted to return all of it to you, split in proportion to what each family paid for registration.",
    "non_refundable_per_player": "15.00",
    "non_refundable_quote": "each family's share is based on what you paid minus the $15.00 uniform fee per player",
    "excluded_families": [
        {
            "family": "Okafor",
            "reason": "already refunded in full when they moved",
            "quote": "The Okafor family already received a full refund when they moved away in March, so they are not included.",
        }
    ],
    "questions": [],
}


class SimulatedCrash(Exception):
    pass


def crash():
    raise SimulatedCrash


class Season:
    def __init__(self, testcase):
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.ledger = Ledger(str(Path(tmp.name) / "ledger.db"))
        testcase.addCleanup(self.ledger.close)
        self.paypal = FakePayPal()
        clock = itertools.count()

        def pay(label, cents):
            order_id, capture_id = f"O-{label}", f"C-{label}"
            self.paypal.add_capture(order_id, capture_id, cents)
            return order_id, capture_id, f"2027-02-01T10:{next(clock):02d}:00Z"

        def refund_in_full(capture_id, cents):
            return self.paypal.refund_now(capture_id, cents).refund_id

        self.payments = season.seed(self.ledger, pay, refund_in_full)

    def round(self, use_request_ids=True, approve=True):
        r = rules.Rules.from_json(DEMO_RULES)
        lines = rules.plan(r, self.payments, demo.TITLE)
        round_id = self.ledger.create_round(demo.TITLE, demo.NOTICE, r.to_json(), r.pool_cents, lines, use_request_ids)
        if approve:
            self.ledger.approve(round_id)
        return round_id


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.season = Season(self)

    def test_correct_reading_passes_every_check(self):
        r = rules.Rules.from_json(DEMO_RULES)
        self.assertEqual(rules.check(r, demo.NOTICE, self.season.payments), [])

    def test_made_up_quote_is_caught(self):
        bad = dict(DEMO_RULES, pool_quote="The city refunded $1,000.00 to every family.")
        problems = rules.check(rules.Rules.from_json(bad), demo.NOTICE, self.season.payments)
        self.assertEqual([p.field for p in problems], ["pool"])

    def test_amount_that_is_not_in_its_quote_is_caught(self):
        bad = dict(DEMO_RULES, pool_amount="1100.00")
        problems = rules.check(rules.Rules.from_json(bad), demo.NOTICE, self.season.payments)
        self.assertIn("does not appear in its quote", problems[0].message)

    def test_unknown_family_is_caught(self):
        bad = dict(DEMO_RULES, excluded_families=[dict(DEMO_RULES["excluded_families"][0], family="Okafur")])
        fields = [p.field for p in rules.check(rules.Rules.from_json(bad), demo.NOTICE, self.season.payments)]
        self.assertEqual(fields, ["excluded:Okafur", "excluded:Okafur"])

    def test_open_questions_block_approval(self):
        asked = dict(DEMO_RULES, questions=["Should scholarship families get a share?"])
        problems = rules.check(rules.Rules.from_json(asked), demo.NOTICE, self.season.payments)
        self.assertEqual([p.field for p in problems], ["question"])

    def test_plan_adds_up_and_skips_the_excluded_family(self):
        r = rules.Rules.from_json(DEMO_RULES)
        lines = {line.family_id: line for line in rules.plan(r, self.season.payments, demo.TITLE)}
        self.assertEqual(sum(line.share_cents for line in lines.values()), 100000)
        okafor = next(p for p in self.season.payments if p.family_name == "Okafor")
        self.assertEqual((lines[okafor.family_id].share_cents, lines[okafor.family_id].excluded != ""), (0, True))
        by_name = {p.family_name: lines[p.family_id] for p in self.season.payments}
        self.assertEqual(by_name["Alvarez"].share_cents, to_cents("38.89"))
        self.assertEqual(by_name["Brennan"].share_cents, to_cents("73.33"))
        # Scholarship shares are exactly 16.666...; the two who paid first get the last cents.
        self.assertEqual([by_name[n].share_cents for n in ("Dimitrov", "Ito", "Quinn")], [1667, 1667, 1666])
        self.assertEqual(sum(line.extra_cent for line in lines.values()), 17)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.season = Season(self)

    def test_refuses_to_send_an_unapproved_round(self):
        round_id = self.season.round(approve=False)
        with self.assertRaises(NotApproved):
            run(self.season.ledger, self.season.paypal, round_id, log=lambda m: None)
        self.assertEqual(self.season.paypal.calls, 0)

    def test_clean_run_matches_paypal_to_the_cent(self):
        round_id = self.season.round()
        run(self.season.ledger, self.season.paypal, round_id, log=lambda m: None)
        report = reconcile(self.season.ledger, self.season.paypal, round_id)
        self.assertTrue(report.clean)
        self.assertEqual((report.paid_out_cents, report.difference_cents), (100000, 0))
        self.assertEqual(self.season.paypal.calls, 23)

    def test_crash_after_paypal_answered_is_resumed_without_a_second_refund(self):
        round_id = self.season.round()
        with self.assertRaises(SimulatedCrash):
            run(self.season.ledger, self.season.paypal, round_id, crash_after_send=9, die=crash, log=lambda m: None)
        sending = self.season.ledger.rows(round_id, states=("SENDING",))
        self.assertEqual(len(sending), 1)

        run(self.season.ledger, self.season.paypal, round_id, log=lambda m: None)
        row = next(r for r in self.season.ledger.rows(round_id) if r.family_id == sending[0].family_id)
        self.assertEqual((row.state, row.replayed), ("DONE", True))
        report = reconcile(self.season.ledger, self.season.paypal, round_id)
        self.assertTrue(report.clean)
        self.assertEqual(report.difference_cents, 0)

    def test_same_crash_without_request_ids_pays_one_family_twice(self):
        round_id = self.season.round(use_request_ids=False)
        with self.assertRaises(SimulatedCrash):
            run(self.season.ledger, self.season.paypal, round_id, crash_after_send=9, die=crash, log=lambda m: None)
        run(self.season.ledger, self.season.paypal, round_id, log=lambda m: None)
        report = reconcile(self.season.ledger, self.season.paypal, round_id)
        self.assertFalse(report.clean)
        twice = [f for f in report.families if f.status == "UNRECORDED_REFUND"]
        self.assertEqual(len(twice), 1)
        self.assertEqual(report.difference_cents, twice[0].unrecorded[0][1])

    def test_lost_answer_stays_sending_then_resolves_on_the_next_run(self):
        round_id = self.season.round()
        target = self.season.ledger.rows(round_id, states=("PLANNED",))[3]
        self.season.paypal.drop_answer_once.add(target.capture_id)
        run(self.season.ledger, self.season.paypal, round_id, log=lambda m: None)
        self.assertEqual(self.season.ledger.rows(round_id, states=("SENDING",))[0].family_id, target.family_id)

        run(self.season.ledger, self.season.paypal, round_id, log=lambda m: None)
        self.assertEqual(self.season.ledger.rows(round_id, states=("SENDING",)), [])
        self.assertTrue(reconcile(self.season.ledger, self.season.paypal, round_id).clean)

    def test_paypal_refusal_is_held_and_not_retried(self):
        round_id = self.season.round()
        target = self.season.ledger.rows(round_id, states=("PLANNED",))[0]
        self.season.paypal.fail_next[target.capture_id] = RefundOutcome(422, error="TRANSACTION_REFUSED")
        run(self.season.ledger, self.season.paypal, round_id, log=lambda m: None)
        calls = self.season.paypal.calls
        run(self.season.ledger, self.season.paypal, round_id, log=lambda m: None)
        self.assertEqual(self.season.paypal.calls, calls)
        held = self.season.ledger.rows(round_id, states=("HELD",))
        self.assertEqual([(r.family_id, r.detail) for r in held], [(target.family_id, "TRANSACTION_REFUSED")])
        report = reconcile(self.season.ledger, self.season.paypal, round_id)
        self.assertEqual(report.difference_cents, -target.share_cents)

    def test_later_rounds_see_refunds_already_sent(self):
        round_id = self.season.round()
        run(self.season.ledger, self.season.paypal, round_id, log=lambda m: None)
        alvarez = next(p for p in self.season.ledger.payments() if p.family_name == "Alvarez")
        self.assertEqual(alvarez.refunded_before_cents, 3889)

    def test_plan_that_does_not_add_up_is_rejected(self):
        r = rules.Rules.from_json(DEMO_RULES)
        lines = rules.plan(r, self.season.payments, demo.TITLE)
        with self.assertRaises(ValueError):
            self.season.ledger.create_round(demo.TITLE, demo.NOTICE, r.to_json(), r.pool_cents + 1, lines)


if __name__ == "__main__":
    unittest.main()
