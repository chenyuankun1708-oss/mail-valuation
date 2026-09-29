"""Restricted subprocess entry point for one parameterized OTC pricing run."""

import argparse
import json
import os
import sqlite3
import tempfile
import time

from .otc_pricing import run_parametric_pricing


def _atomic_json(path, payload):
    temporary = None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path),
                                         prefix=".pricing-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def execute(project_root, run_id):
    root = os.path.join(os.path.abspath(project_root), "otc_derivatives_data")
    database = os.path.join(root, "otc.sqlite3")
    connection = sqlite3.connect(database, timeout=30)
    try:
        row = connection.execute(
            "SELECT request_json,status FROM pricing_runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row[1] not in ("queued", "running"):
            return 2
        connection.execute("UPDATE pricing_runs SET status='running',started_at=? WHERE id=?",
                           (time.time(), run_id))
        connection.commit()
        request = json.loads(row[0])
    finally:
        connection.close()
    result_path = os.path.join(root, "pricing_runs", run_id, "result.json")
    try:
        result = run_parametric_pricing(request)
        _atomic_json(result_path, result)
        relative = os.path.relpath(result_path, root)
        connection = sqlite3.connect(database, timeout=30)
        try:
            connection.execute(
                "UPDATE pricing_runs SET status='completed',finished_at=?,result_path=?,error=NULL WHERE id=?",
                (time.time(), relative, run_id))
            connection.commit()
        finally:
            connection.close()
        return 0
    except Exception as exc:
        connection = sqlite3.connect(database, timeout=30)
        try:
            connection.execute(
                "UPDATE pricing_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                (time.time(), "%s: %s" % (type(exc).__name__, exc), run_id))
            connection.commit()
        finally:
            connection.close()
        return 1


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--project-root", required=True)
    args = parser.parse_args()
    return execute(args.project_root, args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
