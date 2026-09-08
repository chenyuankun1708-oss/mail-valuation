import argparse
import json

from .pipeline import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="受限白名单指数ETF策略研究")
    parser.add_argument("command", choices=("run", "analyze-factors", "sweep", "attribution"), nargs="?", default="run")
    args = parser.parse_args(argv)
    result = run()
    if args.command == "analyze-factors":
        output = {"status": result.get("status"),
                  "factor_analysis": result.get("factor_analysis", {})}
    elif args.command == "sweep":
        output = {"status": result.get("status"),
                  "parameter_sweep": result.get("parameter_sweep", {})}
    elif args.command == "attribution":
        output = {"status": result.get("status"),
                  "attribution": result.get("attribution", {})}
    else:
        output = {"status": result["status"], "generated_at": result["generated_at"],
                  "latest_signal_date": result["latest_signal_date"],
                  "recommendations": len(result["recommendations"])}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
