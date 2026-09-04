from datetime import date
from unittest import TestCase

from shared.utils.scheme_date_engine import generate_scheme_schedule


class ReadyToRedeemDurationTests(TestCase):
    def test_10_plus_1_maturity_is_11th_month_end(self):
        """Start 1 Feb 2026, 10 payable + 1 bonus → maturity 31 Dec 2026."""
        out = generate_scheme_schedule(date(2026, 2, 1), installment_months=10, bonus_months=1)
        self.assertEqual(out["start_date"], date(2026, 2, 1))
        self.assertEqual(out["maturity_date"], date(2026, 12, 31))
        self.assertEqual(len(out["installment_dates"]), 10)
        self.assertEqual(len(out["bonus_dates"]), 1)
        self.assertEqual(out["bonus_dates"][0], date(2026, 12, 31))
