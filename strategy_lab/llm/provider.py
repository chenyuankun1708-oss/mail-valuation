# -*- coding: utf-8 -*-
"""LLM Provider 抽象：云端 OpenAI 兼容 API（智谱/DeepSeek/OpenAI 等）与本地 ollama。

云端优先、本地兜底。全部用 urllib.request 手写（Python 3.7 兼容，不用 SDK）。
凭据从环境变量读取，绝不写入日志。
"""
import json
import os
import urllib.request


DEFAULT_TIMEOUT = 120  # 秒


def _post_json(url, payload, headers=None, timeout=DEFAULT_TIMEOUT):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class LLMError(Exception):
    pass


class CloudProvider(object):
    """OpenAI 兼容 /chat/completions 接口（智谱、DeepSeek、OpenAI 等）。"""

    name = "cloud"

    def __init__(self, api_base, api_key, model, timeout=DEFAULT_TIMEOUT):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(self, system_prompt, user_prompt):
        if not (self.api_base and self.api_key and self.model):
            raise LLMError("云端 provider 配置不完整（检查 LLM_API_BASE/LLM_API_KEY/LLM_MODEL）")
        url = self.api_base + "/chat/completions"
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt}],
                   "temperature": 0.2}
        headers = {"Authorization": "Bearer %s" % self.api_key}
        try:
            response = _post_json(url, payload, headers=headers, timeout=self.timeout)
        except Exception as exc:
            raise LLMError("云端请求失败: %s" % type(exc).__name__)
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("云端响应格式异常")


class OllamaProvider(object):
    """本地 ollama /api/chat。凭据无需 API key。"""

    name = "ollama"

    def __init__(self, base_url="http://127.0.0.1:11434", model=None, timeout=300):
        self.base_url = base_url.rstrip("/")
        self.model = model or "qwen2.5:7b"
        self.timeout = timeout

    def chat(self, system_prompt, user_prompt):
        if not self.base_url:
            raise LLMError("本地 provider 配置不完整（检查 OLLAMA_URL）")
        url = self.base_url + "/api/chat"
        payload = {"model": self.model, "stream": False,
                   "messages": [{"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt}]}
        try:
            response = _post_json(url, payload, timeout=self.timeout)
        except Exception as exc:
            raise LLMError("本地 ollama 请求失败: %s" % type(exc).__name__)
        try:
            return response["message"]["content"]
        except (KeyError, TypeError):
            raise LLMError("本地 ollama 响应格式异常")


def load_providers(env=None):
    """按 .env/环境变量构造云端+本地 provider 清单（云端优先）。

    返回 (providers, note)。providers 可能为空列表。
    """
    env = env if env is not None else os.environ
    providers = []
    notes = []
    api_base = env.get("LLM_API_BASE") or "https://open.bigmodel.cn/api/paas/v4"
    api_key = env.get("LLM_API_KEY")
    model = env.get("LLM_MODEL") or "glm-4.6"
    if api_key:
        providers.append(CloudProvider(api_base, api_key, model))
        notes.append("cloud:%s" % model)
    else:
        notes.append("cloud:未配置LLM_API_KEY")
    ollama_url = env.get("OLLAMA_URL")
    if ollama_url:
        providers.append(OllamaProvider(ollama_url, env.get("OLLAMA_MODEL")))
        notes.append("ollama:%s" % (env.get("OLLAMA_MODEL") or "default"))
    return providers, "; ".join(notes)
