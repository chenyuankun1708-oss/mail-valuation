import json
import os
import tempfile
from collections import OrderedDict
from datetime import date, datetime, timedelta

try:
    import cx_Oracle
except ImportError:  # pragma: no cover - exercised through a mocked missing driver
    cx_Oracle = None


INDICES = OrderedDict([
    ("000852", {"name": "中证1000", "wind_code": "000852.SH", "table": "AIndexEODPrices"}),
    ("000905", {"name": "中证500", "wind_code": "000905.SH", "table": "AIndexEODPrices"}),
    ("000300", {"name": "沪深300", "wind_code": "000300.SH", "table": "AIndexEODPrices"}),
    ("932000", {"name": "中证2000", "wind_code": "932000.CSI", "table": "AIndexEODPrices"}),
    ("000985", {"name": "中证全指", "wind_code": "000985.CSI", "table": "AIndexEODPrices"}),
    ("000510", {"name": "中证A500", "wind_code": "000510.CSI", "table": "AIndexEODPrices"}),
    ("000922", {"name": "中证红利", "wind_code": "000922.CSI", "table": "AIndexEODPrices"}),
    ("399303", {"name": "国证2000", "wind_code": "399303.SZ", "table": "AIndexEODPrices"}),
    ("NH0100", {"name": "南华商品指数", "wind_code": "NH0100.NHF", "table": "THIRDPARTYINDEXEOD"}),
])
SOURCE_NAME = "Wind Oracle数据库"
DEFAULT_CACHE = os.path.join("market_data", "index_daily.json")
QUERY = """
    SELECT trade_dt, s_dq_close
    FROM {table}
    WHERE s_info_windcode = :code
    ORDER BY trade_dt
"""


def _now_text(now=None):
    current = now or datetime.now()
    return current.replace(microsecond=0).isoformat()


def _load_env(path):
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("'\"")
    return values


def database_config(env_path=".env"):
    env = _load_env(env_path)
    config = {}
    for target, key in (("user", "WIND_DB_USER"),
                        ("password", "WIND_DB_PASSWORD"),
                        ("dsn", "WIND_DB_DSN")):
        config[target] = os.environ.get(key, env.get(key, ""))
    missing = [key for target, key in (("user", "WIND_DB_USER"),
                                       ("password", "WIND_DB_PASSWORD"),
                                       ("dsn", "WIND_DB_DSN"))
               if not config[target]]
    if missing:
        raise RuntimeError("缺少Wind数据库配置：%s" % ", ".join(missing))
    return config


def _empty_cache():
    return {"source": SOURCE_NAME, "updated_at": None, "indices": {}}


def load_cache(path=DEFAULT_CACHE):
    if not os.path.exists(path):
        return _empty_cache()
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or not isinstance(payload.get("indices"), dict):
            raise ValueError("缓存结构无效")
        return payload
    except (OSError, ValueError, TypeError):
        return _empty_cache()


def _normalize_rows(rows, code, name):
    by_date = {}
    for row in rows:
        if not row or len(row) < 2:
            continue
        raw_day, raw_close = row[0], row[1]
        try:
            if isinstance(raw_day, (date, datetime)):
                day = raw_day.strftime("%Y-%m-%d")
            else:
                text = str(raw_day).strip()
                parsed = datetime.strptime(text, "%Y%m%d")
                day = parsed.strftime("%Y-%m-%d")
            close = float(raw_close)
        except (TypeError, ValueError, OverflowError):
            continue
        if close > 0:
            by_date[day] = {"date": day, "close": close}
    points = [by_date[key] for key in sorted(by_date)]
    if not points:
        raise ValueError("%s没有可用历史收盘数据" % name)
    return {"code": code, "name": name, "points": points}


def _query_index(connection, code, details):
    cursor = connection.cursor()
    try:
        query = QUERY.format(table=details["table"])
        cursor.execute(query, code=details["wind_code"])
        return _normalize_rows(cursor.fetchall(), code, details["name"])
    finally:
        cursor.close()


def _atomic_write(path, payload):
    absolute = os.path.abspath(path)
    folder = os.path.dirname(absolute)
    os.makedirs(folder, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder,
                                         prefix=".index-daily-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, absolute)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def _safe_error(exc, context="查询"):
    if isinstance(exc, RuntimeError) and (str(exc).startswith("缺少Wind数据库配置") or
                                          str(exc).startswith("Wind数据库连接失败")):
        return str(exc)
    if isinstance(exc, ImportError):
        return str(exc)
    return "Wind数据库%s失败（%s）" % (context, type(exc).__name__)


def update_cache(path=DEFAULT_CACHE, connect=None, now=None, env_path=".env"):
    previous = load_cache(path)
    # Never relabel an old public-market cache as Wind data.
    if previous.get("source") != SOURCE_NAME:
        previous = _empty_cache()
    attempted_at = _now_text(now)
    previous_indices = previous.get("indices", {})
    has_previous_points = any(item.get("points") for item in previous_indices.values())
    current = {"source": SOURCE_NAME,
               "updated_at": previous.get("updated_at") if has_previous_points else None,
               "attempted_at": attempted_at, "indices": {}}
    successes, failures = [], []
    connection = None
    connection_error = None
    try:
        if connect is None:
            if cx_Oracle is None:
                raise ImportError("未安装cx_Oracle，无法连接Wind数据库")
            config = database_config(env_path)
            connection = cx_Oracle.connect(config["user"], config["password"], config["dsn"])
        else:
            connection = connect()
    except Exception as exc:
        connection_error = _safe_error(exc, "连接")

    try:
        for code, details in INDICES.items():
            try:
                if connection_error:
                    raise RuntimeError(connection_error)
                item = _query_index(connection, code, details)
                item["updated_at"] = attempted_at
                item["error"] = None
                current["indices"][code] = item
                successes.append(code)
            except Exception as exc:
                old = previous_indices.get(code)
                message = _safe_error(exc)
                if old and old.get("points"):
                    item = dict(old)
                    item.update({"code": code, "name": details["name"], "error": message})
                    current["indices"][code] = item
                else:
                    current["indices"][code] = {
                        "code": code, "name": details["name"], "updated_at": None,
                        "error": message, "points": [],
                    }
                failures.append({"code": code, "name": details["name"],
                                 "error": message,
                                 "used_cache": bool(old and old.get("points"))})
    finally:
        if connection is not None:
            connection.close()
    if successes:
        current["updated_at"] = attempted_at
    _atomic_write(path, current)
    return {"cache": os.path.abspath(path), "successes": successes,
            "failures": failures, "updated_at": current["updated_at"],
            "attempted_at": attempted_at}


def page_payload(path=DEFAULT_CACHE, earliest_date=None):
    cache = load_cache(path)
    cutoff = None
    if earliest_date:
        try:
            cutoff = (date.fromisoformat(earliest_date) - timedelta(days=10)).isoformat()
        except ValueError:
            pass
    indices = OrderedDict()
    for code, details in INDICES.items():
        raw = cache.get("indices", {}).get(code, {})
        points = raw.get("points") or []
        if cutoff:
            points = [point for point in points if point.get("date", "") >= cutoff]
        indices[code] = {
            "code": code, "name": details["name"],
            "updated_at": raw.get("updated_at"), "error": raw.get("error"),
            "points": points,
        }
    return {"source": cache.get("source", SOURCE_NAME),
            "updated_at": cache.get("updated_at"),
            "attempted_at": cache.get("attempted_at"),
            "available": any(item["points"] for item in indices.values()),
            "indices": indices}
