import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime

from .labels import normalize_name
from .parser import _holding_column, _number, _rows, parse_valuation
from .underlying_assets import _product_name, is_configured_top_product


BENCHMARK_CODES = ("000852", "000905", "000300", "932000")
INVESTMENT_PREFIXES = ("1102", "1103", "1105", "1109", "3102", "3201")
EXCLUDED_NAMES = ("冲销", "成本", "估值增值", "汇总", "合计")
MAX_RESPONSE_HOLDINGS = 10000


def _cell(row, index):
    return row[index] if index is not None and index < len(row) else None


def _asset_type(code, name):
    prefix = code[:4]
    if prefix == "1102":
        return "股票"
    if prefix == "1103":
        return "债券/转债"
    if prefix in ("1105", "1109"):
        return "基金"
    if "期权" in name or re.search(r"(?:购|沽)\d*月", name):
        return "期权"
    if "期货" in name or prefix in ("3102", "3201"):
        return "期货/衍生品"
    return "其他投资"


def parse_leaf_investments(path):
    """Parse local valuation leaf investments without opening a security query surface."""
    rows = _rows(path)
    header_index = next((i for i, row in enumerate(rows)
                         if any("科目代码" in str(value or "") for value in row)
                         and any("科目名称" in str(value or "") for value in row)), None)
    if header_index is None:
        raise ValueError("缺少科目代码/科目名称表头")
    header = rows[header_index]
    subheader = rows[header_index + 1] if header_index + 1 < len(rows) else []
    columns = {
        "code": _holding_column(header, subheader, ("科目代码",)),
        "name": _holding_column(header, subheader, ("科目名称",)),
        "quantity": _holding_column(header, subheader, ("数量",)),
        "price": _holding_column(header, subheader, ("市价", "行情")),
        "market_value": _holding_column(header, subheader, ("市值",)),
        "cost": _holding_column(header, subheader, ("成本",)),
        "valuation_gain": _holding_column(header, subheader, ("估值增值",)),
    }
    required = ("code", "name", "quantity", "price", "market_value")
    if any(columns[key] is None for key in required):
        raise ValueError("缺少投资科目代码、名称、数量、价格或市值列")
    merged = {}
    for row in rows[header_index + 1:]:
        code = str(_cell(row, columns["code"]) or "").strip()
        name = str(_cell(row, columns["name"]) or "").replace(" ", "").strip()
        quantity = _number(_cell(row, columns["quantity"]))
        price = _number(_cell(row, columns["price"]))
        market_value = _number(_cell(row, columns["market_value"]))
        if (not code.startswith(INVESTMENT_PREFIXES) or len(code) <= 6 or not name
                or quantity in (None, 0) or price in (None, 0) or market_value is None
                or any(term in name for term in EXCLUDED_NAMES)):
            continue
        key = "%s|%s" % (code, name)
        item = merged.setdefault(key, {
            "key": key, "code": code, "name": name,
            "asset_type": _asset_type(code, name), "quantity": 0.0,
            "price": price, "market_value": 0.0, "cost": 0.0,
            "valuation_gain": 0.0, "has_cost": False, "has_gain": False,
        })
        item["quantity"] += quantity
        item["market_value"] += market_value
        cost = _number(_cell(row, columns["cost"]))
        gain = _number(_cell(row, columns["valuation_gain"]))
        if cost is not None:
            item["cost"] += cost
            item["has_cost"] = True
        if gain is not None:
            item["valuation_gain"] += gain
            item["has_gain"] = True
    result = []
    for item in merged.values():
        if item["quantity"]:
            item["price"] = item["market_value"] / item["quantity"] \
                if item["asset_type"] not in ("期货/衍生品", "期权") else item["price"]
        item["cost"] = item["cost"] if item.pop("has_cost") else None
        item["valuation_gain"] = item["valuation_gain"] if item.pop("has_gain") else None
        result.append(item)
    return sorted(result, key=lambda item: (item["asset_type"], item["name"], item["code"]))


def _connect(cache_path):
    folder = os.path.dirname(os.path.abspath(cache_path))
    if not os.path.isdir(folder):
        os.makedirs(folder)
    connection = sqlite3.connect(cache_path, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS files ("
                       "path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT, "
                       "product_id TEXT, product_name TEXT, valuation_date TEXT, nav REAL, "
                       "accumulated_nav REAL, net_assets REAL, shares REAL, holdings_json TEXT, "
                       "holding_count INTEGER, error TEXT, updated_at TEXT)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_bottom_product_date "
                       "ON files(product_id, valuation_date)")
    return connection


def _excel_files(folder):
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, name) for name in os.listdir(folder)
            if not name.startswith("~$") and name.lower().endswith((".xls", ".xlsx"))]


