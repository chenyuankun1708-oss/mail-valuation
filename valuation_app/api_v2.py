"""Bounded version-two API projections over generated local data."""
from __future__ import unicode_literals

import datetime as dt
import json
import os

from .calculations import alpha_beta, holding_attribution, risk_metrics, valuation_boundaries, xirr
from .config import TOP_PRODUCT_IDS, TOP_PRODUCTS_BY_ID


API_VERSION = "2.0"
BENCHMARKS = ("000852", "000905", "000300", "932000")
SCOPES = {
    "all": set(TOP_PRODUCTS_BY_ID),
    "fof1": set(TOP_PRODUCTS_BY_ID) - {"top-006", "top-009", "top-016", "top-017"},
    "fof2": {"top-006", "top-016", "top-017"},
    "other": {"top-009"},
}
FILTER_FIELDS = ("primary", "secondary", "vehicle", "department")
MARKET_MODULES = {
    "macro": ("macro",), "industry": ("industry_indices",),
    "style": ("style_factor_returns",), "futures": ("sentiment", "arbitrage"),
    "options": ("option_surfaces",), "etf": ("etf_mapping", "allocation"),
}


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def iso_date(value, field):
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError("%s must be an ISO date" % field)
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("%s must be an ISO date" % field)
    return value


def query_one(query, allowed):
    if set(query) - set(allowed) or any(len(values) != 1 for values in query.values()):
        raise ValueError("unsupported or repeated query parameter")
    return {key: values[0] for key, values in query.items()}


def envelope(page, data, status="current", sources=0, warnings=None):
    warnings = list(warnings or [])
    return {"version": API_VERSION, "generated_at": page.get("page_updated_at"),
            "data_cutoff": page.get("default_end"), "source_count": int(sources),
            "status": status, "warning_count": len(warnings), "warnings": warnings,
            "data": data}


def bootstrap(page, module_status=None):
    products = []
    for item in page.get("products", []):
        points = item.get("points", [])
        products.append({"product_id": item.get("product_id") or TOP_PRODUCT_IDS.get(item.get("name")),
                         "name": item.get("name"), "first_date": points[0]["valuation_date"] if points else None,
                         "last_date": points[-1]["valuation_date"] if points else None,
                         "available": len(points) >= 2})
    navigation = ["home", "overview", "core", "returns", "bottom-returns", "market", "labels",
                  "strategy", "underlying", "barra", "attribution", "knowledge", "lab", "risk", "data"]
    data = {"navigation": navigation, "products": products,
            "default_start": page.get("default_start"), "default_end": page.get("default_end"),
            "scopes": list(SCOPES), "benchmarks": list(BENCHMARKS),
            "modules": module_status or {}, "legacy_data_url": "/api/page-data"}
    warnings = list(page.get("parse_errors", [])) + list(page.get("ledger_errors", []))
    return envelope(page, data, sources=len(products), warnings=warnings[:50])


def _dividends(points):
    output = []
    for previous, current in zip(points, points[1:]):
        gap = ((current.get("accumulated_nav") or 0) - (current.get("nav") or 0) -
               ((previous.get("accumulated_nav") or 0) - (previous.get("nav") or 0)))
        if gap > max((current.get("nav") or 0) * .0001, .0001):
            output.append({"date": current["valuation_date"], "per_share": gap,
                           "amount": gap * (previous.get("shares") or 0),
                           "status": "估值表推定，待公告或流水确认"})
    return output


