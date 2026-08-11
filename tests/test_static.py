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

    def test_nav_risk_metrics_are_shown_separately_from_xirr(self):
        self.assertIn("function riskMetrics(points)", HTML)
        self.assertIn("RISK_FREE_RATE=.02", HTML)
        self.assertIn("risk=riskMetrics(points)", HTML)
        self.assertIn("净值年化收益率", HTML)
        self.assertIn("夏普率（Rf 2%）", HTML)
        self.assertIn("最大回撤", HTML)
        self.assertIn("年化波动率", HTML)
        self.assertIn("有效估值点数", HTML)
        self.assertIn("最大回撤区间", HTML)
        self.assertIn("区间少于30天", HTML)

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
        self.assertIn("function contributionChart(p)", HTML)
        self.assertIn("function drawContribution(p)", HTML)
        self.assertIn("底层产品收益率贡献", HTML)
        self.assertIn("(Number(h.period_profit)||0)/base*100", HTML)
        self.assertIn("收益率贡献：${pct(item.value)}", HTML)

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
        self.assertIn("FOF1证投组", HTML)
        self.assertIn("FOF2研究所组", HTML)
        self.assertIn("其他组", HTML)
        self.assertIn("中金财富私享1554号FOF单一资产管理计划", HTML)
        self.assertIn("国金资管盛乾同行5号FOF单一资产管理计划", HTML)
        self.assertIn("五矿证券FOF50号单一资产管理计划", HTML)
        self.assertIn("五矿证券FOF51号单一资产管理计划", HTML)
        self.assertIn("项目说明", HTML)
        self.assertIn("<summary>计算口径</summary>", HTML)
        self.assertIn("所有产品期末资产：", HTML)
        self.assertIn("区间收益：", HTML)

    def test_project_guidance_sections_are_rendered(self):
        self.assertIn("<summary>代码指令</summary>", HTML)
        self.assertIn("python app.py refresh", HTML)
        self.assertIn("python app.py build", HTML)
        self.assertIn("python app.py share", HTML)
        self.assertIn("<summary>历史改动</summary>", HTML)
        self.assertIn("2026-08-11", HTML)
        self.assertIn("已计算产品", HTML)

    def test_self_operated_group_lookthrough_excludes_yanbo(self):
        self.assertIn("exclude_holding:YANBO", HTML)
        self.assertIn("include_holding:YANBO", HTML)
        self.assertIn("ending-=endHolding?endHolding.market_value||0:0", HTML)
        self.assertIn("profit-=periodHolding?periodHolding.period_profit||0:0", HTML)
        self.assertIn("investment-=latestHolding?latestHolding.cost||0:0", HTML)
        self.assertIn("ending+=endHolding?endHolding.market_value||0:0", HTML)
        self.assertIn("profit+=periodHolding?periodHolding.period_profit||0:0", HTML)
        self.assertIn("investment+=latestHolding?latestHolding.cost||0:0", HTML)
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

    def test_benchmark_analysis_is_interactive(self):
        self.assertIn('id="benchmarkSummary"', HTML)
        self.assertIn("function benchmarkMetrics", HTML)
        self.assertIn("function alphaBeta", HTML)
        self.assertIn("renderBenchmarks(start,end)", HTML)
        self.assertIn("年化Alpha（中证1000）", HTML)
        self.assertIn("Beta（中证1000）", HTML)
        self.assertIn("id=comparisonChart", HTML)
        self.assertIn("function drawComparison", HTML)
        self.assertIn("python app.py benchmark", HTML)
        self.assertIn("指数同期表现</h2>", HTML)
        self.assertNotIn("三只指数同期表现</h2>", HTML)
        self.assertIn("id=comparisonControls", HTML)
        self.assertIn("function toggleComparison", HTML)
        self.assertIn("RAW.products.forEach((product,i)=>", HTML)
        self.assertIn("new Set(['product:'+i,'index:000852','index:000905','index:000300'])", HTML)
        self.assertNotIn("${benchmarkNote}<canvas id=chart", HTML)


    def test_calculated_count_requires_both_valuation_boundaries(self):
        self.assertIn("function nonTradingDate(d)", HTML)
        self.assertIn("boundaryComplete(prior,start)&&boundaryComplete(last,end)", HTML)
        self.assertIn("complete=valid.filter(x=>x.boundary_complete)", HTML)
        self.assertIn("calculated:complete.length", HTML)
        self.assertNotIn("calculated:valid.length", HTML)


if __name__ == "__main__":
    unittest.main()
