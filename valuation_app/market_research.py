import json
import math
import os
import tempfile
from collections import OrderedDict
from datetime import date, datetime, timedelta

from .benchmark import load_cache as load_benchmark_cache
from .risk import load_cache as load_risk_cache, _rqdata_config


DEFAULT_CACHE = os.path.join("market_data", "market_research.json")
START_DATE = "2010-01-01"
MACRO_FACTORS = OrderedDict([
    ("PMI", "采购经理指数PMI_当月值"),
    ("CPI同比", "居民消费价格指数CPI_当月同比_(上年同月=100)"),
    ("PPI同比", "工业品出厂价格指数PPI_当月同比_(上年同月=100)"),
    ("M2同比", "货币和准货币M2_同比增长"),
    ("工业增加值同比", "规模以上工业增加值_同比增长"),
])
STYLE_FACTORS = OrderedDict([
    ("规模", "size"), ("动量", "momentum"), ("价值", "book_to_price"),
    ("盈利", "earnings_yield"), ("成长", "growth"),
    ("流动性", "liquidity"), ("残余波动", "residual_volatility"),
])
SW1_INDICES = OrderedDict([
    ("801010.INDX", "农林牧渔"), ("801030.INDX", "基础化工"),
    ("801040.INDX", "钢铁"), ("801050.INDX", "有色金属"),
    ("801080.INDX", "电子"), ("801110.INDX", "家用电器"),
    ("801120.INDX", "食品饮料"), ("801130.INDX", "纺织服饰"),
    ("801140.INDX", "轻工制造"), ("801150.INDX", "医药生物"),
    ("801160.INDX", "公用事业"), ("801170.INDX", "交通运输"),
    ("801180.INDX", "房地产"), ("801200.INDX", "商贸零售"),
    ("801210.INDX", "社会服务"), ("801230.INDX", "综合"),
    ("801710.INDX", "建筑材料"), ("801720.INDX", "建筑装饰"),
    ("801730.INDX", "电力设备"), ("801740.INDX", "国防军工"),
    ("801750.INDX", "计算机"), ("801760.INDX", "传媒"),
    ("801770.INDX", "通信"), ("801780.INDX", "银行"),
    ("801790.INDX", "非银金融"), ("801880.INDX", "汽车"),
    ("801890.INDX", "机械设备"), ("801950.INDX", "煤炭"),
    ("801960.INDX", "石油石化"), ("801970.INDX", "环保"),
    ("801980.INDX", "美容护理"),
])
STYLE_INDICES = OrderedDict([
    ("000918.XSHG", "沪深300成长"), ("000919.XSHG", "沪深300价值"),
    ("000922.XSHG", "中证红利"), ("930939.INDX", "质量"),
    ("930782.INDX", "低波"),
])
OPTION_UNDERLYINGS = OrderedDict([
    ("510050.XSHG", "50ETF"), ("510300.XSHG", "300ETF"),
    ("510500.XSHG", "500ETF"), ("000852.XSHG", "中证1000"),
])
COMMODITIES = OrderedDict([
    ("CU", "铜"), ("AU", "黄金"), ("RB", "螺纹钢"),
    ("SC", "原油"), ("M", "豆粕"), ("P", "棕榈油"),
])
ETF_CANDIDATES = OrderedDict([
    ("000300", ["510300.XSHG", "159919.XSHE"]),
    ("000905", ["510500.XSHG", "159922.XSHE"]),
    ("000852", ["512100.XSHG", "159845.XSHE"]),
    ("000510", ["563360.XSHG", "159338.XSHE"]),
    ("000922", ["510880.XSHG", "515180.XSHG"]),
    ("000918", ["159520.XSHE"]), ("000919", ["159521.XSHE"]),
])


def _empty_cache():
    return {"updated_at": None, "attempted_at": None, "macro": {},
            "industry_indices": {}, "style_factor_returns": {},
            "sentiment": {}, "arbitrage": {}, "option_surfaces": {},
            "etf_mapping": {}, "allocation": {}, "errors": []}


def load_cache(path=DEFAULT_CACHE):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else _empty_cache()
    except (OSError, ValueError, TypeError):
        return _empty_cache()


