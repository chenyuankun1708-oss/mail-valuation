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

    def test_opening_assets_and_drawdown_period_are_metric_cards(self):
        self.assertIn("['期初资产',money(p.first.net_assets),''],['期末资产'", HTML)
        self.assertIn("['最大回撤区间',drawdownPeriod,'']", HTML)
        self.assertIn("drawdownPeriod=risk.drawdown_peak&&risk.drawdown_trough", HTML)
        self.assertNotIn("${riskWarn}${drawdownNote}${benchmarkNote}", HTML)

    def test_calculation_method_has_formula_reference_table(self):
        self.assertIn("function metricFormulaHtml()", HTML)
        self.assertIn("指标公式速查", HTML)
        self.assertIn("P = A₁ − A₀ − S + R + D", HTML)
        self.assertIn("Σ CFᵢ/(1+r)^((tᵢ−t₀)/365) = 0", HTML)
        self.assertIn("VaRc = Qc((yₜ−yₜ₋₁)×10000)", HTML)
        self.assertIn("分类、标签匹配、数据来源和边界选择属于规则口径", HTML)

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
        self.assertNotIn("<th>占FOF期初资产</th>", HTML)
        self.assertIn("strategy-chart-grid", HTML)
        self.assertIn("期末市值结构", HTML)
        self.assertIn("strategyPieTip", HTML)
        self.assertIn("strategyBarTip", HTML)
        self.assertIn("底层产品估值价格代理走势", HTML)
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
        self.assertIn("年化波动率", HTML)
        self.assertIn("市场研究与指数ETF配置看板", HTML)
        self.assertIn("IF / IC / IM 年化贴水", HTML)
        self.assertIn("月度指数ETF建议权重", HTML)
        self.assertNotIn("holdings:'持仓与穿透'", HTML)
        self.assertNotIn("function renderDashHoldings", HTML)
        self.assertIn("if(page==='holdings')page='returns'", HTML)
        self.assertIn("function dashRoute", HTML)
        self.assertIn("history.pushState", HTML)
        self.assertIn("多因子分析", HTML)
        self.assertIn("function renderMultiFactor", HTML)
        self.assertIn("不是MSCI Barra正式风险模型", HTML)
        self.assertIn("底层逐笔交易流水", HTML)
        self.assertIn("数据接入前保持禁用", HTML)

    def test_summary_shows_all_products_current_investment(self):
        self.assertIn("当前投资额 / 底层成本", HTML)
        self.assertIn("products.reduce((s,p)=>s+(p.total_investment||0),0)", HTML)
        self.assertNotIn("['选择区间',start+' 至 '+end]", HTML)

    def test_all_products_ending_assets_only_depends_on_end_date(self):
        self.assertIn("products.map(p=>before(p.points,end)).filter(Boolean)", HTML)
        self.assertIn("<th>期末资产 / 底层市值</th>", HTML)
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
        self.assertIn("const group=underlyingFofKey(a).localeCompare", HTML)
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

    def test_underlying_asset_selection_recalculates_totals(self):
        self.assertIn("function underlyingItemKey(x)", HTML)
        self.assertIn("function underlyingTotals(items)", HTML)
        self.assertIn("class=underlying-check", HTML)
        self.assertIn("id=underlyingSelectAll", HTML)
        self.assertIn("id=underlyingSelectAllButton", HTML)
        self.assertIn("id=underlyingSelectNoneButton", HTML)
        self.assertIn("仅合计勾选项", HTML)
        self.assertIn("renderUnderlyingDate(win,date,false)", HTML)

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


    def test_legacy_generated_report_button_is_removed(self):
        self.assertIn("function openReport()", HTML)
        self.assertIn("function promoteTopControls()", HTML)
        self.assertIn("function removeLegacyReportButton()", HTML)
        self.assertIn("#dashReport .report-button", HTML)
        self.assertIn("initInvestmentDashboard();installKnowledgeNav();removeLegacyReportButton()", HTML)
        self.assertIn("section.style.display='none'", HTML)
        self.assertIn("function reportAllStrategyShares()", HTML)
        self.assertIn("所有策略占比", HTML)
        self.assertIn("data.items.map((item,index)", HTML)

    def test_label_field_mapping_is_documented_in_page(self):
        self.assertIn("策略组合分析使用有效一级标签", HTML)
        self.assertIn("data_sources/product_labels.json", HTML)
        self.assertIn("迁移后Excel只作为历史备份", HTML)
        self.assertIn("迁移记录中原管理人清单来源优先", HTML)
        self.assertIn("python app.py labels-migrate", HTML)

    def test_online_label_editor_updates_dependent_views(self):
        self.assertIn("async function openLabelEditor()", HTML)
        self.assertIn("expected_revision:catalog.revision", HTML)
        self.assertIn("async function labelApi", HTML)
        self.assertIn("refreshLabelDependentViews(catalog)", HTML)
        self.assertIn("renderDashStrategy()", HTML)
        self.assertIn("department:'无'", HTML)
        self.assertIn("只接受人工在线填写", HTML)

    def test_strategy_filters_recalculate_from_label_selection(self):
        self.assertIn("globalStrategySummary(globalStrategyFilters)", HTML)
        self.assertIn("globalStrategyFilterSelect('department','营业部')", HTML)
        self.assertIn("占筛选结果市值", HTML)
        self.assertIn("筛选后的同一产品集合重算", HTML)
        self.assertIn("data-action=disable", HTML)

    def test_global_strategy_indexes_by_standard_product_name(self):
        self.assertIn("function strategyProductRows(p)", HTML)
        self.assertIn("product=indexed?label.product:h.name", HTML)
        self.assertIn("holdingReport(points,p.inception)", HTML)
        self.assertIn("strategyProductRows(p).forEach", HTML)
        self.assertIn("function strategyNameIndexIssues()", HTML)
        self.assertIn("策略组合产品名称索引提示", HTML)
        self.assertIn("未匹配到产品标签中的标准产品名称", HTML)
        self.assertIn("'fof','code','file'", HTML)

    def test_global_strategy_lists_filtered_underlying_positions(self):
        self.assertIn("function globalHoldingList(items)", HTML)
        self.assertIn("底层产品（名称索引）", HTML)
        self.assertIn("科目代码（追溯）", HTML)
        self.assertIn("产品按标准产品名称索引", HTML)
        self.assertIn("index===0?globalHoldingList(items):''", HTML)

    def test_local_knowledge_base_page_and_commands(self):
        self.assertIn("knowledge:'知识库'", HTML)
        self.assertIn("function renderKnowledge()", HTML)
        self.assertIn("/api/knowledge/upload", HTML)
        self.assertIn("/api/knowledge/import-inbox", HTML)
        self.assertIn("python app.py knowledge-check", HTML)
        self.assertIn("knowledge_base/inbox", HTML)

    def test_holding_change_attribution_is_estimated_and_exportable(self):
        self.assertIn("attribution:'持仓变动归因（估算）'", HTML)
        self.assertIn("function attributionInterval(fof,a,b)", HTML)
        self.assertIn("priceImpact=q0*(p1-p0)", HTML)
        self.assertIn("positionAmount=dq*p1", HTML)
        self.assertIn("部分可归属", HTML)
        self.assertIn("现金费用及其他未归属", HTML)
        self.assertIn("/api/attribution.xlsx", HTML)
        self.assertIn("if(page==='attribution')renderAttribution()", HTML)

    def test_strategy_lab_is_deferred_and_research_only(self):
        self.assertIn("lab:['strategy-lab']", HTML)
        self.assertIn("function renderStrategyLab()", HTML)
        self.assertIn("function strategyDrivers(item)", HTML)
        self.assertIn("最近已结束月份形成信号", HTML)
        self.assertIn("无前视", HTML)
        self.assertIn("python app.py strategy-lab", HTML)
        self.assertIn("不生成订单、不连接券商", HTML)

    def test_strategy_lab_standalone_page(self):
        self.assertIn("lab:'策略实验室'", HTML)
        self.assertIn("if(page==='lab')renderStrategyLab()", HTML)
        self.assertIn("id=dashLab", HTML)
        self.assertIn("/api/strategy-lab/llm", HTML)
        self.assertIn("/api/strategy-lab/task/", HTML)
        self.assertIn("labMethodology", HTML)
        self.assertIn("生成策略规格", HTML)

    def test_core_report_engine_uses_deterministic_evidenced_rules(self):
        self.assertIn("function coreReportData()", HTML)
        self.assertIn("reconciliation=ending-expected", HTML)
        self.assertIn("单产品贡献≥绝对收益变动10%", HTML)
        self.assertIn("前三大产品占比≥50%", HTML)
        self.assertIn("策略占比绝对变化≥5个百分点", HTML)
        self.assertIn("最大回撤≤-10%", HTML)
        self.assertIn("年化波动率≥30%", HTML)
        self.assertIn("不含“其他管理”的风控日报VaR", HTML)
        self.assertIn("product_flows:flowSummary", HTML)
        self.assertIn("positionAmount=!a?afterValue:!b?-beforeValue:(b.quantity-a.quantity)*b.price", HTML)
        self.assertIn("return_rate:b.price/a.price-1", HTML)
        self.assertIn("holding_vs_median:.05", HTML)
        self.assertIn("strategy_vs_median:.03", HTML)
        self.assertIn("核心结论", HTML)
        self.assertIn("底层持仓增减金额（估算）", HTML)
        self.assertIn("CORE_LOOKTHROUGH_FOFS", HTML)
        self.assertIn("顶层产品申购按产品汇总", HTML)
        self.assertIn("narrativeParagraphs.join('\\n\\n')", HTML)

    def test_core_report_has_independent_page_and_exports(self):
        self.assertIn("core:'核心汇报'", HTML)
        self.assertIn("main=['overview','core','returns'", HTML)
        self.assertIn("function renderCoreReport()", HTML)
        self.assertIn("/api/core-report.xlsx", HTML)
        self.assertIn("下载Excel", HTML)
        self.assertIn("打印 / PDF", HTML)
        self.assertIn("core:'▣'", HTML)
        self.assertIn("health.style.display=page==='core'?'none':''", HTML)

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
        self.assertIn("邮件与估值表", HTML)
        self.assertIn("行情与专项数据", HTML)
        self.assertIn("网页生成与运行", HTML)
        self.assertIn("检查与测试", HTML)
        self.assertIn("每次复制并执行完整一行", HTML)
        self.assertIn("class=command-group", HTML)
        self.assertIn("grid-template-columns:minmax(320px,48%)", HTML)
        self.assertIn("overflow-wrap:anywhere", HTML)
        for command in ("underlying-mail", "underlying-organize", "factor",
                        "market-dashboard", "portfolio-var"):
            self.assertIn("python app.py " + command, HTML)
        self.assertIn("function documentTimingChange()", HTML)
        self.assertIn("邮件与文件、数据库与缓存、数据计算和网页更新", HTML)
        self.assertIn("logs\\\\refresh.log", HTML)
        self.assertIn("function documentTailscaleShareChange()", HTML)
        self.assertIn("Tailscale Funnel", HTML)
        self.assertIn("setup_tailscale_share.ps1", HTML)

    def test_calculated_count_requires_both_valuation_boundaries(self):
        self.assertIn("function nonTradingDate(d)", HTML)
        self.assertIn("function tradingCalendarDates()", HTML)
        self.assertIn("if(calendar.includes(d))return false", HTML)
        self.assertNotIn("!RAW.products.some(p=>p.points.some(x=>x.valuation_date===d))", HTML)
        self.assertIn("(inception||boundaryComplete(prior,start))&&boundaryComplete(last,end)", HTML)
        self.assertIn("complete=valid.filter(x=>x.boundary_complete)", HTML)
        self.assertIn("calculated:complete.length", HTML)
        self.assertNotIn("calculated:valid.length", HTML)
        self.assertIn("status:'not_started'", HTML)
        self.assertIn("所选结束日期时产品尚未成立", HTML)
        self.assertIn("total:eligible.length", HTML)


    def test_project_documents_are_structured_and_include_database_help(self):
        self.assertIn("structuredCalculationHtml", HTML)
        self.assertIn("六、风控日报全资产简化VaR", HTML)
        self.assertIn("风险与米筐数据库", HTML)
        self.assertIn("get_factor_return", HTML)
        self.assertIn("sortProjectHistory", HTML)

    def test_holding_rows_open_price_and_market_value_curves(self):
        self.assertIn("showHoldingHistory", HTML)
        self.assertIn("drawHoldingComposite", HTML)
        self.assertIn("淡色柱：投资金额", HTML)
        self.assertIn("holding-inline-detail", HTML)
        self.assertIn("showHoldingHistory(this,active", HTML)
        self.assertIn("holding-detail-link", HTML)
        self.assertIn("function timeScale", HTML)
        self.assertIn("function holdingTradingScale", HTML)
        self.assertIn("横轴：Wind A股交易日", HTML)
        self.assertIn("交易日历覆盖不足，已回退真实日历轴", HTML)

    def test_monthly_report_and_fof1_subgroups(self):
        self.assertIn("报告出具", HTML)
        self.assertIn("downloadMonthlyReport", HTML)
        self.assertIn("/api/monthly-report?as_of=", HTML)
        self.assertIn("/api/valuation-archive?date=", HTML)
        self.assertIn("生成并下载估值表压缩包", HTML)
        self.assertIn("FULL_MANDATE", HTML)
        self.assertIn("FOF1_SPECIAL", HTML)
        self.assertIn("全委组合", HTML)
        self.assertIn("策略组合", HTML)
        self.assertIn("专户组合", HTML)
        self.assertIn("padding-left:48px", HTML)
        self.assertIn("padding-left:88px", HTML)
        self.assertIn("parentComplete=!!(calculated&&calculated.boundary_complete)", HTML)
        self.assertIn("x.parent_complete&&x.period", HTML)
        self.assertIn("维护与变更记录", HTML)
        self.assertIn("logs/CHANGELOG.md", HTML)
        self.assertIn("重要改动五处同步制度", HTML)
        self.assertIn("中信建投聚智多策略9号FOF单一资产管理计划", HTML)
        self.assertIn("西南证券嘉盈1号FOF单一资产管理计划", HTML)
        self.assertIn("function lookthroughGroupRow", HTML)
        self.assertIn("label.vehicle==='专户'", HTML)
        self.assertIn("if(h.name===YANBO)return", HTML)
        self.assertIn("function groupResidual", HTML)
        self.assertIn("未穿透差额（现金及净应收应付）", HTML)

    def test_overview_has_selectable_top_level_product_returns(self):
        self.assertIn("FOF产品收益计算", HTML)
        self.assertIn("function productCapitalUsage(raw,calculated)", HTML)
        self.assertIn("function productReturnAggregate(items)", HTML)
        self.assertIn("coverage=new Set", HTML)
        self.assertIn("setAllProductReturns(true)", HTML)
        self.assertIn("有效纳入 ${total.valid} / 已勾选 ${total.selected}", HTML)
        self.assertIn("组合XIRR合并所选有效产品", HTML)
        self.assertIn("productReturnContext=null;calcOld()", HTML)
        self.assertIn("setUTCDate", HTML)
        self.assertIn("renderMonthlyReportBox();renderProductReturnTable(true)", HTML)
        self.assertNotIn("productReturnSelection.add(YANBO)", HTML)
        self.assertIn("residual:true", HTML)
        self.assertIn("r.residual?'—':money(r.profit)", HTML)
        self.assertNotIn("groupResidualNote", HTML)
        self.assertIn("portfolioGroupRows(end)", HTML)


import os
import shutil
import subprocess
import tempfile
import unittest

from valuation_app.static import HTML, DASHBOARD_JS


class StaticJSSyntaxTest(unittest.TestCase):
    """app.js 由 static.py 的巨型字符串拼出，任何语法错误都会导致整站白屏。

    此处把 HTML 内全部 <script> 内容交给 node 做语法编译，
    在测试阶段拦截漏分号、多余括号等错误（浏览器无法给出可读报错）。
    """

    def test_all_inline_scripts_compile(self):
        if not shutil.which("node"):
            self.skipTest("本机无 node，跳过 JS 语法编译检查")
        # HTML 内嵌 script 与 DASHBOARD_JS 是同一代码（数据占位符版），
        # 只编译 DASHBOARD_JS 即可覆盖全部浏览器端 JS。
        folder = tempfile.mkdtemp()
        try:
            path = os.path.join(folder, "syntax_check.js")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(DASHBOARD_JS)
            result = subprocess.run(
                ["node", "--check", path], capture_output=True, text=True, timeout=60)
            self.assertEqual(
                result.returncode, 0,
                "app.js 存在语法错误，网页会白屏：\n%s" % result.stderr[:2000])
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
