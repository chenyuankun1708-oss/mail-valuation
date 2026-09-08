import math
import calendar
from collections import OrderedDict
from datetime import date

from .config import BENCHMARK, FEATURE_NAMES, asset_class


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def month_ends(points, as_of=None):
    values = OrderedDict()
    for point in points:
        day = str(point.get("date") or "")[:10]
        if day:
            values[day[:7]] = day
    days = list(values.values())
    if as_of:
        cutoff = str(as_of)[:10]
        current_month = cutoff[:7]
        cutoff_date = date.fromisoformat(cutoff)
        calendar_end = cutoff_date.day == calendar.monthrange(cutoff_date.year, cutoff_date.month)[1]
        if not calendar_end:
            days = [day for day in days if day[:7] < current_month]
    return days


def before_points(points, day):
    return [point for point in points if point.get("date", "") <= day and finite(point.get("close"))]


def next_point(points, day):
    return next((point for point in points if point.get("date", "") > day and finite(point.get("close"))), None)


def _return(points, lookback):
    if len(points) <= lookback:
        return None
    end, start = finite(points[-1]["close"]), finite(points[-1 - lookback]["close"])
    return end / start - 1 if end and start and start > 0 else None


def _volatility(points, length=60):
    values = []
    for index in range(max(1, len(points) - length), len(points)):
        a, b = finite(points[index - 1]["close"]), finite(points[index]["close"])
        if a and b and a > 0 and b > 0:
            values.append(math.log(b / a))
    if len(values) < 20:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance * 252)


def _latest_macro(macro, day):
    values = []
    for points in macro.values():
        eligible = [point for point in points if str(point.get("info_date") or point.get("date") or "")[:10] <= day]
        if len(eligible) >= 2:
            a, b = finite(eligible[-2].get("value")), finite(eligible[-1].get("value"))
            if a is not None and b is not None:
                values.append(1.0 if b > a else -1.0 if b < a else 0.0)
    return sum(values) / len(values) if values else 0.0


def _latest_style(styles, day):
    values = []
    for points in styles.values():
        eligible = [finite(point.get("value")) for point in points if point.get("date", "") <= day]
        values.extend(value for value in eligible[-20:] if value is not None)
    return max(-1.0, min(1.0, sum(values))) if values else 0.0


def _latest_sentiment(options, day):
    values = []
    for item in options.values():
        for records in (item.get("maturities") or {}).values():
            eligible = [row for row in records if row.get("date", "") <= day]
            if eligible:
                pcr = finite(eligible[-1].get("VL_PCR"))
                skew = finite(eligible[-1].get("skew"))
                if pcr is not None:
                    values.append(-(pcr - 1.0))
                if skew is not None:
                    values.append(-skew)
    return max(-3.0, min(3.0, sum(values) / len(values))) if values else 0.0


def feature_rows(series, research, as_of=None):
    benchmark = series.get(BENCHMARK, {}).get("points", [])
    dates = month_ends(benchmark, as_of=as_of)
    macro = (research.get("macro") or {}).get("data", {})
    styles = (research.get("style_factor_returns") or {}).get("data", {})
    options = (research.get("option_surfaces") or {}).get("data", {})
    rows = []
    for day in dates:
        benchmark_points = before_points(benchmark, day)
        b3, b6 = _return(benchmark_points, 63), _return(benchmark_points, 126)
        common = (_latest_macro(macro, day), _latest_style(styles, day), _latest_sentiment(options, day))
        for code, item in series.items():
            if not asset_class(code):
                continue
            points = before_points(item.get("points", []), day)
            values = {"momentum_1m": _return(points, 21), "momentum_3m": _return(points, 63),
                      "momentum_6m": _return(points, 126), "momentum_12m": _return(points, 252),
                      "volatility_60d": _volatility(points),
                      "relative_3m": None, "relative_6m": None,
                      "macro_state": common[0], "style_state": common[1], "sentiment_state": common[2]}
            values["relative_3m"] = values["momentum_3m"] - b3 if values["momentum_3m"] is not None and b3 is not None else None
            values["relative_6m"] = values["momentum_6m"] - b6 if values["momentum_6m"] is not None and b6 is not None else None
            if all(values[name] is not None for name in FEATURE_NAMES):
                rows.append({"date": day, "code": code, "name": item.get("name", code),
                             "asset_class": asset_class(code), "features": values,
                             "available_at": day + "T15:01:00+08:00"})
    return rows


def attach_targets(rows, series):
    by_code = {}
    for row in rows:
        by_code.setdefault(row["code"], []).append(row)
    benchmark = series[BENCHMARK]["points"]
    for code, values in by_code.items():
        points = series[code]["points"]
        for index, row in enumerate(values[:-1]):
            next_day = values[index + 1]["date"]
            p0, p1 = next_point(points, row["date"]), next_point(points, next_day)
            b0, b1 = next_point(benchmark, row["date"]), next_point(benchmark, next_day)
            if p0 and p1 and b0 and b1:
                row["target_end"] = b1["date"]
                row["target_excess_return"] = p1["close"] / p0["close"] - b1["close"] / b0["close"]
                row["asset_return"] = p1["close"] / p0["close"] - 1
                row["benchmark_return"] = b1["close"] / b0["close"] - 1
    return rows
