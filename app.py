import argparse
import atexit
import json
import os
import sys

from dotenv import load_dotenv

from valuation_app.analytics import analyze
from valuation_app.backup import BackupError, check_backup, create_backup, restore_backup
from valuation_app.benchmark import update_cache as update_benchmark_cache
from valuation_app.risk import update_cache as update_risk_cache
from valuation_app.risk import update_portfolio_var_cache
from valuation_app.market_research import update_cache as update_market_research_cache
from valuation_app.underlying_mail import download_underlying_archives
from valuation_app.underlying_archive import organize_zip_7zip
from valuation_app.factors import update_cache as update_factor_cache
from valuation_app.mail import download_valuations
from valuation_app.labels import migrate_catalog
from valuation_app.knowledge import KnowledgeStore
from valuation_app.knowledge_sources import KnowledgeSourceStore
from valuation_app.knowledge_diligence import KnowledgeDiligenceStore
from valuation_app.otc_ledger import migrate_workbook as migrate_otc_ledger
from valuation_app.otc_store import OtcStore
from valuation_app.organize import organize_products
from valuation_app.parser import scan_valuations
from valuation_app.static import build_index
from valuation_app.timing import TimingRecorder
from valuation_app.web import serve
from strategy_lab.pipeline import run as run_strategy_lab


