"""Isolated OTC pricing research primitives.

This module is deliberately not imported by the web server.  A pricing run is
accepted only with a versioned, self-contained market snapshot.  Outputs are
model estimates, never settlement values, accounting fair values or quotes.
"""

from __future__ import division

import hashlib
import json
import math
import os
import tempfile
import calendar
from bisect import bisect_left, bisect_right
from datetime import datetime

import numpy as np
from scipy.stats import norm, qmc

PRICING_MODEL_VERSION = "local-vol-rqmc-research-v1"
SUPPORTED_UNDERLYINGS = {"000852": "IM", "000905": "IC", "000300": "IF"}
PARAMETRIC_MODEL_VERSION = "constant-vol-gbm-relative-v1"
PARAMETRIC_STRUCTURE = "classic_snowball"
PARAMETRIC_BATCH_COUNT = 8
PARAMETRIC_PATHS_PER_BATCH = 4096
PARAMETRIC_SEED = 20260924


class PricingInputError(ValueError):
    pass


def black_scholes(spot, strike, maturity, rate, dividend, volatility, call=True):
    """Black-Scholes vanilla value used only for calibration regression tests."""
    values = [spot, strike, maturity, volatility]
    if any(float(value) <= 0 for value in values):
        raise PricingInputError("spot、strike、maturity和volatility必须为正数")
    root_t = math.sqrt(maturity)
    d1 = (math.log(spot / strike) + (rate - dividend + .5 * volatility ** 2) * maturity) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    if call:
        return spot * math.exp(-dividend * maturity) * norm.cdf(d1) - strike * math.exp(-rate * maturity) * norm.cdf(d2)
    return strike * math.exp(-rate * maturity) * norm.cdf(-d2) - spot * math.exp(-dividend * maturity) * norm.cdf(-d1)


def snapshot_hash(snapshot):
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def save_market_snapshot(root, snapshot):
    """Persist one immutable content-addressed snapshot below the OTC runtime root."""
    validation = validate_market_snapshot(snapshot)
    digest = validation["snapshot_sha256"]
    directory = os.path.join(os.path.abspath(root), "pricing_snapshots")
    path = os.path.join(directory, "%s.json" % digest)
    os.makedirs(directory, exist_ok=True)
    if os.path.isfile(path):
        return path, validation
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".snapshot-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(snapshot, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)
    return path, validation


def load_market_snapshot(root, digest):
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise PricingInputError("市场快照哈希无效")
    base = os.path.join(os.path.abspath(root), "pricing_snapshots")
    path = os.path.realpath(os.path.join(base, "%s.json" % digest))
    if os.path.commonpath((os.path.realpath(base), path)) != os.path.realpath(base):
        raise PricingInputError("市场快照路径越界")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, ValueError):
        raise PricingInputError("市场快照不存在或损坏")
    validation = validate_market_snapshot(snapshot)
    if validation["snapshot_sha256"] != digest:
        raise PricingInputError("市场快照哈希校验失败")
    return snapshot, validation