def _registry(root):
    four_level = os.path.join(root, "四级估值表")
    products = []
    if not os.path.isdir(four_level):
        return products
    for product_id in sorted(os.listdir(four_level)):
        folder = os.path.join(four_level, product_id)
        if not os.path.isdir(folder) or not re.match(r"^[A-Za-z0-9_-]+$", product_id):
            continue
        files = sorted(_excel_files(folder), reverse=True)
        product_name = _product_name(files[0], []) if files else ""
        if product_name and not is_configured_top_product(product_name):
            products.append({"product_id": product_id, "product": product_name,
                             "normalized": normalize_name(product_name), "folder": folder})
    return products


def _candidate_files(root, product):
    paths = list(_excel_files(product["folder"]))
    history_root = os.path.join(root, "历史估值表")
    if os.path.isdir(history_root):
        for name in os.listdir(history_root):
            folder = os.path.join(history_root, name)
            if not os.path.isdir(folder):
                continue
            short_name = name.split("--", 1)[-1]
            candidate = normalize_name(short_name)
            if candidate and (candidate == product["normalized"] or
                              candidate in product["normalized"] or
                              product["normalized"] in candidate):
                paths.extend(_excel_files(folder))
    for path in _excel_files(root):
        if normalize_name(_product_name(path, [])) == product["normalized"]:
            paths.append(path)
    return sorted(set(os.path.abspath(path) for path in paths))


def _parent_products(product_name, fof_holdings):
    target = normalize_name(product_name)
    parents = set()
    for fof, holding in fof_holdings:
        candidate = normalize_name(holding)
        if target and candidate and (target == candidate or
                                     (min(len(target), len(candidate)) >= 4 and
                                      (target in candidate or candidate in target))):
            parents.add(fof)
    return sorted(parents)


def _parent_map(products, fof_holdings):
    result = {product["product_id"]: set() for product in products}
    for fof, holding in fof_holdings:
        candidate = normalize_name(holding)
        for product in products:
            target = product["normalized"]
            if target and candidate and (
                    target == candidate or (min(len(target), len(candidate)) >= 4 and
                                            (target in candidate or candidate in target))):
                result[product["product_id"]].add(fof)
    return {key: sorted(value) for key, value in result.items()}


def sync_bottom_return_cache(root, fof_holdings=(), cache_path=None):
    cache_path = cache_path or os.path.join(os.path.dirname(root), "market_data",
                                            "bottom_returns.sqlite3")
    products = _registry(root)
    connection = _connect(cache_path)
    seen = set()
    now = datetime.now().replace(microsecond=0).isoformat()
    try:
        cached_files = {row[0]: (row[1], row[2]) for row in connection.execute(
            "SELECT path,size,mtime_ns FROM files").fetchall()}
        digest_cache = {row[0]: row[1:] for row in connection.execute(
            "SELECT sha256,valuation_date,nav,accumulated_nav,net_assets,shares,"
            "holdings_json,holding_count,error FROM files WHERE sha256 IS NOT NULL").fetchall()}
        for product in products:
            for path in _candidate_files(root, product):
                seen.add(path)
                stat = os.stat(path)
                cached = cached_files.get(path)
                if cached == (stat.st_size, stat.st_mtime_ns):
                    continue
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
                values = {"valuation_date": None, "nav": None, "accumulated_nav": None,
                          "net_assets": None, "shares": None, "holdings": [], "error": None}
                duplicate = digest_cache.get(digest)
                if duplicate:
                    (values["valuation_date"], values["nav"], values["accumulated_nav"],
                     values["net_assets"], values["shares"], holdings_json, _count,
                     values["error"]) = duplicate
                    values["holdings"] = json.loads(holdings_json or "[]")
                else:
                    try:
                        snapshot = parse_valuation(path, product=product["product"])
                        values.update({"valuation_date": snapshot.valuation_date,
                                       "nav": snapshot.nav,
                                       "accumulated_nav": snapshot.accumulated_nav,
                                       "net_assets": snapshot.net_assets,
                                       "shares": snapshot.shares,
                                       "holdings": parse_leaf_investments(path)})
                    except Exception as exc:
                        values["error"] = "%s: %s" % (type(exc).__name__, exc)
                    digest_cache[digest] = (
                        values["valuation_date"], values["nav"], values["accumulated_nav"],
                        values["net_assets"], values["shares"],
                        json.dumps(values["holdings"], ensure_ascii=False, separators=(",", ":")),
                        len(values["holdings"]), values["error"])
                connection.execute(
                    "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (path, stat.st_size, stat.st_mtime_ns, digest, product["product_id"],
                     product["product"], values["valuation_date"], values["nav"],
                     values["accumulated_nav"], values["net_assets"], values["shares"],
                     json.dumps(values["holdings"], ensure_ascii=False, separators=(",", ":")),
                     len(values["holdings"]), values["error"], now))
        # SQLite builds commonly limit a statement to 999 bound parameters, while the
        # local archive can contain thousands of files.  Remove stale rows one at a time.
        for cached_path, in connection.execute("SELECT path FROM files").fetchall():
            if cached_path not in seen:
                connection.execute("DELETE FROM files WHERE path=?", (cached_path,))
        connection.commit()
        manifest = []
        parents_by_product = _parent_map(products, fof_holdings)
        for product in products:
            rows = connection.execute(
                "SELECT valuation_date,holding_count,error FROM files WHERE product_id=?",
                (product["product_id"],)).fetchall()
            dates = sorted({row[0] for row in rows if row[0]})
            parents = parents_by_product[product["product_id"]]
            if not parents:
                continue
            manifest.append({
                "product_id": product["product_id"], "product": product["product"],
                "fof_products": parents, "available_dates": dates,
                "start_date": dates[0] if dates else None,
                "end_date": dates[-1] if dates else None,
                "snapshot_count": len(dates),
                "holding_snapshot_count": sum(1 for row in rows if row[1] > 0),
                "status": "ok" if len(dates) >= 2 else "数据不足",
                "error_count": sum(1 for row in rows if row[2]),
            })
        manifest.sort(key=lambda item: item["product"])
        return {"schema_version": 1, "updated_at": now,
                "products": manifest, "product_count": len(manifest),
                "benchmark_codes": list(BENCHMARK_CODES),
                "source": "本地底层资产估值表增量缓存"}
    finally:
        connection.close()


