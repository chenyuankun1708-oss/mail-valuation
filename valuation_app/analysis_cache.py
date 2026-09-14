"""Versioned, atomically replaced read-only analysis database.

The database is a normalized copy of an already validated build payload.  It
does not replace source valuation sheets; it gives the web/API layer a stable
Python-owned query surface without changing any business matching rule.
"""
from __future__ import unicode_literals

import hashlib
import json
import os
import sqlite3
import tempfile


SCHEMA_VERSION = 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _known_hashes(path):
    if not os.path.exists(path):
        return {}
    try:
        connection = sqlite3.connect(path)
        try:
            rows = connection.execute(
                "SELECT path,size,mtime_ns,sha256 FROM source_files").fetchall()
        finally:
            connection.close()
        return {(row[0], row[1], row[2]): row[3] for row in rows}
    except (sqlite3.Error, OSError):
        return {}


def source_record(path, known=None):
    absolute = os.path.abspath(path)
    stat = os.stat(absolute)
    key = (absolute, stat.st_size, getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)))
    digest = (known or {}).get(key) or _sha256(absolute)
    return {"path": absolute, "size": key[1], "mtime_ns": key[2], "sha256": digest}


def build_analysis_database(path, products, generated_at, source_records=()):
    """Write *path* through a checked temporary SQLite database."""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".analysis-", suffix=".sqlite3", dir=parent)
    os.close(descriptor)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript("""
                PRAGMA journal_mode=DELETE;
                PRAGMA synchronous=FULL;
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE products(product_id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,
                                      ordinal INTEGER NOT NULL);
                CREATE TABLE valuation_snapshots(
                    product_id TEXT NOT NULL,valuation_date TEXT NOT NULL,nav REAL,
                    accumulated_nav REAL,net_assets REAL,shares REAL,source_file TEXT,
                    source_sha256 TEXT,payload_json TEXT NOT NULL,
                    PRIMARY KEY(product_id,valuation_date,source_file));
                CREATE INDEX valuation_product_date
                    ON valuation_snapshots(product_id,valuation_date);
                CREATE TABLE holdings(
                    product_id TEXT NOT NULL,valuation_date TEXT NOT NULL,position INTEGER NOT NULL,
                    code TEXT,name TEXT,quantity REAL,price REAL,market_value REAL,
                    cost REAL,valuation_gain REAL,payload_json TEXT NOT NULL,
                    PRIMARY KEY(product_id,valuation_date,position));
                CREATE TABLE cash_flows(
                    product_id TEXT NOT NULL,position INTEGER NOT NULL,flow_date TEXT,
                    amount REAL,flow_type TEXT,payload_json TEXT NOT NULL,
                    PRIMARY KEY(product_id,position));
                CREATE TABLE source_files(
                    path TEXT PRIMARY KEY,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE);
            """)
            connection.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", [
                ("schema_version", str(SCHEMA_VERSION)), ("generated_at", generated_at)])
            for ordinal, product in enumerate(products):
                product_id = product["product_id"]
                connection.execute("INSERT INTO products VALUES(?,?,?)",
                                   (product_id, product["name"], ordinal))
                for point in product.get("points", []):
                    connection.execute(
                        "INSERT INTO valuation_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
                        (product_id, point.get("valuation_date"), point.get("nav"),
                         point.get("accumulated_nav"), point.get("net_assets"), point.get("shares"),
                         point.get("source_file"), point.get("source_sha256"), _json(point)))
                    for position, holding in enumerate(point.get("holdings", [])):
                        connection.execute(
                            "INSERT INTO holdings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (product_id, point.get("valuation_date"), position,
                             holding.get("code"), holding.get("name"), holding.get("quantity"),
                             holding.get("price"), holding.get("market_value"), holding.get("cost"),
                             holding.get("valuation_gain"), _json(holding)))
                for position, flow in enumerate(product.get("flows", [])):
                    connection.execute("INSERT INTO cash_flows VALUES(?,?,?,?,?,?)",
                                       (product_id, position, flow.get("flow_date"), flow.get("amount"),
                                        flow.get("flow_type") or flow.get("type"), _json(flow)))
            connection.executemany("INSERT INTO source_files VALUES(?,?,?,?)", [
                (item["path"], item["size"], item["mtime_ns"], item["sha256"])
                for item in source_records])
            connection.commit()
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("analysis database integrity check failed: %s" % result)
        finally:
            connection.close()
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return target


def load_products(path):
    """Load analysis products in configured order; callers never write here."""
    uri = "file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/")
    connection = sqlite3.connect(uri, uri=True)
    try:
        products = connection.execute(
            "SELECT product_id,name FROM products ORDER BY ordinal").fetchall()
        result = []
        for product_id, name in products:
            points = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM valuation_snapshots WHERE product_id=? "
                "ORDER BY valuation_date,source_file", (product_id,))]
            flows = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM cash_flows WHERE product_id=? ORDER BY position",
                (product_id,))]
            result.append({"product_id": product_id, "name": name,
                           "points": points, "flows": flows})
    finally:
        connection.close()
    return result
