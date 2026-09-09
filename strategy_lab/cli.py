import argparse
import json

from .pipeline import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="受限白名单指数ETF策略研究")
    parser.add_argument("command", choices=("run", "analyze-factors", "sweep", "attribution",
                                            "llm-generate"), nargs="?", default="run")
    parser.add_argument("--methodology", default="risk_parity_score",
                        help="LLM策略方法论：risk_parity_score/momentum_tilt/value_tilt/defensive")
    parser.add_argument("--objective", default="", help="策略目标（自由文本）")
    args = parser.parse_args(argv)
    if args.command == "llm-generate":
        from .pipeline import run_llm
        from .llm.provider import load_providers
        providers, note = load_providers()
        result = run_llm(args.methodology, args.objective, providers=providers)
        print(json.dumps({"status": result["status"], "provider": result["provider"],
                          "methodology": result["methodology"],
                          "spec": result["spec"], "saved_to": result["saved_to"],
                          "providers_note": note,
                          "errors": result["errors"]}, ensure_ascii=False, indent=2))
        return 0
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
