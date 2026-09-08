import argparse
import json

from .pipeline import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="受限白名单指数ETF策略研究")
    parser.add_argument("command", choices=("run",), nargs="?", default="run")
    args = parser.parse_args(argv)
    del args
    result = run()
    print(json.dumps({"status": result["status"], "generated_at": result["generated_at"],
                      "latest_signal_date": result["latest_signal_date"],
                      "recommendations": len(result["recommendations"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
