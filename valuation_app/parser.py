import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

import openpyxl
import xlrd

from .config import match_product


DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-./年]?(0?[1-9]|1[0-2])[-./月]?(0?[1-9]|[12]\d|3[01])日?(?!\d)")


@dataclass
class Snapshot:
    product: str
    valuation_date: str
    nav: float
    accumulated_nav: float
    net_assets: float
    shares: float
    source_file: str
    holdings: list = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip().replace(",", "").replace("，", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def _rows(path):
    if path.lower().endswith(".xls"):
        book = xlrd.open_workbook(path, on_demand=True)
        try:
            sheet = book.sheet_by_index(0)
            return [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]
        finally:
            book.release_resources()
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book.active
        return [[cell.value for cell in row] for row in sheet.iter_rows()]
    finally:
        book.close()


def _find_date(rows, filename):
    texts = [str(v) for row in rows[:10] for v in row[:12] if v not in (None, "")]
    texts.append(filename)
    found = []
    for text in texts:
        for y, m, d in DATE_RE.findall(text):
            try:
                found.append(datetime(int(y), int(m), int(d)).date())
            except ValueError:
                pass
    if not found:
        raise ValueError("无法识别估值日期")
    # 表头优先；文件名可能同时包含邮件日期和估值日期。
    for text in texts[:-1]:
        match = DATE_RE.search(text)
        if match and ("估值" in text or "日期" in text):
            y, m, d = match.groups()
            return datetime(int(y), int(m), int(d)).date().isoformat()
    return max(found).isoformat()


def _row_numbers(row, start=1):
    return [value for value in (_number(v) for v in row[start:]) if value is not None]


def _metric(rows, labels, strategy="first"):
    for row in rows:
        label = "".join(str(v or "") for v in row[:2]).replace(" ", "")
        if any(key in label for key in labels):
            values = _row_numbers(row)
            if not values:
                # 某些模板把“单位净值：1.02”放在同一单元格。
                values = [_number(v) for v in row if _number(v) is not None]
            if values:
                return max(values) if strategy == "max" else values[0]
    return None


def _metric_nearest(rows, labels, expected):
    """从多列估值汇总行中选择最接近份额×单位净值的市值列。"""
    for row in rows:
        label = "".join(str(v or "") for v in row[:2]).replace(" ", "")
        if any(key in label for key in labels):
            values = [value for value in _row_numbers(row) if value > 0]
            if values:
                return min(values, key=lambda value: abs(value - expected))
    return None


def _holding_column(header, subheader, labels):
    for index, value in enumerate(header):
        text = str(value or "").replace(" ", "")
        if text in labels:
            # 部分托管模板将原币/本币拆成两列，收益分析统一取本币列。
            if index + 1 < len(subheader) and "本币" in str(subheader[index + 1] or ""):
                return index + 1
            return index
    return None


def _parse_holdings(rows):
    """提取底层基金/资管产品的数量、价格、成本、市值和浮盈亏。"""
    header_index = next((i for i, row in enumerate(rows)
                         if any("科目代码" in str(v or "") for v in row)
                         and any("科目名称" in str(v or "") for v in row)), None)
    if header_index is None:
        return []
    header = rows[header_index]
    subheader = rows[header_index + 1] if header_index + 1 < len(rows) else []
    columns = {
        "code": _holding_column(header, subheader, ("科目代码",)),
        "name": _holding_column(header, subheader, ("科目名称",)),
        "quantity": _holding_column(header, subheader, ("数量",)),
        "unit_cost": _holding_column(header, subheader, ("单位成本",)),
        "cost": _holding_column(header, subheader, ("成本",)),
        "price": _holding_column(header, subheader, ("市价", "行情", "估值价格")),
        "market_value": _holding_column(header, subheader, ("市值",)),
        "valuation_gain": _holding_column(header, subheader, ("估值增值",)),
    }
    if any(columns[key] is None for key in ("code", "name", "quantity", "price", "market_value")):
        return []

    def cell(row, column):
        return row[column] if column is not None and column < len(row) else None

    holdings = []
    for row in rows[header_index + 1:]:
        code = str(cell(row, columns["code"]) or "").strip()
        name = str(cell(row, columns["name"]) or "").strip()
        quantity = _number(cell(row, columns["quantity"]))
        price = _number(cell(row, columns["price"]))
        market_value = _number(cell(row, columns["market_value"]))
        if not code or not name or not code.startswith(("1105", "1108", "1109")):
            continue
        # 汇总/分类行通常没有单位价格；只有叶子持仓才进入明细。
        if not quantity or not price or market_value is None:
            continue
        cost = _number(cell(row, columns["cost"]))
        unit_cost = _number(cell(row, columns["unit_cost"]))
        gain = _number(cell(row, columns["valuation_gain"]))
        if gain is None and cost is not None:
            gain = market_value - cost
        holdings.append({
            "code": code, "name": name, "quantity": quantity,
            "unit_cost": unit_cost, "cost": cost, "price": price,
            "market_value": market_value, "valuation_gain": gain,
        })
    return holdings


def parse_valuation(path, product=None):
    rows = _rows(path)
    searchable = os.path.basename(path) + " " + " ".join(str(v) for row in rows[:8] for v in row[:12] if v)
    product = product or match_product(searchable)
    if not product:
        raise ValueError("不属于配置的15只产品")

    # 少数管理人发送两行式净值报告，而不是会计科目估值表。
    tabular = {}
    if len(rows) >= 2:
        headers = [str(value or "").strip() for value in rows[0]]
        tabular = {header: rows[1][index] for index, header in enumerate(headers) if header and index < len(rows[1])}
    nav = (_number(tabular.get("单位净值")) or _metric(rows, ("今日单位净值",))
           or _metric(rows, ("基金单位净值",)))
    accumulated_nav = _number(tabular.get("累计单位净值")) or _metric(rows, ("累计单位净值",)) or nav
    shares = _number(tabular.get("产品份额")) or _metric(rows, ("实收资本金额", "实收资本", "基金总份额", "基金份额"), "max")
    expected_assets = shares * nav if shares and nav else None
    net_assets = (_number(tabular.get("资产净值")) or
                  (_metric_nearest(rows, ("基金资产净值", "资产资产净值", "资产净值合计", "资产净值"), expected_assets)
                   if expected_assets else None) or
                  _metric(rows, ("基金资产净值", "资产资产净值", "资产净值合计", "资产净值")))
    missing = [name for name, value in (("单位净值", nav), ("资产净值", net_assets), ("份额", shares)) if not value]
    if missing:
        raise ValueError("缺少关键字段：" + "、".join(missing))
    # 份额保留估值表披露值。不能因资产净值/单位净值存在列选择或精度差异，
    # 就用反推值覆盖真实份额；反推份额会随净值波动并制造虚假申赎。
    valuation_date = None
    tabular_date = tabular.get("日期")
    if hasattr(tabular_date, "date"):
        valuation_date = tabular_date.date().isoformat()
    return Snapshot(product, valuation_date or _find_date(rows, os.path.basename(path)), nav, accumulated_nav,
                    net_assets, shares, os.path.abspath(path), _parse_holdings(rows))


def scan_valuations(root):
    snapshots, errors = [], []
    for base, _, files in os.walk(root):
        for filename in files:
            if not filename.lower().endswith((".xls", ".xlsx")) or filename.startswith("~$"):
                continue
            path = os.path.join(base, filename)
            try:
                snapshots.append(parse_valuation(path))
            except Exception as exc:
                # 无关历史附件不作为系统错误。
                if "不属于配置" not in str(exc):
                    errors.append({"file": os.path.abspath(path), "error": str(exc)})
    unique = {}
    for item in snapshots:
        key = (item.product, item.valuation_date)
        existing = unique.get(key)
        # 同日可能同时收到汇总净值报告和完整基金估值表，优先保留可解析持仓更多的版本。
        if existing is None or len(item.holdings) > len(existing.holdings):
            unique[key] = item
    return sorted(unique.values(), key=lambda x: (x.product, x.valuation_date)), errors
