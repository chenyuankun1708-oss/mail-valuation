# -*- coding: utf-8 -*-
"""M3 LLM 双通道测试：spec 校验、provider 抽象、generate 重试、executor 执行。

全部使用 mock provider，不发真实网络请求。
"""
import json
import os
import shutil
import tempfile
import unittest

from strategy_lab.llm import generate, provider, spec, executor


def _rows(n=6):
    rows = []
    for i in range(n):
        date = "2025-%02d-28" % (i + 1)
        for j, code in enumerate(("000300", "000905")):
            rows.append({"date": date, "code": code, "name": code, "asset_class": "broad",
                         "features": {"momentum_1m": 0.01 * (i + j), "momentum_3m": 0.03 * (i - j),
                                      "momentum_6m": 0.06 * i, "momentum_12m": 0.12 * i,
                                      "volatility_60d": 0.2 + 0.01 * j,
                                      "relative_3m": 0.01, "relative_6m": 0.02,
                                      "macro_state": 0.5, "style_state": 0.1, "sentiment_state": 0.0},
                         "available_at": date + "T15:01:00+08:00",
                         "target_excess_return": 0.01, "target_end": date})
    return rows


class MockProvider(object):
    def __init__(self, replies, name="mock"):
        self.replies = list(replies)
        self.name = name
        self.calls = 0

    def chat(self, system_prompt, user_prompt):
        self.calls += 1
        if not self.replies:
            raise provider.LLMError("mock exhausted")
        return self.replies.pop(0)


class SpecTest(unittest.TestCase):
    def test_valid_spec_passes(self):
        candidate = {"spec_version": spec.SPEC_VERSION, "methodology": "risk_parity_score",
                      "params": {"score_weight": 0.6, "vol_floor": 0.05},
                      "universe": ["000300", "000905"], "rationale": "测试"}
        ok, errors = spec.validate_spec(candidate)
        self.assertTrue(ok, errors)
        self.assertEqual(errors, [])

    def test_out_of_range_param_rejected(self):
        candidate = {"spec_version": spec.SPEC_VERSION, "methodology": "risk_parity_score",
                     "params": {"score_weight": 5.0}}
        ok, errors = spec.validate_spec(candidate)
        self.assertFalse(ok)
        self.assertTrue(any("超出范围" in e for e in errors))

    def test_unknown_methodology_rejected(self):
        candidate = {"spec_version": spec.SPEC_VERSION, "methodology": "magic",
                     "params": {}}
        ok, errors = spec.validate_spec(candidate)
        self.assertFalse(ok)

    def test_non_whitelist_universe_rejected(self):
        candidate = {"spec_version": spec.SPEC_VERSION, "methodology": "defensive",
                     "params": {"vol_threshold": 0.2},
                     "universe": ["600519"]}
        ok, errors = spec.validate_spec(candidate)
        self.assertFalse(ok)
        self.assertTrue(any("白名单" in e for e in errors))

    def test_sanitize_drops_extra_keys(self):
        candidate = {"spec_version": spec.SPEC_VERSION, "methodology": "momentum_tilt",
                     "params": {"lookback_months": 3, "top_n": 3, "tilt_strength": 0.2,
                                "hack_key": 999},
                     "universe": ["000300", "600519"], "rationale": "x" * 600,
                     "evil": "rm -rf"}
        clean = spec.sanitize_spec(candidate)
        self.assertNotIn("evil", clean)
        self.assertNotIn("hack_key", clean["params"])
        self.assertEqual(clean["universe"], ["000300"])
        self.assertLessEqual(len(clean["rationale"]), 500)


class ExtractJsonTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(generate.extract_json('{"a": 1}'), {"a": 1})

    def test_markdown_fence(self):
        text = '```json\n{"a": 2}\n```'
        self.assertEqual(generate.extract_json(text), {"a": 2})

    def test_with_prose(self):
        text = '好的，这是我的规格：{"a": 3} 希望有帮助。'
        self.assertEqual(generate.extract_json(text), {"a": 3})

    def test_invalid(self):
        self.assertIsNone(generate.extract_json("完全没有JSON"))
        self.assertIsNone(generate.extract_json('{"a": broken}'))


