"""Fixed-task LLM gateway shared by web modules.

Disabled by default. It never accepts arbitrary prompts, paths, filenames or
security codes from browser requests.
"""

import json
import threading
import time
import uuid

from strategy_lab.llm.generate import extract_json
from strategy_lab.llm.provider import load_providers


TASK_TYPES = ("strategy_spec", "otc_field_audit")
AUDIT_STATUSES = ("match", "mismatch", "missing_in_document", "missing_in_product", "uncertain")
AUDIT_FIELDS = {
    "name", "strategy_name", "structure", "index_code", "notional", "start_date", "end_date",
    "performance_hurdle", "performance_fee_rate", "term_months", "lock_period_months",
    "knock_in_ratio", "knock_out_initial", "knock_out_decrease_monthly", "first_coupon",
    "second_coupon", "coupon_switch_months", "dividend_barrier", "monthly_dividend",
    "max_loss", "dcn_max_loss", "snowball_max_loss", "dcn_weight", "snowball_weight",
}


class LLMDisabledError(ValueError):
    pass


class LLMGateway(object):
    def __init__(self, root, otc_store, env=None, max_tasks=20):
        self.root = root
        self.otc_store = otc_store
        self.env = dict(env or {})
        self.enabled = str(self.env.get("LLM_ENABLED") or "0").strip().lower() in ("1", "true", "yes", "on")
        self.cloud_enabled = str(self.env.get("LLM_CLOUD_ENABLED") or "0").strip().lower() in (
            "1", "true", "yes", "on")
        self.max_tasks = max_tasks
        self.tasks = {}
        self.order = []
        self.lock = threading.Lock()

    def status(self):
        return {"enabled": self.enabled, "task_types": list(TASK_TYPES),
                "cloud_configured": bool(self.env.get("LLM_API_KEY")),
                "cloud_enabled": self.cloud_enabled,
                "local_configured": bool(self.env.get("OLLAMA_URL")),
                "default_field_audit_provider": "ollama",
                "investor_lists_external_allowed": False,
                "message": "已启用" if self.enabled else "大模型功能未启用（LLM_ENABLED=0）"}

    def _remember(self, task):
        with self.lock:
            self.tasks[task["id"]] = task
            self.order.append(task["id"])
            while len(self.order) > self.max_tasks:
                self.tasks.pop(self.order.pop(0), None)

    def submit_strategy(self, methodology, objective, actor="system"):
        if not self.enabled:
            raise LLMDisabledError("大模型功能未启用")
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "task_type": "strategy_spec", "status": "running",
                "created_at": time.time(), "actor": actor, "result": None, "error": None}
        self._remember(task)
        worker = threading.Thread(target=self._run_strategy,
                                  args=(task_id, methodology, objective))
        worker.daemon = True
        worker.start()
        return task_id

    def submit_field_audit(self, product_id, expected_revision, actor="system"):
        if not self.enabled:
            raise LLMDisabledError("字段排查未启用")
        product, attachments = self.otc_store.field_audit_context(product_id, expected_revision)
        if not attachments:
            raise ValueError("没有可用于字段排查的非投资者名单附件")
        audit = self.otc_store.create_field_audit(product_id, actor)
        task = {"id": audit["id"], "task_type": "otc_field_audit", "status": "running",
                "created_at": time.time(), "actor": actor, "product_id": product_id,
                "product_revision": product["revision"], "result": None, "error": None}
        self._remember(task)
        worker = threading.Thread(target=self._run_field_audit,
                                  args=(audit["id"], product, attachments))
        worker.daemon = True
        worker.start()
        return audit["id"]

    def _run_strategy(self, task_id, methodology, objective):
        try:
            from strategy_lab.pipeline import run_llm
            providers, _note = load_providers(self.env)
            result = run_llm(methodology, objective, project_root=self.root, providers=providers)
            self._finish_memory(task_id, result.get("status", "failed"), result, None)
        except Exception as exc:
            self._finish_memory(task_id, "failed", None, "%s: %s" % (type(exc).__name__, exc))

    @staticmethod
    def _audit_prompt(product, attachments):
        fields = {key: product.get(key) for key in
                  ("name", "strategy_name", "structure", "index_code", "notional",
                   "start_date", "end_date", "performance_hurdle", "performance_fee_rate")}
        fields.update(product.get("terms") or {})
        documents, used = [], 0
        for item in attachments:
            text = str(item.get("text_content") or "")[:50000]
            if used + len(text) > 200000:
                text = text[:max(0, 200000 - used)]
            used += len(text)
            documents.append({"attachment_id": item["id"], "category": item["category"],
                              "name": item["original_name"], "text": text})
            if used >= 200000:
                break
        payload = {"task": "核对产品人工字段与附件文字是否一致", "product_fields": fields,
                   "documents": documents, "allowed_fields": sorted(AUDIT_FIELDS),
                   "output_schema": {"fields": [{"field": "白名单字段", "input_value": "人工值",
                       "document_value": "文件值", "status": "match|mismatch|missing_in_document|missing_in_product|uncertain",
                       "evidence": [{"attachment_id": "附件UUID", "location": "页码或工作表",
                                     "quote": "不超过120字的证据"}]}]}}
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _sanitize_audit(candidate, attachment_ids):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("fields"), list):
            raise ValueError("字段排查响应不符合固定格式")
        rows = []
        for raw in candidate["fields"][:100]:
            if not isinstance(raw, dict) or raw.get("field") not in AUDIT_FIELDS or raw.get("status") not in AUDIT_STATUSES:
                continue
            evidence = []
            for item in (raw.get("evidence") or [])[:5]:
                if not isinstance(item, dict) or item.get("attachment_id") not in attachment_ids:
                    continue
                evidence.append({"attachment_id": item["attachment_id"],
                                 "location": str(item.get("location") or "")[:100],
                                 "quote": str(item.get("quote") or "")[:120]})
            rows.append({"field": raw["field"], "input_value": raw.get("input_value"),
                         "document_value": raw.get("document_value"), "status": raw["status"],
                         "evidence": evidence})
        if not rows:
            raise ValueError("字段排查未返回有效字段")
        return {"fields": rows, "summary": {status: sum(1 for row in rows if row["status"] == status)
                                               for status in AUDIT_STATUSES}}

    def _run_field_audit(self, audit_id, product, attachments):
        providers, _note = load_providers(self.env)
        if not self.cloud_enabled:
            providers = [provider for provider in providers if provider.name != "cloud"]
        providers = sorted(providers, key=lambda provider: 0 if provider.name == "ollama" else 1)
        prompt = self._audit_prompt(product, attachments)
        attachment_ids = {item["id"] for item in attachments}
        errors = []
        for provider in providers:
            try:
                raw = provider.chat(
                    "你是合同字段核对助手。只输出符合给定schema的JSON；不得推测缺失字段，不得提出修改操作。",
                    prompt)
                result = self._sanitize_audit(extract_json(raw), attachment_ids)
                model = getattr(provider, "model", None)
                stored = self.otc_store.finish_field_audit(audit_id, "completed", provider.name, model, result)
                self._finish_memory(audit_id, "completed", stored, None)
                return
            except Exception as exc:
                errors.append("%s:%s" % (provider.name, type(exc).__name__))
        error = "；".join(errors) if errors else "没有已配置的大模型提供方"
        stored = self.otc_store.finish_field_audit(audit_id, "failed", error=error)
        self._finish_memory(audit_id, "failed", stored, error)

    def _finish_memory(self, task_id, status, result, error):
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id].update(status=status, result=result, error=error,
                                           finished_at=time.time())

    def get(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                return dict(task)
        try:
            stored = self.otc_store.get_field_audit(task_id)
            stored["task_type"] = "otc_field_audit"
            return stored
        except KeyError:
            return None
