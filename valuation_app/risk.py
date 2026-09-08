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
YIELD_CODE = "CN10Y"
YIELD_TENOR = "10Y"
YIELD_SOURCE = "RQData，中债国债收益率曲线，来源中央结算公司"
US_YIELD_CODE = "US_TREASURY"
US_YIELD_SOURCE = "RQData，美国国债收益率曲线"
US_TENORS = ("10Y",)
FX_CODE = "USDCNY"
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
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    return datetime.strptime(text[:8], "%Y%m%d").strftime("%Y-%m-%d")


def _safe_error(exc, context="查询"):
    source = "RQData" if str(context).startswith("RQData") else "Wind数据库"
    label = str(context)[6:] if source == "RQData" else context
    return "%s%s失败（%s）" % (source, label, type(exc).__name__)


def _empty_cache():
    return {"source": SOURCE_NAME, "updated_at": None, "indices": {}, "futures": {},
            "yield_curves": {}, "exchange_rates": {}, "portfolio_var": {}}


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


def _normalize_yield_curve(frame):
    by_date = {}
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    for index, row in frame.iterrows():
        try:
            day = _day(index)
            value = float(row[YIELD_TENOR])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if value > 0 and math.isfinite(value):
            by_date[day] = {"date": day, "yield": value}
    return [by_date[key] for key in sorted(by_date)]


def _normalize_multi_curve(frame, tenors):
    by_date = {}
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    for index, row in frame.iterrows():
        try:
            day = _day(index)
        except (TypeError, ValueError, OverflowError):
            continue
        point = {"date": day}
        for tenor in tenors:
            try:
                value = float(row[tenor])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if value > 0 and math.isfinite(value):
                point[tenor] = value
        if len(point) > 1:
            by_date[day] = point
    return [by_date[key] for key in sorted(by_date)]


def _normalize_fx(frame):
    points = _normalize_multi_curve(frame, ("USD/CNY",))
    return [{"date": point["date"], "rate": point["USD/CNY"]} for point in points]


