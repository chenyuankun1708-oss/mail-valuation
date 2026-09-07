import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from unittest.mock import patch

from valuation_app.parser import Snapshot
from valuation_app.valuation_archive import build_valuation_archive


class ValuationArchiveTest(unittest.TestCase):
    def test_exact_date_files_are_zipped_without_forward_fill(self):
        with tempfile.TemporaryDirectory() as root:
            product_a = os.path.join(root, "产品A")
            product_b = os.path.join(root, "产品B")
            os.makedirs(product_a)
            os.makedirs(product_b)
            path_a = os.path.join(product_a, "A.xlsx")
            path_b = os.path.join(product_b, "B.xlsx")
            with open(path_a, "wb") as handle:
                handle.write(b"a")
            with open(path_b, "wb") as handle:
                handle.write(b"b")
            snapshots = [
                Snapshot("产品A", "2026-09-02", 1, 1, 1, 1, path_a, []),
                Snapshot("产品B", "2026-09-01", 1, 1, 1, 1, path_b, []),
            ]
            with patch("valuation_app.valuation_archive.scan_valuations",
                       return_value=(snapshots, [])):
                body, count = build_valuation_archive(root, "2026-09-02")
            self.assertEqual(count, 1)
            with zipfile.ZipFile(BytesIO(body)) as archive:
                self.assertEqual(archive.namelist(), ["产品A/A.xlsx"])
                self.assertEqual(archive.read("产品A/A.xlsx"), b"a")

    def test_invalid_or_empty_date_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                build_valuation_archive(root, "2026-02-30")
            with patch("valuation_app.valuation_archive.scan_valuations",
                       return_value=([], [])):
                with self.assertRaises(ValueError):
                    build_valuation_archive(root, "2026-09-02")


if __name__ == "__main__":
    unittest.main()
