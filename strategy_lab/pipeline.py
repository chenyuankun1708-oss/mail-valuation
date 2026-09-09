import os
from datetime import datetime

from . import BASELINE_VERSION, MODEL_VERSION, SCHEMA_VERSION
from .attribution import summarize_attribution
from .backtest import walk_forward
from .config import ALLOWED_CODES, MIN_TRAIN_MONTHS, STYLE, asset_class
from .factor_analysis import analyze as analyze_factors
from .features import attach_targets, feature_rows
from .model import fit_ridge, predict
from .portfolio import target_weights, validate
from .storage import atomic_json, file_hash, read_json
from .sweep import run_sweep


def _series(index_cache, research):
    result = {}
    for code, item in (index_cache.get("indices") or {}).items():
        if code in ALLOWED_CODES:
            result[code] = {"name": item.get("name", code), "points": item.get("points", []), "asset_class": asset_class(code)}
    for code, item in ((research.get("industry_indices") or {}).get("data") or {}).items():
        short = code.split(".")[0]
        if short in STYLE or short.startswith("801"):
            result[short] = {"name": item.get("name", short), "points": item.get("points", []), "asset_class": asset_class(short)}
    return result


def _etf_at(mapping, index_code, day):
    candidates = ((mapping.get("etf_mapping") or {}).get("data") or {}).get(index_code, {}).get("candidates", [])
    eligible = []
    for item in candidates:
        history = [point for point in item.get("history", []) if point.get("date", "") <= day]
        if len(history) >= 120:
            eligible.append((history[-1].get("aum", 0), item.get("code")))
    eligible.sort(reverse=True)
    return eligible[0][1] if eligible else None


def _latest_signal(rows):
    """Fit only known targets and produce weights for the latest month-end."""
    dates = sorted(set(row["date"] for row in rows))
    if not dates:
        return {}
    signal = dates[-1]
    current = [row for row in rows if row["date"] == signal]
    training = [row for row in rows
                if row.get("target_excess_return") is not None
                and row.get("target_end", "") <= signal]
    train_months = sorted(set(row["date"] for row in training))
    if len(train_months) < MIN_TRAIN_MONTHS or not current:
        return {"signal_date": signal, "status": "insufficient_history",
                "training_months": len(train_months), "predictions": [], "weights": {}}
    model = fit_ridge(training)
    scores, predictions = {}, []
    for row in current:
        value, contributions = predict(model, row)
        scores[row["code"]] = {"score": value, "components": contributions}
        predictions.append({"code": row["code"], "name": row["name"],
                            "prediction": value, "contributions": contributions})
    weights = target_weights(current, scores)
    return {"signal_date": signal, "status": "ok", "training_months": len(train_months),
            "training_start": model["training_start"], "training_end": model["training_end"],
            "training_samples": model["samples"], "alpha": model["alpha"],
            "coefficients": model["coefficients"], "predictions": predictions,
            "weights": weights, "constraints": validate(weights)}


