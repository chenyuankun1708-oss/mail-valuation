import hashlib
import json
import os
import shutil
import sqlite3
import tempfile

from .config import PRODUCTS, TOP_PRODUCT_IDS
from .analysis_cache import build_analysis_database, source_record, _known_hashes
from .benchmark import page_payload as benchmark_page_payload
from .bottom_returns import sync_bottom_return_cache
from .ledger import load_ledger, load_product_metadata
from .labels import build_label_payload, read_workbook
from .parser import scan_valuations
from .risk import page_payload as risk_page_payload
from .underlying_assets import build_underlying_asset_payload
from .factors import page_payload as factor_page_payload
from .market_research import page_payload as market_research_page_payload


_ASSET_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web_assets")

def _asset_text(name):
    with open(os.path.join(_ASSET_ROOT, name), "r", encoding="utf-8") as handle:
        return handle.read()

STYLE = _asset_text("styles.css")
CORE_JS = _asset_text("core.js")
DASHBOARD_JS = _asset_text("dashboard.js")
OTC_DERIVATIVES_JS = _asset_text("otc_derivatives.js")
BOOTSTRAP_JS = _asset_text("bootstrap.js")
HELP_DATA = json.loads(_asset_text("help.json"))
HISTORY_DATA = json.loads(_asset_text("history.json"))
ARCHITECTURE_DATA = json.loads(_asset_text("architecture.json"))
SHELL = _asset_text("shell.html")
# Compatibility aggregate for calculation tests and downstream imports.
HTML = SHELL.replace('<link rel="stylesheet" href="__STYLE_URL__">',
                     '<style>' + STYLE + '</style>').replace(
                         '__APP_LOADER__', '<script>' + CORE_JS + OTC_DERIVATIVES_JS + DASHBOARD_JS + '</script>')


