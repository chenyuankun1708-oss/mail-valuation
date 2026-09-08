"""策略绩效归因：把组合月度收益拆成资产贡献与因子驱动。

两层归因（全部基于走步回测的已实现结果，无前视）：
1. 资产贡献归因：每个信号期的组合收益 = Σ (权重 × 该资产当期收益)，
   按资产累计贡献、正贡献次数等汇总。
2. 因子驱动归因：对每个信号期，用当期 Ridge 模型的预测贡献
   （predict 返回的 contributions）按资产加权，得到"模型认为的收益来源"。
"""
from .model import fit_ridge, predict


def _period_asset_return(series, code, signal, next_signal):
    points = series.get(code, {}).get("points", [])
    from .features import next_point
    start, end = next_point(points, signal), next_point(points, next_signal)
    if start and end and start.get("close"):
        return end["close"] / start["close"] - 1
    return None


def asset_attribution(periods, series):
    """逐期资产贡献：periods 来自 walk_forward 的输出。"""
    totals, positives, negatives = {}, {}, {}
    for period in periods:
        weights = (period.get("weights") or {}).get("ridge") or {}
        for code, weight in weights.items():
            ret = _period_asset_return(series, code, period["signal_date"], period["next_signal_date"])
            if ret is None:
                continue
            contribution = weight * ret
            totals[code] = totals.get(code, 0.0) + contribution
            if contribution >= 0:
                positives[code] = positives.get(code, 0) + 1
            else:
                negatives[code] = negatives.get(code, 0) + 1
    rows = []
    for code, contribution in totals.items():
        positive, negative = positives.get(code, 0), negatives.get(code, 0)
        rows.append({"code": code, "total_contribution": round(contribution, 6),
                     "positive_periods": positive, "negative_periods": negative,
                     "hit_rate": round(positive / float(positive + negative), 4) if positive + negative else None})
    rows.sort(key=lambda item: -item["total_contribution"])
    return rows


def factor_attribution(periods):
    """逐期因子驱动：用回测记录里的预测贡献（ridge）按权重加权。"""
    totals = {}
    count = 0
    for period in periods:
        weights = (period.get("weights") or {}).get("ridge") or {}
        predictions = {item["code"]: item for item in period.get("predictions") or []}
        if not weights or not predictions:
            continue
        count += 1
        for code, weight in weights.items():
            item = predictions.get(code)
            if not item:
                continue
            for name, value in (item.get("contributions") or {}).items():
                totals[name] = totals.get(name, 0.0) + weight * float(value)
    if not count:
        return []
    rows = [{"factor": name, "weighted_contribution": round(value / count, 6)}
            for name, value in totals.items()]
    rows.sort(key=lambda item: -item["weighted_contribution"])
    return rows


def summarize_attribution(periods, series):
    """归因汇总入口。"""
    return {"asset_contribution": asset_attribution(periods, series),
            "factor_contribution": factor_attribution(periods),
            "attribution_periods": len(periods),
            "note": "资产贡献按当期权重×当期收益累计；因子驱动为模型预测贡献的权重加权平均，两者均为研究归因而非交易归因。"}