def validate_market_snapshot(snapshot):
    """Validate the minimum reproducible full-chain research snapshot."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise PricingInputError("市场快照schema_version必须为1")
    underlying = str(snapshot.get("underlying") or "")
    if underlying not in SUPPORTED_UNDERLYINGS:
        raise PricingInputError("标的不在固定研究清单")
    try:
        datetime.strptime(str(snapshot.get("as_of")), "%Y-%m-%d")
        spot = float(snapshot["spot"])
    except (KeyError, TypeError, ValueError):
        raise PricingInputError("市场快照缺少有效日期或现货点位")
    if spot <= 0:
        raise PricingInputError("现货点位必须为正数")
    curves = ("discount_curve", "forward_curve")
    for name in curves:
        rows = snapshot.get(name) or []
        if len(rows) < 2:
            raise PricingInputError("%s至少需要两个期限点" % name)
        tenors = [float(row["tenor_years"]) for row in rows]
        if tenors != sorted(tenors) or len(set(tenors)) != len(tenors) or min(tenors) <= 0:
            raise PricingInputError("%s期限必须严格递增且为正" % name)
    quotes = snapshot.get("option_quotes") or []
    if len(quotes) < 12:
        raise PricingInputError("完整期权链至少需要12条双边报价")
    expiries = set(); strikes = set(); violations = []
    calls = {}
    total_variance = {}
    for row in quotes:
        expiry = float(row["tenor_years"]); strike = float(row["strike"])
        bid = float(row["bid"]); ask = float(row["ask"]); iv = float(row["implied_vol"])
        option_type = str(row.get("call_put") or "").upper()
        if expiry <= 0 or strike <= 0 or bid < 0 or ask < bid or iv <= 0 or option_type not in ("C", "P"):
            violations.append("invalid_quote")
        expiries.add(expiry); strikes.add(strike)
        if option_type == "C":
            calls.setdefault(expiry, []).append((strike, (bid + ask) / 2.0))
            total_variance.setdefault(strike, []).append((expiry, iv * iv * expiry))
    if len(expiries) < 2 or len(strikes) < 3:
        raise PricingInputError("期权链至少覆盖两个期限和三个执行价")
    for expiry, rows in calls.items():
        ordered = sorted(rows)
        prices = [item[1] for item in ordered]
        if any(prices[index] < prices[index + 1] - 1e-10 for index in range(len(prices) - 1)):
            violations.append("call_monotonicity:%s" % expiry)
        slopes = [(prices[index + 1] - prices[index]) / (ordered[index + 1][0] - ordered[index][0])
                  for index in range(len(prices) - 1)]
        if any(slopes[index] > slopes[index + 1] + 1e-10 for index in range(len(slopes) - 1)):
            violations.append("call_convexity:%s" % expiry)
    for strike, rows in total_variance.items():
        ordered = sorted(rows)
        if any(ordered[index][1] > ordered[index + 1][1] + 1e-10 for index in range(len(ordered) - 1)):
            violations.append("calendar_total_variance:%s" % strike)
    grid = snapshot.get("local_vol_grid") or {}
    if len(grid.get("times") or []) < 2 or len(grid.get("moneyness") or []) < 3:
        raise PricingInputError("缺少经无套利清洗生成的局部波动率网格")
    values = np.asarray(grid.get("values"), dtype=float)
    if values.shape != (len(grid["times"]), len(grid["moneyness"])) or not np.all(np.isfinite(values)) or np.min(values) <= 0:
        raise PricingInputError("局部波动率网格形状或数值无效")
    if not snapshot.get("source_hashes"):
        raise PricingInputError("市场快照缺少来源哈希")
    calendar_dates = snapshot.get("trading_calendar") or []
    try:
        parsed_calendar = [datetime.strptime(str(value), "%Y-%m-%d") for value in calendar_dates]
    except (TypeError, ValueError):
        raise PricingInputError("交易日历必须为ISO日期数组")
    if len(parsed_calendar) < 50 or parsed_calendar != sorted(set(parsed_calendar)):
        raise PricingInputError("交易日历必须严格递增且覆盖定价期限")
    return {"snapshot_sha256": snapshot_hash(snapshot), "static_arbitrage_violations": sorted(set(violations)),
            "option_quote_count": len(quotes), "expiry_count": len(expiries), "strike_count": len(strikes)}


def _curve(rows, key, maturity):
    x = np.asarray([float(row["tenor_years"]) for row in rows])
    y = np.asarray([float(row[key]) for row in rows])
    return float(np.interp(maturity, x, y, left=y[0], right=y[-1]))


def _local_vol(snapshot, time_value, moneyness):
    grid = snapshot["local_vol_grid"]
    times = np.asarray(grid["times"], dtype=float)
    money = np.asarray(grid["moneyness"], dtype=float)
    values = np.asarray(grid["values"], dtype=float)
    at_money = np.asarray([np.interp(moneyness, money, row, left=row[0], right=row[-1]) for row in values])
    return float(np.interp(time_value, times, at_money, left=at_money[0], right=at_money[-1]))


def _local_vol_vector(snapshot, time_value, moneyness):
    grid = snapshot["local_vol_grid"]
    times = np.asarray(grid["times"], dtype=float)
    money = np.asarray(grid["moneyness"], dtype=float)
    values = np.asarray(grid["values"], dtype=float)
    upper = int(np.searchsorted(times, time_value, side="right"))
    if upper <= 0:
        surface = values[0]
    elif upper >= len(times):
        surface = values[-1]
    else:
        weight = (time_value - times[upper - 1]) / (times[upper] - times[upper - 1])
        surface = values[upper - 1] * (1 - weight) + values[upper] * weight
    return np.interp(moneyness, money, surface, left=surface[0], right=surface[-1])


def _sobol_normals(path_count, dimensions, seed):
    power = int(math.ceil(math.log(max(path_count // 2, 2), 2)))
    engine = qmc.Sobol(d=dimensions, scramble=True, seed=int(seed))
    uniforms = engine.random_base2(power)[:max(path_count // 2, 1)]
    uniforms = np.clip(uniforms, 1e-12, 1 - 1e-12)
    normals = norm.ppf(uniforms)
    return np.vstack((normals, -normals))[:path_count]


def _add_months(day, months):
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return day.replace(year=year, month=month,
                       day=min(day.day, calendar.monthrange(year, month)[1]))


def price_classic_snowball(snapshot, terms, path_count=32768, seed=20260924):
    """Research PV for a classic snowball using daily KI and monthly KO.

    Cashflows are discounted at their event time. Fair coupon is solved using
    common paths, so the root comparison is not polluted by resampling noise.
    """
    market = validate_market_snapshot(snapshot)
    if market["static_arbitrage_violations"]:
        raise PricingInputError("期权链存在静态套利，拒绝定价")
    months = int(terms.get("term_months") or 0)
    if months < 1 or months > 60:
        raise PricingInputError("期限必须为1至60个月")
    lock = int(terms.get("lock_period_months") or 0)
    if lock < 1 or lock > months:
        raise PricingInputError("锁定期无效")
    valuation_date = datetime.strptime(snapshot["as_of"], "%Y-%m-%d")
    maturity_contract = _add_months(valuation_date, months)
    full_calendar = [datetime.strptime(value, "%Y-%m-%d") for value in snapshot["trading_calendar"]]
    start_index = bisect_right(full_calendar, valuation_date)
    maturity_index = bisect_left(full_calendar, maturity_contract)
    if maturity_index >= len(full_calendar):
        raise PricingInputError("交易日历未覆盖顺延后的到期日")
    dates = full_calendar[start_index:maturity_index + 1]
    if not dates or dates[0] <= valuation_date:
        raise PricingInputError("交易日历无法形成有效定价区间")
    maturity = (dates[-1] - valuation_date).days / 365.0
    steps = len(dates)
    normals = _sobol_normals(path_count, steps, seed)
    spot = float(snapshot["spot"])
    rate = _curve(snapshot["discount_curve"], "zero_rate", maturity)
    forward = _curve(snapshot["forward_curve"], "forward", maturity)
    dividend = rate - math.log(forward / spot) / maturity
    prices = np.full(path_count, spot, dtype=float)
    minimum = prices.copy()
    observations = {}
    for month in range(lock, months + 1):
        contract_date = _add_months(valuation_date, month)
        position = bisect_left(dates, contract_date)
        if position < len(dates):
            observations[position + 1] = month
    exit_step = np.full(path_count, steps, dtype=int)
    knocked_out = np.zeros(path_count, dtype=bool)
    active = np.ones(path_count, dtype=bool)
    elapsed = []
    previous = valuation_date
    elapsed_years = 0.0
    for step, day in enumerate(dates, 1):
        dt = max((day - previous).days / 365.0, 1.0 / 365.0)
        elapsed_years += dt; elapsed.append(elapsed_years); previous = day
        sigma = _local_vol_vector(snapshot, elapsed_years, prices / spot)
        prices *= np.exp((rate - dividend - .5 * sigma ** 2) * dt + sigma * math.sqrt(dt) * normals[:, step - 1])
        minimum = np.minimum(minimum, prices)
        month = observations.get(step)
        if month is not None:
            barrier = float(terms["knock_out_initial"]) - (month - lock) * float(terms["knock_out_decrease_monthly"])
            hit = active & (prices / spot > barrier) & ~np.isclose(prices / spot, barrier)
            knocked_out[hit] = True; exit_step[hit] = step; active[hit] = False
    knocked_in = minimum / spot <= float(terms["knock_in_ratio"])
    elapsed = np.asarray(elapsed, dtype=float)
    years = elapsed[np.maximum(exit_step - 1, 0)]
    terminal_return = prices / spot - 1.0
    max_loss = terms.get("max_loss")

    switch_year = float(terms.get("coupon_switch_months", months)) / 12.0

    def payoff(coupon, second_coupon=None):
        late_coupon = coupon if second_coupon is None else second_coupon
        coupons = np.where(years <= switch_year, coupon, late_coupon)
        values = np.where(knocked_out | ~knocked_in, coupons * years, terminal_return)
        if max_loss is not None:
            values = np.maximum(values, -float(max_loss))
        return values

    discounts = np.exp(-rate * years)
    base = payoff(float(terms.get("coupon", terms.get("first_coupon", 0.0))),
                  float(terms.get("second_coupon", terms.get("coupon", terms.get("first_coupon", 0.0)))))
    pv_paths = discounts * base
    pv = float(np.mean(pv_paths)); stderr = float(np.std(pv_paths, ddof=1) / math.sqrt(path_count))
    lower = 0.0; upper = .80
    for _ in range(40):
        midpoint = (lower + upper) / 2.0
        candidate = float(np.mean(discounts * payoff(midpoint)))
        if candidate > 0:
            upper = midpoint
        else:
            lower = midpoint
        if upper - lower <= .0001:
            break
    return {
        "status": "research_only", "eligible_for_web": False,
        "model_version": PRICING_MODEL_VERSION, "snapshot_sha256": market["snapshot_sha256"],
        "present_value_ratio": pv, "fair_coupon": (lower + upper) / 2.0,
        "issuance_spread_ratio": -pv, "standard_error_ratio": stderr,
        "confidence_interval_95": [pv - 1.96 * stderr, pv + 1.96 * stderr],
        "path_count": path_count, "daily_steps": steps, "seed": seed,
        "disclaimer": "模型估值，不是正式结算价、会计公允价值或交易报价",
    }


def evaluate_release_gates(calibration_rmse, barrier_error_ratio, doubled_pv_change,
                           doubled_coupon_change, standard_error_ratio, static_violations):
    checks = {
        "static_arbitrage_free": not static_violations,
        "vanilla_calibration_rmse_le_1vol": calibration_rmse <= .01,
        "barrier_benchmark_error_le_0_10pct": barrier_error_ratio <= .001,
        "pv_convergence_le_0_10pct": doubled_pv_change <= .001,
        "coupon_convergence_le_5bp": doubled_coupon_change <= .0005,
        "mc_standard_error_le_0_05pct": standard_error_ratio <= .0005,
    }
    return {"checks": checks, "passed": all(checks.values()),
            "eligible_for_web": all(checks.values())}


# The functions below implement the deliberately simpler, user-assumption-driven
# web research model.  The snapshot/local-vol functions above remain available
# for the later scientific calibration work and its regression tests.

def pricing_parameter_definitions():
    return [
        {"name": "term_months", "label": "期限（月）", "type": "integer", "min": 1, "max": 60, "default": 24},
        {"name": "lock_period_months", "label": "敲出锁定期（月）", "type": "integer", "min": 1, "max_from": "term_months", "default": 3},
        {"name": "knock_in_ratio", "label": "敲入比例", "type": "number", "min": .30, "max": .99, "step": .01, "default": .70},
        {"name": "knock_out_initial", "label": "初始敲出比例", "type": "number", "min": .80, "max": 1.20, "step": .01, "default": 1.00},
        {"name": "knock_out_decrease_monthly", "label": "每月降敲", "type": "number", "min": 0, "max": .05, "step": .001, "default": .005},
        {"name": "first_coupon", "label": "前段年化票息", "type": "number", "min": 0, "max": .50, "step": .001, "default": .12},
        {"name": "second_coupon", "label": "后段年化票息", "type": "number", "min": 0, "max": .50, "step": .001, "default": .12},
        {"name": "coupon_switch_months", "label": "票息切换月", "type": "integer", "min": 1, "max_from": "term_months", "default": 12},
        {"name": "max_loss", "label": "最大亏损（可空）", "type": "number", "min": .01, "max": 1, "step": .01, "default": None},
        {"name": "volatility", "label": "年化波动率", "type": "number", "min": .01, "max": 1, "step": .001, "default": .20},
        {"name": "basis_rate", "label": "年化贴水率", "type": "number", "min": -.50, "max": .50, "step": .001, "default": 0},
        {"name": "discount_rate", "label": "年化贴现率", "type": "number", "min": -.05, "max": .20, "step": .001, "default": .02},
        {"name": "notional", "label": "名义本金（可空）", "type": "number", "min_exclusive": 0, "default": None},
    ]


def _plain_name(value):
    value = str(value or "").strip()
    if len(value) > 120 or any(char in value for char in ("/", "\\", "\x00")):
        raise PricingInputError("产品名称最长120字，且不能包含路径字符")
    return value


def validate_parametric_request(payload, allow_zero_volatility=False):
    if not isinstance(payload, dict):
        raise PricingInputError("定价请求必须为对象")
    allowed = {"structure", "index_code", "product_name", "product_id", "product_revision"}
    allowed.update(item["name"] for item in pricing_parameter_definitions())
    unknown = set(payload) - allowed
    if unknown:
        raise PricingInputError("存在未登记参数：%s" % ", ".join(sorted(unknown)))
    if payload.get("structure", PARAMETRIC_STRUCTURE) != PARAMETRIC_STRUCTURE:
        raise PricingInputError("第一版定价研究只支持经典雪球")
    index_code = str(payload.get("index_code") or "")
    if index_code not in SUPPORTED_UNDERLYINGS:
        raise PricingInputError("标的不在固定指数清单")
    defaults = {item["name"]: item.get("default") for item in pricing_parameter_definitions()}
    values = dict(defaults)
    values.update({key: value for key, value in payload.items() if key in defaults})
    integer_fields = ("term_months", "lock_period_months", "coupon_switch_months")
    for key in integer_fields:
        try:
            value = int(values[key])
        except (TypeError, ValueError):
            raise PricingInputError("%s必须为整数" % key)
        if str(values[key]).strip() not in (str(value), "%s.0" % value):
            raise PricingInputError("%s必须为整数" % key)
        values[key] = value
    ranges = {
        "term_months": (1, 60), "knock_in_ratio": (.30, .99),
        "knock_out_initial": (.80, 1.20), "knock_out_decrease_monthly": (0, .05),
        "first_coupon": (0, .50), "second_coupon": (0, .50),
        "volatility": (0 if allow_zero_volatility else .01, 1), "basis_rate": (-.50, .50),
        "discount_rate": (-.05, .20),
    }
    for key, limits in ranges.items():
        try:
            values[key] = float(values[key])
        except (TypeError, ValueError):
            raise PricingInputError("%s必须为有效数值" % key)
        if not math.isfinite(values[key]) or not limits[0] <= values[key] <= limits[1]:
            raise PricingInputError("%s超出允许范围" % key)
    months = values["term_months"]
    if not 1 <= values["lock_period_months"] <= months:
        raise PricingInputError("敲出锁定期必须介于1和期限之间")
    if not 1 <= values["coupon_switch_months"] <= months:
        raise PricingInputError("票息切换月必须介于1和期限之间")
    for key in ("max_loss", "notional"):
        raw = values.get(key)
        if raw in (None, ""):
            values[key] = None
            continue
        try:
            values[key] = float(raw)
        except (TypeError, ValueError):
            raise PricingInputError("%s必须为有效数值或留空" % key)
        if not math.isfinite(values[key]):
            raise PricingInputError("%s必须为有限数值" % key)
    if values["max_loss"] is not None and not .01 <= values["max_loss"] <= 1:
        raise PricingInputError("最大亏损必须介于0.01和1之间")
    if values["notional"] is not None and values["notional"] <= 0:
        raise PricingInputError("名义本金必须为正数")
    request = {"structure": PARAMETRIC_STRUCTURE, "index_code": index_code,
               "product_name": _plain_name(payload.get("product_name"))}
    request.update(values)
    if payload.get("product_id") is not None:
        request["product_id"] = str(payload.get("product_id"))
        try:
            request["product_revision"] = int(payload.get("product_revision"))
        except (TypeError, ValueError):
            raise PricingInputError("产品修订号无效")
    return request


def _parametric_normals(path_count, dimensions, seed):
    half = max(int(path_count) // 2, 1)
    power = int(math.ceil(math.log(half, 2)))
    engine = qmc.Sobol(d=int(dimensions), scramble=True, seed=int(seed))
    uniforms = np.clip(engine.random_base2(power)[:half], 1e-12, 1 - 1e-12)
    values = norm.ppf(uniforms)
    return np.vstack((values, -values))[:int(path_count)]


def _cashflows_from_outcomes(request, knocked_out, knocked_in, exit_month,
                             exit_time, terminal_ratio, first_coupon, second_coupon):
    switch = int(request["coupon_switch_months"])
    coupon = np.where(exit_month <= switch, first_coupon, second_coupon)
    cash = np.ones(len(exit_time), dtype=float)
    safe = knocked_out | ~knocked_in
    cash[safe] += coupon[safe] * exit_time[safe]
    loss = np.minimum(terminal_ratio - 1.0, 0.0)
    if request.get("max_loss") is not None:
        loss = np.maximum(loss, -float(request["max_loss"]))
    cash[~safe] += loss[~safe]
    return cash


def run_parametric_pricing(payload, batch_count=PARAMETRIC_BATCH_COUNT,
                           paths_per_batch=PARAMETRIC_PATHS_PER_BATCH,
                           seed=PARAMETRIC_SEED, allow_zero_volatility=False):
    """Price a new-issuance classic snowball under a relative-time GBM.

    Batch estimates, rather than individual quasi-random paths, are the
    independent observations used for the reported standard error.
    """
    request = validate_parametric_request(payload, allow_zero_volatility)
    batch_count = int(batch_count); paths_per_batch = int(paths_per_batch)
    if batch_count < 2 or paths_per_batch < 2:
        raise PricingInputError("至少需要2个独立批次且每批至少2条路径")
    months = int(request["term_months"])
    steps = int(math.ceil(months * 365.0 / 12.0))
    dt = 1.0 / 365.0
    observations = {}
    for month in range(int(request["lock_period_months"]), months + 1):
        observations[min(steps, max(1, int(round(month * 365.0 / 12.0))))] = month
    batch_values = []
    all_ko = []; all_ki = []; all_months = []; all_times = []
    all_terminal = []; all_discounted = []
    sigma = float(request["volatility"]); basis = float(request["basis_rate"])
    discount_rate = float(request["discount_rate"])
    for batch in range(batch_count):
        normals = _parametric_normals(paths_per_batch, steps, seed + batch)
        price = np.ones(paths_per_batch, dtype=float)
        minimum = price.copy(); active = np.ones(paths_per_batch, dtype=bool)
        knocked_out = np.zeros(paths_per_batch, dtype=bool)
        exit_month = np.full(paths_per_batch, months, dtype=int)
        exit_time = np.full(paths_per_batch, months / 12.0, dtype=float)
        drift = (-basis - .5 * sigma * sigma) * dt
        diffusion = sigma * math.sqrt(dt)
        for step in range(1, steps + 1):
            price *= np.exp(drift + diffusion * normals[:, step - 1])
            minimum[active] = np.minimum(minimum[active], price[active])
            month = observations.get(step)
            if month is not None:
                barrier = (float(request["knock_out_initial"]) -
                           (month - int(request["lock_period_months"])) *
                           float(request["knock_out_decrease_monthly"]))
                hit = active & (price > barrier)
                knocked_out[hit] = True; active[hit] = False
                exit_month[hit] = month; exit_time[hit] = month / 12.0
        knocked_in = minimum <= float(request["knock_in_ratio"])
        cash = _cashflows_from_outcomes(
            request, knocked_out, knocked_in, exit_month, exit_time, price,
            float(request["first_coupon"]), float(request["second_coupon"]))
        discounted = np.exp(-discount_rate * exit_time) * cash
        batch_values.append(float(np.mean(discounted)))
        all_ko.append(knocked_out); all_ki.append(knocked_in); all_months.append(exit_month)
        all_times.append(exit_time); all_terminal.append(price); all_discounted.append(discounted)
    knocked_out = np.concatenate(all_ko); knocked_in = np.concatenate(all_ki)
    exit_month = np.concatenate(all_months); exit_time = np.concatenate(all_times)
    terminal_ratio = np.concatenate(all_terminal); discounted = np.concatenate(all_discounted)
    batch_values_array = np.asarray(batch_values, dtype=float)
    value = float(np.mean(batch_values_array))
    standard_error = float(np.std(batch_values_array, ddof=1) / math.sqrt(batch_count))

    def value_for(first_coupon, second_coupon):
        cash = _cashflows_from_outcomes(request, knocked_out, knocked_in, exit_month,
                                       exit_time, terminal_ratio, first_coupon, second_coupon)
        return float(np.mean(np.exp(-discount_rate * exit_time) * cash))

    first = float(request["first_coupon"]); second = float(request["second_coupon"])
    flat = first == 0 and second == 0
    low = 0.0; high = 1.0 if flat else 10.0
    target_high = value_for(high, high) if flat else value_for(first * high, second * high)
    fair_available = target_high >= 1.0
    if fair_available:
        for _ in range(60):
            middle = (low + high) / 2.0
            candidate = value_for(middle, middle) if flat else value_for(first * middle, second * middle)
            if candidate >= 1.0:
                high = middle
            else:
                low = middle
            if high - low <= .0001:
                break
        fair_value = (low + high) / 2.0
    else:
        fair_value = None
    unique_months, month_counts = np.unique(exit_month, return_counts=True)
    hist_counts, hist_edges = np.histogram((discounted - 1.0) * 100.0, bins=20)
    cumulative = [float(np.mean(batch_values_array[:index + 1]) * 100.0)
                  for index in range(batch_count)]
    loss_at_maturity = (~knocked_out) & knocked_in & (terminal_ratio < 1.0)
    per100 = value * 100.0
    notional = request.get("notional")
    result = {
        "status": "completed", "research_only": True,
        "model_version": PARAMETRIC_MODEL_VERSION,
        "model_name": "常数波动率风险中性GBM相对时间模型",
        "request": request, "seed": int(seed), "batch_count": batch_count,
        "paths_per_batch": paths_per_batch, "path_count": batch_count * paths_per_batch,
        "daily_steps": steps,
        "value_per_100": per100,
        "valuation_amount": None if notional is None else value * float(notional),
        "par_spread_per_100": per100 - 100.0,
        "issuer_liability_per_100": -per100,
        "standard_error_per_100": standard_error * 100.0,
        "confidence_interval_95_per_100": [
            (value - 1.96 * standard_error) * 100.0,
            (value + 1.96 * standard_error) * 100.0],
        "fair_coupon": {
            "available": fair_available,
            "mode": "flat_coupon" if flat else "common_multiplier",
            "multiplier": None if flat else fair_value,
            "first_coupon": fair_value if flat else (None if fair_value is None else first * fair_value),
            "second_coupon": fair_value if flat else (None if fair_value is None else second * fair_value),
            "tolerance": .0001,
        },
        "probabilities": {
            "knock_in": float(np.mean(knocked_in)),
            "knock_out": float(np.mean(knocked_out)),
            "maturity_loss": float(np.mean(loss_at_maturity)),
        },
        "average_life_years": float(np.mean(exit_time)),
        "termination_distribution": [{"month": int(month), "count": int(count),
                                      "probability": float(count / len(exit_month))}
                                     for month, count in zip(unique_months, month_counts)],
        "return_distribution": [{"lower": float(hist_edges[index]),
                                 "upper": float(hist_edges[index + 1]),
                                 "count": int(hist_counts[index])}
                                for index in range(len(hist_counts))],
        "batch_convergence": [{"batch": index + 1,
                               "value_per_100": float(batch_values_array[index] * 100.0),
                               "cumulative_value_per_100": cumulative[index]}
                              for index in range(batch_count)],
        "assumptions": {"spot": 1.0, "drift": "-basis_rate", "time_step": "1/365",
                        "observation": "m/12", "knock_out_comparison": "strict_greater_than",
                        "discounting": "all_principal_and_payoff_cashflows"},
        "disclaimer": "用户假设驱动的参数化理论定价研究，不是市场公允价值、交易报价、会计估值或结算价。",
    }
    return result


def pricing_reference_data(index_path, risk_path):
    """Return optional local historical references without internal identifiers."""
    response = {"indices": [], "discount_rate": {"available": False}}
    try:
        with open(index_path, "r", encoding="utf-8") as handle:
            index_data = json.load(handle)
    except (OSError, ValueError):
        index_data = {}
    try:
        with open(risk_path, "r", encoding="utf-8") as handle:
            risk_data = json.load(handle)
    except (OSError, ValueError):
        risk_data = {}
    future_map = {"000852": "IM", "000905": "IC", "000300": "IF"}
    for code in ("000852", "000905", "000300"):
        item = (index_data.get("indices") or {}).get(code) or {}
        points = [point for point in (item.get("points") or []) if float(point.get("close") or 0) > 0]
        closes = np.asarray([float(point["close"]) for point in points], dtype=float)
        vols = {}
        for window in (20, 60, 120, 250):
            if len(closes) >= window + 1:
                returns = np.diff(np.log(closes[-(window + 1):]))
                vols[str(window)] = float(np.std(returns, ddof=1) * math.sqrt(252.0))
            else:
                vols[str(window)] = None
        future = (risk_data.get("futures") or {}).get(future_map[code]) or {}
        future_points = future.get("points") or []
        tenors = []
        for tenor in ("当月", "下月", "当季", "下季"):
            series = []
            for point in future_points:
                value = (point.get(tenor) or {}).get("value")
                if value is not None:
                    series.append((point.get("date"), float(value) / 100.0))
            tenors.append({"tenor": tenor, "available": bool(series),
                           "as_of": series[-1][0] if series else None,
                           "latest": series[-1][1] if series else None,
                           "mean_20": float(np.mean([row[1] for row in series[-20:]])) if series else None,
                           "mean_60": float(np.mean([row[1] for row in series[-60:]])) if series else None})
        response["indices"].append({"code": code, "name": item.get("name") or code,
                                    "as_of": points[-1].get("date") if points else None,
                                    "volatility": vols, "basis": tenors})
    yields = ((risk_data.get("yield_curves") or {}).get("CN10Y") or {}).get("points") or []
    if yields:
        response["discount_rate"] = {"available": True, "as_of": yields[-1].get("date"),
                                     "value": float(yields[-1].get("yield"))}
    return response