def analyze_top_product(product, start, end, benchmark_points):
    points_all = product.get("points", [])
    last = max((point for point in points_all if point["valuation_date"] <= end),
               key=lambda item: item["valuation_date"], default=None)
    prior = max((point for point in points_all if point["valuation_date"] <= start),
                key=lambda item: item["valuation_date"], default=None)
    dated = sorted((flow for flow in product.get("flows", []) if flow.get("flow_date")),
                   key=lambda item: item["flow_date"])
    first_investment = next((flow for flow in dated if (flow.get("amount") or 0) > 0), None)
    ledger_net = sum(flow.get("amount") or 0 for flow in dated)
    tolerance = max(1, abs(product.get("total_investment") or 0) * .000001)
    reconciled = product.get("total_investment") is not None and abs(
        ledger_net - product["total_investment"]) <= tolerance
    inception = prior is None
    if not last:
        return {"product_id": product["product_id"], "name": product["name"],
                "status": "insufficient", "message": "结束日期之前没有估值表"}
    if prior and last["valuation_date"] <= prior["valuation_date"]:
        return {"product_id": product["product_id"], "name": product["name"],
                "status": "insufficient", "message": "区间内不足两个不同估值日"}
    if not reconciled or (inception and not first_investment):
        return {"product_id": product["product_id"], "name": product["name"],
                "status": "insufficient", "message": "资金台账边界不完整"}
    first = prior or {"valuation_date": first_investment["flow_date"], "net_assets": 0}
    points = [point for point in points_all if first["valuation_date"] <= point["valuation_date"] <= last["valuation_date"]]
    included = [flow for flow in dated if
                ((flow["flow_date"] >= first["valuation_date"]) if inception else
                 (flow["flow_date"] > first["valuation_date"])) and
                flow["flow_date"] <= last["valuation_date"]]
    dividends = _dividends(points)
    net = sum(flow.get("amount") or 0 for flow in included)
    dividend_amount = sum(item["amount"] for item in dividends)
    profit = (last.get("net_assets") or 0) - (first.get("net_assets") or 0) - net + dividend_amount
    cashflows = ([{"date": first["valuation_date"], "value": -(first.get("net_assets") or 0)}] +
                 [{"date": flow["flow_date"], "value": -(flow.get("amount") or 0)} for flow in included] +
                 [{"date": item["date"], "value": item["amount"]} for item in dividends] +
                 [{"date": last["valuation_date"], "value": last.get("net_assets") or 0}])
    annual = xirr(cashflows)
    return {"product_id": product["product_id"], "name": product["name"], "status": "ok",
            "requested_start": start, "requested_end": end, "first": first, "last": last,
            "points": points, "flows": included, "dividends": dividends,
            "subscriptions": sum(flow["amount"] for flow in included if flow["amount"] > 0),
            "redemptions": -sum(flow["amount"] for flow in included if flow["amount"] < 0),
            "net": net, "dividend_amount": dividend_amount, "profit": profit,
            "annualized": annual * 100 if annual is not None else None,
            "risk": risk_metrics(points), "benchmark": alpha_beta(points, benchmark_points),
            "holdings": holding_attribution(points, inception), "inception": inception,
            "total_investment": product.get("total_investment"),
            "approved_quota": product.get("approved_quota")}


def top_returns(page, product_id, start, end, benchmark):
    if product_id not in TOP_PRODUCTS_BY_ID:
        raise KeyError("product is not registered")
    if benchmark not in BENCHMARKS:
        raise ValueError("benchmark is not whitelisted")
    product = next((item for item in page.get("products", []) if item.get("product_id") == product_id), None)
    if not product:
        raise KeyError("product data is unavailable")
    index = page.get("benchmarks", {}).get("indices", {}).get(benchmark, {})
    result = analyze_top_product(product, start, end, index.get("points", []))
    return envelope(page, result, sources=len(result.get("points", [])),
                    warnings=[] if result["status"] == "ok" else [result.get("message")])


def overview(page, start, end, scope):
    if scope not in SCOPES:
        raise ValueError("scope is not registered")
    benchmark = page.get("benchmarks", {}).get("indices", {}).get("000852", {}).get("points", [])
    rows = [analyze_top_product(item, start, end, benchmark) for item in page.get("products", [])
            if item.get("product_id") in SCOPES[scope]]
    valid = [row for row in rows if row["status"] == "ok"]
    data = {"scope": scope, "requested_start": start, "requested_end": end,
            "products": [{key: row.get(key) for key in ("product_id", "name", "status", "message", "first", "last", "profit", "annualized", "risk")}
                         for row in rows],
            "summary": {"products": len(rows), "calculable": len(valid),
                        "ending_assets": sum(row["last"].get("net_assets") or 0 for row in valid),
                        "profit": sum(row.get("profit") or 0 for row in valid)}}
    return envelope(page, data, sources=sum(len(row.get("points", [])) for row in valid),
                    warnings=[row.get("message") for row in rows if row["status"] != "ok"])


