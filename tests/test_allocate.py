import random
import unittest
from fractions import Fraction

from remainder.allocate import AllocationError, Claim, allocate


def claim(key, weight, cap=None, tie=None):
    return Claim(key, weight, weight * 10 if cap is None else cap, tie or key)


class AllocateTest(unittest.TestCase):
    def test_demo_season_leaves_seventeen_cents_to_place(self):
        # 15 single players, 5 sibling pairs, 3 scholarship players; uniform fee already removed.
        claims = (
            [claim(f"single-{i:02d}", 10500, 12000) for i in range(15)]
            + [claim(f"pair-{i}", 19800, 22800) for i in range(5)]
            + [claim(f"scholar-{i}", 4500, 6000, tie=f"t{i}") for i in range(3)]
        )
        shares = {s.key: s for s in allocate(100000, claims)}
        self.assertEqual(sum(s.cents for s in shares.values()), 100000)
        self.assertEqual(sum(s.extra_cent for s in shares.values()), 17)
        self.assertTrue(all(shares[f"single-{i:02d}"].cents == 3889 for i in range(15)))
        self.assertTrue(all(shares[f"pair-{i}"].cents == 7333 for i in range(5)))
        # Two of the three scholarship shares (exactly 1666.67) get the last two cents, by tie_key.
        self.assertEqual([shares[f"scholar-{i}"].cents for i in range(3)], [1667, 1667, 1666])

    def test_exact_shares_need_no_extra_cents(self):
        shares = allocate(900, [claim("a", 100), claim("b", 200)])
        self.assertEqual([s.cents for s in shares], [300, 600])
        self.assertFalse(any(s.extra_cent for s in shares))

    def test_one_dollar_three_ways(self):
        shares = allocate(100, [claim("a", 1, 100, "2"), claim("b", 1, 100, "1"), claim("c", 1, 100, "3")])
        self.assertEqual({s.key: s.cents for s in shares}, {"a": 33, "b": 34, "c": 33})

    def test_zero_weight_gets_nothing(self):
        shares = allocate(1000, [claim("a", 0, cap=5000), claim("b", 300)])
        self.assertEqual([s.cents for s in shares], [0, 1000])

    def test_cap_is_respected_and_rest_is_reshared(self):
        # "a" would get 500 but only 100 is left on its payment.
        shares = allocate(1000, [claim("a", 100, cap=100), claim("b", 50), claim("c", 50)])
        self.assertEqual({s.key: (s.cents, s.capped) for s in shares}, {"a": (100, True), "b": (450, False), "c": (450, False)})

    def test_rejects_pool_larger_than_refundable(self):
        with self.assertRaises(AllocationError):
            allocate(1001, [claim("a", 1, cap=500), claim("b", 1, cap=500)])

    def test_rejects_pool_with_no_one_to_pay(self):
        with self.assertRaises(AllocationError):
            allocate(1, [claim("a", 0)])

    def test_zero_pool(self):
        self.assertEqual([s.cents for s in allocate(0, [claim("a", 5)])], [0])
        self.assertEqual(allocate(0, []), [])

    def test_rejects_duplicate_keys_and_negatives(self):
        with self.assertRaises(AllocationError):
            allocate(10, [claim("a", 1), claim("a", 2)])
        with self.assertRaises(AllocationError):
            allocate(10, [claim("a", -1)])

    def test_random_cases_hold_the_invariants(self):
        rng = random.Random(20261101)
        for _ in range(3000):
            n = rng.randint(1, 40)
            claims = [
                Claim(f"k{i}", rng.randint(0, 50000), rng.randint(0, 60000), f"t{rng.randint(0, 5)}")
                for i in range(n)
            ]
            room = sum(c.cap for c in claims if c.weight > 0)
            if room == 0:
                continue
            pool = rng.randint(0, room)
            shares = allocate(pool, claims)
            self.assertEqual(sum(s.cents for s in shares), pool)
            for c, s in zip(claims, shares):
                self.assertLessEqual(s.cents, c.cap)
                if c.weight == 0:
                    self.assertEqual(s.cents, 0)
                elif not s.capped:
                    # Never more than one cent away from the exact share.
                    self.assertLess(abs(Fraction(s.cents) - s.exact), 1)
            self.assertEqual(shares, allocate(pool, claims))


if __name__ == "__main__":
    unittest.main()
