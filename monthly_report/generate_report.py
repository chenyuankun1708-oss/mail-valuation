"""独立生成 FOF Excel 月报。

重要：本脚本与网页、index.html、分享服务和 GitHub 同步无关。它只复用只读的
估值表/台账解析器，不调用网页生成代码，也不会修改原始文件。
"""
from __future__ import print_function

import argparse
import calendar
import os
import sys
from collections import defaultdict
from datetime import date, datetime

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from valuation_app.ledger import load_ledger  # noqa: E402
from valuation_app.parser import scan_valuations  # noqa: E402


FISCAL_START = date(2025, 10, 31)
FISCAL_END = date(2026, 10, 31)
DETAIL_PRODUCTS = (
    "第一创业天玑13号单一资产管理计划",
    "第一创业源泉优享FOF3号单一资产管理计划",
    "国金资管盛乾同行2号FOF单一资产管理计划",
)
SHORT_NAMES = {
    "第一创业天玑13号单一资产管理计划": "第一创业天玑13号",
    "第一创业源泉优享FOF3号单一资产管理计划": "第一创业源泉优享FOF3号",
    "国金资管盛乾同行2号FOF单一资产管理计划": "国金资管盛乾同行2号FOF",
}


def iso(value):
    return value.isoformat()


def month_bounds(month_text):
    try:
        year, month = [int(value) for value in month_text.split("-")]
        end = date(year, month, calendar.monthrange(year, month)[1])
    except (ValueError, TypeError):
        raise ValueError("月份必须使用 YYYY-MM，例如 2026-07")
    if month == 1:
        start = date(year - 1, 12, 31)
    else:
        start = date(year, month - 1, calendar.monthrange(year, month - 1)[1])
    if end < FISCAL_START or end > FISCAL_END:
        raise ValueError("本生成器的2026财年范围是2025-10-31至2026-10-31")
    return start, end