class GenerateSpecTest(unittest.TestCase):
    def test_success_first_try(self):
        reply = json.dumps({"spec_version": spec.SPEC_VERSION, "methodology": "risk_parity_score",
                            "params": {"score_weight": 0.5, "vol_floor": 0.05},
                            "universe": ["000300"], "rationale": "测试"})
        mock = MockProvider([reply])
        result = generate.generate_spec([mock], "risk_parity_score", "目标")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider"], "mock")
        self.assertEqual(mock.calls, 1)

    def test_retry_then_success(self):
        bad = "不是JSON"
        good = json.dumps({"spec_version": spec.SPEC_VERSION, "methodology": "risk_parity_score",
                           "params": {"score_weight": 0.5}, "universe": ["000300"]})
        mock = MockProvider([bad, good])
        result = generate.generate_spec([mock], "risk_parity_score", "目标")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(mock.calls, 2)

    def test_cloud_fail_fallback_to_local(self):
        """云端失败 → 本地兜底。"""
        cloud = MockProvider([], name="cloud")
        good = json.dumps({"spec_version": spec.SPEC_VERSION, "methodology": "defensive",
                           "params": {"vol_threshold": 0.2}, "universe": ["000300"]})
        local = MockProvider([good], name="ollama")
        result = generate.generate_spec([cloud, local], "defensive", "目标")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider"], "ollama")

    def test_all_fail_reports_errors(self):
        mock = MockProvider(["junk", "junk2", "junk3"])
        result = generate.generate_spec([mock], "risk_parity_score", "目标")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["errors"])


class ExecutorTest(unittest.TestCase):
    def test_risk_parity_score_executes(self):
        sp = {"methodology": "risk_parity_score", "params": {"score_weight": 0.5, "vol_floor": 0.05},
              "universe": ["000300", "000905"]}
        scores, note = executor.execute_spec(_rows(), sp)
        self.assertIn("000300", scores)
        self.assertIn("000905", scores)
        self.assertIn("portfolio.target_weights", note)

    def test_momentum_tilt_executes(self):
        sp = {"methodology": "momentum_tilt", "params": {"lookback_months": 3, "top_n": 2,
                                                          "tilt_strength": 0.2},
              "universe": ["000300", "000905"]}
        scores, _ = executor.execute_spec(_rows(), sp)
        self.assertEqual(len(scores), 2)

    def test_universe_filter(self):
        sp = {"methodology": "defensive", "params": {"vol_threshold": 0.2},
              "universe": ["000905"]}
        scores, _ = executor.execute_spec(_rows(), sp)
        self.assertEqual(list(scores), ["000905"])


class SaveRunTest(unittest.TestCase):
    def test_save_run_writes_file_without_credentials(self):
        folder = tempfile.mkdtemp()
        try:
            result = {"status": "ok", "provider": "cloud", "spec": {"methodology": "x"},
                      "generated_at": "2026-09-08T12:00:00"}
            path = generate.save_run(folder, result, now=None)
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["status"], "ok")
            self.assertNotIn("api_key", json.dumps(saved))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class ProviderEnvTest(unittest.TestCase):
    def test_load_providers_empty_env(self):
        providers, note = provider.load_providers(env={})
        self.assertEqual(providers, [])
        self.assertIn("未配置", note)

    def test_load_providers_cloud_and_local(self):
        env = {"LLM_API_KEY": "test-key", "LLM_API_BASE": "https://api.example.com/v1",
               "LLM_MODEL": "test-model", "OLLAMA_URL": "http://127.0.0.1:11434"}
        providers, note = provider.load_providers(env=env)
        self.assertEqual(len(providers), 2)
        self.assertEqual(providers[0].name, "cloud")
        self.assertEqual(providers[1].name, "ollama")


if __name__ == "__main__":
    unittest.main()
