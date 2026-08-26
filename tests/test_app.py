import unittest
from unittest.mock import patch

import app


class RefreshCommandTest(unittest.TestCase):
    def test_refresh_output_is_safe_for_gbk_logs(self):
        output = app.refresh_output({"file": "估值报表_\ufffd.xls"})
        output.encode("gbk")
        self.assertIn("\\ufffd", output)

    @patch("app.update_factor_cache", return_value={"available": True, "errors": []})
    @patch("app.download_underlying_archives", return_value={"downloaded": [], "failures": []})
    @patch("app.os.path.exists", return_value=False)
    @patch("app.update_risk_cache", return_value={"successes": ["IF", "IC", "IM"], "failures": []})
    @patch("app.update_benchmark_cache", return_value={"successes": ["000852", "000905", "000300"], "failures": []})
    @patch("app.build_index", return_value=("index.html", {"summary": {"analyzable_products": 15}}))
    @patch("app.organize_products", return_value={"copied": [], "duplicates_skipped": [], "errors": []})
    @patch("app.download_valuations", side_effect=RuntimeError("one mailbox failed"))
    def test_refresh_builds_even_when_download_fails(self, download, organize, build, benchmark, risk,
                                                     exists, underlying_mail, factor):
        result = app.refresh_data("products")
        self.assertIn("one mailbox failed", result["download"]["failures"][0])
        organize.assert_called_once_with("products")
        build.assert_called_once_with("products")
        benchmark.assert_called_once_with()
        risk.assert_called_once_with()
        self.assertEqual(result["build"]["path"], "index.html")


if __name__ == "__main__":
    unittest.main()