def _atomic_write(path, payload):
    absolute = os.path.abspath(path)
    folder = os.path.dirname(absolute)
    os.makedirs(folder, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder,
                                         prefix=".market-research-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, absolute)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def _day(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value)[:10]
    return text if len(text) == 10 else datetime.strptime(text[:8], "%Y%m%d").strftime("%Y-%m-%d")


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _frame_points(frame, value_field="value"):
    result = {}
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    for index, row in frame.iterrows():
        try:
            raw_day = index[-1] if isinstance(index, tuple) else index
            day = _day(raw_day)
            value = _finite(row[value_field])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if value is not None:
            result[day] = {"date": day, "value": value}
    return [result[key] for key in sorted(result)]


def _price_series(frame, codes):
    values = {code: {} for code in codes}
    if frame is None or not hasattr(frame, "iterrows"):
        return values
    for index, row in frame.iterrows():
        if isinstance(index, tuple):
            code, raw_day = str(index[0]), index[-1]
        elif len(codes) == 1:
            code, raw_day = codes[0], index
        else:
            continue
        close = _finite(row.get("close") if hasattr(row, "get") else None)
        if code in values and close is not None and close > 0:
            values[code][_day(raw_day)] = close
    return {code: [{"date": day, "close": by_day[day]} for day in sorted(by_day)]
            for code, by_day in values.items()}


def _returns(points):
    result = []
    for index in range(1, len(points)):
        a, b = _finite(points[index - 1].get("close")), _finite(points[index].get("close"))
        if a and b and a > 0 and b > 0:
            result.append(math.log(b / a))
    return result


def _stdev(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _metric(points, benchmark=None):
    if not points:
        return {"status": "missing"}
    closes = [point["close"] for point in points]
    def momentum(days):
        return closes[-1] / closes[max(0, len(closes) - 1 - days)] - 1 if len(closes) > days else None
    returns = _returns(points)
    peak, drawdown = closes[0], 0.0
    for close in closes:
        peak = max(peak, close)
        drawdown = min(drawdown, close / peak - 1)
    m3, m6 = momentum(63), momentum(126)
    relative = None
    if benchmark and len(benchmark) > 126:
        relative = m6 - (benchmark[-1]["close"] / benchmark[-127]["close"] - 1) if m6 is not None else None
    return {"status": "ok", "latest_date": points[-1]["date"], "latest": closes[-1],
            "momentum_1m": momentum(21), "momentum_3m": m3,
            "momentum_6m": m6, "momentum_12m": momentum(252),
            "volatility_20d": (_stdev(returns[-20:]) or 0.0) * math.sqrt(252) if len(returns) >= 20 else None,
            "max_drawdown": drawdown, "relative_6m": relative,
            "trend": "上行" if m3 is not None and m6 is not None and m3 > 0 and m6 > 0 else "偏弱"}


def _zscore(values):
    clean = [value for value in values if value is not None]
    if len(clean) < 2:
        return [0.0 for _ in values]
    mean, std = sum(clean) / len(clean), _stdev(clean)
    return [max(-3.0, min(3.0, (value - mean) / std)) if value is not None and std else 0.0
            for value in values]


def _before_close(points, day):
    value = None
    for point in points:
        if point["date"] <= day:
            value = point["close"]
        else:
            break
    return value


def _performance(returns):
    if not returns:
        return {}
    nav, peak, drawdown = 1.0, 1.0, 0.0
    for value in returns:
        nav *= 1 + value
        peak = max(peak, nav)
        drawdown = min(drawdown, nav / peak - 1)
    annual = nav ** (12.0 / len(returns)) - 1
    vol = (_stdev(returns) or 0.0) * math.sqrt(12)
    return {"total_return": nav - 1, "annual_return": annual, "annual_volatility": vol,
            "sharpe": annual / vol if vol > 1e-12 else None, "max_drawdown": drawdown,
            "calmar": annual / abs(drawdown) if drawdown < -1e-12 else None,
            "monthly_win_rate": sum(value > 0 for value in returns) / float(len(returns))}


def _backtest(index_series, benchmark_code):
    benchmark = index_series.get(benchmark_code, [])
    if len(benchmark) < 280:
        return {"status": "insufficient_history", "benchmark": "沪深300",
                "cost_scenarios_bp": [0, 10, 20], "rebalance": "monthly"}
    month_ends = []
    for point in benchmark:
        if month_ends and month_ends[-1][0] == point["date"][:7]:
            month_ends[-1] = (point["date"][:7], point["date"])
        else:
            month_ends.append((point["date"][:7], point["date"]))
    month_ends = [item[1] for item in month_ends]
    previous, periods, scenario_returns = {}, [], {0: [], 10: [], 20: []}
    benchmark_returns = []
    for index in range(12, len(month_ends) - 1):
        signal_day, end_day = month_ends[index], month_ends[index + 1]
        sliced = {code: [p for p in points if p["date"] <= signal_day]
                  for code, points in index_series.items()}
        allocation = build_allocation(sliced, benchmark_code, include_backtest=False)
        target = {item["code"]: item["weight"] for item in allocation.get("recommendations", [])}
        if not target:
            continue
        keys = set(previous) | set(target)
        turnover = 0.5 * sum(abs(target.get(key, 0.0) - previous.get(key, 0.0)) for key in keys)
        if previous and turnover > 0.30:
            alpha = 0.30 / turnover
            target = {key: previous.get(key, 0.0) + alpha * (target.get(key, 0.0) - previous.get(key, 0.0))
                      for key in keys}
            turnover = 0.30
        gross = 0.0
        for code, weight in target.items():
            start_close = _before_close(index_series.get(code, []), signal_day)
            end_close = _before_close(index_series.get(code, []), end_day)
            if start_close and end_close:
                gross += weight * (end_close / start_close - 1)
        for cost in scenario_returns:
            scenario_returns[cost].append(gross - turnover * cost / 10000.0)
        b0, b1 = _before_close(benchmark, signal_day), _before_close(benchmark, end_day)
        benchmark_returns.append(b1 / b0 - 1 if b0 and b1 else 0.0)
        effective_day = next((point["date"] for point in benchmark if point["date"] > signal_day), end_day)
        periods.append({"signal_date": signal_day, "effective_date": effective_day,
                        "end_date": end_day, "turnover": turnover,
                        "gross_return": gross, "weights": target})
        previous = target
    metrics = {str(cost): _performance(values) for cost, values in scenario_returns.items()}
    base = metrics.get("10", {})
    benchmark_metrics = _performance(benchmark_returns)
    if base and benchmark_metrics:
        base["relative_annual_return"] = base.get("annual_return", 0) - benchmark_metrics.get("annual_return", 0)
    return {"status": "ok" if periods else "insufficient_history", "benchmark": "沪深300",
            "cost_scenarios_bp": [0, 10, 20], "rebalance": "monthly",
            "metrics": metrics, "benchmark_metrics": benchmark_metrics, "periods": periods}


def build_allocation(index_series, benchmark_code="000300", include_backtest=True):
    """Create a transparent current score/risk-budget allocation."""
    metrics = OrderedDict((code, _metric(points, index_series.get(benchmark_code)))
                          for code, points in index_series.items() if len(points) >= 253)
    codes = list(metrics)
    if not codes:
        return {"status": "missing", "recommendations": [], "backtest": {}}
    trend_raw = [sum((metrics[c].get(k) or 0.0) for k in
                     ("momentum_1m", "momentum_3m", "momentum_6m", "momentum_12m")) / 4.0 for c in codes]
    relative_raw = [metrics[c].get("relative_6m") for c in codes]
    volatility_raw = [-(metrics[c].get("volatility_20d") or 0.0) for c in codes]
    trend_z, relative_z, sentiment_z = _zscore(trend_raw), _zscore(relative_raw), _zscore(volatility_raw)
    scored = []
    for index, code in enumerate(codes):
        score = 0.35 * trend_z[index] + 0.20 * relative_z[index] + 0.15 * sentiment_z[index]
        # Macro and valuation/style sleeves are neutral when their point-in-time
        # data are unavailable; their contributions remain explicit.
        components = {"trend": 0.35 * trend_z[index], "macro": 0.0,
                      "relative_strength": 0.20 * relative_z[index],
                      "sentiment_volatility": 0.15 * sentiment_z[index], "style_valuation": 0.0}
        scored.append({"code": code, "score": score, "components": components,
                       "volatility_60d": (_stdev(_returns(index_series[code])[-60:]) or 0.01) * math.sqrt(252)})
    broad_codes = ("000300", "000905", "000852", "000510")
    positive = [item for item in scored if item["score"] >= 0]
    if not positive:
        positive = [max(scored, key=lambda item: item["score"])]
    raw = [max(0.01, item["score"] + 1.0) / max(0.01, item["volatility_60d"]) for item in positive]
    total = sum(raw)
    for item, value in zip(positive, raw):
        item["weight"] = value / total
    # Apply instrument caps iteratively. At least half is assigned to broad
    # indices; broad indices with a negative score may therefore act as the
    # transparent long-only completion sleeve.
    by_code = {item["code"]: item for item in scored}
    broad = [by_code[code] for code in broad_codes if code in by_code]
    # A fully invested long-only portfolio needs enough eligible capacity even
    # when only one or two broad scores are non-negative.
    if broad and sum(0.35 if item["code"] in broad_codes else 0.10 for item in positive) < 1.0:
        for item in sorted(broad, key=lambda value: -value["score"]):
            if item not in positive:
                item["weight"] = 0.0
                positive.append(item)
            if sum(0.35 if value["code"] in broad_codes else 0.10 for value in positive) >= 1.0:
                break
    if broad and sum(item["weight"] for item in positive if item["code"] in broad_codes) < 0.50:
        for item in sorted(broad, key=lambda value: -value["score"]):
            if item not in positive:
                item["weight"] = 0.0
                positive.append(item)
        non_broad = [item for item in positive if item["code"] not in broad_codes]
        non_total = sum(item.get("weight", 0.0) for item in non_broad)
        if non_total > 0.50:
            scale = 0.50 / non_total
            for item in non_broad:
                item["weight"] *= scale
        broad_target = 1.0 - sum(item.get("weight", 0.0) for item in non_broad)
        broad_raw = [max(0.01, item["score"] + 1.0) / max(0.01, item["volatility_60d"]) for item in broad]
        broad_sum = sum(broad_raw)
        for item, value in zip(broad, broad_raw):
            item["weight"] = broad_target * value / broad_sum
    for _ in range(10):
        excess = 0.0
        open_items = []
        for item in positive:
            cap = 0.35 if item["code"] in broad_codes else (0.15 if item["code"] in {"000918", "000919", "000922", "930939", "930782"} else 0.10)
            if item.get("weight", 0.0) > cap:
                excess += item["weight"] - cap
                item["weight"] = cap
            elif item.get("weight", 0.0) < cap:
                open_items.append((item, cap))
        if excess < 1e-12 or not open_items:
            break
        room = sum(cap - item["weight"] for item, cap in open_items)
        for item, cap in open_items:
            item["weight"] += excess * (cap - item["weight"]) / room
    result = {"status": "ok", "as_of": max(points[-1]["date"] for points in index_series.values() if points),
            "method": "月度多维评分＋风险预算（研究建议，不是交易指令）",
            "recommendations": sorted(positive, key=lambda item: -item["weight"]),
            "all_scores": sorted(scored, key=lambda item: -item["score"]),
            "backtest": {"status": "not_requested", "benchmark": "沪深300",
                         "cost_scenarios_bp": [0, 10, 20], "rebalance": "monthly"}}
    if include_backtest:
        result["backtest"] = _backtest(index_series, benchmark_code)
    return result


def _safe_module(old, key, loader, attempted, errors):
    try:
        value = loader()
        if value in (None, {}, []):
            raise ValueError("无有效数据")
        return {"data": value, "updated_at": attempted, "error": None, "used_cache": False}
    except Exception as exc:
        previous = old.get(key, {})
        errors.append({"module": key, "error": "RQData%s更新失败（%s）" % (key, type(exc).__name__),
                       "used_cache": bool(previous.get("data"))})
        if previous.get("data"):
            value = dict(previous)
            value.update({"error": errors[-1]["error"], "used_cache": True})
            return value
        return {"data": {}, "updated_at": None, "error": errors[-1]["error"], "used_cache": False}


def _merge_points(previous, incoming, date_field="date"):
    merged = {}
    for point in list(previous or []) + list(incoming or []):
        if isinstance(point, dict) and point.get(date_field):
            merged[point[date_field]] = point
    return [merged[key] for key in sorted(merged)]


def update_cache(path=DEFAULT_CACHE, now=None, env_path=".env", rqdata_client=None):
    old = load_cache(path)
    attempted = (now or datetime.now()).replace(microsecond=0).isoformat()
    if rqdata_client is None:
        import rqdatac as rqdata_client
    uri = _rqdata_config(env_path)
    if not uri:
        raise RuntimeError("未配置RQDATAC_CONF")
    rqdata_client.init(uri=uri, connect_timeout=5, timeout=60)
    end = attempted[:10]
    incremental_start = (date.fromisoformat(end) - timedelta(days=400)).isoformat()
    errors = []
    current = _empty_cache()
    current["attempted_at"] = attempted

    def macro_loader():
        result = {}
        for label, factor in MACRO_FACTORS.items():
            if label == "M2同比":
                frame = rqdata_client.econ.get_money_supply(start_date=START_DATE, end_date=end)
                value_fields = ("m2_yoy", "M2同比", "m2")
            else:
                frame = rqdata_client.econ.get_factors(factors=factor, start_date=START_DATE, end_date=end)
                value_fields = ("value",)
            points = []
            if frame is None or not hasattr(frame, "iterrows"):
                result[label] = []
                continue
            for index, row in frame.iterrows():
                info_date = index[-1] if isinstance(index, tuple) else index
                value = next((_finite(row.get(field)) for field in value_fields if _finite(row.get(field)) is not None), None)
                if value is not None:
                    period = row.get("end_date")
                    points.append({"info_date": _day(info_date),
                                   "period_end": _day(period) if period is not None else _day(info_date),
                                   "value": value})
            result[label] = sorted(points, key=lambda item: item["info_date"])
        return result
    current["macro"] = _safe_module(old, "macro", macro_loader, attempted, errors)

    research_codes = list(SW1_INDICES) + list(STYLE_INDICES)
    def industry_loader():
        old_data = old.get("industry_indices", {}).get("data", {})
        query_start = incremental_start if old_data else START_DATE
        frame = rqdata_client.get_price(research_codes, query_start, end, fields=["close"], adjust_type="none")
        series = _price_series(frame, research_codes)
        benchmark = load_benchmark_cache().get("indices", {}).get("000300", {}).get("points", [])
        result = {}
        for code, points in series.items():
            merged = _merge_points(old_data.get(code, {}).get("points", []), points)
            result[code] = {"name": SW1_INDICES.get(code, STYLE_INDICES.get(code)), "points": merged,
                            "metrics": _metric(merged, benchmark)}
        return result
    current["industry_indices"] = _safe_module(old, "industry_indices", industry_loader, attempted, errors)

    def style_loader():
        old_data = old.get("style_factor_returns", {}).get("data", {})
        query_start = incremental_start if old_data else START_DATE
        frame = rqdata_client.get_factor_return(query_start, end, factors=list(STYLE_FACTORS.values()),
                                                universe="whole_market", industry_mapping="sws_2021", model="v1")
        result = {}
        for label, field in STYLE_FACTORS.items():
            points = []
            for index, row in frame.iterrows():
                value = _finite(row.get(field))
                if value is not None:
                    points.append({"date": _day(index), "value": value})
            result[label] = _merge_points(old_data.get(label, []), points)
        return result
    current["style_factor_returns"] = _safe_module(old, "style_factor_returns", style_loader, attempted, errors)

    def arbitrage_loader():
        old_data = old.get("arbitrage", {}).get("data", {})
        query_start = incremental_start if old_data else START_DATE
        result = {}
        for symbol, name in COMMODITIES.items():
            frame = rqdata_client.futures.get_roll_yield(symbol, query_start, end, type="main_sub", rule=0)
            points = _frame_points(frame, "annualized_yield")
            result[symbol] = {"name": name, "points": _merge_points(old_data.get(symbol, {}).get("points", []), points)}
        return result
    current["arbitrage"] = _safe_module(old, "arbitrage", arbitrage_loader, attempted, errors)

    def options_loader():
        result = {}
        today = date.fromisoformat(end)
        maturities = []
        year, month = today.year, today.month
        for offset in (0, 1):
            value = month + offset
            maturities.append("%02d%02d" % ((year + (value - 1) // 12) % 100, (value - 1) % 12 + 1))
        for code, name in OPTION_UNDERLYINGS.items():
            result[code] = {"name": name, "maturities": {}}
            for maturity in maturities:
                frame = rqdata_client.options.get_indicators(
                    code, maturity, start_date=(today - timedelta(days=365)).isoformat(), end_date=end,
                    fields=["VL_PCR", "OI_PCR", "iv_025_dela", "iv_minus_025_dela", "skew"])
                records = []
                if frame is not None and hasattr(frame, "iterrows"):
                    for index, row in frame.iterrows():
                        record = {"date": _day(index[-1] if isinstance(index, tuple) else index)}
                        for field in ("VL_PCR", "OI_PCR", "iv_025_dela", "iv_minus_025_dela", "skew"):
                            record[field] = _finite(row.get(field))
                        records.append(record)
                result[code]["maturities"][maturity] = records
        return result
    current["option_surfaces"] = _safe_module(old, "option_surfaces", options_loader, attempted, errors)
    current["sentiment"] = {"data": {"options": current["option_surfaces"].get("data", {})},
                            "updated_at": current["option_surfaces"].get("updated_at"),
                            "error": current["option_surfaces"].get("error")}

    def etf_loader():
        result = {}
        start = (date.fromisoformat(end) - timedelta(days=550)).isoformat()
        for index_code, candidates in ETF_CANDIDATES.items():
            rows = []
            for code in candidates:
                units = rqdata_client.etf.get_daily_units(code, start, end)
                nav = rqdata_client.etf.get_nav(code, start, end)
                unit_points, nav_points = _frame_points(units, "units"), _frame_points(nav, "nav")
                nav_by_day = {point["date"]: point["value"] for point in nav_points}
                history = [{"date": point["date"], "aum": point["value"] * nav_by_day[point["date"]]}
                           for point in unit_points if point["date"] in nav_by_day]
                if len(history) >= 120:
                    rows.append({"code": code, "latest_aum": history[-1]["aum"], "history": history})
            rows.sort(key=lambda item: -item["latest_aum"])
            result[index_code] = {"selected": rows[0]["code"] if rows else None, "candidates": rows,
                                  "basis": "最新ETF份额×NAV规模；至少120个交易日"}
        return result
    current["etf_mapping"] = _safe_module(old, "etf_mapping", etf_loader, attempted, errors)

    benchmark = load_benchmark_cache().get("indices", {})
    index_series = {code: item.get("points", []) for code, item in benchmark.items()
                    if code in ("000300", "000905", "000852", "000510", "000922")}
    research = current["industry_indices"].get("data", {})
    for code, item in research.items():
        short_code = code.split(".")[0]
        if code in STYLE_INDICES or code in SW1_INDICES:
            index_series[short_code] = item.get("points", [])
    current["allocation"] = build_allocation(index_series)
    current["errors"] = errors
    if any(section.get("data") for section in (current["macro"], current["industry_indices"],
                                                current["style_factor_returns"], current["arbitrage"],
                                                current["option_surfaces"], current["etf_mapping"])):
        current["updated_at"] = attempted
    else:
        current["updated_at"] = old.get("updated_at")
    _atomic_write(path, current)
    return {"cache": os.path.abspath(path), "updated_at": current["updated_at"],
            "errors": errors, "modules": {key: bool(current[key].get("data")) for key in
            ("macro", "industry_indices", "style_factor_returns", "sentiment", "arbitrage",
             "option_surfaces", "etf_mapping")}}


def page_payload(path=DEFAULT_CACHE):
    return load_cache(path)
