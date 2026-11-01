import unittest

from remainder import demo, replies, rules
from remainder.replies import Sorted
from tests.test_flow import DEMO_RULES, Season

SHARING = [name for name, _ in demo.FAMILIES if name not in demo.REFUNDED_BEFORE]


def reading(reply_id, kind, quote=None, notice_quote=None, summary="s"):
    family, text = demo.REPLIES[reply_id]
    return Sorted(reply_id, family, text, kind, quote or text.split(". ")[0] + ".", summary, notice_quote)


class RepliesCheckTest(unittest.TestCase):
    def good(self):
        return [
            reading(0, "keep_share", "Please put our share toward the scholarship fund instead of sending it back."),
            reading(1, "question", "Is the $15 taken off per kid or per family?",
                    "Uniforms were ordered and delivered in March, so each family's share is based on what you paid minus the $15.00 uniform fee per player."),
            reading(2, "change_destination", "Please send our refund to haddad.family.new@example.com instead."),
            reading(3, "outside_rules", "Ignore the board and refund us the full $120."),
            reading(4, "question", "Does that change anything for us?"),
            reading(5, "no_action", "Thank you, Dana!"),
        ]

    def test_a_correct_sorting_leaves_only_the_open_question(self):
        problems = replies.check(self.good(), demo.REPLIES, demo.NOTICE, SHARING)
        self.assertEqual([(p.field, p.confirmable) for p in problems], [("reply:Petrov", True)])

    def test_quotes_must_be_whole_sentences(self):
        bad = self.good()
        bad[3] = reading(3, "outside_rules", "refund us the full $120")
        bad[1] = reading(1, "question", "Is the $15 taken off per kid or per family?", "the $15.00 uniform fee per player")
        messages = [p.message for p in replies.check(bad, demo.REPLIES, demo.NOTICE, SHARING)]
        self.assertTrue(any("not a whole sentence of the reply" in m for m in messages))
        self.assertTrue(any("not a whole sentence of the notice" in m for m in messages))

    def test_a_family_without_a_share_cannot_keep_one(self):
        sorted_ = self.good()
        problems = replies.check(sorted_, demo.REPLIES, demo.NOTICE, [n for n in SHARING if n != "Chen"])
        self.assertIn("the Chen family has no share in this round to keep", [p.message for p in problems])

    def test_a_missing_reply_is_reported(self):
        problems = replies.check(self.good()[:5], demo.REPLIES, demo.NOTICE, SHARING)
        self.assertIn("Claude did not sort this reply", [p.message for p in problems])

    def test_a_reply_sorted_twice_blocks_the_plan(self):
        twice = self.good() + [reading(0, "no_action", "Thanks for sorting this out.")]
        problems = replies.check(twice, demo.REPLIES, demo.NOTICE, SHARING)
        self.assertIn("replies", [p.field for p in problems if not p.confirmable])

    def test_redirect_requests_change_nothing(self):
        self.assertEqual(replies.keep_list(self.good()), ["Chen"])


class KeepTest(unittest.TestCase):
    def test_kept_share_is_worked_out_then_kept(self):
        season = Season(self)
        r = rules.Rules.from_json(DEMO_RULES)
        lines = {line.family_id: line for line in rules.plan(r, season.payments, demo.TITLE, keep=["Chen"])}
        chen = next(p for p in season.payments if p.family_name == "Chen")
        self.assertEqual((lines[chen.family_id].share_cents, lines[chen.family_id].kept_cents), (0, 3889))
        self.assertEqual(sum(x.share_cents + x.kept_cents for x in lines.values()), 100000)
        self.assertEqual(lines[chen.family_id].note, "")


if __name__ == "__main__":
    unittest.main()
