import unittest
from datetime import date

from monthly_report.generate_report import cutoff_bounds


class MonthlyReportBoundaryTest(unittest.TestCase):
    def test_exact_cutoff_uses_prior_month_end(self):
        start, end = cutoff_bounds("2026-08-27")
        self.assertEqual(start, date(2026, 7, 31))
        self.assertEqual(end, date(2026, 8, 27))

    def test_january_rolls_to_prior_year(self):
        start, end = cutoff_bounds("2027-01-15")
        self.assertEqual(start, date(2026, 12, 31))
        self.assertEqual(end, date(2027, 1, 15))

    def test_invalid_cutoff_is_rejected(self):
        with self.assertRaises(ValueError):
            cutoff_bounds("2026-02-30")


if __name__ == "__main__":
    unittest.main()