def _snapshots(cache_path, product_id):
    connection = sqlite3.connect(cache_path)
    try:
        rows = connection.execute(
            "SELECT valuation_date,nav,accumulated_nav,net_assets,shares,holdings_json,"
            "holding_count,sha256,error FROM files WHERE product_id=? AND valuation_date IS NOT NULL",
            (product_id,)).fetchall()
        errors = [row[0] for row in connection.execute(
            "SELECT error FROM files WHERE product_id=? AND error IS NOT NULL",
            (product_id,)).fetchall()]
    finally:
        connection.close()
    by_date = {}
    seen_hashes = set()
    for row in rows:
        date, nav, accumulated_nav, net_assets, shares, holdings_json, count, digest, error = row
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        item = {"valuation_date": date, "nav": nav, "accumulated_nav": accumulated_nav,
                "net_assets": net_assets, "shares": shares,
                "holdings": json.loads(holdings_json or "[]")}
        old = by_date.get(date)
        if old is None or len(item["holdings"]) > len(old["holdings"]):
            by_date[date] = item
    return [by_date[key] for key in sorted(by_date)], errors


def _days(start, end):
    return (datetime.strptime(end, "%Y-%m-%d") -
            datetime.strptime(start, "%Y-%m-%d")).days


def _risk_metrics(points):
    clean = [point for point in points if point.get("accumulated_nav") and
             point["accumulated_nav"] > 0]
    empty = {"nav_annualized": None, "annual_volatility": None, "sharpe": None,
             "max_drawdown": None, "drawdown_peak": None, "drawdown_trough": None,
             "valid_points": len(clean), "total_days": 0}
    if len(clean) < 2:
        return empty
    total_days = _days(clean[0]["valuation_date"], clean[-1]["valuation_date"])
    if total_days <= 0:
        return empty
    annualized = (math.pow(clean[-1]["accumulated_nav"] / clean[0]["accumulated_nav"],
                          365.0 / total_days) - 1) * 100
    peak = clean[0]
    drawdown = 0.0
    peak_date = trough_date = peak["valuation_date"]
    intervals = []
    for index, point in enumerate(clean):
        if point["accumulated_nav"] > peak["accumulated_nav"]:
            peak = point
        current = point["accumulated_nav"] / peak["accumulated_nav"] - 1
        if current < drawdown:
            drawdown, peak_date, trough_date = current, peak["valuation_date"], point["valuation_date"]
        if index:
            days = _days(clean[index - 1]["valuation_date"], point["valuation_date"])
            if days > 0:
                intervals.append((days, math.log(point["accumulated_nav"] /
                                                  clean[index - 1]["accumulated_nav"]) / days))
    volatility = sharpe = None
    weight = sum(item[0] for item in intervals)
    if len(intervals) >= 2 and weight > 1:
        mean = sum(days * rate for days, rate in intervals) / weight
        variance = sum(days * math.pow(rate - mean, 2) for days, rate in intervals) / (weight - 1)
        volatility_decimal = math.sqrt(max(0, variance) * 365)
        volatility = volatility_decimal * 100
        if volatility_decimal > 1e-12:
            sharpe = (mean * 365 - math.log(1.02)) / volatility_decimal
    return {"nav_annualized": annualized, "annual_volatility": volatility, "sharpe": sharpe,
            "max_drawdown": drawdown * 100, "drawdown_peak": peak_date,
            "drawdown_trough": trough_date, "valid_points": len(clean),
            "total_days": total_days}


