import math

from . import MODEL_VERSION
from .config import BENCHMARK, COST_SCENARIOS_BP, MIN_TRAIN_MONTHS
from .features import next_point
from .model import baseline_scores, fit_ridge, predict
from .portfolio import limit_turnover, target_weights, validate


def performance(values):
    if not values:
        return {}
    nav, peak, drawdown = 1.0, 1.0, 0.0
    for value in values:
        nav *= 1 + value
        peak = max(peak, nav)
        drawdown = min(drawdown, nav / peak - 1)
    annual = nav ** (12.0 / len(values)) - 1
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / max(len(values) - 1, 1)
    volatility = math.sqrt(variance * 12)
    return {"total_return": nav - 1, "annual_return": annual, "annual_volatility": volatility,
            "sharpe": annual / volatility if volatility else None, "max_drawdown": drawdown,
            "calmar": annual / abs(drawdown) if drawdown else None,
            "monthly_win_rate": sum(value > 0 for value in values) / float(len(values))}


def _period_return(series, weights, signal, next_signal):
    result, available = 0.0, 0.0
    for code, weight in weights.items():
        points = series.get(code, {}).get("points", [])
        start, end = next_point(points, signal), next_point(points, next_signal)
        if start and end and start["close"]:
            result += weight * (end["close"] / start["close"] - 1)
            available += weight
    return result / available if available > .999 else None


def walk_forward(rows, series, alpha=None, min_train_months=None):
    dates = sorted(set(row["date"] for row in rows))
    by_date = {day: [row for row in rows if row["date"] == day] for day in dates}
    min_months = min_train_months if min_train_months is not None else MIN_TRAIN_MONTHS
    histories = {"ridge": {cost: [] for cost in COST_SCENARIOS_BP},
                 "baseline": {cost: [] for cost in COST_SCENARIOS_BP}, "benchmark": []}
    previous = {"ridge": {}, "baseline": {}}
    periods, hits = [], []
    for index in range(1, len(dates)):
        signal, next_signal = dates[index - 1], dates[index]
        training = [row for row in rows if row.get("target_excess_return") is not None and row.get("target_end", "") <= signal]
        train_months = sorted(set(row["date"] for row in training))
        current = by_date[signal]
        if len(train_months) < min_months or not current:
            continue
        model = fit_ridge(training, alpha=alpha) if alpha is not None else fit_ridge(training)
        ridge_scores, predictions = {}, []
        for row in current:
            value, contributions = predict(model, row)
            ridge_scores[row["code"]] = {"score": value, "components": contributions}
            predictions.append({"code": row["code"], "name": row["name"], "prediction": value,
                                "contributions": contributions})
        score_sets = {"ridge": ridge_scores, "baseline": baseline_scores(current)}
        record = {"signal_date": signal, "next_signal_date": next_signal,
                  "training_start": model["training_start"], "training_end": model["training_end"],
                  "training_samples": model["samples"], "predictions": predictions, "models": {}, "weights": {}}
        for name, scores in score_sets.items():
            target = target_weights(current, scores)
            weights, turnover = limit_turnover(previous[name], target)
            gross = _period_return(series, weights, signal, next_signal)
            if gross is None:
                continue
            for cost in COST_SCENARIOS_BP:
                histories[name][cost].append(gross - turnover * cost / 10000.0)
            previous[name] = weights
            record["weights"][name] = weights
            record["models"][name] = {"gross_return": gross, "turnover": turnover,
                                      "constraints": validate(weights)}
        benchmark_return = _period_return(series, {BENCHMARK: 1.0}, signal, next_signal)
        if benchmark_return is not None:
            histories["benchmark"].append(benchmark_return)
        actual = {row["code"]: row.get("target_excess_return") for row in current}
        hits.extend((item["prediction"] >= 0) == (actual[item["code"]] >= 0)
                    for item in predictions if actual.get(item["code"]) is not None)
        if record["weights"]:
            periods.append(record)
    metrics = {name: {str(cost): performance(values) for cost, values in scenarios.items()}
               for name, scenarios in histories.items() if name != "benchmark"}
    metrics["benchmark"] = performance(histories["benchmark"])
    if metrics.get("ridge", {}).get("10") and metrics.get("benchmark"):
        metrics["ridge"]["10"]["relative_annual_return"] = metrics["ridge"]["10"].get("annual_return", 0) - metrics["benchmark"].get("annual_return", 0)
    return {"status": "ok" if periods else "insufficient_history", "model_version": MODEL_VERSION,
            "periods": periods, "metrics": metrics,
            "prediction_hit_rate": sum(hits) / float(len(hits)) if hits else None}
