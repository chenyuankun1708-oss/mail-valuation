import unittest

from valuation_app.static import HTML


class StaticCalculationTest(unittest.TestCase):
    def test_profit_uses_assets_and_ledger_cashflows(self):
        self.assertIn(
            "profit=last.net_assets-first.net_assets-net+dividendAmount",
            HTML,
        )
        self.assertNotIn(
            "points[i-1].shares*(points[i].accumulated_nav-points[i-1].accumulated_nav)",
            HTML,
        )

    def test_annualized_return_uses_xirr_cashflows(self):
        self.assertIn("annual=xirr(xf)", HTML)
        self.assertIn("总份额（仅核验）", HTML)

    def test_inception_is_decided_by_reconciled_ledger(self):
        self.assertIn("firstInvestment=dated.find(f=>f.amount>0)", HTML)
        self.assertIn("ledgerReconciled=p.total_investment!=null", HTML)
        self.assertIn("firstInvestment.flow_date<start", HTML)
        self.assertIn("const first=prior||{valuation_date:firstInvestment.flow_date,net_assets:0}", HTML)
        self.assertIn("台账确认该产品在所选区间内首次建仓", HTML)
        self.assertIn("历史建仓/追加/赎回流水不完整", HTML)

    def test_holding_profit_analysis_is_rendered(self):
        self.assertIn("function holdingReport(points,inception)", HTML)
        self.assertIn("底层投资期间收益", HTML)
        self.assertIn("区间退出，退出收益未归属", HTML)

    def test_summary_shows_all_products_current_investment(self):
        self.assertIn("所有产品当前总投资额", HTML)
        self.assertIn("products.reduce((s,p)=>s+(p.total_investment||0),0)", HTML)
        self.assertNotIn("['选择区间',start+' 至 '+end]", HTML)

    def test_all_products_ending_assets_only_depends_on_end_date(self):
        self.assertIn("products.map(p=>before(p.points,end)).filter(Boolean)", HTML)
        self.assertIn("<th>所有产品期末资产</th>", HTML)
        self.assertNotIn("['区间期末资产',money(valid.reduce", HTML)

    def test_group_summaries_and_scope_are_rendered(self):
        self.assertIn("创新金融业务总部所有组（所有产品）", HTML)
        self.assertIn("自营-证投组", HTML)
        self.assertIn("FOF1组", HTML)
        self.assertIn("FOF2研究所组", HTML)
        self.assertIn("中金财富私享1554号FOF单一资产管理计划", HTML)
        self.assertIn("国金资管盛乾同行5号FOF单一资产管理计划", HTML)
        self.assertIn("五矿证券FOF50号、FOF51号不在当前15只产品白名单中", HTML)
        self.assertIn("汇总计算口径", HTML)
        self.assertIn('display:grid;gap:6px', HTML)
        self.assertIn('<div>所有产品期末资产：', HTML)
        self.assertIn('<div>区间收益：', HTML)

    def test_self_operated_group_lookthrough_excludes_yanbo(self):
        self.assertIn("exclude_holding:YANBO", HTML)
        self.assertIn("ending-=endHolding?endHolding.market_value||0:0", HTML)
        self.assertIn("profit-=periodHolding?periodHolding.period_profit||0:0", HTML)
        self.assertIn("investment-=latestHolding?latestHolding.cost||0:0", HTML)
        self.assertNotIn("已穿透扣除砚博：期末市值", HTML)

    def test_yanbo_is_listed_as_standalone_underlying_product(self):
        self.assertIn("底层产品（穿透列示）", HTML)
        self.assertIn("id=yanboItem", HTML)
        self.assertIn("function showYanbo()", HTML)
        self.assertIn("所选区间估算收益", HTML)

    def test_nav_chart_has_hover_values(self):
        self.assertIn("c.onmousemove=e=>", HTML)
        self.assertIn("单位净值：${p.nav}", HTML)
        self.assertIn("累计净值：${p.accumulated_nav}", HTML)
        self.assertIn("资产净值：${money(p.net_assets)}", HTML)


    def test_calculated_count_requires_both_valuation_boundaries(self):
        self.assertIn("function nonTradingDate(d)", HTML)
        self.assertIn("boundaryComplete(prior,start)&&boundaryComplete(last,end)", HTML)
        self.assertIn("complete=valid.filter(x=>x.boundary_complete)", HTML)
        self.assertIn("calculated:complete.length", HTML)
        self.assertNotIn("calculated:valid.length", HTML)


if __name__ == "__main__":
    unittest.main()