def _benchmark_metrics(points, market_points):
    market = sorted((item for item in market_points if item.get("date") and item.get("close")),
                    key=lambda item: item["date"])
    intervals = []
    for index in range(1, len(points)):
        left, right = points[index - 1], points[index]
        if not left.get("accumulated_nav") or not right.get("accumulated_nav"):
            continue
        a = [item for item in market if item["date"] <= left["valuation_date"]]
        b = [item for item in market if item["date"] <= right["valuation_date"]]
        if not a or not b or b[-1]["date"] <= a[-1]["date"]:
            continue
        days = _days(left["valuation_date"], right["valuation_date"])
        if days > 0 and a[-1]["close"] > 0 and b[-1]["close"] > 0:
            intervals.append((days, math.log(b[-1]["close"] / a[-1]["close"]) / days,
                              math.log(right["accumulated_nav"] / left["accumulated_nav"]) / days))
    result = {"status": "样本不足", "alpha": None, "beta": None,
              "intervals": len(intervals)}
    if len(intervals) < 10 or _days(points[0]["valuation_date"], points[-1]["valuation_date"]) < 30:
        return result
    weight = sum(item[0] for item in intervals)
    x_mean = sum(item[0] * item[1] for item in intervals) / weight
    y_mean = sum(item[0] * item[2] for item in intervals) / weight
    variance = sum(item[0] * math.pow(item[1] - x_mean, 2) for item in intervals) / weight
    if variance <= 1e-18:
        return result
    covariance = sum(item[0] * (item[1] - x_mean) * (item[2] - y_mean)
                     for item in intervals) / weight
    beta = covariance / variance
    risk_free = math.log(1.02) / 365
    alpha_daily = y_mean - (risk_free + beta * (x_mean - risk_free))
    result.update({"status": "ok", "alpha": (math.exp(alpha_daily * 365) - 1) * 100,
                   "beta": beta})
    return result


def _holding_summaries(points, opening_assets):
    summaries = {}
    price_series = {}
    for point in points:
        for item in point["holdings"]:
            price_series.setdefault(item["key"], []).append(
                (point["valuation_date"], item.get("price")))
    for index in range(1, len(points)):
        previous = {item["key"]: item for item in points[index - 1]["holdings"]}
        current = {item["key"]: item for item in points[index]["holdings"]}
        for key in set(previous) | set(current):
            left, right = previous.get(key), current.get(key)
            source = right or left
            summary = summaries.setdefault(key, {
                "key": key, "code": source["code"], "name": source["name"],
                "asset_type": source["asset_type"], "estimated_profit": 0.0,
                "has_estimate": False, "uncertain": False, "statuses": set(),
            })
            if left and right and left.get("price") and right.get("price"):
                left_units = left.get("market_value", 0) / left["price"]
                right_units = right.get("market_value", 0) / right["price"]
                if left_units and right_units and left_units * right_units > 0:
                    common = math.copysign(min(abs(left_units), abs(right_units)), left_units)
                    summary["estimated_profit"] += common * (right["price"] - left["price"])
                    summary["has_estimate"] = True
                    summary["statuses"].add("存续持仓价格影响")
                else:
                    summary["uncertain"] = True
                    summary["statuses"].add("方向变化，未归属")
            elif right:
                gain = right.get("valuation_gain")
                if gain is None and right.get("cost") is not None:
                    gain = right["market_value"] - right["cost"]
                if gain is not None:
                    summary["estimated_profit"] += gain
                    summary["has_estimate"] = True
                    summary["statuses"].add("区间新增，按估值增值")
                else:
                    summary["uncertain"] = True
                    summary["statuses"].add("区间新增，收益未归属")
            else:
                summary["uncertain"] = True
                summary["statuses"].add("区间退出，无成交价")
    first_map = {item["key"]: item for item in points[0]["holdings"]}
    last_map = {item["key"]: item for item in points[-1]["holdings"]}
    result = []
    for key, summary in summaries.items():
        first, last = first_map.get(key), last_map.get(key)
        series = [(date, price) for date, price in price_series.get(key, []) if price]
        ending = last or {}
        latest = series[-1] if series else (None, None)
        previous = series[-2] if len(series) > 1 else (None, None)
        week = next((item for item in reversed(series)
                     if latest[0] and _days(item[0], latest[0]) >= 7), (None, None))
        first_price = series[0][1] if series else None
        summary.update({
            "start_quantity": first.get("quantity") if first else None,
            "start_price": first.get("price") if first else None,
            "end_quantity": last.get("quantity") if last else None,
            "end_price": last.get("price") if last else None,
            "end_market_value": ending.get("market_value", 0),
            "daily_change": ((latest[1] / previous[1] - 1) * 100
                             if latest[1] and previous[1] else None),
            "weekly_change": ((latest[1] / week[1] - 1) * 100
                              if latest[1] and week[1] else None),
            "period_change": ((latest[1] / first_price - 1) * 100
                              if latest[1] and first_price else None),
            "estimated_profit": summary["estimated_profit"] if summary["has_estimate"] else None,
            "contribution": (summary["estimated_profit"] / opening_assets * 100
                             if summary["has_estimate"] and opening_assets else None),
            "status": "；".join(sorted(summary.pop("statuses"))),
        })
        summary.pop("has_estimate")
        result.append(summary)
    result.sort(key=lambda item: abs(item.get("estimated_profit") or 0), reverse=True)
    return result


