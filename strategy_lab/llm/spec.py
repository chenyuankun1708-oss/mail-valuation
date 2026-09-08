# -*- coding: utf-8 -*-
"""策略 spec 的 schema 定义与校验。

LLM 必须输出符合本 schema 的 JSON；任何越界值在服务端被拒绝，
不进入回测。字段全部白名单化，多余键直接丢弃。
"""
from ..config import ALLOWED_CODES

SPEC_VERSION = "llm-spec-v1"

# 方法论白名单：本地可解释执行的策略族
METHODOLOGIES = ("risk_parity_score", "momentum_tilt", "value_tilt", "defensive")

# 每种方法论允许的参数及其数值范围（闭区间）
PARAM_RANGES = {
    "risk_parity_score": {
        "score_weight": (0.0, 1.0),       # 预测分数权重（其余为波动率平价）
        "vol_floor": (0.01, 0.20),          # 波动率下限（年化）
    },
    "momentum_tilt": {
        "lookback_months": (1, 12),        # 动量回看月数
        "top_n": (2, 10),                  # 倾斜的资产数
        "tilt_strength": (0.0, 0.5),       # 倾斜强度（从等权向高分转移的权重）
    },
    "value_tilt": {
        "lookback_months": (6, 36),
        "tilt_strength": (0.0, 0.5),
    },
    "defensive": {
        "vol_lookback_days": (20, 120),    # 波动率回看天数
        "vol_threshold": (0.10, 0.50),     # 触发防御的波动率阈值（年化）
        "broad_weight": (0.50, 1.0),       # 防御时宽基保底权重
    },
}

SPEC_KEYS = ("spec_version", "methodology", "params", "universe", "rationale")


def validate_spec(spec):
    """校验 LLM 输出的策略 spec。返回 (ok, errors)。"""
    errors = []
    if not isinstance(spec, dict):
        return False, ["spec 必须是 JSON 对象"]

    if spec.get("spec_version") != SPEC_VERSION:
        errors.append("spec_version 必须是 %s" % SPEC_VERSION)

    methodology = spec.get("methodology")
    if methodology not in METHODOLOGIES:
        errors.append("methodology 必须是 %s 之一" % "/".join(METHODOLOGIES))

    params = spec.get("params")
    if not isinstance(params, dict) or not params:
        errors.append("params 必须是非空对象")
        params = {}
    if methodology in PARAM_RANGES:
        ranges = PARAM_RANGES[methodology]
        for key, value in params.items():
            if key not in ranges:
                errors.append("未知参数 %s（方法论 %s 不支持）" % (key, methodology))
                continue
            low, high = ranges[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append("参数 %s 必须是数值" % key)
            elif not (low <= value <= high):
                errors.append("参数 %s=%s 超出范围 [%s, %s]" % (key, value, low, high))

    universe = spec.get("universe")
    if universe is None:
        universe = sorted(ALLOWED_CODES)
    if not isinstance(universe, list) or not universe:
        errors.append("universe 必须是非空数组")
    else:
        for code in universe:
            if code not in ALLOWED_CODES:
                errors.append("universe 含白名单外代码 %s" % code)

    rationale = spec.get("rationale")
    if rationale is not None and (not isinstance(rationale, str) or len(rationale) > 500):
        errors.append("rationale 必须是不超过500字的字符串")

    return (not errors), errors


def sanitize_spec(spec):
    """白名单净化：只保留合法键与合法值，丢弃多余内容。"""
    clean = {"spec_version": SPEC_VERSION,
             "methodology": spec.get("methodology") if spec.get("methodology") in METHODOLOGIES else None,
             "params": {}, "universe": [], "rationale": ""}
    if clean["methodology"]:
        ranges = PARAM_RANGES[clean["methodology"]]
        for key, (low, high) in ranges.items():
            value = (spec.get("params") or {}).get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
                clean["params"][key] = value
        universe = spec.get("universe") or []
        clean["universe"] = [c for c in universe if c in ALLOWED_CODES] or sorted(ALLOWED_CODES)
        rationale = spec.get("rationale")
        if isinstance(rationale, str):
            clean["rationale"] = rationale[:500]
    return clean
