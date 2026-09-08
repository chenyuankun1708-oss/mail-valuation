import json
import shutil
import subprocess
import unittest

from valuation_app.static import HTML


class ProductReturnTableJavascriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("Node.js is unavailable for JavaScript formula tests")
        lines = HTML.splitlines()
        cls.capital_source = next(line for line in lines if line.startswith("function productCapitalUsage("))
        cls.aggregate_source = next(line for line in lines if line.startswith("function productReturnAggregate("))

    def evaluate(self):
        raw1 = {"name": "A", "total_investment": 130, "flows": [
            {"flow_date": "2026-01-01", "amount": 100},
            {"flow_date": "2026-01-03", "amount": 50},
            {"flow_date": "2026-01-04", "amount": -20},
        ]}
        calculated1 = {
            "status": "ok", "profit": 13,
            "first": {"valuation_date": "2026-01-01", "net_assets": 100},
            "last": {"valuation_date": "2026-01-05", "net_assets": 143},
            "flows": [{"flow_date": "2026-01-03", "amount": 50},
                      {"flow_date": "2026-01-04", "amount": -20}],
            "dividends": [], "subscriptions": 50, "redemptions": 20,
            "dividendAmount": 0,
        }
        raw2 = {"name": "B", "total_investment": 200, "flows": [
            {"flow_date": "2026-01-01", "amount": 200},
        ]}
        calculated2 = {
            "status": "ok", "profit": 10,
            "first": {"valuation_date": "2026-01-01", "net_assets": 200},
            "last": {"valuation_date": "2026-01-05", "net_assets": 210},
            "flows": [], "dividends": [], "subscriptions": 0,
            "redemptions": 0, "dividendAmount": 0,
        }
        script = (
            "const DAY=86400000;"
            "function ds(a,b){return Math.round((new Date(b+'T00:00:00Z')-new Date(a+'T00:00:00Z'))/DAY)}"
            "function shift(d,n){const x=new Date(d+'T00:00:00Z');x.setUTCDate(x.getUTCDate()+n);return x.toISOString().slice(0,10)}"
            "function xnpv(rate,flows){if(rate<=-1)return Infinity;const d0=flows[0].date;return flows.reduce((s,f)=>s+f.value/Math.pow(1+rate,ds(d0,f.date)/365),0)}"
            "function xirr(flows){if(!flows.some(x=>x.value<0)||!flows.some(x=>x.value>0))return null;let lo=-.9999,hi=1000000,a=xnpv(lo,flows);for(let i=0;i<200;i++){const mid=(lo+hi)/2,m=xnpv(mid,flows);if(a*m<=0)hi=mid;else{lo=mid;a=m}}return(lo+hi)/2}"
            + self.capital_source + self.aggregate_source
            + "const raw1=" + json.dumps(raw1) + ";"
            + "const calculated1=" + json.dumps(calculated1) + ";"
            + "const raw2=" + json.dumps(raw2) + ";"
            + "const calculated2=" + json.dumps(calculated2) + ";"
            + "const productReturnSelection=new Set(['A','B']);"
            + "process.stdout.write(JSON.stringify({usage:productCapitalUsage(raw1,calculated1),"
              "aggregate:productReturnAggregate([{raw:raw1,calculated:calculated1},{raw:raw2,calculated:calculated2}])}));"
        )
        return json.loads(subprocess.check_output(["node", "-e", script], text=True, encoding="utf-8"))

    def test_natural_day_principal_and_flow_day_effect(self):
        usage = self.evaluate()["usage"]
        self.assertEqual(usage["days"], 5)
        self.assertEqual(usage["capital_days"], 610)
        self.assertEqual(usage["average"], 122)
        self.assertAlmostEqual(usage["rate"], 13 / 122 * 100)

    def test_selected_aggregate_sums_amounts_and_recalculates_returns(self):
        aggregate = self.evaluate()["aggregate"]
        self.assertEqual(aggregate["selected"], 2)
        self.assertEqual(aggregate["valid"], 2)
        self.assertEqual(aggregate["profit"], 23)
        self.assertEqual(aggregate["average"], 322)
        self.assertAlmostEqual(aggregate["capital_rate"], 23 / 322 * 100)
        self.assertIsNotNone(aggregate["annualized"])


if __name__ == "__main__":
    unittest.main()
