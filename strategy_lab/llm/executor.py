# -*- coding: utf-8 -*-
"""spec 执行器：把校验通过的 LLM 策略 spec 解释为本地评分函数。

四种方法论均复用现有 factor rows 与 portfolio 约束体系：
- risk_parity_score: score_weight 混合（预测分数 vs 纯波动率平价）
- momentum_tilt: 动量加权倾斜 top_n
- value_tilt: 长回看动量逆向倾斜（价值/均值回归代理）
- defensive: 高波动时收缩到宽基
"""
import math

from ..config import asset_class, BROAD


def _score_rows(rows, spec):
    """按方法论计算每个 code 的分数（越大越好）。"""
    methodology = spec["methodology"]
    params = spec["params"]
    scores = {}
    if methodology == "risk_parity_score":
        # 预测分数混合波动率平价：用 momentum_3m 作为预测分数代理
        weight = params.get("score_weight", 0.5)
        floor = params.get("vol_floor", 0.05)
        for row in rows:
            vol = max(float(row["features"]["volatility_60d"]), floor)
            score = float(row["features"].get("momentum_3m") or 0.0)
            scores[row["code"]] = weight * score + (1 - weight) * (1.0 / vol)
    elif methodology == "momentum_tilt":
        lookback = {1: "momentum_1m", 3: "momentum_3m", 6: "momentum_6m", 12: "momentum_12m"}.get(
            int(params.get("lookback_months", 3)), "momentum_3m")
        ranked = sorted(rows, key=lambda r: -float(r["features"][lookback] or 0.0))
        top_codes = set(r["code"] for r in ranked[:int(params.get("top_n", 5))])
        strength = params.get("tilt_strength", 0.2)
        for row in rows:
            base = float(row["features"][lookback] or 0.0)
            scores[row["code"]] = base + (strength if row["code"] in top_codes else 0.0)
    elif methodology == "value_tilt":
        lookback = {6: "momentum_6m", 12: "momentum_12m"}.get(
            int(params.get("lookback_months", 12)), "momentum_12m")
        strength = params.get("tilt_strength", 0.2)
        for row in rows:
            # 价值/均值回归：低动量者得分更高
            scores[row["code"]] = -float(row["features"][lookback] or 0.0) * strength
    elif methodology == "defensive":
        threshold = params.get("vol_threshold", 0.25)
        for row in rows:
            vol = float(row["features"]["volatility_60d"])
            # 高波动惩罚：超过阈值后按超出部分线性扣分
            scores[row["code"]] = -max(vol - threshold, 0.0) * 10.0
    else:
        for row in rows:
            scores[row["code"]] = 0.0
    return scores


def execute_spec(rows, spec):
    """对当前截面执行 spec，返回 {code: score} 及执行说明。"""
    universe = set(spec.get("universe") or [])
    selected = [row for row in rows if not universe or row["code"] in universe]
    if not selected:
        selected = list(rows)
    scores = _score_rows(selected, spec)
    note = "方法论 %s 参数 %s；universe %d 只；评分经 portfolio.target_weights 施加全部组合约束" % (
        spec["methodology"], json_dumps(spec["params"]), len(universe))
    return scores, note


def json_dumps(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