def run(project_root=".", output_root="strategy_lab_data", now=None):
    root = os.path.abspath(project_root)
    output = os.path.abspath(os.path.join(root, output_root))
    index_path = os.path.join(root, "market_data", "index_daily.json")
    research_path = os.path.join(root, "market_data", "market_research.json")
    index_cache, research = read_json(index_path), read_json(research_path)
    attempted = (now or datetime.now()).replace(microsecond=0).isoformat()
    raw = {"schema_version": SCHEMA_VERSION, "captured_at": attempted,
           "sources": {"index_daily": {"path": "market_data/index_daily.json", "sha256": file_hash(index_path) if os.path.isfile(index_path) else None},
                       "market_research": {"path": "market_data/market_research.json", "sha256": file_hash(research_path) if os.path.isfile(research_path) else None}},
           "source_updated_at": {"index_daily": index_cache.get("updated_at"), "market_research": research.get("updated_at")}}
    series = _series(index_cache, research)
    clean = {"schema_version": SCHEMA_VERSION, "created_at": attempted, "series": series,
             "errors": [] if series else ["固定白名单行情缓存为空"]}
    rows = attach_targets(feature_rows(series, research, as_of=attempted[:10]), series) if "000300" in series else []
    factors = {"schema_version": SCHEMA_VERSION, "created_at": attempted,
               "feature_names": list(rows[0]["features"]) if rows else [], "rows": rows}
    backtest = walk_forward(rows, series) if rows else {"status": "insufficient_history", "periods": [], "metrics": {}}
    latest = _latest_signal(rows)
    recommendations = []
    for code, weight in (latest.get("weights") or {}).items():
        prediction = next((item for item in latest.get("predictions", []) if item["code"] == code), {})
        etf = _etf_at(research, code, latest.get("signal_date", ""))
        recommendations.append({"code": code, "name": series.get(code, {}).get("name", code),
                                "asset_class": asset_class(code), "weight": weight,
                                "prediction": prediction.get("prediction"),
                                "feature_contributions": prediction.get("contributions", {}),
                                "etf": etf,
                                "execution_status": "ETF可用" if etf else "无当时可得合格ETF，仅保留指数研究信号"})
    periods = backtest.get("periods", [])
    factor_report = analyze_factors(rows) if rows else {}
    sweep_report = run_sweep(rows, series) if rows else {}
    attribution_report = summarize_attribution(periods, series) if periods else {}
    validation = {
        "no_lookahead": all(item.get("training_end", "") <= item.get("signal_date", "") for item in periods),
        "out_of_sample_periods": len(periods),
        "portfolio_constraints": bool(latest.get("constraints")) and all(latest.get("constraints", {}).values()),
        "cost_sensitivity_bp": [0, 10, 20],
        "missing_feature_rows": sum(1 for row in rows if any(value is None for value in row["features"].values())),
    }
    metrics = backtest.get("metrics", {})
    ridge_10 = (metrics.get("ridge") or {}).get("10") or {}
    baseline_10 = (metrics.get("baseline") or {}).get("10") or {}
    model_comparison = {
        "ridge_annual_return_10bp": ridge_10.get("annual_return"),
        "baseline_annual_return_10bp": baseline_10.get("annual_return"),
        "annual_return_improvement": (ridge_10.get("annual_return") - baseline_10.get("annual_return"))
        if ridge_10.get("annual_return") is not None and baseline_10.get("annual_return") is not None else None,
    }
    result = {"schema_version": SCHEMA_VERSION, "generated_at": attempted, "status": backtest.get("status"),
              "research_only": True, "model_version": MODEL_VERSION, "baseline_version": BASELINE_VERSION,
              "benchmark": "沪深300", "rebalance": "月末信号、下一交易日模拟执行",
              "latest_signal_date": latest.get("signal_date"), "recommendations": recommendations,
              "latest_model": latest, "backtest": backtest, "validation": validation,
              "model_comparison": model_comparison, "factor_analysis": factor_report,
              "parameter_sweep": sweep_report, "attribution": attribution_report,
              "data_lineage": raw["sources"],
              "disclaimer": "研究输出，不生成订单、不连接券商、不构成投资指令。"}
    for name, value in (("raw_snapshot.json", raw), ("clean_snapshot.json", clean),
                        ("factor_snapshot.json", factors), ("result.json", result)):
        atomic_json(os.path.join(output, name), value)
    return result


def run_llm(methodology, objective="", project_root=".", output_root="strategy_lab_data",
            now=None, providers=None):
    """LLM 双通道策略生成入口：云端优先本地兜底，spec 校验后执行并回测。

    providers 可注入（测试用 mock）；None 时按环境变量构造。
    """
    from .llm.generate import generate_spec, save_run
    from .llm.provider import load_providers
    from .llm.executor import execute_spec
    from .portfolio import target_weights, validate as validate_weights
    from .factor_analysis import rank_ic_series

    root = os.path.abspath(project_root)
    output = os.path.abspath(os.path.join(root, output_root))
    index_cache = read_json(os.path.join(root, "market_data", "index_daily.json"))
    research = read_json(os.path.join(root, "market_data", "market_research.json"))
    series = _series(index_cache, research)
    rows = attach_targets(feature_rows(series, research, as_of=(now or datetime.now()).isoformat()[:10]),
                          series) if "000300" in series else []

    if providers is None:
        providers, _note = load_providers()
    # 因子上下文：最近 RankIC 均值，帮助 LLM 了解因子近期有效性
    factor_summary = {}
    if rows:
        ic = rank_ic_series(rows).get("summary", {})
        factor_summary = {name: (item or {}).get("mean_ic") for name, item in ic.items()}

    generation = generate_spec(providers, methodology, objective, factor_summary=factor_summary,
                               now=now)
    result = dict(generation)
    result["methodology"] = methodology
    result["objective"] = objective
    if generation["status"] == "ok":
        # 执行 spec：评分 → 约束权重 → 校验
        current = [row for row in rows if row["date"] == max(r["date"] for r in rows)] if rows else []
        scores, note = execute_spec(current, generation["spec"])
        weights = target_weights(current, {code: {"score": value, "components": {}}
                                            for code, value in scores.items()}) if current else {}
        result["spec"]["executed"] = {"weights": weights, "constraints": validate_weights(weights),
                                        "note": note, "signal_date": max(r["date"] for r in rows) if rows else None}
    path = save_run(output, result, now=now)
    result["saved_to"] = path
    return result