def build_index(products_dir="products", output="index.html", year=None, month=None,
                timing_callback=None):
    import time
    calculation_started = time.perf_counter()
    if timing_callback:
        timing_callback("start", "估值扫描与数据计算", "数据计算")
    snapshots, parse_errors = scan_valuations(products_dir)
    ledger_path = os.path.join(os.path.dirname(os.path.abspath(products_dir)), "专户资金台账.xlsx")
    flows, ledger_errors = load_ledger(ledger_path, snapshots) if os.path.exists(ledger_path) else ([], [{"error": "未找到专户资金台账.xlsx"}])
    metadata, metadata_errors = load_product_metadata(ledger_path) if os.path.exists(ledger_path) else ({}, [])
    ledger_errors.extend(metadata_errors)
    grouped = {name: {"product_id": TOP_PRODUCT_IDS[name], "name": name, "points": [], "flows": [],
                      "total_investment": metadata.get(name, {}).get("total_investment"),
                      "approved_quota": metadata.get(name, {}).get("approved_quota", 0)} for name in PRODUCTS}
    root = os.path.dirname(os.path.abspath(products_dir))
    analysis_path = os.path.join(root, ".runtime", "analysis.sqlite3")
    known_source_hashes = _known_hashes(analysis_path)
    source_records = {}
    for snapshot in snapshots:
        point = snapshot.as_dict()
        record = source_record(point["source_file"], known_source_hashes)
        source_records[record["path"]] = record
        point["source_sha256"] = record["sha256"]
        point["source_file"] = os.path.basename(point.get("source_file", ""))
        grouped[snapshot.product]["points"].append(point)
    for flow in flows:
        grouped[flow.product]["flows"].append(flow.as_dict())
    dates = []
    for product in grouped.values():
        product["points"].sort(key=lambda item: item["valuation_date"])
        product["flows"].sort(key=lambda item: (item["flow_date"] or "9999", item["flow_id"]))
        dates.extend(item["valuation_date"] for item in product["points"])
    holding_names = [holding["name"] for product in grouped.values() for point in product["points"]
                     for holding in point.get("holdings", [])]
    end_holding_names = [holding["name"] for product in grouped.values()
                         for holding in ((product["points"][-1].get("holdings", []))
                                         if product["points"] else [])]
    labels = build_label_payload(
        holding_names, os.path.join(root, "data_sources", "product_labels.json"),
        auto_add_names=end_holding_names,
        audit_path=os.path.join(root, "logs", "label-audit.jsonl"))
    def json_cell(value):
        return value.isoformat() if hasattr(value, "isoformat") else value
    try:
        raw_ledger_tables = read_workbook(ledger_path) if os.path.exists(ledger_path) else {}
        ledger_tables = {name: [[json_cell(value) for value in row] for row in rows]
                         for name, rows in raw_ledger_tables.items()}
    except Exception as exc:
        ledger_tables = {"读取提示": [["资金台账读取失败", type(exc).__name__]]}
    safe_parse_errors = [dict(item, file=os.path.basename(item.get("file", ""))) for item in parse_errors]
    fof_holdings = [(product["name"], holding["name"]) for product in grouped.values()
                    for point in product["points"] for holding in point.get("holdings", [])]
    underlying_assets = build_underlying_asset_payload(os.path.join(root, "底层资产"), fof_holdings)
    bottom_returns = sync_bottom_return_cache(os.path.join(root, "底层资产"), fof_holdings)
    risk_payload = risk_page_payload()
    benchmark_payload = benchmark_page_payload(earliest_date=min(dates))
    try:
        with open(os.path.join(root, "strategy_lab_data", "result.json"), "r", encoding="utf-8") as handle:
            strategy_result = json.load(handle)
        if not isinstance(strategy_result, dict):
            strategy_lab = {}
        else:
            backtest = strategy_result.get("backtest") or {}
            latest_model = strategy_result.get("latest_model") or {}
            strategy_lab = {key: strategy_result.get(key) for key in (
                "schema_version", "generated_at", "status", "research_only", "model_version",
                "baseline_version", "benchmark", "rebalance", "latest_signal_date",
                "recommendations", "validation", "model_comparison", "data_lineage", "disclaimer",
                "factor_analysis", "parameter_sweep", "attribution")}
            strategy_lab["backtest"] = {key: backtest.get(key) for key in (
                "status", "model_version", "metrics", "prediction_hit_rate")}
            strategy_lab["latest_model"] = {key: latest_model.get(key) for key in (
                "signal_date", "status", "training_months", "training_start", "training_end",
                "training_samples", "alpha", "coefficients", "constraints")}
    except (OSError, ValueError):
        strategy_lab = {}
    knowledge_path = os.path.join(root, "knowledge_base", "knowledge.sqlite3")
    otc_path = os.path.join(root, "otc_derivatives_data", "otc.sqlite3")
    otc_product_count = 0
    if os.path.exists(otc_path):
        try:
            connection = sqlite3.connect("file:%s?mode=ro" % otc_path.replace("\\", "/"), uri=True)
            try:
                otc_product_count = connection.execute("SELECT COUNT(*) FROM otc_products").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.Error:
            otc_product_count = 0
    module_status = {
        "valuations": {"name": "估值", "status": "current" if dates else "missing",
                       "data_cutoff": max(dates) if dates else None,
                       "source_count": len(snapshots), "warnings": len(parse_errors)},
        "market": {"name": "行情", "status": "current" if benchmark_payload.get("available") else "missing",
                   "data_cutoff": benchmark_payload.get("updated_at") or (max(dates) if dates else None),
                   "source_count": len((benchmark_payload.get("indices") or {})),
                   "warnings": 0},
        "labels": {"name": "标签", "status": "current" if labels.get("records") is not None else "missing",
                   "data_cutoff": labels.get("updated_at"), "source_count": labels.get("record_count", 0),
                   "warnings": len(labels.get("errors") or [])},
        "knowledge": {"name": "知识库", "status": "current" if os.path.exists(knowledge_path) else "missing",
                      "data_cutoff": (__import__("datetime").datetime.fromtimestamp(os.path.getmtime(knowledge_path)).isoformat()
                                      if os.path.exists(knowledge_path) else None),
                      "source_count": 1 if os.path.exists(knowledge_path) else 0, "warnings": 0},
        "bottom_returns": {"name": "底层收益", "status": "current" if bottom_returns.get("products") else "missing",
                           "data_cutoff": bottom_returns.get("updated_at"),
                           "source_count": len(bottom_returns.get("products") or []),
                           "warnings": len(bottom_returns.get("errors") or [])},
        "strategy_lab": {"name": "策略实验室", "status": strategy_lab.get("status") or "missing",
                         "data_cutoff": strategy_lab.get("generated_at"),
                         "source_count": len(strategy_lab.get("recommendations") or []),
                         "warnings": 0},
        "otc_backtest": {"name": "场外衍生品",
                         "status": "current" if all((benchmark_payload.get("indices") or {}).get(code)
                                                    for code in ("000852", "000905", "000300"))
                         else "missing",
                         "data_cutoff": benchmark_payload.get("updated_at"),
                         "source_count": otc_product_count,
                         "warnings": 0},
    }
    payload = {"products": list(grouped.values()), "parse_errors": safe_parse_errors, "ledger_errors": ledger_errors,
               "page_updated_at": __import__("datetime").datetime.now().replace(microsecond=0).isoformat(),
               "benchmarks": benchmark_payload, "risk": risk_payload,
               "labels": labels, "ledger_tables": ledger_tables, "underlying_assets": underlying_assets,
               "bottom_returns": bottom_returns,
               "factors": factor_page_payload(), "market_research": market_research_page_payload(),
               "strategy_lab": strategy_lab,
               "module_status": module_status,
               "documentation": {"help": HELP_DATA, "architecture": ARCHITECTURE_DATA},
               "default_start": "2026-06-30" if "2026-06-30" in dates else min(dates), "default_end": max(dates)}
    build_analysis_database(analysis_path, payload["products"], payload["page_updated_at"],
                            source_records.values())
    if timing_callback:
        timing_callback("finish", "估值扫描与数据计算", "数据计算",
                        time.perf_counter() - calculation_started, "success")
    output = os.path.abspath(output)
    output_dir = os.path.dirname(output)
    os.makedirs(output_dir, exist_ok=True)
    from .portfolio_var import export_assets_excel
    export_started = time.perf_counter()
    if timing_callback:
        timing_callback("start", "VaR明细Excel导出", "网页更新")
    try:
        export_assets_excel(risk_payload.get("portfolio_var", {}),
                            os.path.join(output_dir, "全资产VaR明细.xlsx"))
    except BaseException:
        if timing_callback:
            timing_callback("finish", "VaR明细Excel导出", "网页更新",
                            time.perf_counter() - export_started, "failed")
        raise
    if timing_callback:
        timing_callback("finish", "VaR明细Excel导出", "网页更新",
                        time.perf_counter() - export_started, "success")
    write_started = time.perf_counter()
    if timing_callback:
        timing_callback("start", "网页序列化与原子替换", "网页更新")
    temporary = None
    try:
        runtime_dir = os.path.join(output_dir, ".runtime")
        os.makedirs(runtime_dir, exist_ok=True)
        modules_dir = os.path.join(runtime_dir, "modules")
        os.makedirs(modules_dir, exist_ok=True)
        deferred_modules = {
            "factors": payload.pop("factors", {}),
            "market-research": payload.pop("market_research", {}),
            "strategy-lab": payload.pop("strategy_lab", {}),
            "underlying-assets": payload.pop("underlying_assets", {}),
            "ledger-tables": payload.pop("ledger_tables", {}),
            "label-workbooks": payload.get("labels", {}).pop("workbooks", {}),
            "portfolio-var": payload.get("risk", {}).pop("portfolio_var", {}),
            "bottom-returns": payload.pop("bottom_returns", {}),
        }
        payload.update({"factors": {}, "market_research": {}, "strategy_lab": {}, "underlying_assets": {},
                        "ledger_tables": {}, "bottom_returns": {}})
        payload.setdefault("labels", {})["workbooks"] = {}
        payload.setdefault("risk", {})["portfolio_var"] = {}
        runtime_core = CORE_JS.replace("const RAW=__DATA__,DAY=", "const RAW=window.__FOF_DATA__,DAY=")
        runtime_dashboard = DASHBOARD_JS
        version_seed = (payload["page_updated_at"] + STYLE + runtime_core + OTC_DERIVATIVES_JS + runtime_dashboard + BOOTSTRAP_JS).encode("utf-8")
        asset_version = hashlib.sha256(version_seed).hexdigest()[:16]
        asset_root = os.path.join(runtime_dir, "assets")
        os.makedirs(asset_root, exist_ok=True)
        version_dir = os.path.join(asset_root, asset_version)
        temporary_assets = tempfile.mkdtemp(prefix=".assets-", dir=asset_root)

        def write_asset(name, content):
            path = os.path.join(temporary_assets, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            return {"path": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "bytes": len(content.encode("utf-8"))}

        asset_manifest = {"schema_version": 1, "version": asset_version,
                          "generated_at": payload["page_updated_at"], "files": {}}
        asset_manifest["files"]["styles.css"] = write_asset("styles.css", STYLE)
        asset_manifest["files"]["core.js"] = write_asset("core.js", runtime_core)
        asset_manifest["files"]["dashboard.js"] = write_asset("dashboard.js", runtime_dashboard)
        asset_manifest["files"]["otc_derivatives.js"] = write_asset("otc_derivatives.js", OTC_DERIVATIVES_JS)
        asset_manifest["files"]["bootstrap.js"] = write_asset("bootstrap.js", BOOTSTRAP_JS)
        write_asset("manifest.json", json.dumps(asset_manifest, ensure_ascii=False, indent=2))
        if os.path.isdir(version_dir):
            shutil.rmtree(temporary_assets)
        else:
            os.replace(temporary_assets, version_dir)
        temporary_assets = None
        loader = r'''<script>window.__FOF_ASSETS__={core:'__CORE_URL__',otc:'__OTC_URL__',dashboard:'__DASHBOARD_URL__'};</script><script src="__BOOTSTRAP_URL__"></script>'''
        prefix = "/assets/%s/" % asset_version
        loader = loader.replace("__CORE_URL__", prefix + "core.js").replace(
            "__OTC_URL__", prefix + "otc_derivatives.js").replace(
            "__DASHBOARD_URL__", prefix + "dashboard.js").replace(
                "__BOOTSTRAP_URL__", prefix + "bootstrap.js")
        shell = SHELL.replace("__STYLE_URL__", prefix + "styles.css").replace("__APP_LOADER__", loader)

        def atomic_text(path, content, prefix):
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path),
                                                 prefix=prefix, suffix=".tmp", delete=False) as handle:
                    temp_path = handle.name
                    handle.write(content)
                os.replace(temp_path, path)
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)

        atomic_text(os.path.join(runtime_dir, "page-data.json"),
                    json.dumps(payload, ensure_ascii=False), ".page-data-")
        for module_name, module_payload in deferred_modules.items():
            atomic_text(os.path.join(modules_dir, module_name + ".json"),
                        json.dumps(module_payload, ensure_ascii=False), ".module-")
        atomic_text(os.path.join(runtime_dir, "app.js"), runtime_core + runtime_dashboard, ".app-js-")
        atomic_text(os.path.join(asset_root, "manifest.json"),
                    json.dumps(asset_manifest, ensure_ascii=False, indent=2), ".asset-manifest-")
        atomic_text(output, shell, ".index-")
    except BaseException:
        if timing_callback:
            timing_callback("finish", "网页序列化与原子替换", "网页更新",
                            time.perf_counter() - write_started, "failed")
        raise
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)
        if "temporary_assets" in locals() and temporary_assets and os.path.isdir(temporary_assets):
            shutil.rmtree(temporary_assets, ignore_errors=True)
    if timing_callback:
        timing_callback("finish", "网页序列化与原子替换", "网页更新",
                        time.perf_counter() - write_started, "success")
    return output, {"summary": {"analyzable_products": sum(len(p["points"]) >= 2 for p in grouped.values())},
                    "ledger_flows": len(flows), "ledger_errors": ledger_errors}
