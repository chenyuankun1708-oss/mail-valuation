# -*- coding: utf-8 -*-
"""LLM 策略生成：prompt 构造（注入可用因子/品种/约束）、spec 解析与重试、版本化落盘。"""
import json
import os
import re
from datetime import datetime

from ..config import ALLOWED_CODES, FEATURE_NAMES
from .provider import LLMError
from .spec import SPEC_VERSION, METHODOLOGIES, PARAM_RANGES, validate_spec, sanitize_spec

MAX_ATTEMPTS = 3


def build_system_prompt():
    return (
        "你是量化策略研究助手。你只能输出一个 JSON 对象（不要 markdown 代码块、不要解释文字），"
        "描述一个受约束的指数轮动策略规格。本地引擎会解释执行并回测验证你的规格；"
        "任何不符合 schema 的输出都会被拒绝。"
    )


def build_user_prompt(methodology, objective, factor_summary=None):
    ranges = PARAM_RANGES.get(methodology, {})
    params_doc = "; ".join("%s∈[%s,%s]" % (k, v[0], v[1]) for k, v in ranges.items()) or "无参数"
    factor_doc = ""
    if factor_summary:
        tops = sorted(factor_summary.items(), key=lambda kv: -abs(kv[1] or 0))[:5]
        factor_doc = "近期因子RankIC参考：" + "、".join(
            "%s=%.3f" % (k, v) for k, v in tops if v is not None) + "。"
    return json.dumps({
        "spec_version": SPEC_VERSION,
        "task": "生成策略规格",
        "methodology": methodology,
        "objective": objective or "在遵守全部组合约束的前提下提高样本外风险调整后收益",
        "allowed_methodologies": METHODOLOGIES,
        "methodology_params": params_doc,
        "universe": sorted(ALLOWED_CODES),
        "available_factors": list(FEATURE_NAMES),
        "factor_context": factor_doc,
        "constraints": ["无杠杆、无做空、100%投入", "宽基不低于50%", "风格/行业组各不超30%",
                          "单只宽基/风格/行业上限35%/15%/10%", "单边换手不超30%", "月末信号、次日执行"],
        "output_schema": {"spec_version": SPEC_VERSION, "methodology": "字符串",
                           "params": "数值对象（只含该方法论白名单参数）",
                           "universe": "universe 的子集数组", "rationale": "不超过200字的设计理由"},
    }, ensure_ascii=False)


def extract_json(text):
    """从 LLM 回复中提取第一个 JSON 对象（容忍 markdown 围栏与前后废话）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def generate_spec(providers, methodology, objective, factor_summary=None, now=None):
    """依次尝试 providers（云端优先本地兜底）生成并校验 spec。

    返回 dict：{status, spec, provider, attempts, errors, generated_at}。
    """
    errors = []
    for provider in providers:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = provider.chat(build_system_prompt(),
                                    build_user_prompt(methodology, objective, factor_summary))
            except LLMError as exc:
                errors.append("[%s#%d] %s" % (provider.name, attempt, exc))
                break  # 同一 provider 网络级失败直接换下一个
            candidate = extract_json(raw)
            if candidate is None:
                errors.append("[%s#%d] 回复中未找到 JSON" % (provider.name, attempt))
                continue
            ok, validation_errors = validate_spec(candidate)
            if not ok:
                errors.append("[%s#%d] 校验失败: %s" % (provider.name, attempt,
                                                          "；".join(validation_errors)))
                continue
            return {"status": "ok", "spec": sanitize_spec(candidate), "provider": provider.name,
                    "attempts": attempt, "errors": errors,
                    "generated_at": (now or datetime.now()).replace(microsecond=0).isoformat()}
    return {"status": "failed", "spec": None, "provider": None,
            "attempts": MAX_ATTEMPTS, "errors": errors,
            "generated_at": (now or datetime.now()).replace(microsecond=0).isoformat()}


def save_run(output_root, run_result, now=None):
    """版本化落盘 strategy_lab_data/llm_runs/<timestamp>.json。"""
    folder = os.path.join(output_root, "llm_runs")
    if not os.path.isdir(folder):
        os.makedirs(folder)
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    path = os.path.join(folder, "%s.json" % stamp)
    payload = dict(run_result)
    # 绝不写入任何凭据：provider 只存 name
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path
