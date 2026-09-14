"""Canonical Python implementations for web-visible financial calculations."""
from __future__ import division, unicode_literals

import datetime as dt
import math


RISK_FREE_RATE = 0.02


def _date(value):
    return dt.datetime.strptime(value[:10], "%Y-%m-%d").date()


def day_span(start, end):
    return (_date(end) - _date(start)).days


def xnpv(rate, flows):
    if rate <= -1 or not flows:
        return float("inf")
    origin = flows[0]["date"]
    return sum(float(flow["value"]) /
               math.pow(1 + rate, day_span(origin, flow["date"]) / 365.0)
               for flow in flows)


def xirr(flows):
    if not any(item["value"] < 0 for item in flows) or not any(
            item["value"] > 0 for item in flows):
        return None
    grid = [-.9999, -.99, -.9, -.5, 0, .05, .1, .2, .5, 1, 2, 5, 10,
            100, 1000, 1000000]
    for index in range(1, len(grid)):
        low, high = grid[index - 1], grid[index]
        a, b = xnpv(low, flows), xnpv(high, flows)
        if not math.isfinite(a) or not math.isfinite(b) or a * b > 0:
            continue
        for _ in range(150):
            middle = (low + high) / 2
            value = xnpv(middle, flows)
            if abs(value) < .01:
                return middle
            if a * value <= 0:
                high, b = middle, value
            else:
                low, a = middle, value
        return (low + high) / 2
    return None


def valuation_before(points, requested, date_key="valuation_date"):
    candidates = [point for point in points if point.get(date_key) and
                  point[date_key] <= requested]
    return max(candidates, key=lambda item: item[date_key]) if candidates else None


def valuation_boundaries(points, start, end):
    first = valuation_before(points, start)
    last = valuation_before(points, end)
    if not first or not last or last["valuation_date"] <= first["valuation_date"]:
        return None, None
    return first, last


def risk_metrics(points):
    by_date = {}
    for point in points or []:
        try:
            value = float(point.get("accumulated_nav"))
        except (TypeError, ValueError):
            continue
        if point.get("valuation_date") and value > 0:
            by_date[point["valuation_date"]] = value
    clean = sorted(by_date.items())
    empty = {"nav_annualized": None, "sharpe": None, "max_drawdown": None,
             "annual_volatility": None, "valid_points": len(clean), "total_days": 0,
             "drawdown_peak": None, "drawdown_trough": None, "short_period": False}
    if len(clean) < 2:
        return empty
    total_days = day_span(clean[0][0], clean[-1][0])
    if total_days <= 0:
        return empty
    annualized = (math.pow(clean[-1][1] / clean[0][1], 365.0 / total_days) - 1) * 100
    peak = clean[0]
    maximum_drawdown = 0
    peak_date = trough_date = peak[0]
    for date, value in clean:
        if value > peak[1]:
            peak = (date, value)
        drawdown = value / peak[1] - 1
        if drawdown < maximum_drawdown:
            maximum_drawdown, peak_date, trough_date = drawdown, peak[0], date
    intervals = []
    for previous, current in zip(clean, clean[1:]):
        days = day_span(previous[0], current[0])
        if days > 0:
            intervals.append((days, math.log(current[1] / previous[1]) / days))
    volatility = sharpe = None
    if len(clean) >= 3 and len(intervals) >= 2:
        weight = sum(item[0] for item in intervals)
        mean = sum(days * rate for days, rate in intervals) / weight
        denominator = weight - 1
        if denominator > 0:
            variance = sum(days * math.pow(rate - mean, 2)
                           for days, rate in intervals) / denominator
            vol = math.sqrt(max(variance, 0) * 365)
            volatility = vol * 100
            if vol > 1e-12:
                sharpe = (mean * 365 - math.log(1 + RISK_FREE_RATE)) / vol
    return {"nav_annualized": annualized, "sharpe": sharpe,
            "max_drawdown": maximum_drawdown * 100, "annual_volatility": volatility,
            "valid_points": len(clean), "total_days": total_days,
            "drawdown_peak": peak_date, "drawdown_trough": trough_date,
            "short_period": total_days < 30}


def market_before(points, requested):
    candidates = []
    for point in points or []:
        try:
            close = float(point.get("close"))
        except (TypeError, ValueError):
            continue
        if point.get("date") and point["date"] <= requested and close > 0:
            candidates.append({"date": point["date"], "close": close})
    return max(candidates, key=lambda item: item["date"]) if candidates else None


