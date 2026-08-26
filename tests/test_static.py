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

    def test_tagged_holding_strategy_and_proxy_nav_features(self):
        self.assertIn("function holdingLabel", HTML)
        self.assertIn("卡玛", HTML)
        self.assertIn("策略组合分析", HTML)
        self.assertIn("底层产品净值代理走势", HTML)
        self.assertIn("choices.slice(0,10)", HTML)
        self.assertIn("function openInfo", HTML)
        self.assertIn("资金台账", HTML)
        self.assertIn("产品标签", HTML)
        self.assertIn("function globalStrategySummary", HTML)
        self.assertIn("策略组合分析（全部FOF底仓）", HTML)
        self.assertIn("function selectAllUnderlying", HTML)
        self.assertIn("全不选", HTML)
        self.assertIn("绝对收益率", HTML)
        self.assertIn("item.return_value+=market*rate/100", HTML)
        self.assertIn("绝对收益率（期末市值加权）", HTML)
        self.assertIn("收益率覆盖期末市值", HTML)
        self.assertIn("function globalHoldingDetails", HTML)
        self.assertIn("globalPieTip", HTML)
        self.assertIn("globalBarTip", HTML)
        self.assertNotIn("占全部FOF期初资产</th>", HTML)

    def test_holding_matrix_and_portfolio_title(self):
        self.assertIn("function updatePortfolioPresentation", HTML)
        self.assertIn("创新金融业务总部fof投资资产组合", HTML)
        self.assertIn("class=holding-matrix", HTML)
        self.assertIn("rowspan=${primaryCounts.get(p1)}", HTML)
        self.assertIn("<th>日涨跌%</th><th>周涨跌%</th>", HTML)
        self.assertNotIn("<th>成本(万)</th>", HTML)
        self.assertNotIn("<th>对应基准 / Alpha / Beta</th>", HTML)
        self.assertIn(".layout{grid-template-columns:390px minmax(0,1fr)}", HTML)
        self.assertIn(".layout>.panel{min-width:0}", HTML)

    def test_investment_dashboard_navigation_and_factor_module(self):
        self.assertIn("function initInvestmentDashboard", HTML)
        self.assertIn("创新金融业务总部FOF投资驾驶舱", HTML)
        self.assertIn("投资总览", HTML)
        self.assertIn("收益分析", HTML)
        self.assertIn("市场与基准", HTML)
        self.assertIn("持仓与穿透", HTML)
        self.assertIn("function dashRoute", HTML)
        self.assertIn("history.pushState", HTML)
        self.assertIn("多因子分析", HTML)
        self.assertIn("function renderMultiFactor", HTML)
        self.assertIn("不是MSCI Barra正式风险模型", HTML)
        self.assertIn("底层逐笔交易流水", HTML)
        self.assertIn("数据接入前保持禁用", HTML)

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
        self.assertIn("group-parent", HTML)
        self.assertIn("group-child", HTML)
        self.assertIn("↳ 子组", HTML)
        self.assertIn("function documentGroupHierarchy()", HTML)

    def test_underlying_asset_analysis_is_rendered(self):
        self.assertIn("function openUnderlyingAssets()", HTML)
        self.assertIn("底层分析", HTML)
        self.assertIn("RAW.underlying_assets", HTML)
        self.assertIn("function renderUnderlyingDate", HTML)
        self.assertIn("排序：关联FOF → 敞口比例降序", HTML)
        self.assertIn("const row=document.querySelector('.top-info')", HTML)
        self.assertIn("risk.textContent='风控页面'", HTML)
        self.assertIn("underlying.textContent='底层分析'", HTML)
        self.assertIn("网页更新时间：", HTML)
        self.assertIn("'000852','000905','000300','932000'", HTML)
        self.assertNotIn("'868008','NH0100','IXIC'", HTML)
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
        self.assertIn("2026-08-21", HTML)
        self.assertIn("2026-08-24", HTML)
        self.assertIn("2026-08-25", HTML)
        self.assertIn("ASCII 安全 JSON", HTML)
        self.assertIn("10年期国债收益率历史模拟VaR", HTML)
        self.assertIn("不折算金额", HTML)
        self.assertIn("来源中央结算公司", HTML)
        self.assertIn("估值报表", HTML)
        self.assertIn("错过后登录补跑", HTML)
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


    def test_fixed_report_and_top_project_links(self):
        self.assertIn("function openReport()", HTML)
        self.assertIn("生成报告", HTML)
        self.assertIn("导出PDF / 打印报告", HTML)
        self.assertIn("整体各组收益表现", HTML)
        self.assertIn("各策略最好和最差的底层产品", HTML)
        self.assertIn("function promoteTopControls()", HTML)
        self.assertIn("section.style.display='none'", HTML)
        self.assertIn("function reportAllStrategyShares()", HTML)
        self.assertIn("所有策略占比", HTML)
        self.assertIn("data.items.map((item,index)", HTML)

    def test_label_field_mapping_is_documented_in_page(self):
        self.assertIn("策略组合分析使用有效一级标签", HTML)
        self.assertIn("B列“管理人名称”", HTML)
        self.assertIn("CD列“一级标签”", HTML)
        self.assertIn("《管理人清单》优先", HTML)

    def test_risk_page_var_basis_and_stress(self):
        self.assertIn("function openRisk()", HTML)
        self.assertIn("风控页面", HTML)
        self.assertIn("function historicalVar", HTML)
        self.assertIn("function basisVar", HTML)
        self.assertIn("RISK_SCENARIOS", HTML)
        self.assertIn("股票对冲=指数0.5＋贴水0.5", HTML)
        self.assertIn("统一使用IM贴水代理", HTML)
        self.assertIn("风控日报全资产简化估算VaR", HTML)
        self.assertIn("权益FOF固定按5%", HTML)
        self.assertIn("日报金额是敞口代理", HTML)
        self.assertIn("固定信用利差", HTML)
        self.assertIn("原始金额", HTML)
        self.assertIn("利率VaR(bp)", HTML)
        self.assertIn("信用冲击(bp)", HTML)
        self.assertIn("碳价VaR率", HTML)
        self.assertIn("计算公式", HTML)
        self.assertIn("历史模拟VaR风险因子总表", HTML)
        self.assertIn("Excel统一碳价涨跌幅", HTML)
        self.assertIn("全部美元利率资产统一使用美国国债10Y VaR", HTML)
        self.assertIn("下载全部资产VaR明细（Excel）", HTML)
        self.assertNotIn("查看产品VaR汇总", HTML)
        self.assertIn("other:'其他管理'", HTML)
        self.assertIn("python app.py portfolio-var", HTML)
        self.assertIn("该模块独立于FOF底层风控", HTML)
        self.assertIn("风控日报全资产简化VaR计算逻辑", HTML)
        self.assertIn("固定汇率、Quanto或已对冲结构", HTML)
        self.assertIn("组合VaR为300项资产VaR直接求和", HTML)

    def test_command_guide_lists_one_command_per_row(self):
        self.assertIn("function renderCommandGuide()", HTML)
        self.assertIn("首次安装与分享设置", HTML)
        self.assertIn("数据下载与整理", HTML)
        self.assertIn("网页生成与运行", HTML)
        self.assertIn("检查与测试", HTML)
        self.assertIn("每次只复制并执行一整行", HTML)

    def test_calculated_count_requires_both_valuation_boundaries(self):
        self.assertIn("function nonTradingDate(d)", HTML)
        self.assertIn("(inception||boundaryComplete(prior,start))&&boundaryComplete(last,end)", HTML)
        self.assertIn("complete=valid.filter(x=>x.boundary_complete)", HTML)
        self.assertIn("calculated:complete.length", HTML)
        self.assertNotIn("calculated:valid.length", HTML)
        self.assertIn("status:'not_started'", HTML)
        self.assertIn("所选结束日期时产品尚未成立", HTML)
        self.assertIn("total:eligible.length", HTML)


if __name__ == "__main__":
    unittest.main()