def analyze_bottom_return(cache_path, manifest, product_id, start, end,
                          benchmark_code, benchmark_points):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", start or "") or not re.match(
            r"^\d{4}-\d{2}-\d{2}$", end or "") or start >= end:
        raise ValueError("缺少有效起止日")
    if benchmark_code not in BENCHMARK_CODES:
        raise ValueError("基准指数不在固定白名单")
    product = next((item for item in manifest.get("products", [])
                    if item.get("product_id") == product_id), None)
    if product is None:
        raise KeyError("底层产品不在固定清单")
    snapshots, errors = _snapshots(cache_path, product_id)
    first_candidates = [item for item in snapshots if item["valuation_date"] <= start]
    last_candidates = [item for item in snapshots if item["valuation_date"] <= end]
    if not first_candidates or not last_candidates or first_candidates[-1]["valuation_date"] >= last_candidates[-1]["valuation_date"]:
        return dict(product=product, status="数据不足", requested_start=start, requested_end=end,
                    actual_start=first_candidates[-1]["valuation_date"] if first_candidates else None,
                    actual_end=last_candidates[-1]["valuation_date"] if last_candidates else None,
                    points=[], holdings=[], errors=errors)
    first, last = first_candidates[-1], last_candidates[-1]
    points = [item for item in snapshots
              if first["valuation_date"] <= item["valuation_date"] <= last["valuation_date"]]
    risk = _risk_metrics(points)
    benchmark = _benchmark_metrics(points, benchmark_points)
    holdings = _holding_summaries(points, first.get("net_assets"))
    market_start = [item for item in benchmark_points
                    if item.get("date", "") <= first["valuation_date"]]
    market_end = [item for item in benchmark_points
                  if item.get("date", "") <= last["valuation_date"]]
    market = []
    if market_start and market_end:
        start_date = market_start[-1]["date"]
        end_date = market_end[-1]["date"]
        market = [item for item in benchmark_points
                  if start_date <= item.get("date", "") <= end_date]
    return {"status": "ok", "product": product, "requested_start": start,
            "requested_end": end, "actual_start": first["valuation_date"],
            "actual_end": last["valuation_date"], "first": dict(first, holdings=None),
            "last": dict(last, holdings=None), "points": [dict(item, holdings=None) for item in points],
            "period_return": (last["accumulated_nav"] / first["accumulated_nav"] - 1) * 100,
            "risk": risk, "benchmark_code": benchmark_code, "benchmark": benchmark,
            "benchmark_points": market, "holdings": holdings[:MAX_RESPONSE_HOLDINGS],
            "holding_count": len(holdings),
            "holdings_truncated": len(holdings) > MAX_RESPONSE_HOLDINGS,
            "estimated_holding_profit": sum(item.get("estimated_profit") or 0 for item in holdings),
            "uncertain_holding_count": sum(1 for item in holdings if item.get("uncertain")),
            "amount_profit": None, "xirr": None,
            "cashflow_status": "无法计算（无底层资金台账）", "errors": errors,
            "disclaimer": "持仓收益仅为本地估值表可归属估算，不是交易流水或正式业绩归因。"}
