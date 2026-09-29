import json
import os
import tempfile
import unittest

import pandas as pd

from valuation_app.otc_backtest import (Terms, _single, export_excel,
                                        parameter_definitions, run_backtest,
                                        validate_request)
from valuation_app.otc_report import export_pdf


class OtcBacktestTest(unittest.TestCase):
    def test_request_uses_fixed_structure_index_and_parameter_bounds(self):
        request = validate_request({
            "structure": "classic_snowball", "index_code": "000852",
            "start_date": "2024-01-01", "end_date": "2026-01-01",
        })
        self.assertEqual(request["term_months"], 24)
        self.assertEqual(request["product_name"], "经典雪球－中证1000－2026-01-01")
        named = validate_request(dict(request, product_name="虹桥雪球一号"))
        self.assertEqual(named["product_name"], "虹桥雪球一号")
        with self.assertRaises(ValueError):
            validate_request(dict(request, index_code="000016"))
        with self.assertRaises(ValueError):
            validate_request(dict(request, path="../secret"))
        with self.assertRaises(ValueError):
            validate_request(dict(request, term_months=61))
        with self.assertRaises(ValueError):
            validate_request(dict(request, product_name="../report.pdf"))

    def test_month_end_observation_is_derived_from_entry_and_rolled_forward(self):
        dates = pd.bdate_range("2024-01-31", "2024-05-03")
        frame = pd.DataFrame({"date": dates, "price": 90.0})
        frame.loc[frame["date"] == pd.Timestamp("2024-01-31"), "price"] = 100.0
        frame.loc[frame["date"] == pd.Timestamp("2024-04-30"), "price"] = 101.0
        terms = Terms(3, 1, .68, 1.0, 0, .12, .08, 2, None, .8, .01)
        row = _single(frame, 0, terms, "classic_snowball")
        self.assertEqual(row["exit_date"], "2024-04-30")
        self.assertEqual(row["exit_observation_month"], 3)
        self.assertEqual(row["holding_calendar_days"], 90)
        self.assertAlmostEqual(row["annualized_return"], .08)

    def test_barrier_equality_is_not_knock_out(self):
        dates = pd.bdate_range("2024-01-02", "2024-03-04")
        frame = pd.DataFrame({"date": dates, "price": 100.0})
        frame.loc[frame["date"] >= pd.Timestamp("2024-03-04"), "price"] = 101.0
        terms = Terms(2, 1, .7, 1.0, 0, .12, .12, 1, None, .8, .01)
        row = _single(frame, 0, terms, "classic_snowball")
        self.assertEqual(row["exit_observation_month"], 2)

    def test_local_wind_cache_only_and_combo_weights_are_not_normalized(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "index.json")
            points = []
            for day in pd.bdate_range("2024-01-02", "2024-07-05"):
                points.append({"date": day.strftime("%Y-%m-%d"), "close": 100 + len(points) * .1})
            payload = {"source": "Wind Oracle数据库", "updated_at": "2024-07-05",
                       "indices": {"000852": {"points": points, "updated_at": "2024-07-05"}}}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            request = {"structure": "dcn_snowball_combo", "index_code": "000852",
                       "start_date": "2024-01-02", "end_date": "2024-07-05",
                       "term_months": 3, "lock_period_months": 1,
                       "dcn_weight": 1, "snowball_weight": .2}
            result = run_backtest(request, path)
            self.assertEqual(result["report_schema_version"], 2)
            self.assertEqual(len(result["report_pages"]), 16)
            self.assertEqual(set(result["report_components"]), {"dcn", "snowball", "combo"})
            report_path = os.path.join(root, "combo.pdf")
            export_pdf(result, report_path)
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader
            self.assertEqual(len(PdfReader(report_path).pages), 16)
            row = next(item for item in result["samples"] if not item["still_running"])
            expected = row["dcn_absolute_return"] + .2 * row["snowball_absolute_return"]
            self.assertAlmostEqual(row["absolute_return"], expected)
            payload["source"] = "simulated"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaises(ValueError):
                run_backtest(request, path)

    def test_combo_uses_separate_component_loss_caps(self):
        request = validate_request({"structure": "dcn_snowball_combo", "index_code": "000852",
                                    "start_date": "2024-01-01", "end_date": "2026-01-01",
                                    "dcn_max_loss": .5, "snowball_max_loss": .8})
        self.assertEqual(request["dcn_max_loss"], .5)
        self.assertEqual(request["snowball_max_loss"], .8)

    def test_html_number_steps_accept_registered_defaults(self):
        definitions = {item["name"]: item for item in parameter_definitions()}
        for name in ("dcn_weight", "snowball_weight"):
            item = definitions[name]
            steps = (item["default"] - item["min"]) / item["step"]
            self.assertAlmostEqual(steps, round(steps))
        self.assertEqual(definitions["dcn_weight"]["min"], .01)
        self.assertEqual(definitions["max_loss"]["min"], .01)

    def test_pdf_is_generated_from_the_same_canonical_result(self):
        try:
            from pypdf import PdfReader
        except ImportError:  # developer image may expose the compatible package name
            from PyPDF2 import PdfReader
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "report.pdf")
            result = {"structure": "classic_snowball", "structure_name": "经典雪球",
                      "product_name": "报告测试产品", "report_schema_version": 2,
                      "index_name": "中证1000", "request": {"term_months": 24},
                      "source": {"actual_start_date": "2020-01-01", "actual_end_date": "2026-01-01",
                                 "cache_sha256": "a" * 64},
                      "summary": {"total_samples": 1, "completed_samples": 1,
                                  "still_running_samples": 0, "average_absolute_return": .1},
                      "charts": {"sample_returns": [{"date": "2020-01-01", "value": .1}],
                                 "holding_months": [{"month": 12, "count": 1}]},
                      "samples": [{"entry_date": "2020-01-01", "entry_price": 100,
                                   "exit_date": "2021-01-01", "holding_calendar_days": 366,
                                   "knocked_in": False, "knocked_out": True,
                                   "absolute_return": .1, "annualized_return": .1}]}
            export_pdf(result, path)
            reader = PdfReader(path)
            self.assertEqual(len(reader.pages), 5)
            self.assertNotIn("SHA-256", "\n".join(page.extract_text() or "" for page in reader.pages))
            self.assertGreater(os.path.getsize(path), 1000)

            excel_path = os.path.join(root, "report.xlsx")
            export_excel(result, excel_path)
            parameters = pd.read_excel(excel_path, sheet_name="参数与报告信息", header=None)
            self.assertNotIn("market_sha256", parameters.astype(str).values)


if __name__ == "__main__":
    unittest.main()
