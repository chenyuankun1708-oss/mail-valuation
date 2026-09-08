from collections import defaultdict
from datetime import date

from .config import PRODUCTS


def _pct(value):
    return None if value is None else round(value * 100, 4)


def analyze(snapshots):
    grouped = defaultdict(list)
    for snapshot in snapshots:
        grouped[snapshot.product].append(snapshot)
    products = []
    for product in PRODUCTS:
        series = sorted(grouped.get(product, []), key=lambda x: x.valuation_date)
        products.append(_analyze_product(product, series))
    valid = [p for p in products if p["status"] == "ok"]
    return {
        "summary": {
            "configured_products": len(PRODUCTS),
            "products_with_data": sum(bool(grouped.get(name)) for name in PRODUCTS),
            "analyzable_products": len(valid),
            "latest_assets": round(sum(p["ending_assets"] for p in valid), 2),
            "total_profit": round(sum(p["profit"] for p in valid), 2),
        },
        "products": products,
    }


def analyze_month(snapshots, year, month):
    """使用月初前最后估值和月内最后估值计算指定自然月。"""
    from calendar import monthrange
    grouped = defaultdict(list)
    for snapshot in snapshots:
        grouped[snapshot.product].append(snapshot)
    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])
    products = []
    for name in PRODUCTS:
        series = sorted(grouped.get(name, []), key=lambda x: x.valuation_date)
        opening = [s for s in series if date.fromisoformat(s.valuation_date) < month_start]
        within = [s for s in series if month_start <= date.fromisoformat(s.valuation_date) <= month_end]
        if not opening or not within:
            products.append({"name": name, "status": "insufficient", "snapshot_count": len(within), "events": [], "points": [],
                             "message": "缺少月初基准估值表" if not opening else "缺少本月估值表"})
            continue
        baseline = opening[-1]
        baseline_gap = (month_start - date.fromisoformat(baseline.valuation_date)).days
        selected = [baseline] + within
        result = _analyze_product(name, selected)
        result["period"] = "%04d-%02d" % (year, month)
        if baseline_gap > 7:
            result.setdefault("warnings", []).append(
                "缺少6月底估值表，月初采用最接近的 %s；结果为该日至截止日的近似收益" % baseline.valuation_date)
        products.append(result)
    valid = [p for p in products if p["status"] == "ok"]
    return {"period": "%04d-%02d" % (year, month), "summary": {
        "configured_products": len(PRODUCTS), "products_with_data": len(valid), "analyzable_products": len(valid),
        "latest_assets": round(sum(p["ending_assets"] for p in valid), 2),
        "total_profit": round(sum(p["profit"] for p in valid), 2)}, "products": products}


def _analyze_product(name, series):
    base = {"name": name, "snapshot_count": len(series), "events": [], "points": []}
    if not series:
        return dict(base, status="missing", message="尚未找到估值表")
    if len(series) == 1:
        return dict(base, status="insufficient", message="至少需要两个估值日才能计算收益", latest=series[0].as_dict())

    events, flows, growth = [], [], 1.0
    warnings = []
    for previous, current in zip(series, series[1:]):
        nav_return = current.accumulated_nav / previous.accumulated_nav - 1
        growth *= 1 + nav_return
        delta_shares = current.shares - previous.shares
        share_threshold = max(previous.shares * 0.0001, 1.0)
        amount = delta_shares * current.nav
        if abs(delta_shares) > share_threshold:
            kind = "追加申购" if delta_shares > 0 else "部分赎回"
            confidence = "方向高、金额低"
            flows.append(amount)
            events.append({"date": current.valuation_date, "type": kind, "amount": round(abs(amount), 2), "confidence": confidence,
                           "basis": "两张估值表的总份额发生净变化；方向可判断，金额仅按份额净变化×后一估值日净值近似，交易日期和确认净值未知"})
        # 累计净值与单位净值差额扩大，通常是分红除权；只能标记候选。
        acc_gap_change = (current.accumulated_nav - current.nav) - (previous.accumulated_nav - previous.nav)
        if acc_gap_change > max(current.nav * 0.0001, 0.0001):
            dividend = acc_gap_change * current.shares
            flows.append(-dividend)
            events.append({"date": current.valuation_date, "type": "分红（候选）", "amount": round(dividend, 2), "confidence": "中",
                           "basis": "累计单位净值与单位净值的差额扩大；需用分红公告或银行流水确认"})
        if (date.fromisoformat(current.valuation_date) - date.fromisoformat(previous.valuation_date)).days > 45:
            warnings.append("估值表间隔超过45天，期间多笔现金流可能被合并为净额")

    start, end = series[0], series[-1]
    subscriptions = sum(value for value in flows if value > 0)
    withdrawals = -sum(value for value in flows if value < 0)
    profit = end.net_assets + withdrawals - subscriptions - start.net_assets
    # Modified Dietz：现金流日期只有估值日，仍比简单收益/本金更可解释。
    total_days = max((date.fromisoformat(end.valuation_date) - date.fromisoformat(start.valuation_date)).days, 1)
    weighted_flows = 0.0
    for event, value in zip([e for e in events if "basis" in e], flows):
        remaining = (date.fromisoformat(end.valuation_date) - date.fromisoformat(event["date"])).days
        weighted_flows += value * remaining / total_days
    denominator = start.net_assets + weighted_flows
    dietz = profit / denominator if denominator else None
    nav_profit = start.net_assets * (growth - 1)
    if abs(profit - nav_profit) > max(start.net_assets * 0.005, 1000):
        warnings.append("金额收益与披露净值收益存在差异，通常由单位净值精度或区间内未识别现金流造成，建议核对流水")
    points = [{"date": s.valuation_date, "nav": s.nav, "accumulated_nav": s.accumulated_nav,
               "net_assets": round(s.net_assets, 2), "shares": round(s.shares, 4)} for s in series]
    return dict(base, status="ok", start_date=start.valuation_date, end_date=end.valuation_date,
                starting_assets=round(start.net_assets, 2), ending_assets=round(end.net_assets, 2),
                subscriptions=round(subscriptions, 2), withdrawals_and_dividends=round(withdrawals, 2),
                profit=round(profit, 2), time_weighted_return_pct=_pct(growth - 1),
                modified_dietz_return_pct=_pct(dietz), events=events, points=points,
                warnings=sorted(set(warnings)))
