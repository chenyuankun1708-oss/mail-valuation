import json
import shutil
import subprocess
import unittest

from valuation_app.static import HTML


def helper(name):
    prefix = "function %s" % name
    return "\n".join(line for line in HTML.splitlines() if line.startswith(prefix))


@unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript tests")
class UnderlyingSelectionTest(unittest.TestCase):
    def test_totals_only_sum_supplied_selected_items(self):
        source = """
%s
const selected=%s;
console.log(JSON.stringify(underlyingTotals(selected)));
""" % (helper("underlyingTotals"), json.dumps([
            {"stock_market_value": 100, "index_futures_long": 20,
             "index_futures_short": 5, "long_exposure": 115},
            {"stock_market_value": 50, "index_futures_long": None,
             "index_futures_short": 10, "long_exposure": 40},
        ]))
        result = json.loads(subprocess.check_output(["node", "-e", source]).decode("utf-8"))
        self.assertEqual(result, {"count": 2, "stock": 150, "long": 20,
                                  "short": 15, "exposure": 155})


if __name__ == "__main__":
    unittest.main()
