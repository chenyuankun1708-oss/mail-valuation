import json
import os
import subprocess
import unittest


class AttributionJavascriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "..", "valuation_app", "static.py")
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        cls.map_source = next(line for line in lines if line.startswith("function attributionHoldingMap("))
        cls.interval_source = next(line for line in lines if line.startswith("function attributionInterval("))

    def run_interval(self):
        first = {"valuation_date": "2026-08-01", "holdings": [
            {"code": "A", "name": "存续", "quantity": 10, "price": 2, "market_value": 20},
            {"code": "C", "name": "清仓", "quantity": 5, "price": 4, "market_value": 20},
        ]}
        second = {"valuation_date": "2026-08-02", "holdings": [
            {"code": "A", "name": "存续", "quantity": 12, "price": 3, "market_value": 36},
            {"code": "N", "name": "新增", "quantity": 2, "price": 5, "market_value": 10},
        ]}
        script = ("const holdingLabel=h=>({primary:'股票指增',manager:'管理人'});" + self.map_source +
                  self.interval_source + "process.stdout.write(JSON.stringify(attributionInterval('FOF'," +
                  json.dumps(first, ensure_ascii=False) + "," + json.dumps(second, ensure_ascii=False) + ")));" )
        return json.loads(subprocess.check_output(["node", "-e", script], text=True, encoding="utf-8"))

    def test_existing_position_obeys_decomposition_identity(self):
        row = next(item for item in self.run_interval() if item["code"] == "A")
        self.assertEqual(row["action"], "增持")
        self.assertEqual(row["price_impact"], 10)
        self.assertEqual(row["position_amount"], 6)
        self.assertEqual(row["market_change"], 16)
        self.assertEqual(row["formula_difference"], 0)
        self.assertEqual(row["quality"], "可归属")

    def test_new_and_closed_positions_are_partial_estimates(self):
        rows = {item["code"]: item for item in self.run_interval()}
        self.assertEqual(rows["N"]["position_amount"], 10)
        self.assertEqual(rows["N"]["quality"], "部分可归属")
        self.assertEqual(rows["C"]["position_amount"], -20)
        self.assertEqual(rows["C"]["quality"], "部分可归属")


if __name__ == "__main__":
    unittest.main()
