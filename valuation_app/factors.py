import json
import math
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime

from .benchmark import database_config
from .parser import _find_date, _holding_column, _number, _rows
from .underlying_assets import _product_name


CACHE_PATH = os.path.join("market_data", "factor_exposure.json")
FACTORS = {
    "规模": "SIZE_LnMarketValue",
    "价值": "VALUE_BPS_LR_to_P",
    "动量": "MOMENTUM_StockZF_6M",
    "波动": "VOLATILITY_DASTD",
    "流动性": "LIQUIDITY_TurnOver_AVG_1M",
    "成长": "GROWTH_NetProfit_SQ_YOY",
    "盈利": "PROFIT_ROE_TTM",
    "杠杆": "LEVERAGE_Debt2AssetRatio_LR",
    "股息": "DIVIDEND_DividendYieldRatio_TTM",
}


def _cell(row, index):
    return row[index] if index is not None and index < len(row) else None


def _wind_code(account_code):
    match = re.search(r"(\d{6})$", str(account_code or ""))
    if not match:
        return None
    code = match.group(1)
    if code.startswith(("6", "9")):
        return code + ".SH"
    if code.startswith(("0", "3")):
        return code + ".SZ"
    if code.startswith(("4", "8")):
        return code + ".BJ"
    return None


def parse_stock_positions(path):
    rows = _rows(path)
    header_index = next((i for i, row in enumerate(rows)
                         if any("科目代码" in str(v or "") for v in row)
                         and any("科目名称" in str(v or "") for v in row)), None)
    if header_index is None:
        raise ValueError("缺少科目代码/科目名称表头")
    header = rows[header_index]
    subheader = rows[header_index + 1] if header_index + 1 < len(rows) else []
    code_col = _holding_column(header, subheader, ("科目代码",))
    name_col = _holding_column(header, subheader, ("科目名称",))
    quantity_col = _holding_column(header, subheader, ("数量",))
    price_col = _holding_column(header, subheader, ("市价", "行情"))
    market_col = _holding_column(header, subheader, ("市值",))
    if None in (code_col, name_col, market_col):
        raise ValueError("缺少证券持仓字段")
    positions = defaultdict(lambda: {"market_value": 0.0, "quantity": 0.0})
    for row in rows[header_index + 1:]:
        account_code = str(_cell(row, code_col) or "").strip()
        wind_code = _wind_code(account_code)
        name = str(_cell(row, name_col) or "").replace(" ", "").strip()
        market = _number(_cell(row, market_col))
        # A-share leaf accounting codes end in the six-digit security code.
        # Aggregate rows and valuation/cost rows have no valid suffix and drop out.
        if (not account_code.startswith("1102") or not wind_code or market is None
                or market == 0 or "冲销" in name or "成本" in name or "估值增值" in name):
            continue
        item = positions[wind_code]
        item.update({"code": wind_code, "name": name,
                     "price": _number(_cell(row, price_col))})
        item["market_value"] += market
        item["quantity"] += _number(_cell(row, quantity_col)) or 0.0
    product = _product_name(path, rows)
    for row in rows[:5]:
        text = " ".join(str(value or "") for value in row[:5])
        if "___" in text:
            pieces = [value.strip() for value in text.split("___") if value.strip()]
            if len(pieces) >= 2:
                product = pieces[1]
                break
    return {"product": product,
            "valuation_date": _find_date(rows, os.path.basename(path)),
            "source_file": os.path.basename(path),
            "positions": list(positions.values())}


def latest_files(root):
    selected = {}
    for base, _, files in os.walk(root):
        for filename in files:
            if filename.startswith("~$") or not filename.lower().endswith((".xls", ".xlsx")):
                continue
            match = re.search(r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)", filename)
            date = "-".join(match.groups()) if match else ""
            key = os.path.abspath(base).lower()
            if not date:
                continue
            old = selected.get(key)
            if old is None or date > old[0]:
                selected[key] = (date, os.path.join(base, filename))
    return [value[1] for value in selected.values()]


def _percentile(values, proportion):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * proportion
    low, high = int(math.floor(position)), int(math.ceil(position))
    if low == high:
        return ordered[low]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def standardize(rows):
    result = {}
    grouped = defaultdict(dict)
    for code, factor, value in rows:
        if value is not None and math.isfinite(float(value)):
            grouped[factor][code] = float(value)
    for factor, values_by_code in grouped.items():
        values = list(values_by_code.values())
        low, high = _percentile(values, .01), _percentile(values, .99)
        clipped = {code: min(high, max(low, value)) for code, value in values_by_code.items()}
        mean = sum(clipped.values()) / len(clipped)
        variance = sum((value - mean) ** 2 for value in clipped.values()) / len(clipped)
        deviation = math.sqrt(variance)
        for code, value in clipped.items():
            result.setdefault(code, {})[factor] = (value - mean) / deviation if deviation else 0.0
    return result


def _atomic_json(path, payload):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder,
                                     prefix=".factor-", suffix=".tmp", delete=False) as handle:
        temporary = handle.name
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, path)


