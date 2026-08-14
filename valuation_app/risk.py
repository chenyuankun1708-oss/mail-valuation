import json
import math
import os
import tempfile
from collections import OrderedDict, defaultdict
from datetime import date, datetime

from .benchmark import SOURCE_NAME, database_config

try:
    import cx_Oracle
except ImportError:  # pragma: no cover
    cx_Oracle = None


DEFAULT_CACHE = os.path.join("market_data", "risk_daily.json")
START_DATE = "20100101"
FUTURES = OrderedDict([
    ("IF", {"name": "沪深300股指期货", "index": "000300", "wind_index": "000300.SH"}),
    ("IC", {"name": "中证500股指期货", "index": "000905", "wind_index": "000905.SH"}),
    ("IM", {"name": "中证1000股指期货", "index": "000852", "wind_index": "000852.SH"}),
])
TENORS = ("当月", "下月", "当季", "下季")
INDEX_QUERY = """
SELECT trade_dt, s_dq_close FROM AIndexEODPrices
WHERE s_info_windcode=:code AND trade_dt>=:start_date ORDER BY trade_dt
"""
FUTURES_QUERY = """
SELECT p.trade_dt, p.s_info_windcode, p.s_dq_close, d.s_info_delistdate
FROM CIndexFuturesEODPrices p
JOIN CFuturesDescription d ON d.s_info_windcode=p.s_info_windcode
WHERE d.fs_info_sccode=:prefix AND p.trade_dt>=:start_date
ORDER BY p.trade_dt, d.s_info_delistdate, p.s_info_windcode
"""


def _day(value):
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return datetime.strptime(str(value).strip()[:8], "%Y%m%d").strftime("%Y-%m-%d")


def _safe_error(exc, context="查询"):
    return "Wind数据库%s失败（%s）" % (context, type(exc).__name__)


def _empty_cache():
    return {"source": SOURCE_NAME, "updated_at": None, "indices": {}, "futures": {}}


def load_cache(path=DEFAULT_CACHE):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return _empty_cache()


def _atomic_write(path, payload):
    absolute = os.path.abspath(path)
    folder = os.path.dirname(absolute)
    os.makedirs(folder, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder,
                                         prefix=".risk-daily-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, absolute)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def _normalize_index(rows):
    by_date = {}
    for row in rows:
        try:
            day, close = _day(row[0]), float(row[1])
        except (TypeError, ValueError, OverflowError):
            continue
        if close > 0:
            by_date[day] = {"date": day, "close": close}
    return [by_date[key] for key in sorted(by_date)]


def _normalize_futures(rows, spot_points):
    spot = {point["date"]: point["close"] for point in spot_points}
    by_day = defaultdict(dict)
    for row in rows:
        try:
            day, code, close, expiry = _day(row[0]), str(row[1]), float(row[2]), _day(row[3])
            days = (date.fromisoformat(expiry) - date.fromisoformat(day)).days
        except (TypeError, ValueError, OverflowError):
            continue
        if close <= 0 or days <= 0 or day not in spot:
            continue
        key = (code, expiry)
        by_day[day][key] = {"contract": code, "expiry": expiry, "close": close, "days": days}
    result = []
    for day in sorted(by_day):
        contracts = sorted(by_day[day].values(), key=lambda item: (item["expiry"], item["contract"]))
        point = {"date": day}
        for tenor, contract in zip(TENORS, contracts[:4]):
            annual_basis = ((spot[day] - contract["close"]) / spot[day]) * 365.0 / contract["days"] * 100.0
            if math.isfinite(annual_basis):
                point[tenor] = {"value": annual_basis, "contract": contract["contract"],
                                "expiry": contract["expiry"], "days": contract["days"]}
        if len(point) > 1:
            result.append(point)
    return result


def _query(connection, sql, **params):
    cursor = connection.cursor()
    try:
        cursor.execute(sql, **params)
        return cursor.fetchall()
    finally:
        cursor.close()


def update_cache(path=DEFAULT_CACHE, connect=None, now=None, env_path=".env"):
    previous = load_cache(path)
    attempted = (now or datetime.now()).replace(microsecond=0).isoformat()
    current = {"source": SOURCE_NAME, "updated_at": previous.get("updated_at"),
               "attempted_at": attempted, "indices": {}, "futures": {}}
    successes, failures, connection = [], [], None
    try:
        if connect is None:
            if cx_Oracle is None:
                raise ImportError("未安装cx_Oracle")
            config = database_config(env_path)
            connection = cx_Oracle.connect(config["user"], config["password"], config["dsn"])
        else:
            connection = connect()
        for prefix, details in FUTURES.items():
            try:
                index_points = _normalize_index(_query(connection, INDEX_QUERY,
                                                       code=details["wind_index"], start_date=START_DATE))
                if not index_points:
                    raise ValueError("指数无有效数据")
                futures_points = _normalize_futures(_query(connection, FUTURES_QUERY,
                                                            prefix=prefix, start_date=START_DATE), index_points)
                if not futures_points:
                    raise ValueError("期货无有效数据")
                current["indices"][details["index"]] = {"code": details["index"],
                    "name": details["name"].replace("股指期货", ""), "points": index_points,
                    "updated_at": attempted, "error": None}
                current["futures"][prefix] = {"code": prefix, "name": details["name"],
                    "index": details["index"], "points": futures_points,
                    "updated_at": attempted, "error": None}
                successes.append(prefix)
            except Exception as exc:
                message = _safe_error(exc)
                old_index = previous.get("indices", {}).get(details["index"], {})
                old_future = previous.get("futures", {}).get(prefix, {})
                current["indices"][details["index"]] = dict(old_index) if old_index.get("points") else {
                    "code": details["index"], "name": details["name"].replace("股指期货", ""), "points": []}
                current["futures"][prefix] = dict(old_future) if old_future.get("points") else {
                    "code": prefix, "name": details["name"], "index": details["index"], "points": []}
                current["indices"][details["index"]]["error"] = message
                current["futures"][prefix]["error"] = message
                failures.append({"code": prefix, "error": message,
                                 "used_cache": bool(old_future.get("points"))})
    except Exception as exc:
        message = _safe_error(exc, "连接")
        for prefix, details in FUTURES.items():
            old_index = previous.get("indices", {}).get(details["index"], {})
            old_future = previous.get("futures", {}).get(prefix, {})
            current["indices"][details["index"]] = dict(old_index)
            current["futures"][prefix] = dict(old_future)
            current["indices"][details["index"]].setdefault("error", message)
            current["futures"][prefix].setdefault("error", message)
            failures.append({"code": prefix, "error": message, "used_cache": bool(old_future.get("points"))})
    finally:
        if connection is not None:
            connection.close()
    if successes:
        current["updated_at"] = attempted
    _atomic_write(path, current)
    return {"cache": os.path.abspath(path), "successes": successes, "failures": failures,
            "updated_at": current.get("updated_at"), "attempted_at": attempted}


def page_payload(path=DEFAULT_CACHE):
    cache = load_cache(path)
    return {"source": cache.get("source", SOURCE_NAME), "updated_at": cache.get("updated_at"),
            "attempted_at": cache.get("attempted_at"),
            "available": any(x.get("points") for x in cache.get("futures", {}).values()),
            "indices": cache.get("indices", {}), "futures": cache.get("futures", {})}
