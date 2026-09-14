import json
import unittest

from valuation_app import api_v2


class VersionTwoProjectionTest(unittest.TestCase):
    def page(self):
        points = [{"valuation_date": "2026-01-01", "nav": 1, "accumulated_nav": 1,
                   "net_assets": 100, "shares": 100, "holdings": []},
                  {"valuation_date": "2026-02-01", "nav": 1.1, "accumulated_nav": 1.1,
                   "net_assets": 110, "shares": 100, "holdings": []}]
        return {"page_updated_at": "2026-09-14T12:00:00", "default_start": "2026-01-01",
                "default_end": "2026-02-01", "products": [{"product_id": "top-001", "name": "一",
                    "points": points, "flows": [], "total_investment": 0,
                    "approved_quota": 0}], "benchmarks": {"indices": {"000852": {"points": []}}},
                "labels": {"matches": {}, "deduped_records": []}}

    def test_bootstrap_stays_below_first_screen_budget(self):
        payload = api_v2.bootstrap(self.page(), {})
        self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")), 1024 * 1024)

    def test_date_benchmark_scope_and_pagination_are_bounded(self):
        with self.assertRaises(ValueError):
            api_v2.iso_date("../secret", "start")
        with self.assertRaises(ValueError):
            api_v2.overview(self.page(), "2026-01-01", "2026-02-01", "custom")
        with self.assertRaises(KeyError):
            api_v2.top_returns(self.page(), "../../secret", "2026-01-01", "2026-02-01", "000852")


if __name__ == "__main__":
    unittest.main()
