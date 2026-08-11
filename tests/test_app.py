import unittest
from unittest.mock import patch

import app


class RefreshCommandTest(unittest.TestCase):
    @patch("app.update_benchmark_cache", return_value={"successes": ["000852", "000905", "000300"], "failures": []})
    @patch("app.build_index", return_value=("index.html", {"summary": {"analyzable_products": 15}}))
    @patch("app.organize_products", return_value={"copied": [], "duplicates_skipped": [], "errors": []})
    @patch("app.download_valuations", side_effect=RuntimeError("one mailbox failed"))
    def test_refresh_builds_even_when_download_fails(self, download, organize, build, benchmark):
        result = app.refresh_data("products")
        self.assertIn("one mailbox failed", result["download"]["failures"][0])
        organize.assert_called_once_with("products")
        build.assert_called_once_with("products")
        benchmark.assert_called_once_with()
        self.assertEqual(result["build"]["path"], "index.html")


if __name__ == "__main__":
    unittest.main()
