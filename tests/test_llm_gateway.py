import json
import os
import tempfile
import unittest
from unittest.mock import patch

from valuation_app.llm_gateway import LLMDisabledError, LLMGateway
from valuation_app.otc_store import OtcStore


class _LocalProvider(object):
    name = "ollama"
    model = "test-local"

    def __init__(self, attachment_id):
        self.attachment_id = attachment_id
        self.prompt = None

    def chat(self, system_prompt, user_prompt):
        self.prompt = user_prompt
        return json.dumps({"fields": [{"field": "notional", "input_value": 1000,
            "document_value": 1000, "status": "match", "evidence": [{
                "attachment_id": self.attachment_id, "location": "第1页", "quote": "本金1000"}]}]})


class LlmGatewayTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = OtcStore(self.temporary.name)
        self.product = self.store.create_product({"name": "核对产品", "notional": 1000,
            "terms": {}, "reference": {}}, "alice")
        contract = os.path.join(self.temporary.name, "contract.md")
        with open(contract, "w", encoding="utf-8") as output:
            output.write("本金1000")
        self.contract, _ = self.store.add_attachment(
            self.product["id"], contract, "合同.md", "产品合同", "alice")
        investor = os.path.join(self.temporary.name, "investor.md")
        with open(investor, "w", encoding="utf-8") as output:
            output.write("投资者敏感名单")
        self.store.add_attachment(
            self.product["id"], investor, "名单.md", "投资者名单", "alice")

    def tearDown(self):
        self.temporary.cleanup()

    def test_disabled_gateway_never_loads_a_provider(self):
        gateway = LLMGateway(self.temporary.name, self.store, {"LLM_ENABLED": "0"})
        self.assertFalse(gateway.status()["enabled"])
        with patch("valuation_app.llm_gateway.load_providers") as providers:
            with self.assertRaises(LLMDisabledError):
                gateway.submit_field_audit(self.product["id"], self.product["revision"])
            providers.assert_not_called()

    def test_local_field_audit_excludes_investor_list_and_does_not_edit_product(self):
        provider = _LocalProvider(self.contract["id"])
        gateway = LLMGateway(self.temporary.name, self.store, {
            "LLM_ENABLED": "1", "OLLAMA_URL": "http://127.0.0.1:11434"})
        product, attachments = self.store.field_audit_context(
            self.product["id"], self.product["revision"])
        audit = self.store.create_field_audit(self.product["id"], "alice")
        with patch("valuation_app.llm_gateway.load_providers", return_value=([provider], "local")):
            gateway._run_field_audit(audit["id"], product, attachments)
        stored = self.store.get_field_audit(audit["id"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["provider"], "ollama")
        self.assertIn("合同.md", provider.prompt)
        self.assertNotIn("名单.md", provider.prompt)
        self.assertNotIn("投资者敏感名单", provider.prompt)
        self.assertEqual(self.store.get_product(self.product["id"])["revision"], 1)


if __name__ == "__main__":
    unittest.main()