def benchmark_metrics(item, start, end):
    points = item.get("points", [])
    first, last = market_before(points, start), market_before(points, end)
    if not first or not last or last["date"] <= first["date"]:
        return {"status": "暂无行情"}
    used = [point for point in points if first["date"] <= point.get("date", "") <= last["date"]
            and float(point.get("close") or 0) > 0]
    days = day_span(first["date"], last["date"])
    period = (last["close"] / first["close"] - 1) * 100
    cagr = (math.pow(last["close"] / first["close"], 365.0 / days) - 1) * 100
    returns = [math.log(float(current["close"]) / float(previous["close"]))
               for previous, current in zip(used, used[1:])]
    volatility = None
    if len(returns) >= 20:
        mean = sum(returns) / len(returns)
        variance = sum(math.pow(value - mean, 2) for value in returns) / (len(returns) - 1)
        volatility = math.sqrt(variance * 252) * 100
    peak, maximum, peak_date, trough_date = first, 0, first["date"], first["date"]
    for point in used:
        current = {"date": point["date"], "close": float(point["close"])}
        if current["close"] > peak["close"]:
            peak = current
        drawdown = current["close"] / peak["close"] - 1
        if drawdown < maximum:
            maximum, peak_date, trough_date = drawdown, peak["date"], current["date"]
    return {"status": "ok", "first": first, "last": last, "period": period,
            "cagr": cagr, "annual_volatility": volatility,
            "volatility_samples": len(returns), "max_drawdown": maximum * 100,
            "drawdown_peak": peak_date, "drawdown_trough": trough_date}


def alpha_beta(points, benchmark_points):
    clean_by_date = {}
    for point in points or []:
        try:
            value = float(point.get("accumulated_nav"))
        except (TypeError, ValueError):
            continue
        if point.get("valuation_date") and value > 0:
            clean_by_date[point["valuation_date"]] = value
    clean = sorted(clean_by_date.items())
    intervals = []
    for previous, current in zip(clean, clean[1:]):
        days = day_span(previous[0], current[0])
        before = market_before(benchmark_points, previous[0])
        after = market_before(benchmark_points, current[0])
        if days > 0 and before and after and after["date"] > before["date"]:
            intervals.append({"days": days,
                              "x": math.log(after["close"] / before["close"]) / days,
                              "y": math.log(current[1] / previous[1]) / days,
                              "start": previous[0], "end": current[0]})
    span = day_span(intervals[0]["start"], intervals[-1]["end"]) if intervals else 0
    empty = {"status": "样本不足", "alpha": None, "beta": None,
             "intervals": len(intervals), "start": intervals[0]["start"] if intervals else None,
             "end": intervals[-1]["end"] if intervals else None}
    if len(intervals) < 10 or span < 30:
        return empty
    weight = sum(row["days"] for row in intervals)
    x_mean = sum(row["x"] * row["days"] for row in intervals) / weight
    y_mean = sum(row["y"] * row["days"] for row in intervals) / weight
    variance = sum(row["days"] * math.pow(row["x"] - x_mean, 2)
                   for row in intervals) / weight
    if variance <= 1e-18:
        return empty
    covariance = sum(row["days"] * (row["x"] - x_mean) * (row["y"] - y_mean)
                     for row in intervals) / weight
    beta = covariance / variance
    daily_free = math.log(1 + RISK_FREE_RATE) / 365
    alpha_daily = y_mean - (daily_free + beta * (x_mean - daily_free))
    return {"status": "ok", "alpha": (math.exp(alpha_daily * 365) - 1) * 100,
            "beta": beta, "intervals": len(intervals), "start": intervals[0]["start"],
            "end": intervals[-1]["end"]}


def holding_attribution(points, inception=False):
    series = ([{"holdings": []}] + list(points)) if inception else list(points)
    result = {}
    for previous, current in zip(series, series[1:]):
        old = {item.get("code"): item for item in previous.get("holdings", [])}
        new = {item.get("code"): item for item in current.get("holdings", [])}
        for code in set(old) | set(new):
            before, after = old.get(code), new.get(code)
            row = result.setdefault(code, {"code": code, "name": (after or before).get("name"),
                                                  "period_profit": 0, "uncertain": False,
                                                  "status": set(), "ending": None})
            if before and after:
                basis = min(before.get("quantity") or 0, after.get("quantity") or 0)
                row["period_profit"] += basis * ((after.get("price") or 0) - (before.get("price") or 0))
                changed = abs((before.get("quantity") or 0) - (after.get("quantity") or 0)) > max(
                    (before.get("quantity") or 0) * .0001, 1)
                row["status"].add("持仓变化，按可比份额估算" if changed else "持仓不变")
            elif after:
                gain = after.get("valuation_gain")
                if gain is None and after.get("cost") is not None:
                    gain = (after.get("market_value") or 0) - after["cost"]
                row["period_profit"] += gain or 0
                row["status"].add("区间新增，按当前浮盈亏")
            else:
                row["uncertain"] = True
                row["status"].add("区间退出，退出收益未归属")
            if after:
                row["ending"] = after
    output = []
    for row in result.values():
        ending = row.pop("ending")
        row["status"] = "；".join(sorted(row["status"]))
        row["current_gain"] = ending.get("valuation_gain") if ending else None
        row["market_value"] = ending.get("market_value", 0) if ending else 0
        row["quantity"] = ending.get("quantity", 0) if ending else 0
        output.append(row)
    return sorted(output, key=lambda item: -abs(item["period_profit"]))