def refresh_data(products_dir="products", account_users=None, timing=None):
    timing = timing or TimingRecorder(stream=None)
    result = {}
    try:
        with timing.step("顶层估值邮件处理", "邮件与文件"):
            result["download"] = download_valuations(products_dir, latest_only=True,
                                                      account_users=account_users)
    except Exception as exc:
        result["download"] = {"downloaded": [], "duplicates": 0,
                              "failures": ["%s: %s" % (type(exc).__name__, exc)], "accounts": []}
    with timing.step("估值表整理", "邮件与文件"):
        result["organize"] = organize_products(products_dir)
    try:
        with timing.step("底层分析邮件处理", "邮件与文件"):
            result["underlying_mail"] = download_underlying_archives()
    except Exception as exc:
        result["underlying_mail"] = {"failures": ["%s: %s" % (type(exc).__name__, exc)]}
    archive_path = os.path.join("底层资产", "估值表.zip")
    if os.path.exists(archive_path):
        try:
            with timing.step("底层压缩包整理", "邮件与文件"):
                organized = organize_zip_7zip(archive_path, os.path.join("底层资产", "历史估值表"))
            result["underlying_organize"] = {key: value for key, value in organized.items()
                                              if key != "files"}
        except Exception as exc:
            result["underlying_organize"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        with timing.step("Wind指数缓存更新", "数据库与缓存"):
            result["benchmark"] = update_benchmark_cache()
    except Exception as exc:
        result["benchmark"] = {"successes": [], "failures": [
            {"error": "%s: %s" % (type(exc).__name__, exc), "used_cache": True}
        ]}
    try:
        with timing.step("风控行情缓存更新", "数据库与缓存"):
            result["risk"] = update_risk_cache()
    except Exception as exc:
        result["risk"] = {"successes": [], "failures": [
            {"error": "%s: %s" % (type(exc).__name__, exc), "used_cache": True}
        ]}
    try:
        with timing.step("RQData市场研究缓存更新", "数据库与缓存"):
            result["market_dashboard"] = update_market_research_cache()
    except Exception as exc:
        result["market_dashboard"] = {"errors": [
            {"error": "%s: %s" % (type(exc).__name__, exc), "used_cache": True}
        ]}
    try:
        with timing.step("多因子缓存更新", "数据库与缓存"):
            result["factor"] = update_factor_cache()
    except Exception as exc:
        result["factor"] = {"available": False, "errors": [
            {"error": "%s: %s" % (type(exc).__name__, exc), "used_cache": True}
        ]}
    path, report = build_index(products_dir, timing_callback=timing.callback)
    result["build"] = {"path": path, "report": report}
    result["timing"] = timing.summary()
    return result


def refresh_output(result):
    """Return a log-safe refresh summary for Windows scheduled tasks."""
    download = result.get("download") or {}
    organize = result.get("organize") or {}
    underlying_mail = result.get("underlying_mail") or {}
    benchmark = result.get("benchmark") or {}
    risk = result.get("risk") or {}
    market = result.get("market_dashboard") or {}
    factor = result.get("factor") or {}
    build = result.get("build") or {}
    build_report = build.get("report") or {}
    summary = {
        "download": {
            "downloaded": len(download.get("downloaded") or []),
            "duplicates": download.get("duplicates", 0),
            "failures": download.get("failures") or [],
            "accounts": len(download.get("accounts") or []),
        },
        "organize": {
            "copied": len(organize.get("copied") or []),
            "duplicates_skipped": len(organize.get("duplicates_skipped") or []),
            "errors": organize.get("errors") or [],
        },
        "underlying_mail": {
            "downloaded": len(underlying_mail.get("downloaded") or []),
            "failures": underlying_mail.get("failures") or [],
        },
        "benchmark": {
            "successes": len(benchmark.get("successes") or []),
            "failures": benchmark.get("failures") or [],
        },
        "risk": {
            "successes": len(risk.get("successes") or []),
            "failures": risk.get("failures") or [],
        },
        "market_dashboard": {
            "modules": len(market.get("modules") or {}),
            "errors": market.get("errors") or [],
        },
        "factor": {
            "available": factor.get("available"),
            "valuation_date": factor.get("valuation_date"),
            "factor_date": factor.get("factor_date"),
            "products": len(factor.get("products") or []),
            "errors": factor.get("errors") or [],
        },
        "build": {
            "path": build.get("path"),
            "summary": build_report.get("summary") or {},
        },
        "timing": result.get("timing") or {},
    }
    if "underlying_organize" in result:
        summary["underlying_organize"] = result["underlying_organize"]
    return json.dumps(summary, ensure_ascii=True, indent=2)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="从邮件估值表计算单一投资人的收益与收益率")
    parser.add_argument("command", choices=("download", "underlying-mail", "underlying-organize", "factor", "organize", "benchmark", "risk", "market-dashboard", "strategy-lab", "portfolio-var", "labels-migrate", "otc-ledger-migrate", "knowledge-add", "knowledge-import", "knowledge-check", "knowledge-reindex", "knowledge-export", "knowledge-wechat-scan", "knowledge-review-export", "knowledge-review-import", "knowledge-diligence-export", "knowledge-diligence-import", "knowledge-obsidian-export", "backup", "backup-check", "backup-restore", "analyze", "build", "run", "share", "refresh"), nargs="?", default="run")
    parser.add_argument("--products-dir", default="products")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--latest-only", action="store_true", help="下载时仅检查最近2000封邮件")
    parser.add_argument("--account", action="append", help="只使用指定邮箱账号，可重复传入")
    parser.add_argument("--knowledge-root", default="knowledge_base", help="本地知识库根目录")
    parser.add_argument("--knowledge-file", help="knowledge-add要导入的单个文件")
    parser.add_argument("--batch", help="知识库助手审阅批次ID")
    parser.add_argument("--backup-root", help="备份仓库；默认读取BACKUP_DIR")
    parser.add_argument("--snapshot", help="备份快照ID；默认使用最新快照")
    parser.add_argument("--target", help="backup-restore恢复到不存在或为空的目录")
    parser.add_argument("--source", help="otc-ledger-migrate只读迁移的源工作簿")
    args = parser.parse_args()
    timing = TimingRecorder()
    atexit.register(timing.print_summary)
    if args.command == "download":
        with timing.step("邮件估值表下载", "命令执行"):
            value = download_valuations(args.products_dir, latest_only=args.latest_only,
                                        account_users=args.account)
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == "underlying-mail":
        with timing.step("底层分析邮件处理", "命令执行"):
            value = download_underlying_archives(latest_only=args.latest_only)
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == "underlying-organize":
        with timing.step("底层压缩包整理", "命令执行"):
            value = organize_zip_7zip(os.path.join("底层资产", "估值表.zip"),
                                      os.path.join("底层资产", "历史估值表"))
        print(json.dumps({key: item for key, item in value.items() if key != "files"},
                         ensure_ascii=False, indent=2))
    elif args.command == "factor":
        with timing.step("多因子缓存更新", "命令执行"):
            value = update_factor_cache()
        print(json.dumps({"available": value.get("available"),
                          "updated_at": value.get("updated_at"),
                          "valuation_date": value.get("valuation_date"),
                          "factor_date": value.get("factor_date"),
                          "products": len(value.get("products", [])),
                          "errors": value.get("errors", [])}, ensure_ascii=False, indent=2))
    elif args.command == "organize":
        with timing.step("估值表整理", "命令执行"):
            value = organize_products(args.products_dir)
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == "benchmark":
        with timing.step("Wind指数缓存更新", "命令执行"):
            value = update_benchmark_cache()
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == "risk":
        with timing.step("风控行情缓存更新", "命令执行"):
            value = update_risk_cache()
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == "market-dashboard":
        with timing.step("市场研究缓存更新", "数据库与缓存"):
            result = update_market_research_cache()
        path, report = build_index(args.products_dir, timing_callback=timing.callback)
        result["build"] = {"path": path,
                           "analyzable_products": report["summary"]["analyzable_products"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "strategy-lab":
        with timing.step("指数ETF策略研究", "数据计算"):
            result = run_strategy_lab()
        path, report = build_index(args.products_dir, timing_callback=timing.callback)
        print(json.dumps({"status": result.get("status"), "generated_at": result.get("generated_at"),
                          "latest_signal_date": result.get("latest_signal_date"),
                          "recommendations": len(result.get("recommendations", [])),
                          "build": {"path": path, "analyzable_products": report["summary"]["analyzable_products"]}},
                         ensure_ascii=False, indent=2))
    elif args.command == "portfolio-var":
        with timing.step("风控日报全资产VaR计算", "数据计算"):
            result = update_portfolio_var_cache()
        path, report = build_index(args.products_dir, timing_callback=timing.callback)
        result["build"] = {"path": path,
                           "analyzable_products": report["summary"]["analyzable_products"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "labels-migrate":
        with timing.step("标签Excel一次性迁移", "命令执行"):
            root = os.path.abspath(os.path.dirname(args.products_dir))
            result = migrate_catalog(os.path.join(root, "产品标签.xlsx"),
                                     os.path.join(root, "管理人清单.xlsx"),
                                     os.path.join(root, "data_sources", "product_labels.json"))
        print(json.dumps({"path": os.path.join(root, "data_sources", "product_labels.json"),
                          "schema_version": result["schema_version"],
                          "revision": result["revision"],
                          "record_count": len(result["records"]),
                          "updated_at": result["updated_at"]}, ensure_ascii=False, indent=2))
    elif args.command == "otc-ledger-migrate":
        if not args.source:
            parser.error("otc-ledger-migrate必须提供--source")
        with timing.step("场外衍生品产品簿记迁移", "命令执行"):
            root = os.path.abspath(os.path.dirname(args.products_dir))
            result = migrate_otc_ledger(OtcStore(root), args.source)
        print(json.dumps({key: value for key, value in result.items() if key != "products"},
                         ensure_ascii=False, indent=2))
    elif args.command in ("knowledge-wechat-scan", "knowledge-review-export", "knowledge-review-import",
                          "knowledge-diligence-export", "knowledge-diligence-import", "knowledge-obsidian-export"):
        project_root = os.path.abspath(os.path.dirname(args.products_dir))
        source_store = KnowledgeSourceStore(project_root)
        diligence_store = KnowledgeDiligenceStore(project_root)
        with timing.step("知识源、尽调事实与知识图谱", "命令执行"):
            if args.command == "knowledge-wechat-scan":
                value = source_store.scan()
            elif args.command == "knowledge-review-export":
                value = source_store.export_review(args.batch)
            elif args.command == "knowledge-review-import":
                if not args.batch:
                    parser.error("knowledge-review-import必须提供--batch")
                value = source_store.import_review(args.batch)
            elif args.command == "knowledge-diligence-export":
                value = diligence_store.export_batch(args.batch)
            elif args.command == "knowledge-diligence-import":
                if not args.batch:
                    parser.error("knowledge-diligence-import必须提供--batch")
                value = diligence_store.import_batch(args.batch)
            else:
                value = source_store.export_obsidian()
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command.startswith("knowledge-"):
        store = KnowledgeStore(args.knowledge_root)
        with timing.step("本地知识库维护", "命令执行"):
            if args.command == "knowledge-add":
                if not args.knowledge_file:
                    parser.error("knowledge-add必须提供 --knowledge-file")
                item, duplicate = store.add_file(args.knowledge_file)
                value = {"document": item, "duplicate": duplicate}
            elif args.command == "knowledge-import":
                value = store.import_inbox()
            elif args.command == "knowledge-check":
                value = store.integrity_check()
            elif args.command == "knowledge-reindex":
                value = store.rebuild_index()
            else:
                value = store.export_metadata(os.path.join(args.knowledge_root, "exports", "metadata.json"))
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command in ("backup", "backup-check", "backup-restore"):
        backup_root = args.backup_root or os.environ.get("BACKUP_DIR")
        if not backup_root:
            parser.error("请通过BACKUP_DIR或--backup-root配置备份目录")
        project_root = os.path.abspath(os.path.dirname(args.products_dir))
        try:
            with timing.step("业务数据备份", "命令执行"):
                if args.command == "backup":
                    retention = int(os.environ.get("BACKUP_RETENTION_COUNT", "30"))
                    value = create_backup(project_root, backup_root, retention)
                elif args.command == "backup-check":
                    value = check_backup(backup_root, args.snapshot)
                else:
                    if not args.target:
                        parser.error("backup-restore必须提供--target")
                    value = restore_backup(backup_root, args.target, args.snapshot)
        except (BackupError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == "build":
        path, report = build_index(args.products_dir, timing_callback=timing.callback)
        print("已生成：%s；可计算产品：%d" % (path, report["summary"]["analyzable_products"]))
    elif args.command == "analyze":
        with timing.step("估值扫描与收益分析", "命令执行"):
            snapshots, errors = scan_valuations(args.products_dir)
            result = analyze(snapshots); result["parse_errors"] = errors
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "refresh":
        result = refresh_data(args.products_dir, args.account, timing=timing)
        timing.print_summary()
        result["timing"] = timing.summary()
        print(refresh_output(result))
        return 0
    elif args.command in ("run", "share"):
        if args.command == "share" and args.host != "127.0.0.1":
            parser.error("share命令只允许使用 --host 127.0.0.1")
        timing.event("start", "网页服务启动", "服务运行")
        serve("index.html", args.host, args.port, args.command == "run" and not args.no_browser,
              timing_callback=timing.callback)
    else:
        return 2
    timing.print_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
