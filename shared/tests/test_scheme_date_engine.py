"""
Validation tests for scheme date engine (start date, maturity, installment schedule).

Business rules:
- If enrollment date <= 15 → start_date = 1st of current month
- If enrollment date > 15 → start_date = 1st of next month
- Each installment due date = last day of that installment's month
- Maturity date = last day of (start_date + total_duration_months - 1)
"""
from datetime import date
from django.test import TestCase

from shared.utils.scheme_date_engine import generate_scheme_schedule


class SchemeDateEngineTests(TestCase):
    """Test generate_scheme_schedule for business rules."""

    def test_enrollment_10_feb_start_first_of_same_month(self):
        """If enrollment date <= 15 → start_date = 1st of current month. Due = last day."""
        today = date(2026, 2, 10)
        out = generate_scheme_schedule(today, installment_months=10, bonus_months=1)
        self.assertEqual(out["start_date"], date(2026, 2, 1))
        self.assertEqual(out["installment_dates"][0], date(2026, 2, 28))

    def test_enrollment_15_feb_start_first_of_same_month(self):
        """15th is boundary: <= 15 → 1st of current month. Due = last day."""
        today = date(2026, 2, 15)
        out = generate_scheme_schedule(today, installment_months=10, bonus_months=1)
        self.assertEqual(out["start_date"], date(2026, 2, 1))
        self.assertEqual(out["installment_dates"][0], date(2026, 2, 28))

    def test_enrollment_16_feb_start_first_of_next_month(self):
        """If enrollment date > 15 → start_date = 1st of next month. Due = last day of Mar."""
        today = date(2026, 2, 16)
        out = generate_scheme_schedule(today, installment_months=10, bonus_months=1)
        self.assertEqual(out["start_date"], date(2026, 3, 1))
        self.assertEqual(out["installment_dates"][0], date(2026, 3, 31))

    def test_example_17_feb_tenure_10_bonus_1(self):
        """Today = 17 Feb 2026, Tenure = 10, Bonus = 1 → start 1 Mar, maturity 31 Jan 2027."""
        today = date(2026, 2, 17)
        out = generate_scheme_schedule(today, installment_months=10, bonus_months=1)
        self.assertEqual(out["start_date"], date(2026, 3, 1))
        self.assertEqual(out["maturity_date"], date(2027, 1, 31))
        self.assertEqual(out["installment_dates"][0], date(2026, 3, 31))
        self.assertEqual(out["installment_dates"][1], date(2026, 4, 30))
        self.assertEqual(out["installment_dates"][9], date(2026, 12, 31))
        self.assertEqual(len(out["installment_dates"]), 10)
        self.assertEqual(len(out["bonus_dates"]), 1)
        self.assertEqual(out["bonus_dates"][0], date(2027, 1, 31))

    def test_31_dec_enrollment_start_next_january(self):
        """31 Dec enrollment (> 15) → start_date = 1st Jan next year. Due = Jan 31."""
        today = date(2025, 12, 31)
        out = generate_scheme_schedule(today, installment_months=3, bonus_months=0)
        self.assertEqual(out["start_date"], date(2026, 1, 1))
        self.assertEqual(out["installment_dates"][0], date(2026, 1, 31))
        self.assertEqual(out["installment_dates"][1], date(2026, 2, 28))
        self.assertEqual(out["installment_dates"][2], date(2026, 3, 31))

    def test_leap_year_february_maturity_last_day(self):
        """Maturity and due date use last day of month (e.g. Feb 29 in leap year)."""
        today = date(2024, 2, 1)
        out = generate_scheme_schedule(today, installment_months=1, bonus_months=0)
        self.assertEqual(out["start_date"], date(2024, 2, 1))
        self.assertEqual(out["maturity_date"], date(2024, 2, 29))
        self.assertEqual(out["installment_dates"][0], date(2024, 2, 29))

    def test_non_leap_february_maturity_28(self):
        """Feb in non-leap year → 28."""
        today = date(2025, 2, 1)
        out = generate_scheme_schedule(today, installment_months=1, bonus_months=0)
        self.assertEqual(out["maturity_date"], date(2025, 2, 28))
        self.assertEqual(out["installment_dates"][0], date(2025, 2, 28))

    def test_total_duration_maturity_month(self):
        """Maturity = last day of (start_date + total_duration_months - 1)."""
        today = date(2026, 3, 1)
        out = generate_scheme_schedule(today, installment_months=10, bonus_months=1)
        self.assertEqual(out["start_date"], date(2026, 3, 1))
        self.assertEqual(out["maturity_date"], date(2027, 1, 31))
        self.assertEqual(out["installment_dates"][0], date(2026, 3, 31))
        self.assertEqual(out["installment_dates"][9], date(2026, 12, 31))

    def test_mar_1_enrollment_10_months(self):
        """Enrollment Mar 1 with 10 months: each due date is the last day of its month."""
        today = date(2026, 3, 1)
        out = generate_scheme_schedule(today, installment_months=10, bonus_months=0)
        self.assertEqual(out["start_date"], date(2026, 3, 1))
        expected = [
            date(2026, 3, 31),
            date(2026, 4, 30),
            date(2026, 5, 31),
            date(2026, 6, 30),
            date(2026, 7, 31),
            date(2026, 8, 31),
            date(2026, 9, 30),
            date(2026, 10, 31),
            date(2026, 11, 30),
            date(2026, 12, 31),
        ]
        self.assertEqual(out["installment_dates"], expected)
        self.assertEqual(out["maturity_date"], date(2026, 12, 31))