def cutoff_bounds(cutoff_text):
    """Return prior month-end and an exact report cutoff date."""
    try:
        end = datetime.strptime(cutoff_text, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise ValueError("截止日必须使用 YYYY-MM-DD，例如 2026-08-27")
    previous_month = end.month - 1 or 12
    previous_year = end.year if end.month > 1 else end.year - 1
    start = date(previous_year, previous_month,
                 calendar.monthrange(previous_year, previous_month)[1])
    return start, end


def point_on_or_before(points, boundary):
    eligible = [point for point in points if point.valuation_date <= iso(boundary)]
    return eligible[-1] if eligible else None


def flows_between(flows, start, end, include_start=False):
    return [flow for flow in flows if flow.flow_date and
            (iso(start) <= flow.flow_date if include_start else iso(start) < flow.flow_date) and
            flow.flow_date <= iso(end)]


def dietz(start_asset, end_asset, flows, start, end, dividends=0.0):
    days = (end - start).days
    if days <= 0:
        return None, None
    net_flow = sum(flow.amount for flow in flows)
    profit = end_asset - start_asset - net_flow + dividends
    weighted = start_asset
    for flow in flows:
        flow_day = datetime.strptime(flow.flow_date, "%Y-%m-%d").date()
        weighted += flow.amount * float((end - flow_day).days) / days
    return profit, (profit / weighted if abs(weighted) > 0.01 else None)


def annualize(rate, days):
    if rate is None or days <= 0 or rate <= -1:
        return None
    return (1.0 + rate) ** (365.0 / days) - 1.0


def holding_period(start_point, end_point):
    """估算当前/退出底层持仓的区间收益；不推断未知成交价。"""
    previous = {item["code"]: item for item in (start_point.holdings if start_point else [])}
    current = {item["code"]: item for item in (end_point.holdings if end_point else [])}
    rows = []
    for code in sorted(set(previous) | set(current)):
        before, after = previous.get(code), current.get(code)
        name = (after or before)["name"]
        uncertain = False
        if before and after:
            comparable = min(before["quantity"], after["quantity"])
            profit = comparable * (after["price"] - before["price"])
            basis = max(after["market_value"] - profit, 0.0)
            note = "按期初期末可比份额估算"
            if abs(before["quantity"] - after["quantity"]) > max(abs(before["quantity"]) * 0.0001, 1):
                note += "；区间持仓发生变化"
        elif after:
            profit = after.get("valuation_gain")
            if profit is None and after.get("cost") is not None:
                profit = after["market_value"] - after["cost"]
            profit = profit or 0.0
            basis = after.get("cost") or max(after["market_value"] - profit, 0.0)
            note = "区间新增，按期末浮盈亏估算"
        else:
            profit, basis = 0.0, before.get("market_value") or 0.0
            note, uncertain = "区间退出，缺少成交价，退出收益无法确认", True
        rows.append({
            "code": code, "name": name, "profit": profit, "basis": basis,
            "rate": profit / basis if basis else None, "ending": after,
            "uncertain": uncertain, "note": note,
        })
    return rows


def combine_holding_metrics(points, month_start, fiscal_start, end):
    month_first = point_on_or_before(points, month_start)
    fiscal_first = point_on_or_before(points, fiscal_start)
    last = point_on_or_before(points, end)
    monthly = {row["code"]: row for row in holding_period(month_first, last)}
    fiscal = {row["code"]: row for row in holding_period(fiscal_first, last)}
    result = []
    for code in sorted(set(monthly) | set(fiscal)):
        m, f = monthly.get(code), fiscal.get(code)
        row = f or m
        ending = row.get("ending")
        if ending is None and m:
            ending = m.get("ending")
        result.append({
            "name": row["name"], "ending": ending,
            "month_profit": m["profit"] if m else 0.0,
            "month_rate": m["rate"] if m else None,
            "fiscal_profit": f["profit"] if f else 0.0,
            "fiscal_rate": f["rate"] if f else None,
            "fiscal_annual": annualize(f["rate"], (end - fiscal_start).days) if f else None,
            "uncertain": bool((m and m["uncertain"]) or (f and f["uncertain"])),
            "note": "；".join(filter(None, (m and m["note"], f and f["note"]))),
        })
    return sorted(result, key=lambda item: (item["ending"] is None, item["name"]))


def product_metrics(product, points, flows, month_start, end):
    first = points[0]
    last = point_on_or_before(points, end)
    if last is None:
        return None
    month_first = point_on_or_before(points, month_start)
    fiscal_first = point_on_or_before(points, FISCAL_START)
    # 太久以前的估值不能冒充财年期初。宁可留空，并在边界表中提示。
    if fiscal_first:
        fiscal_point_date = datetime.strptime(fiscal_first.valuation_date, "%Y-%m-%d").date()
        if (FISCAL_START - fiscal_point_date).days > 10:
            fiscal_first = None
    inception_start = datetime.strptime(first.valuation_date, "%Y-%m-%d").date()

    def period(start_date, start_point):
        actual_start = start_date
        start_asset = start_point.net_assets if start_point else 0.0
        include_start = False
        if start_point is None:
            all_flow_dates = [datetime.strptime(flow.flow_date, "%Y-%m-%d").date()
                              for flow in flows if flow.flow_date]
            economic_start = min([inception_start] + all_flow_dates)
            if economic_start <= start_date:
                return None, None, start_date, None
            new_flow_dates = [datetime.strptime(flow.flow_date, "%Y-%m-%d").date() for flow in flows
                              if flow.flow_date and iso(start_date) < flow.flow_date <= iso(end)]
            actual_start = min([economic_start] + new_flow_dates)
            include_start = True
        selected = flows_between(flows, actual_start, end, include_start=include_start)
        profit, rate = dietz(start_asset, last.net_assets, selected, actual_start, end)
        return profit, rate, actual_start, start_point

    month_profit, month_rate, _, used_month = period(month_start, month_first)
    fiscal_profit, fiscal_rate, fiscal_actual_start, used_fiscal = period(FISCAL_START, fiscal_first)
    inception_flows = [flow for flow in flows if flow.flow_date and flow.flow_date <= iso(end)]
    dated_flows = [datetime.strptime(flow.flow_date, "%Y-%m-%d").date() for flow in inception_flows]
    inception_boundary = min([inception_start] + dated_flows)
    inception_profit, inception_rate = dietz(0.0, last.net_assets, inception_flows, inception_boundary, end)
    principal = sum(flow.amount for flow in inception_flows)
    return {
        "product": product, "last": last, "month_first": used_month, "fiscal_first": used_fiscal,
        "principal": principal, "month_profit": month_profit, "month_rate": month_rate,
        "fiscal_base": used_fiscal.net_assets if used_fiscal else (0.0 if fiscal_profit is not None else None),
        "fiscal_profit": fiscal_profit, "fiscal_rate": fiscal_rate,
        "fiscal_annual": annualize(fiscal_rate, (end - fiscal_actual_start).days) if fiscal_rate is not None else None,
        "inception_profit": inception_profit, "inception_rate": inception_rate,
    }


THIN = Side(style="thin", color="B7C9DF")
HEADER = PatternFill("solid", fgColor="4472C4")
SUBHEADER = PatternFill("solid", fgColor="D9EAF7")
WARNING = PatternFill("solid", fgColor="FFF2CC")


def style_table(sheet, start_row, end_row):
    for row in sheet.iter_rows(min_row=start_row, max_row=end_row, min_col=1, max_col=12):
        for cell in row:
            cell.border = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row_number in (start_row, start_row + 1):
        for cell in sheet[row_number][:12]:
            cell.fill = HEADER if row_number == start_row else SUBHEADER
            cell.font = Font(bold=True, color="FFFFFF" if row_number == start_row else "000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def write_headers(sheet, row, month_label, detail=True):
    if detail:
        headers = ["FOF", "基金名称", "初始投资金额（万元）", "当前市值（万元）",
                   month_label + "收益", "", "2026财年收益", "", "", "投资以来收益", "", "说明"]
        sub = ["", "", "", "", "绝对收益\n（万元）", "估算收益率", "绝对收益\n（万元）",
               "估算收益率", "年化收益率", "绝对收益\n（万元）", "绝对收益率", ""]
    else:
        headers = ["基金名称", "投资金额\n（万元）", "2026财年期初资产\n（万元）", "当前市值\n（万元）",
                   "投资以来收益", "", "2026财年\n(2025.10.31起)", "", "", month_label, "", "实际估值边界"]
        sub = ["", "", "", "", "合计盈亏（万）", "收益率", "合计盈亏（万）", "收益率",
               "年化收益率", "合计盈亏（万）", "收益率", ""]
    for column, value in enumerate(headers, 1):
        sheet.cell(row, column, value)
    for column, value in enumerate(sub, 1):
        sheet.cell(row + 1, column, value)
    for column in (1, 2, 3, 4, 12):
        sheet.merge_cells(start_row=row, start_column=column, end_row=row + 1, end_column=column)
    for start_col, end_col in ((5, 6), (7, 9), (10, 11)):
        sheet.merge_cells(start_row=row, start_column=start_col, end_row=row, end_column=end_col)


def number(cell, value, percent=False):
    cell.value = value
    cell.number_format = "0.00%;[Green]-0.00%" if percent else "0.00;[Green]-0.00"


def ten_thousand(value):
    return value / 10000.0 if value is not None else None


def build_workbook(metrics, by_product, month_start, end, parse_errors, output):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "%d月月报" % end.month
    month_label = "%d月" % end.month
    write_headers(sheet, 1, month_label, True)
    row = 3
    for product in DETAIL_PRODUCTS:
        points = by_product.get(product, [])
        if not points or product not in metrics:
            continue
        holding_rows = combine_holding_metrics(points, month_start, FISCAL_START, end)
        for item in holding_rows:
            ending = item["ending"]
            sheet.cell(row, 1, SHORT_NAMES.get(product, product))
            sheet.cell(row, 2, item["name"])
            if ending:
                number(sheet.cell(row, 3), (ending.get("cost") or 0.0) / 10000.0)
                number(sheet.cell(row, 4), ending["market_value"] / 10000.0)
                current_profit = (ending.get("valuation_gain") if ending.get("valuation_gain") is not None
                                  else ending["market_value"] - (ending.get("cost") or ending["market_value"]))
                inception_rate = current_profit / ending["cost"] if ending.get("cost") else None
                number(sheet.cell(row, 10), current_profit / 10000.0)
                number(sheet.cell(row, 11), inception_rate, True)
            else:
                sheet.cell(row, 12, "已退出；退出收益无法确认")
                sheet.cell(row, 12).fill = WARNING
            number(sheet.cell(row, 5), item["month_profit"] / 10000.0)
            number(sheet.cell(row, 6), item["month_rate"], True)
            number(sheet.cell(row, 7), item["fiscal_profit"] / 10000.0)
            number(sheet.cell(row, 8), item["fiscal_rate"], True)
            number(sheet.cell(row, 9), item["fiscal_annual"], True)
            if not sheet.cell(row, 12).value:
                sheet.cell(row, 12, item["note"])
            if item["uncertain"]:
                sheet.cell(row, 12).fill = WARNING
            row += 1
        row += 1
    detail_end = max(row - 1, 3)
    style_table(sheet, 1, detail_end)

    summary_start = row + 1
    write_headers(sheet, summary_start, month_label, False)
    row = summary_start + 2
    for product in sorted(metrics):
        item = metrics[product]
        sheet.cell(row, 1, product)
        number(sheet.cell(row, 2), item["principal"] / 10000.0)
        number(sheet.cell(row, 3), ten_thousand(item["fiscal_base"]))
        number(sheet.cell(row, 4), item["last"].net_assets / 10000.0)
        number(sheet.cell(row, 5), ten_thousand(item["inception_profit"]))
        number(sheet.cell(row, 6), item["inception_rate"], True)
        number(sheet.cell(row, 7), ten_thousand(item["fiscal_profit"]))
        number(sheet.cell(row, 8), item["fiscal_rate"], True)
        number(sheet.cell(row, 9), item["fiscal_annual"], True)
        number(sheet.cell(row, 10), ten_thousand(item["month_profit"]))
        number(sheet.cell(row, 11), item["month_rate"], True)
        month_boundary = item["month_first"].valuation_date if item["month_first"] else "成立后"
        fiscal_boundary = item["fiscal_first"].valuation_date if item["fiscal_first"] else "成立后"
        sheet.cell(row, 12, "月:%s→%s；财年:%s→%s" % (
            month_boundary, item["last"].valuation_date, fiscal_boundary, item["last"].valuation_date))
        row += 1
    style_table(sheet, summary_start, row - 1)

    widths = [31, 55, 18, 18, 17, 15, 17, 15, 15, 17, 15, 34]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A3"
    sheet.auto_filter.ref = "A%d:L%d" % (summary_start + 1, row - 1)
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[1].height = 28
    sheet.row_dimensions[2].height = 38

    notes = workbook.create_sheet("数据边界与提示")
    notes.append(["说明", "本文件由独立月报脚本生成，与网页、index.html、分享服务和GitHub无关。"])
    notes.append(["月度名义区间", "%s 至 %s" % (iso(month_start), iso(end))])
    notes.append(["2026财年", "%s 至 %s；本报表截至%s" % (iso(FISCAL_START), iso(FISCAL_END), iso(end))])
    notes.append(["顶层收益", "期末资产－期初资产－台账净申购＋现金分红；收益率采用Modified Dietz。"])
    notes.append(["底层收益", "按估值表可比持仓估算；缺少独立成交流水，退出损益不能确认，不与顶层收益强制勾稽。"])
    notes.append([])
    notes.append(["产品", "月度实际起点", "财年实际起点", "实际终点", "提示"])
    for product in sorted(metrics):
        item = metrics[product]
        notes.append([
            product,
            item["month_first"].valuation_date if item["month_first"] else "成立后",
            item["fiscal_first"].valuation_date if item["fiscal_first"] else "成立后",
            item["last"].valuation_date,
            "实际终点早于名义月末" if item["last"].valuation_date != iso(end) else "",
        ])
    if parse_errors:
        notes.append([])
        notes.append(["解析提示（不含凭据）"])
        for error in parse_errors:
            notes.append([os.path.basename(error.get("file", "")), error.get("error", "")])
    notes.column_dimensions["A"].width = 48
    notes.column_dimensions["B"].width = 90
    notes.column_dimensions["C"].width = 18
    notes.column_dimensions["D"].width = 18
    notes.column_dimensions["E"].width = 28
    for cell in notes[1]:
        cell.font = Font(bold=True)
    notes.freeze_panes = "A7"

    output = os.path.abspath(output)
    parent = os.path.dirname(output)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    temp = output + ".tmp.xlsx"
    workbook.save(temp)
    os.replace(temp, output)
    return output


def _generate_bounds(month_start, end, products_dir, ledger_path, output):
    snapshots, parse_errors = scan_valuations(products_dir)
    by_product = defaultdict(list)
    for snapshot in snapshots:
        if snapshot.valuation_date <= iso(end):
            by_product[snapshot.product].append(snapshot)
    flows, ledger_errors = load_ledger(ledger_path, snapshots)
    flows_by_product = defaultdict(list)
    for flow in flows:
        flows_by_product[flow.product].append(flow)
    metrics = {}
    for product, points in by_product.items():
        points.sort(key=lambda item: item.valuation_date)
        item = product_metrics(product, points, flows_by_product[product], month_start, end)
        if item:
            metrics[product] = item
    return build_workbook(metrics, by_product, month_start, end, parse_errors + ledger_errors, output)


def generate(month_text, products_dir, ledger_path, output):
    month_start, end = month_bounds(month_text)
    return _generate_bounds(month_start, end, products_dir, ledger_path, output)


def generate_as_of(cutoff_text, products_dir, ledger_path, output):
    """Generate the same monthly workbook at an exact cutoff within its fiscal year."""
    global FISCAL_START, FISCAL_END
    month_start, end = cutoff_bounds(cutoff_text)
    fiscal_year = end.year if end.month <= 10 else end.year + 1
    FISCAL_START = date(fiscal_year - 1, 10, 31)
    FISCAL_END = date(fiscal_year, 10, 31)
    return _generate_bounds(month_start, end, products_dir, ledger_path, output)


def main():
    parser = argparse.ArgumentParser(description="独立生成FOF月报Excel（与网页无关）")
    parser.add_argument("--month", required=True, help="报表月份，格式YYYY-MM")
    parser.add_argument("--products", default=os.path.join(ROOT, "products"), help="估值表根目录")
    parser.add_argument("--ledger", default=os.path.join(ROOT, "专户资金台账.xlsx"), help="专户资金台账")
    parser.add_argument("--output", help="输出xlsx路径")
    args = parser.parse_args()
    year, month = [int(value) for value in args.month.split("-")]
    output = args.output or os.path.join(ROOT, "monthly_report", "output", "FOF月报_%d年%02d月.xlsx" % (year, month))
    print(generate(args.month, args.products, args.ledger, output))


if __name__ == "__main__":
    main()
