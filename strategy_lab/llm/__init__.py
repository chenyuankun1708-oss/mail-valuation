# -*- coding: utf-8 -*-
"""strategy_lab.llm 包：LLM 双通道策略生成（云端 OpenAI 兼容 API + 本地 ollama）。

安全边界：
- LLM 只输出受 spec 校验约束的结构化 JSON（方法论/因子选择/参数值），
  本地代码解释执行，绝不 eval LLM 生成的代码。
- 组合约束（宽基≥50%、组≤30%、单只35/15/10%、换手≤30%、无杠杆满仓）
  由 portfolio.validate 强制，LLM 无法绕过。
- 凭据只从 .env 读（LLM_PROVIDER/LLM_API_BASE/LLM_API_KEY/LLM_MODEL/OLLAMA_URL），
  不进源码、日志或测试。
"""
