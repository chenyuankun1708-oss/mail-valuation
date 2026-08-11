import argparse
import json
import os
import sys

from valuation_app.analytics import analyze
from valuation_app.benchmark import update_cache as update_benchmark_cache
from valuation_app.mail import download_valuations
from valuation_app.organize import organize_products
from valuation_app.parser import scan_valuations
from valuation_app.static import build_index
from valuation_app.web import serve


def refresh_data(products_dir="products", account_users=None):
    result = {}
    try:
        result["download"] = download_valuations(products_dir, latest_only=True,
                                                  account_users=account_users)
    except Exception as exc:
        result["download"] = {"downloaded": [], "duplicates": 0,
                              "failures": ["%s: %s" % (type(exc).__name__, exc)], "accounts": []}
    result["organize"] = organize_products(products_dir)
    try:
        result["benchmark"] = update_benchmark_cache()
    except Exception as exc:
        result["benchmark"] = {"successes": [], "failures": [
            {"error": "%s: %s" % (type(exc).__name__, exc), "used_cache": True}
        ]}
    path, report = build_index(products_dir)
    result["build"] = {"path": path, "report": report}
    return result


def main():
    parser = argparse.ArgumentParser(description="从邮件估值表计算单一投资人的收益与收益率")
    parser.add_argument("command", choices=("download", "organize", "benchmark", "analyze", "build", "run", "share", "refresh"), nargs="?", default="run")
    parser.add_argument("--products-dir", default="products")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--latest-only", action="store_true", help="下载时仅检查最近2000封邮件")
    parser.add_argument("--account", action="append", help="只使用指定邮箱账号，可重复传入")
    args = parser.parse_args()
    if args.command == "download":
        print(json.dumps(download_valuations(args.products_dir, latest_only=args.latest_only,
                                             account_users=args.account), ensure_ascii=False, indent=2))
    elif args.command == "organize":
        print(json.dumps(organize_products(args.products_dir), ensure_ascii=False, indent=2))
    elif args.command == "benchmark":
        print(json.dumps(update_benchmark_cache(), ensure_ascii=False, indent=2))
    elif args.command == "build":
        path, report = build_index(args.products_dir)
        print("已生成：%s；可计算产品：%d" % (path, report["summary"]["analyzable_products"]))
    elif args.command == "analyze":
        snapshots, errors = scan_valuations(args.products_dir)
        result = analyze(snapshots); result["parse_errors"] = errors
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "refresh":
        print(json.dumps(refresh_data(args.products_dir, args.account), ensure_ascii=False, indent=2))
    elif args.command in ("run", "share"):
        if args.command == "share" and args.host != "127.0.0.1":
            parser.error("share命令只允许使用 --host 127.0.0.1")
        serve("index.html", args.host, args.port, args.command == "run" and not args.no_browser)
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