def update_cache(root="底层资产", cache_path=CACHE_PATH,
                 connect=None):
    parsed, errors = [], []
    for path in latest_files(root):
        try:
            item = parse_stock_positions(path)
            if (item["positions"] and "FOF" not in item["product"].upper()
                    and "资产管理计划" not in item["product"]):
                parsed.append(item)
        except Exception as exc:
            errors.append({"file": os.path.basename(path),
                           "error": "%s: %s" % (type(exc).__name__, exc)})
    # The same product can occur in the historical archive, the current mail
    # folder and a manually supplied folder. Keep its newest real snapshot.
    newest = {}
    for item in parsed:
        old = newest.get(item["product"])
        if old is None or item["valuation_date"] > old["valuation_date"]:
            newest[item["product"]] = item
    parsed = list(newest.values())
    if not parsed:
        raise RuntimeError("未解析到证券级股票持仓")
    valuation_date = max(item["valuation_date"] for item in parsed if item["valuation_date"])
    connection = None
    try:
        if connect is None:
            import cx_Oracle
            config = database_config()
            connection = cx_Oracle.connect(config["user"], config["password"], config["dsn"])
        else:
            connection = connect()
        cursor = connection.cursor()
        cursor.execute("SELECT MAX(trade_dt) FROM WIND.QUANTFACTORWIND WHERE trade_dt<=:dt",
                       dt=valuation_date.replace("-", ""))
        factor_date = cursor.fetchone()[0]
        names = list(FACTORS.values())
        binds = {"f%d" % i: value for i, value in enumerate(names)}
        placeholders = ",".join(":" + key for key in binds)
        cursor.execute("SELECT s_info_windcode,factor_name,factor_value "
                       "FROM WIND.QUANTFACTORWIND WHERE trade_dt=:dt AND factor_name IN (%s)" % placeholders,
                       dict(binds, dt=factor_date))
        exposures = standardize(cursor.fetchall())
        reverse = {value: key for key, value in FACTORS.items()}
        codes = sorted({position["code"] for item in parsed for position in item["positions"]})
        industries = {}
        for start in range(0, len(codes), 900):
            chunk = codes[start:start + 900]
            params = {"c%d" % i: value for i, value in enumerate(chunk)}
            params["dt"] = factor_date
            marks = ",".join(":" + key for key in params if key != "dt")
            cursor.execute("SELECT c.s_info_windcode,d.industriesname FROM WIND.ASHAREINDUSTRIESCLASS c "
                           "LEFT JOIN WIND.ASHAREINDUSTRIESCODE d ON "
                           "(d.industriescode=c.wind_ind_code OR d.industriescode=c.wind_ind_code||'000000') "
                           "WHERE c.s_info_windcode IN (%s) AND c.entry_dt<=:dt "
                           "AND (c.remove_dt IS NULL OR c.remove_dt>:dt)" % marks, params)
            for code, industry in cursor.fetchall():
                if industry:
                    industries[code] = industry
        products = []
        for item in parsed:
            total = sum(max(0, position["market_value"]) for position in item["positions"])
            portfolio = {name: 0.0 for name in FACTORS}
            covered = 0.0
            for position in item["positions"]:
                raw = exposures.get(position["code"], {})
                position["industry"] = industries.get(position["code"], "未匹配行业")
                position["factors"] = {reverse[key]: value for key, value in raw.items() if key in reverse}
                position["weight"] = position["market_value"] / total if total else 0.0
                if position["factors"]:
                    covered += max(0, position["market_value"])
                for name, value in position["factors"].items():
                    portfolio[name] += position["weight"] * value
            item["stock_market_value"] = total
            item["covered_market_value"] = covered
            item["coverage"] = covered / total if total else 0.0
            item["factor_exposure"] = portfolio
            products.append(item)
        snapshots = []
        try:
            with open(cache_path, encoding="utf-8") as handle:
                previous = json.load(handle)
            snapshots = previous.get("snapshots", [])
            if not snapshots and previous.get("products") and previous.get("valuation_date"):
                snapshots = [{"valuation_date": previous["valuation_date"],
                              "factor_date": previous.get("factor_date"),
                              "products": previous["products"]}]
        except (OSError, ValueError):
            pass
        current_snapshot = {"valuation_date": valuation_date,
                            "factor_date": "%s-%s-%s" % (factor_date[:4], factor_date[4:6], factor_date[6:8]),
                            "products": products}
        snapshots = [item for item in snapshots if item.get("valuation_date") != valuation_date]
        snapshots.append(current_snapshot)
        snapshots.sort(key=lambda item: item.get("valuation_date", ""))
        payload = {"available": True, "model": "自研多因子暴露分析",
                   "disclaimer": "不是MSCI Barra正式风险模型",
                   "updated_at": datetime.now().isoformat(timespec="seconds"),
                   "valuation_date": valuation_date,
                   "factor_date": "%s-%s-%s" % (factor_date[:4], factor_date[4:6], factor_date[6:8]),
                   "factor_definitions": FACTORS, "products": products,
                   "available_dates": [item["valuation_date"] for item in snapshots],
                   "snapshots": snapshots, "errors": errors}
        _atomic_json(cache_path, payload)
        return payload
    except Exception as exc:
        try:
            with open(cache_path, encoding="utf-8") as handle:
                old = json.load(handle)
            old["stale"] = True
            old["update_error"] = type(exc).__name__ + ": 因子数据库更新失败，沿用旧缓存"
            return old
        except (OSError, ValueError):
            raise RuntimeError("%s: Wind因子更新失败且无旧缓存" % type(exc).__name__)
    finally:
        if connection:
            connection.close()


def page_payload(path=CACHE_PATH):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"available": False, "products": [], "errors": [],
                "disclaimer": "不是MSCI Barra正式风险模型"}
