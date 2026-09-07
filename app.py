import argparse
import atexit
import json
import os
import sys

from valuation_app.analytics import analyze
from valuation_app.benchmark import update_cache as update_benchmark_cache
from valuation_app.risk import update_cache as update_risk_cache
from valuation_app.risk import update_portfolio_var_cache
from valuation_app.market_research import update_cache as update_market_research_cache
from valuation_app.underlying_mail import download_underlying_archives
from valuation_app.underlying_archive import organize_zip_7zip
from valuation_app.factors import update_cache as update_factor_cache
from valuation_app.mail import download_valuations
from valuation_app.labels import migrate_catalog
from valuation_app.organize import organize_products
from valuation_app.parser import scan_valuations
from valuation_app.static import build_index
from valuation_app.timing import TimingRecorder
from valuation_app.web import serve


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
    return json.dumps(result, ensure_ascii=True, indent=2)


def main():
    parser = argparse.ArgumentParser(description="从邮件估值表计算单一投资人的收益与收益率")
    parser.add_argument("command", choices=("download", "underlying-mail", "underlying-organize", "factor", "organize", "benchmark", "risk", "market-dashboard", "portfolio-var", "labels-migrate", "analyze", "build", "run", "share", "refresh"), nargs="?", default="run")
    parser.add_argument("--products-dir", default="products")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--latest-only", action="store_true", help="下载时仅检查最近2000封邮件")
    parser.add_argument("--account", action="append", help="只使用指定邮箱账号，可重复传入")
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
