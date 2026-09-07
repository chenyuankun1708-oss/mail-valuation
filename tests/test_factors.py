import os
import tempfile
import unittest

from valuation_app.factors import _wind_code, latest_files, standardize


class FactorTests(unittest.TestCase):
    def test_wind_code_only_accepts_a_share_suffix(self):
        self.assertEqual(_wind_code("11020101600000"), "600000.SH")
        self.assertEqual(_wind_code("11020201000001"), "000001.SZ")
        self.assertEqual(_wind_code("11020301830001"), "830001.BJ")
        self.assertIsNone(_wind_code("1102"))

    def test_standardize_winsorizes_and_centers_each_factor(self):
        rows = [("A", "SIZE", 1), ("B", "SIZE", 2), ("C", "SIZE", 100),
                ("A", "VALUE", 5), ("B", "VALUE", 5), ("C", "VALUE", 5)]
        result = standardize(rows)
        self.assertAlmostEqual(sum(result[code]["SIZE"] for code in "ABC"), 0, places=9)
        self.assertEqual(result["A"]["VALUE"], 0)

    def test_latest_files_uses_one_file_per_source_folder(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "产品A")
            os.makedirs(folder)
            for name in ("2025-01-01_(AAA)估值表.xls", "2025-02-01_内部编号_估值表.xls"):
                open(os.path.join(folder, name), "wb").close()
            selected = latest_files(root)
            self.assertEqual(len(selected), 1)
            self.assertIn("2025-02-01", selected[0])


if __name__ == "__main__":
    unittest.main()
