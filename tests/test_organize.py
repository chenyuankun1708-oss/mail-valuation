import os
import tempfile
import unittest
from unittest.mock import patch

from valuation_app.organize import organize_products
from valuation_app.parser import Snapshot


class OrganizeTest(unittest.TestCase):
    def test_copy_keeps_source_and_skips_same_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            staging = os.path.join(root, "FOF", "incoming")
            os.makedirs(staging)
            source = os.path.join(staging, "sample.xls")
            with open(source, "wb") as handle:
                handle.write(b"valuation")
            snapshot = Snapshot("测试产品", "2026-07-31", 1.0, 1.0, 100.0, 100.0, source)
            with patch("valuation_app.organize.parse_valuation", return_value=snapshot):
                first = organize_products(root)
                second = organize_products(root)
            target = os.path.join(root, "测试产品", "2026-07-31_sample.xls")
            self.assertTrue(os.path.exists(source))
            self.assertTrue(os.path.exists(target))
            self.assertEqual(len(first["copied"]), 1)
            self.assertEqual(len(second["duplicates_skipped"]), 1)


if __name__ == "__main__":
    unittest.main()
