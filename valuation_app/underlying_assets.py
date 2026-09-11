import os
import re

from .config import PRODUCTS
from .labels import normalize_name
from .parser import _find_date, _holding_column, _number, _rows


TOP_LEVEL_PRODUCT_NAMES = frozenset(normalize_name(name) for name in PRODUCTS)


def is_configured_top_product(product_name):
    """Return true only for one of the configured top-level FOF products."""
    normalized = normalize_name(product_name)
    return bool(normalized and normalized in TOP_LEVEL_PRODUCT_NAMES)


def _cell(row, index):
    return row[index] if index is not None and index < len(row) else None


def _product_name(path, rows):
    filename = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"^20\d{2}-?\d{2}-?\d{2}_", "", filename)
    name = re.sub(r"^\([^)]*\)", "", name)
    name = re.sub(r"_?证券投资基金估值表.*$", "", name)
    if name:
        return name.strip("_ -")
    for row in rows[:12]:
        text = " ".join(str(value or "") for value in row[:8])
        match = re.search(r"([^ ]+(?:私募证券投资基金|资产管理计划))", text)
        if match:
            return match.group(1)
    return filename


def parse_underlying_asset(path):
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
    market_col = _holding_column(header, subheader, ("市值",))
    if None in (code_col, name_col, market_col):
        raise ValueError("缺少科目代码、科目名称或市值列")

    stock_market = 0.0
    futures_long = 0.0
    futures_short = 0.0
    evidence = []
    warnings = []
    net_assets = None
    for row in rows:
        text = "".join(str(value or "").replace(" ", "") for value in row[:3])
        if "资产净值" in text:
            values = [_number(value) for value in row]
            values = [value for value in values if value is not None and value > 0]
            if values:
                candidate = max(values)
                if net_assets is None or candidate > net_assets:
                    net_assets = candidate
    for row in rows[header_index + 1:]:
        code = str(_cell(row, code_col) or "").strip()
        name = str(_cell(row, name_col) or "").replace(" ", "").strip()
        market = _number(_cell(row, market_col))
        if market is None:
            continue
        if code == "1102" and "股票" in name:
            stock_market += market
            evidence.append({"code": code, "name": name, "market_value": market, "type": "股票"})
        elif "股指期货初始合约价值" in name and "冲销" not in name:
            # Templates use both 3102 (衍生工具) and 3201 (套期工具), so the
            # accounting code cannot be fixed.  The phrase only occurs on the
            # aggregate initial-notional row; contract leaves do not contain it.
            if "多头" in name:
                futures_long += abs(market)
                evidence.append({"code": code, "name": name, "market_value": market, "type": "股指多头"})
            elif "空头" in name:
                futures_short += abs(market)
                evidence.append({"code": code, "name": name, "market_value": market, "type": "股指空头"})
            else:
                warnings.append("%s %s：无法识别多头/空头方向" % (code, name))
    long_exposure = stock_market + futures_long - futures_short
    return {
        "product": _product_name(path, rows),
        "valuation_date": _find_date(rows, os.path.basename(path)),
        "stock_market_value": stock_market,
        "index_futures_long": futures_long,
        "index_futures_short": futures_short,
        "long_exposure": long_exposure,
        "net_assets": net_assets,
        "long_exposure_ratio": long_exposure / net_assets * 100 if net_assets else None,
        "source_file": os.path.basename(path),
        "evidence": evidence,
        "warnings": warnings,
    }


def build_underlying_asset_payload(root, fof_holdings=()):
    items, errors = [], []
    if not os.path.isdir(root):
        return {"items": [], "errors": [{"file": "", "error": "未找到底层资产文件夹"}]}
    holding_map = []
    for fof, holding in fof_holdings:
        normalized = normalize_name(holding)
        if normalized:
            holding_map.append((fof, holding, normalized))
    for base, dirs, files in os.walk(root):
        # Historical security-level files are preprocessed into the factor cache;
        # rescanning thousands of them here would duplicate the current exposure page.
        dirs[:] = [name for name in dirs if name not in ("历史估值表", ".extracting")]
        for filename in files:
            if filename.startswith("~$") or not filename.lower().endswith((".xls", ".xlsx")):
                continue
            path = os.path.join(base, filename)
            try:
                item = parse_underlying_asset(path)
                if is_configured_top_product(item["product"]):
                    continue
                normalized = normalize_name(item["product"])
                parents = sorted({fof for fof, _, candidate in holding_map
                                  if normalized and (normalized in candidate or candidate in normalized)})
                item["fof_products"] = parents
                items.append(item)
                for warning in item.get("warnings", []):
                    errors.append({"file": filename, "error": warning})
            except Exception as exc:
                errors.append({"file": filename, "error": "%s: %s" % (type(exc).__name__, exc)})
    items.sort(key=lambda item: (item["product"], item["valuation_date"]))
    return {"items": items, "errors": errors,
            "formula": "股票市值＋股指期货多头名义市值－股指期货空头名义市值"}
