import json
import os
import unittest

import valuation_app.static as static


class WebAssetSourceTest(unittest.TestCase):
    def test_frontend_sources_are_split_without_patch_chains(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for name in ("shell.html", "styles.css", "bootstrap.js", "core.js", "dashboard.js",
                     "otc_derivatives.js", "system_qa.json", "architecture.json"):
            self.assertTrue(os.path.isfile(os.path.join(root, "web_assets", name)))
        with open(static.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("HTML = HTML.replace", source)
        self.assertNotIn("DASHBOARD_JS = DASHBOARD_JS.replace", source)
        self.assertLess(len(source), 20000)
        self.assertIn("const RAW=__DATA__", static.CORE_JS)
        self.assertIn("const DASH_PAGES=", static.DASHBOARD_JS)
        self.assertIn("#home", static.HTML)
        self.assertIn("function renderOtcDerivatives", static.OTC_DERIVATIVES_JS)
        self.assertIn("download.pdf", static.OTC_DERIVATIVES_JS)
        self.assertIn("parameter_definitions", static.OTC_DERIVATIVES_JS)
        self.assertIn("otc-wide-table", static.OTC_DERIVATIVES_JS)
        self.assertIn("ledgerSort:'start_date'", static.OTC_DERIVATIVES_JS)
        self.assertIn("起始日", static.OTC_DERIVATIVES_JS)
        self.assertIn("终止日", static.OTC_DERIVATIVES_JS)
        self.assertIn("存续或了结", static.OTC_DERIVATIVES_JS)
        self.assertIn("function otcProductTotalRow", static.OTC_DERIVATIVES_JS)
        self.assertIn("产品数量 ${a.count||0}", static.OTC_DERIVATIVES_JS)
        self.assertNotIn("otc-ledger-summary-grid", static.OTC_DERIVATIVES_JS)
        self.assertNotIn("otc-selection-bar", static.OTC_DERIVATIVES_JS)
        self.assertIn("function applyOtcFilters", static.OTC_DERIVATIVES_JS)
        self.assertIn("筛选条件默认均为全部", static.OTC_DERIVATIVES_JS)
        self.assertIn("/attachments", static.OTC_DERIVATIVES_JS)
        self.assertIn("function otcJsonResponse", static.OTC_DERIVATIVES_JS)
        self.assertIn("当前后台未加载附件接口", static.OTC_DERIVATIVES_JS)
        self.assertIn("字段排查（API待启用）", static.OTC_DERIVATIVES_JS)
        self.assertIn("function renderOtcFieldAudit", static.OTC_DERIVATIVES_JS)
        self.assertIn("p.latest_field_audit", static.OTC_DERIVATIVES_JS)
        self.assertIn("查看字段排查结果（API待启用）", static.OTC_DERIVATIVES_JS)
        self.assertIn("/api/llm/tasks", static.OTC_DERIVATIVES_JS)
        self.assertNotIn("费前利润", static.OTC_DERIVATIVES_JS)
        self.assertNotIn("费后利润", static.OTC_DERIVATIVES_JS)
        self.assertNotIn('name="legacy_annual_return"', static.OTC_DERIVATIVES_JS)
        self.assertIn("openProtectedSystemDoc('qa')", static.CORE_JS)
        self.assertIn("'历史改动','系统QA'", static.CORE_JS)
        self.assertIn("function systemArchitectureHtml()", static.CORE_JS)
        self.assertIn("function systemArchitectureDiagramHtml()", static.CORE_JS)
        self.assertIn("项目端到端架构流程图", static.CORE_JS)
        self.assertIn('viewBox="0 0 1240 1030"', static.CORE_JS)
        self.assertIn("marker-end:url(#archArrow)", static.CORE_JS)
        self.assertIn("arch-task-link", static.CORE_JS)
        self.assertIn("kind==='系统架构'", static.CORE_JS)
        self.assertIn("'系统QA','系统架构'", static.CORE_JS)
        self.assertIn("'历史改动','系统QA','系统架构'", static.DASHBOARD_JS)
        self.assertIn("function knowledgeGraphBranch", static.DASHBOARD_JS)
        self.assertIn("以管理人或产品为中心", static.DASHBOARD_JS)
        self.assertIn("'公司信息','底层私募产品'", static.DASHBOARD_JS)
        self.assertIn("'投资策略','业绩记录'", static.DASHBOARD_JS)
        self.assertIn("材料披露与系统计算分开", static.DASHBOARD_JS)
        self.assertIn("事实审核", static.DASHBOARD_JS)
        self.assertIn("/api/knowledge/facts", static.DASHBOARD_JS)
        self.assertIn("function editKnowledgeFact", static.DASHBOARD_JS)
        self.assertIn("产品条款", static.DASHBOARD_JS)
        self.assertIn("风控信息", static.DASHBOARD_JS)
        self.assertIn("knowledge-diligence-export", static.DASHBOARD_JS)
        self.assertIn("knowledge-diligence-import --batch", static.DASHBOARD_JS)
        self.assertIn("method==='GET'?3:1", static.DASHBOARD_JS)
        self.assertIn("立即重试", static.DASHBOARD_JS)
        self.assertIn("不影响文档、事实审核和图谱操作", static.DASHBOARD_JS)

    def test_otc_view_always_has_a_render_mount(self):
        self.assertIn("k==='otc-derivatives'?'<div id=dashOtcDerivatives></div>'",
                      static.DASHBOARD_JS)
        self.assertIn("if(view&&!document.getElementById('dashOtcDerivatives'))",
                      static.OTC_DERIVATIVES_JS)

    def test_top_returns_and_strategy_are_mounted_into_visible_views(self):
        source = static.DASHBOARD_JS
        self.assertIn("function mountLegacyDashboardViews()", source)
        self.assertIn("returnsView.appendChild(productLayout)", source)
        self.assertIn("strategyView.appendChild(strategyPanel)", source)
        self.assertLess(source.index("initInvestmentDashboard();"),
                        source.index("mountLegacyDashboardViews();"))

    def test_bootstrap_defers_full_payload_on_home_and_preserves_deep_links(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "web_assets", "bootstrap.js"),
                  "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("/api/v2/bootstrap", source)
        self.assertIn("/api/page-data", source)
        self.assertIn("if(hash!=='home')", source)
        self.assertIn("data/ledger", source)
        self.assertIn("overview/report", source)
        self.assertIn("aria-expanded", source)
        self.assertIn("otc-derivatives/backtest", source)

    def test_otc_pricing_third_tab_and_research_disclaimer(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "web_assets", "otc_derivatives.js"),
                  "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("function renderOtcPricing", source)
        self.assertIn("/api/otc/pricing/runs", source)
        self.assertIn("参数化理论定价研究", source)
        self.assertIn("研发中", source)
        self.assertIn("function priceOtcProduct", source)
        self.assertIn("复制到期权定价", source)
        self.assertIn("function syncOtcPricingTerm", source)
        self.assertIn("x.mean_20", source)
        self.assertIn("x.mean_60", source)
        self.assertIn("定价完成，可调整参数后再次试算", source)
        with open(os.path.join(root, "web_assets", "dashboard.js"),
                  "r", encoding="utf-8") as handle:
            dashboard = handle.read()
        self.assertIn("'backtest','ledger','pricing'", dashboard)

    def test_initial_calculation_waits_for_dashboard_dependencies(self):
        core = static.CORE_JS
        dashboard = static.DASHBOARD_JS
        self.assertNotIn("endDate.value=RAW.default_end;calculate();", core)
        self.assertIn("productReturnTableStyles();calculate();initInvestmentDashboard();",
                      dashboard)
        self.assertLess(dashboard.index("function portfolioGroupRows(end)"),
                        dashboard.index("installDataStatusPage();"))

    def test_structured_help_history_and_status_page_are_registered(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for name in ("help.json", "history.json", "architecture.json"):
            with open(os.path.join(root, "web_assets", name), "r", encoding="utf-8") as handle:
                payload = __import__("json").load(handle)
            self.assertEqual(1, payload["schema_version"])
        self.assertIn("data-status", {item["id"] for item in static.HELP_DATA["sections"]})
        self.assertIn("system-architecture", {item["id"] for item in static.HELP_DATA["sections"]})
        self.assertEqual("2026-09-24", static.ARCHITECTURE_DATA["updated_at"])
        self.assertIn("valuation_app/knowledge_sources.py", json.dumps(static.ARCHITECTURE_DATA, ensure_ascii=False))
        self.assertIn("valuation_app/knowledge_diligence.py", json.dumps(static.ARCHITECTURE_DATA, ensure_ascii=False))
        with open(static.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"architecture": ARCHITECTURE_DATA', source)
        self.assertIn("function renderDataStatus()", static.DASHBOARD_JS)
        self.assertIn("DASH_PAGES.status='模块数据状态'", static.DASHBOARD_JS)


if __name__ == "__main__":
    unittest.main()
