"""Persistent task metadata for restricted OTC backtests."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from .otc_backtest import INDICES, STRUCTURES, validate_request


RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class TaskConflictError(ValueError):
    pass


class OtcStore:
    def __init__(self, project_root):
        self.project_root = os.path.abspath(project_root)
        self.root = os.path.join(self.project_root, "otc_derivatives_data")
        self.db_path = os.path.join(self.root, "otc.sqlite3")
        self.runs_root = os.path.join(self.root, "runs")
        os.makedirs(self.runs_root, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        connection = self._connect()
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT,
                    result_path TEXT,
                    excel_path TEXT
                );
                CREATE INDEX IF NOT EXISTS backtest_runs_created ON backtest_runs(created_at DESC);
            """)
            connection.execute("UPDATE backtest_runs SET status='interrupted', "
                               "finished_at=?, error='网页服务重启，任务已中断' "
                               "WHERE status IN ('queued','running')", (time.time(),))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def valid_run_id(run_id):
        return bool(RUN_ID_RE.match(str(run_id or "")))

    def submit(self, request):
        canonical = validate_request(request)
        connection = self._connect()
        try:
            running = connection.execute(
                "SELECT id FROM backtest_runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone()
            if running:
                raise TaskConflictError("已有期权回测任务正在运行")
            run_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO backtest_runs(id,status,request_json,created_at) VALUES(?,?,?,?)",
                (run_id, "queued", json.dumps(canonical, ensure_ascii=False), time.time()))
            connection.commit()
        finally:
            connection.close()
        worker = threading.Thread(target=self._run_worker, args=(run_id,))
        worker.daemon = True
        worker.start()
        return self.get_run(run_id)

    def _run_worker(self, run_id):
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "valuation_app.otc_worker", "--run-id", run_id,
                 "--project-root", self.project_root],
                cwd=self.project_root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1800, check=False)
            if completed.returncode != 0:
                connection = self._connect()
                try:
                    connection.execute(
                        "UPDATE backtest_runs SET status='failed',finished_at=?,error=? "
                        "WHERE id=? AND status IN ('queued','running')",
                        (time.time(), "回测子进程异常退出", run_id))
                    connection.commit()
                finally:
                    connection.close()
        except subprocess.TimeoutExpired:
            connection = self._connect()
            try:
                connection.execute(
                    "UPDATE backtest_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                    (time.time(), "回测超过30分钟限制", run_id))
                connection.commit()
            finally:
                connection.close()

    def _row(self, row, include_request=True):
        if row is None:
            return None
        item = dict(row)
        if include_request:
            item["request"] = json.loads(item.pop("request_json"))
        else:
            item.pop("request_json", None)
        for key in ("created_at", "started_at", "finished_at"):
            if item.get(key) is not None:
                item[key] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(item[key]))
        return item

    def get_run(self, run_id):
        if not self.valid_run_id(run_id):
            raise KeyError("回测任务不存在")
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError("回测任务不存在")
        return self._row(row)

    def list_runs(self, page=1, page_size=20):
        if page < 1 or page_size < 20 or page_size > 200:
            raise ValueError("分页参数超出允许范围")
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size)).fetchall()
        finally:
            connection.close()
        return {"items": [self._row(row) for row in rows], "page": page,
                "page_size": page_size, "total": total}

    def result(self, run_id):
        run = self.get_run(run_id)
        if run["status"] != "completed" or not run.get("result_path"):
            return {"run": run}
        path = os.path.join(self.root, run["result_path"])
        with open(path, "r", encoding="utf-8") as handle:
            result = json.load(handle)
        result.pop("samples", None)
        return {"run": run, "result": result}

    def samples(self, run_id, page=1, page_size=50):
        if page < 1 or page_size < 20 or page_size > 200:
            raise ValueError("分页参数超出允许范围")
        run = self.get_run(run_id)
        if run["status"] != "completed" or not run.get("result_path"):
            raise ValueError("回测结果尚未生成")
        with open(os.path.join(self.root, run["result_path"]), "r", encoding="utf-8") as handle:
            rows = json.load(handle).get("samples", [])
        start = (page - 1) * page_size
        return {"items": rows[start:start + page_size], "page": page,
                "page_size": page_size, "total": len(rows)}

    def download_path(self, run_id, kind):
        run = self.get_run(run_id)
        key = "excel_path" if kind == "xlsx" else "result_path"
        relative = run.get(key)
        if run["status"] != "completed" or not relative:
            raise KeyError("回测下载文件不存在")
        path = os.path.realpath(os.path.join(self.root, relative))
        if os.path.commonpath((self.root, path)) != self.root or not os.path.isfile(path):
            raise KeyError("回测下载文件不存在")
        return path

    def module_payload(self):
        cache_path = os.path.join(self.project_root, "market_data", "index_daily.json")
        market = {"status": "missing", "updated_at": None, "indices": []}
        try:
            with open(cache_path, "r", encoding="utf-8") as handle:
                cache = json.load(handle)
            market["status"] = "current" if cache.get("source") == "Wind Oracle数据库" else "missing"
            market["updated_at"] = cache.get("updated_at")
            for code, name in INDICES.items():
                item = (cache.get("indices") or {}).get(code) or {}
                points = item.get("points") or []
                market["indices"].append({"code": code, "name": name, "available": bool(points),
                                          "first_date": points[0].get("date") if points else None,
                                          "last_date": points[-1].get("date") if points else None})
        except (OSError, ValueError):
            pass
        return {"schema_version": 1, "structures": [
                    {"code": code, "name": name} for code, name in STRUCTURES.items()],
                "market": market, "recent_runs": self.list_runs(1, 20)["items"],
                "ledger": {"total": 0, "active": 0}}
