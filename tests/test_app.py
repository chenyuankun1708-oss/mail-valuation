import io
import os
import unittest
from unittest.mock import ANY, patch

import app
from valuation_app.timing import TimingRecorder, format_duration


class RefreshCommandTest(unittest.TestCase):
    def test_refresh_script_is_windows_powershell_safe_and_prefers_venv(self):
        root = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(root, "scripts", "refresh.ps1")
        with open(path, "r", encoding="utf-8") as handle:
            script = handle.read()
        script.encode("ascii")
        self.assertIn(".venv\\Scripts\\python.exe", script)
        self.assertIn("& $PythonExe app.py refresh --latest-only", script)
        self.assertIn("& $PythonExe app.py backup", script)
        self.assertNotIn("& python app.py", script)
        self.assertNotIn("Tee-Object -FilePath $LogPath", script)
        self.assertIn("Out-File -LiteralPath $LogPath -Encoding utf8 -Append", script)
        self.assertIn("$BackupExitCode = $LASTEXITCODE", script)
        self.assertIn("if ($BackupExitCode -ne 0) { $ExitCode = $BackupExitCode }", script)

    def test_refresh_output_is_safe_for_gbk_logs(self):
        output = app.refresh_output({"download": {"downloaded": ["估值报表_\ufffd.xls"]},
                                     "factor": {"products": [{"holdings": list(range(10000))}]}})
        output.encode("gbk")
        self.assertNotIn("估值报表", output)
        self.assertIn('"downloaded": 1', output)
        self.assertIn('"products": 1', output)
        self.assertLess(len(output), 5000)

    @patch("app.update_market_research_cache", return_value={"modules": {}, "errors": []})
    @patch("app.update_factor_cache", return_value={"available": True, "errors": []})
    @patch("app.download_underlying_archives", return_value={"downloaded": [], "failures": []})
    @patch("app.os.path.exists", return_value=False)
    @patch("app.update_risk_cache", return_value={"successes": ["IF", "IC", "IM"], "failures": []})
    @patch("app.update_benchmark_cache", return_value={"successes": ["000852", "000905", "000300"], "failures": []})
    @patch("app.build_index", return_value=("index.html", {"summary": {"analyzable_products": 15}}))
    @patch("app.organize_products", return_value={"copied": [], "duplicates_skipped": [], "errors": []})
    @patch("app.download_valuations", side_effect=RuntimeError("one mailbox failed"))
    def test_refresh_builds_even_when_download_fails(self, download, organize, build, benchmark, risk,
                                                     exists, underlying_mail, factor, market_dashboard):
        result = app.refresh_data("products")
        self.assertIn("one mailbox failed", result["download"]["failures"][0])
        organize.assert_called_once_with("products")
        build.assert_called_once_with("products", timing_callback=ANY)
        benchmark.assert_called_once_with()
        risk.assert_called_once_with()
        market_dashboard.assert_called_once_with()
        self.assertEqual(result["build"]["path"], "index.html")
        self.assertIn("timing", result)
        self.assertEqual(result["timing"]["steps"][0]["status"], "failed")

    def test_timing_recorder_uses_monotonic_clock_and_separates_groups(self):
        values = iter([10.0, 11.0, 12.25, 13.0])
        output = io.StringIO()
        timer = TimingRecorder(stream=output, clock=lambda: next(values))
        with timer.step("邮件", "邮件与文件"):
            pass
        summary = timer.summary()
        self.assertEqual(summary["steps"][0]["seconds"], 1.25)
        self.assertEqual(summary["groups"]["邮件与文件"], 1.25)
        self.assertEqual(summary["total_seconds"], 3.0)
        self.assertIn("[计时] 开始：邮件", output.getvalue())
        self.assertEqual(format_duration(198.6), "3分18.6秒")


if __name__ == "__main__":
    unittest.main()