def factor_list(page, factors):
    products = []
    for item in factors.get("products", []):
        product_id = TOP_PRODUCT_IDS.get(item.get("product"))
        if product_id:
            products.append({"product_id": product_id, "product": item.get("product"),
                             "valuation_date": item.get("valuation_date"),
                             "positions": item.get("positions"), "coverage": item.get("coverage")})
    data = {"available": factors.get("available"), "model": factors.get("model"),
            "disclaimer": factors.get("disclaimer"), "factor_definitions": factors.get("factor_definitions"),
            "products": products, "available_dates": factors.get("available_dates", [])}
    return envelope(page, data, status="current" if factors.get("available") else "missing",
                    sources=len(products), warnings=factors.get("errors", []))


def factor_detail(page, factors, product_id):
    name = TOP_PRODUCTS_BY_ID.get(product_id)
    if not name:
        raise KeyError("product is not registered")
    items = [item for item in factors.get("products", []) if item.get("product") == name]
    if not items:
        raise KeyError("factor result is unavailable")
    snapshots = [item for item in factors.get("snapshots", []) if item.get("product") == name]
    return envelope(page, {"product_id": product_id, "product": name,
                           "results": items, "snapshots": snapshots}, sources=len(snapshots))


def market_module(page, market, module):
    keys = MARKET_MODULES.get(module)
    if not keys:
        raise KeyError("market module is not registered")
    data = {key: market.get(key) for key in keys}
    data["module"] = module
    data["updated_at"] = market.get("updated_at")
    errors = [item for item in market.get("errors", [])
              if not isinstance(item, dict) or item.get("module") in keys]
    source_count = sum(len(market.get(key) or []) if isinstance(market.get(key), list)
                       else int(bool(market.get(key))) for key in keys)
    return envelope(page, data, status="current" if any(market.get(key) for key in keys) else "missing",
                    sources=source_count, warnings=errors)


def strategy(page, start, end, filters, page_number, page_size):
    matches = page.get("labels", {}).get("matches", {})
    benchmark = page.get("benchmarks", {}).get("indices", {}).get("000852", {}).get("points", [])
    rows = []
    for product in page.get("products", []):
        analyzed = analyze_top_product(product, start, end, benchmark)
        if analyzed["status"] != "ok":
            continue
        current = {item.get("code"): item for item in analyzed["last"].get("holdings", [])}
        profits = {item.get("code"): item for item in analyzed.get("holdings", [])}
        for code, holding in current.items():
            label = matches.get(holding.get("name"), {})
            if any(value and str(label.get(field) or ("无" if field == "department" else "其他")) != value
                   for field, value in filters.items()):
                continue
            estimate = profits.get(code, {})
            rows.append({"fof_id": product["product_id"], "fof": product["name"],
                         "code": code, "name": label.get("product") or holding.get("name"),
                         "market_value": holding.get("market_value"),
                         "estimated_profit": estimate.get("period_profit"),
                         "attribution_status": estimate.get("status"),
                         "labels": {field: label.get(field) or ("无" if field == "department" else "其他")
                                    for field in FILTER_FIELDS}})
    rows.sort(key=lambda item: (item["fof"], item["name"] or ""))
    total = len(rows)
    start_at = (page_number - 1) * page_size
    data = {"filters": filters, "page": page_number, "page_size": page_size, "total": total,
            "items": rows[start_at:start_at + page_size],
            "summary": {"market_value": sum(item.get("market_value") or 0 for item in rows),
                        "estimated_profit": sum(item.get("estimated_profit") or 0 for item in rows)}}
    return envelope(page, data, sources=len(page.get("products", [])))
