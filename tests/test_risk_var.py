import json
import math
import shutil
import subprocess
import unittest

from valuation_app.static import HTML


class RiskVarJavascriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("Node.js is unavailable")
        lines = HTML.splitlines()
        cls.source = "\n".join(next(line for line in lines if line.startswith(prefix))
                               for prefix in ("function quantile", "function historicalVar",
                                              "function basisVar"))

    def run_js(self, expression):
        completed = subprocess.run(["node", "-e", self.source +
                                    "\nconsole.log(JSON.stringify(" + expression + "));"],
                                   check=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, universal_newlines=True)
        return json.loads(completed.stdout)

    def test_historical_one_day_var(self):
        result = self.run_js("historicalVar([{close:100},{close:90},{close:99}],p=>p.close)")
        returns = [math.log(.9) * 100, math.log(1.1) * 100]
        expected_90 = -(returns[0] + (returns[1] - returns[0]) * .1)
        self.assertEqual(result["count"], 2)
        self.assertAlmostEqual(result["values"][0]["value"], expected_90, places=9)

    def test_basis_var_uses_daily_percentage_point_change(self):
        result = self.run_js("basisVar([{'当月':{value:10}},{'当月':{value:5}},{'当月':{value:7}}],'当月')")
        self.assertEqual(result["count"], 2)
        self.assertAlmostEqual(result["values"][0]["value"], 4.3, places=9)
        empty = self.run_js("basisVar([],'当月')")
        self.assertIsNone(empty["values"][0]["value"])


if __name__ == "__main__":
    unittest.main()