def _normalize_us_curve(frame):
    points = _normalize_multi_curve(frame, US_TENORS)
    observed = sorted(point[tenor] for point in points for tenor in US_TENORS
                      if tenor in point)
    scale = 0.01 if observed and observed[len(observed) // 2] > 1.0 else 1.0
    for point in points:
        for tenor in US_TENORS:
            value = point.get(tenor)
            # The live endpoint currently returns percentage numbers (4.70),
            # including historical sub-1% observations. Detect the unit once
            # for the whole frame rather than point by point.
            if value is not None:
                point[tenor] = value * scale
    return points


def _rqdata_config(env_path):
    value = os.environ.get("RQDATAC_CONF", "")
    if value:
        return value
    try:
        with open(env_path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, item = line.split("=", 1)
                if key.strip() == "RQDATAC_CONF":
                    return item.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _update_yield_curve(previous, current, attempted, env_path, rqdata_client=None):
    old = previous.get("yield_curves", {}).get(YIELD_CODE, {})
    try:
        uri = _rqdata_config(env_path)
        if not uri:
            raise RuntimeError("未配置RQDATAC_CONF")
        if rqdata_client is None:
            import rqdatac as rqdata_client
        rqdata_client.init(uri=uri, connect_timeout=5, timeout=60)
        end_date = attempted[:10].replace("-", "")
        frame = rqdata_client.get_yield_curve(start_date=START_DATE, end_date=end_date,
                                              tenor=YIELD_TENOR)
        points = _normalize_yield_curve(frame)
        if not points:
            raise ValueError("国债收益率无有效数据")
        current["yield_curves"][YIELD_CODE] = {
            "code": YIELD_CODE, "name": "10年期国债收益率", "tenor": YIELD_TENOR,
            "unit": "decimal", "source": YIELD_SOURCE, "points": points,
            "updated_at": attempted, "error": None,
        }
        return True, None
    except Exception as exc:
        item = dict(old) if old.get("points") else {
            "code": YIELD_CODE, "name": "10年期国债收益率", "tenor": YIELD_TENOR,
            "unit": "decimal", "source": YIELD_SOURCE, "points": [],
        }
        item["error"] = _safe_error(exc, "RQData国债收益率")
        current["yield_curves"][YIELD_CODE] = item
        return False, {"code": YIELD_CODE, "error": item["error"],
                       "used_cache": bool(old.get("points"))}


def _update_us_market(previous, current, attempted, env_path, rqdata_client=None):
    old_curve = previous.get("yield_curves", {}).get(US_YIELD_CODE, {})
    old_fx = previous.get("exchange_rates", {}).get(FX_CODE, {})
    failures, successes = [], []
    try:
        uri = _rqdata_config(env_path)
        if not uri:
            raise RuntimeError("未配置RQDATAC_CONF")
        if rqdata_client is None:
            import rqdatac as rqdata_client
        if not hasattr(rqdata_client, "econ"):
            raise RuntimeError("当前rqdatac版本不支持美国国债曲线")
        end_date = attempted[:10].replace("-", "")
        curve = rqdata_client.econ.get_us_treasury_yield(
            start_date=START_DATE, end_date=end_date, tenor=list(US_TENORS))
        points = _normalize_us_curve(curve)
        if not points:
            raise ValueError("美国国债收益率无有效数据")
        current["yield_curves"][US_YIELD_CODE] = {
            "code": US_YIELD_CODE, "name": "美国国债收益率曲线", "unit": "decimal",
            "source": US_YIELD_SOURCE, "points": points, "updated_at": attempted, "error": None}
        successes.append(US_YIELD_CODE)
    except Exception as exc:
        item = dict(old_curve) if old_curve.get("points") else {"code": US_YIELD_CODE,
            "name": "美国国债收益率曲线", "unit": "decimal", "source": US_YIELD_SOURCE,
            "points": []}
        item["error"] = _safe_error(exc, "RQData美国国债收益率")
        current["yield_curves"][US_YIELD_CODE] = item
        failures.append({"code": US_YIELD_CODE, "error": item["error"],
                         "used_cache": bool(old_curve.get("points"))})
    try:
        if rqdata_client is None:
            import rqdatac as rqdata_client
        if not hasattr(rqdata_client, "econ"):
            raise RuntimeError("当前rqdatac版本不支持人民币参考汇率")
        end_date = attempted[:10].replace("-", "")
        frame = rqdata_client.econ.get_cny_reference_rate(
            start_date=START_DATE, end_date=end_date, fields="USD/CNY")
        points = _normalize_fx(frame)
        if not points:
            raise ValueError("USD/CNY无有效数据")
        current["exchange_rates"][FX_CODE] = {"code": FX_CODE, "name": "美元兑人民币中间价",
            "unit": "CNY per USD", "source": "RQData，人民币参考汇率中间价",
            "points": points, "updated_at": attempted, "error": None}
        successes.append(FX_CODE)
    except Exception as exc:
        item = dict(old_fx) if old_fx.get("points") else {"code": FX_CODE,
            "name": "美元兑人民币中间价", "unit": "CNY per USD",
            "source": "RQData，人民币参考汇率中间价", "points": []}
        item["error"] = _safe_error(exc, "RQData美元兑人民币")
        current["exchange_rates"][FX_CODE] = item
        failures.append({"code": FX_CODE, "error": item["error"],
                         "used_cache": bool(old_fx.get("points"))})
    return successes, failures


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


def update_cache(path=DEFAULT_CACHE, connect=None, now=None, env_path=".env", rqdata_client=None,
                 include_portfolio_var=False):
    previous = load_cache(path)
    attempted = (now or datetime.now()).replace(microsecond=0).isoformat()
    current = {"source": SOURCE_NAME, "updated_at": previous.get("updated_at"),
               "attempted_at": attempted, "indices": {}, "futures": {}, "yield_curves": {},
               "exchange_rates": {}, "portfolio_var": {}}
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
    yield_success, yield_failure = _update_yield_curve(
        previous, current, attempted, env_path, rqdata_client=rqdata_client)
    us_successes, us_failures = _update_us_market(
        previous, current, attempted, env_path, rqdata_client=rqdata_client)
    portfolio_failure = None
    current["portfolio_var"] = previous.get("portfolio_var", {})
    if include_portfolio_var:
        from .portfolio_var import update_portfolio_var
        current["portfolio_var"], portfolio_failure = update_portfolio_var(
            previous, current, attempted, env_path=env_path)
    _atomic_write(path, current)
    return {"cache": os.path.abspath(path), "successes": successes, "failures": failures,
            "yield_successes": [YIELD_CODE] if yield_success else [],
            "yield_failures": [yield_failure] if yield_failure else [],
            "rqdata_successes": us_successes, "rqdata_failures": us_failures,
            "portfolio_failure": portfolio_failure,
            "updated_at": current.get("updated_at"), "attempted_at": attempted}


def update_portfolio_var_cache(path=DEFAULT_CACHE, now=None, env_path=".env"):
    """Explicitly refresh standalone risk-report VaR without querying markets."""
    current = load_cache(path)
    attempted = (now or datetime.now()).replace(microsecond=0).isoformat()
    from .portfolio_var import update_portfolio_var
    portfolio, failure = update_portfolio_var(current, current, attempted, env_path=env_path)
    current["portfolio_var"] = portfolio
    current["attempted_at"] = attempted
    _atomic_write(path, current)
    return {"cache": os.path.abspath(path), "portfolio_failure": failure,
            "report_date": portfolio.get("report_date"),
            "assets": len(portfolio.get("assets", [])), "attempted_at": attempted}


def page_payload(path=DEFAULT_CACHE):
    cache = load_cache(path)
    return {"source": cache.get("source", SOURCE_NAME), "updated_at": cache.get("updated_at"),
            "attempted_at": cache.get("attempted_at"),
            "available": any(x.get("points") for x in cache.get("futures", {}).values()),
            "indices": cache.get("indices", {}), "futures": cache.get("futures", {}),
            "yield_curves": cache.get("yield_curves", {}),
            "exchange_rates": cache.get("exchange_rates", {}),
            "portfolio_var": cache.get("portfolio_var", {})}
