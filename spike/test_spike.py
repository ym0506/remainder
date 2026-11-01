"""Offline checks for the helpers in spike_refunds.py. No network, no credentials."""

import unittest

from spike_refunds import (
    capture_id_of,
    cents_of,
    error_name,
    money,
    parse_env,
    refunds_of,
    still_refundable,
    summary_lines,
)

# Shape follows the Orders v2 schema: purchase_units[].payments is a payment_collection
# with captures[] and refunds[].
ORDER = {
    "id": "5O190127TN364715T",
    "status": "COMPLETED",
    "purchase_units": [
        {
            "reference_id": "family-01",
            "payments": {
                "captures": [{"id": "3C679366HH908993F", "status": "PARTIALLY_REFUNDED"}],
                "refunds": [
                    {"id": "1JU08902781691411", "status": "COMPLETED", "amount": {"value": "10.00"}},
                    {"id": "2KV19013892702522", "status": "COMPLETED", "amount": {"value": "10.00"}},
                ],
            },
        }
    ],
}


class MoneyTest(unittest.TestCase):
    def test_money_formats_cents(self):
        self.assertEqual(money(12000), "120.00")
        self.assertEqual(money(5), "0.05")
        self.assertEqual(money(10801), "108.01")

    def test_money_rejects_negative(self):
        with self.assertRaises(ValueError):
            money(-1)

    def test_cents_of_is_exact(self):
        self.assertEqual(cents_of("110.00"), 11000)
        self.assertEqual(cents_of("0.29"), 29)
        self.assertEqual(cents_of("60"), 6000)

    def test_cents_of_rejects_fractions_of_a_cent(self):
        with self.assertRaises(ValueError):
            cents_of("33.333")

    def test_round_trip_for_every_cent_up_to_ten_dollars(self):
        for cents in range(0, 1001):
            self.assertEqual(cents_of(money(cents)), cents)


class ParsingTest(unittest.TestCase):
    def test_parse_env_ignores_comments_and_quotes(self):
        text = "# sandbox\nPAYPAL_CLIENT_ID='abc'\n\nPAYPAL_CLIENT_SECRET=\"x=y\"\nBROKEN LINE\n"
        self.assertEqual(parse_env(text), {"PAYPAL_CLIENT_ID": "abc", "PAYPAL_CLIENT_SECRET": "x=y"})

    def test_capture_and_refunds_are_read_from_payments(self):
        self.assertEqual(capture_id_of(ORDER), "3C679366HH908993F")
        self.assertEqual([r["id"] for r in refunds_of(ORDER)], ["1JU08902781691411", "2KV19013892702522"])

    def test_order_without_payments(self):
        empty = {"purchase_units": [{"reference_id": "family-02"}]}
        self.assertIsNone(capture_id_of(empty))
        self.assertEqual(refunds_of(empty), [])

    def test_error_name_prefers_issue(self):
        payload = {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "REFUND_AMOUNT_EXCEEDED"}]}
        self.assertEqual(error_name(payload), "REFUND_AMOUNT_EXCEEDED")
        self.assertEqual(error_name({"name": "INVALID_REQUEST"}), "INVALID_REQUEST")


class RefundableTest(unittest.TestCase):
    def test_failed_and_cancelled_refunds_do_not_count(self):
        payment = {"cents": 12000}
        refunds = [
            {"value": "10.00", "status": "COMPLETED"},
            {"value": "5.00", "status": "PENDING"},
            {"value": "7.00", "status": "FAILED"},
            {"value": "3.00", "status": "CANCELLED"},
        ]
        self.assertEqual(still_refundable(payment, refunds), 10500)


class SummaryTest(unittest.TestCase):
    def test_summary_when_payments_failed(self):
        lines = summary_lines({"E1": {"captured": 0, "asked": 3}})
        self.assertEqual(lines, ["E1 card payments captured without buyer approval: 0/3"])

    def test_summary_reports_unknown_when_lookup_failed(self):
        findings = {"E1": {"captured": 3, "asked": 3}, "E2": {"refunds_on_capture": None}}
        self.assertIn("unknown", summary_lines(findings)[1])


if __name__ == "__main__":
    unittest.main()
