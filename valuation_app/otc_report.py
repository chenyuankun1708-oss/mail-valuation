"""Complete A4-landscape PDF renderer for canonical ACT/365 OTC results.

The visual hierarchy follows the legacy option-backtest reports, while every
number is read from the persisted v2 result. This module never loads market
data, connects to Oracle, or recalculates the authoritative historical payoff.
"""

from __future__ import division

import math
import os

NAVY = "#163f73"
BLUE = "#3978b8"
TEAL = "#0b8b78"
ORANGE = "#e59b28"
RED = "#c94d4d"
INK = "#26384d"
MUTED = "#71839a"
GRID = "#d9e2ec"
PAPER = "#f7f9fc"


def export_pdf(result, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp.pdf"
    with PdfPages(temporary) as pdf:
        if result.get("structure") == "dcn_snowball_combo":
            _scenario_page(plt, pdf, result, "combo", "组合条款情景路径")
            _scenario_page(plt, pdf, result, "dcn", "DCN条款情景路径")
            _scenario_page(plt, pdf, result, "snowball", "雪球条款情景路径")
            _component_section(plt, pdf, result, "dcn", True)
            _component_section(plt, pdf, result, "snowball", False)
            _component_section(plt, pdf, result, "combo", False)
        else:
            _scenario_page(plt, pdf, result, result.get("structure"), "条款情景路径")
            _parameter_page(plt, pdf, result, result.get("structure_name"))
            _summary_page(plt, pdf, result, result.get("summary") or {}, "回测结果汇总")
            _return_page(plt, pdf, result, result.get("charts") or {}, "指数与样本收益")
            _holding_page(plt, pdf, result, result.get("charts") or {}, "存续期分布")
            if result.get("structure") == "dcn":
                _dividend_page(plt, pdf, result, "派息次数分布")
    os.replace(temporary, path)


def _title(result, section):
    return "%s｜%s" % (result.get("product_name") or "期权产品", section)


def _new_page(plt, result, section, subtitle=None):
    fig = plt.figure(figsize=(11.69, 8.27), facecolor="white")
    fig.text(.055, .945, _title(result, section), fontsize=17, fontweight="bold", color=NAVY)
    fig.text(.945, .947, "报告结构 v%s" % result.get("report_schema_version", 2),
             fontsize=8, color=MUTED, ha="right")
    if subtitle:
        fig.text(.055, .912, subtitle, fontsize=8.5, color=MUTED)
    fig.add_artist(plt.Line2D([.055, .945], [.895, .895], color=NAVY, linewidth=1.2))
    return fig


def _save(plt, pdf, fig):
    fig.text(.055, .025, "自然月观察｜非交易日顺延｜ACT/365｜历史回测不代表未来表现",
             fontsize=7.5, color=MUTED)
    pdf.savefig(fig, facecolor="white")
    plt.close(fig)


def _format(value, percent=False, digits=2):
    if value is None:
        return "—"
    if percent:
        return ("{:.%d%%}" % digits).format(value)
    if isinstance(value, float):
        return ("{:.%df}" % digits).format(value)
    return str(value)


def _style_table(table, header=True, font_size=8.5, scale=1.25):
    table.auto_set_font_size(False)
    table.set_fontsize(font_size)
    table.scale(1, scale)
    for (row, _column), cell in table.get_celld().items():
        cell.set_edgecolor(GRID)
        cell.set_linewidth(.6)
        if header and row == 0:
            cell.set_facecolor(NAVY)
            cell.set_text_props(color="white", weight="bold")
        elif row % 2 == 0:
            cell.set_facecolor(PAPER)


def _table_page(plt, pdf, result, section, rows, headers=("项目", "数值"), subtitle=None):
    fig = _new_page(plt, result, section, subtitle)
    axis = fig.add_axes([.055, .075, .89, .79])
    axis.axis("off")
    table = axis.table(cellText=rows, colLabels=headers, cellLoc="left", colLoc="left",
                       loc="upper center", colWidths=[.34, .66] if len(headers) == 2 else None)
    _style_table(table, True, 8.5, 1.30)
    _save(plt, pdf, fig)


def _scenario_series(request, kind):
    months = int(request.get("term_months") or 24)
    lock = int(request.get("lock_period_months") or 3)
    x = list(range(months + 1))
    ko = [None if month < lock else max(.45, request.get("knock_out_initial", 1.0) -
          (month - lock) * request.get("knock_out_decrease_monthly", .005)) for month in x]
    paths = {
        "早期敲出": [1 + .012 * month for month in x],
        "未敲入未敲出": [1 - .006 * month + .025 * math.sin(month / 2.7) for month in x],
        "敲入后修复": [1 - .055 * min(month, 6) + .027 * max(month - 6, 0) for month in x],
        "敲入未敲出": [max(.45, 1 - .025 * month + .018 * math.sin(month / 2.1)) for month in x],
    }
    if kind == "dcn":
        paths.pop("敲入后修复")
    return x, ko, paths


def _scenario_payoff(request, kind, values, ko_line):
    months = int(request.get("term_months") or 24)
    lock = int(request.get("lock_period_months") or 3)
    ko_month = next((month for month in range(lock, months + 1)
                     if ko_line[month] is not None and values[month] > ko_line[month] and
                     not math.isclose(values[month], ko_line[month])), None)
    exit_month = ko_month or months
    european = kind == "european_snowball"
    knocked_in = (values[exit_month] <= request.get("knock_in_ratio", .7) if european else
                  min(values[:exit_month + 1]) <= request.get("knock_in_ratio", .7))

    def snowball_return(max_loss_key="max_loss"):
        if ko_month is None and knocked_in:
            value = values[exit_month] - 1.0
        else:
            coupon = (request.get("first_coupon", .12) if exit_month <= request.get("coupon_switch_months", months)
                      else request.get("second_coupon", .12))
            value = coupon * exit_month / 12.0
        cap = request.get(max_loss_key)
        return max(value, -cap) if cap is not None else value

    def dcn_return():
        count = sum(values[month] >= request.get("dividend_barrier", .8)
                    for month in range(1, exit_month + 1))
        value = count * request.get("monthly_dividend", .0088)
        if ko_month is None and knocked_in:
            value += values[exit_month] - 1.0
        cap = request.get("dcn_max_loss" if kind == "combo" else "max_loss")
        return (max(value, -cap) if cap is not None else value), count

    if kind == "dcn":
        value, dividends = dcn_return()
    elif kind == "combo":
        dcn_value, dividends = dcn_return()
        snow_value = snowball_return("snowball_max_loss")
        value = request.get("dcn_weight", 1.0) * dcn_value + request.get("snowball_weight", .2) * snow_value
    else:
        value = snowball_return(); dividends = None
    event = "第%d月敲出" % ko_month if ko_month is not None else ("到期敲入" if knocked_in else "到期未敲入")
    if dividends is not None:
        event += " / 派息%d次" % dividends
    return event, int(round(exit_month * 365.0 / 12.0)), value


def _scenario_page(plt, pdf, result, kind, section):
    request = result.get("request") or {}
    fig = _new_page(plt, result, section, "条款情景示意，不是预测或定价；路径仅用于解释事件判断顺序。")
    axis = fig.add_axes([.07, .32, .60, .52])
    x, ko, paths = _scenario_series(request, kind)
    for color, (label, values) in zip([TEAL, BLUE, ORANGE, RED], paths.items()):
        axis.plot(x, values, linewidth=1.8, color=color, label=label)
    axis.plot(x, [value if value is not None else float("nan") for value in ko], "--",
              color=NAVY, linewidth=1.3, label="逐月敲出线")
    axis.axhline(request.get("knock_in_ratio", .7), linestyle="--", color=RED,
                 linewidth=1.1, label="敲入线")
    if kind in ("dcn", "combo"):
        axis.axhline(request.get("dividend_barrier", .8), linestyle=":", color=ORANGE,
                     linewidth=1.4, label="派息线")
    axis.axvspan(0, request.get("lock_period_months", 3), color="#dfe8f2", alpha=.65, label="锁定期")
    axis.set_xlabel("自然月")
    axis.set_ylabel("标的价格 / 期初价格")
    axis.grid(True, alpha=.18)
    axis.legend(loc="best", fontsize=7, ncol=2)
    notes = [
        ["观察", "由建仓日直接增加自然月；非交易日顺延"],
        ["敲出", "锁定期后按逐月下降敲出线判断"],
        ["敲入", "欧式雪球仅到期判断；其他结构按存续路径判断"],
        ["现金流", "票息或派息按实际自然日，以ACT/365计量"],
        ["限制", "示意路径不进入历史统计，不构成定价或预测"],
    ]
    table_axis = fig.add_axes([.70, .32, .245, .52]); table_axis.axis("off")
    table = table_axis.table(cellText=notes, colLabels=("规则", "说明"), cellLoc="left",
                             colLoc="left", loc="upper center", colWidths=[.25, .75])
    _style_table(table, True, 7.3, 1.45)
    bottom = fig.add_axes([.07, .085, .875, .17]); bottom.axis("off")
    rows = []
    for name, values in paths.items():
        event, days, value = _scenario_payoff(request, kind, values, ko)
        rows.append([name, event, days, _format(value, True)])
    table = bottom.table(cellText=rows, colLabels=("情景", "事件结果", "持有自然日（示意）", "绝对收益（示意）"),
                         cellLoc="left", colLoc="left", loc="upper center")
    _style_table(table, True, 8, 1.18)
    _save(plt, pdf, fig)


PARAMETERS = (
    ("term_months", "期限（月）", False), ("lock_period_months", "敲出锁定期（月）", False),
    ("knock_in_ratio", "敲入比例", True), ("knock_out_initial", "初始敲出比例", True),
    ("knock_out_decrease_monthly", "每月降敲", True), ("first_coupon", "前段年化票息", True),
    ("second_coupon", "后段年化票息", True), ("coupon_switch_months", "票息切换月", False),
    ("max_loss", "最大亏损", True), ("dividend_barrier", "派息障碍", True),
    ("monthly_dividend", "月派息率", True), ("dcn_weight", "DCN收益组合系数", False),
    ("snowball_weight", "雪球收益组合系数", False), ("dcn_max_loss", "DCN最大亏损", True),
    ("snowball_max_loss", "雪球最大亏损", True),
)


def _parameter_page(plt, pdf, result, component_name):
    request = result.get("request") or {}
    source = result.get("source") or {}
    rows = [
        ["产品名称", result.get("product_name")], ["报告分项", component_name],
        ["结构 / 标的", "%s / %s（%s）" % (result.get("structure_name"), result.get("index_name"), result.get("index_code"))],
        ["历史样本请求区间", "%s → %s" % (request.get("start_date"), request.get("end_date"))],
        ["实际行情边界", "%s → %s" % (source.get("actual_start_date"), source.get("actual_end_date"))],
        ["观察与年化规则", "自然月观察 / 非交易日顺延 / ACT/365"],
        ["引擎版本", result.get("engine_version")],
    ]
    for key, label, percent in PARAMETERS:
        if key in request and request.get(key) is not None:
            rows.append([label, _format(request.get(key), percent, 3)])
    _table_page(plt, pdf, result, "%s参数" % component_name, rows,
                subtitle="参数、数据边界和版本均来自本次任务快照。")


SUMMARY_FIELDS = (
    ("全部建仓样本", "total_samples", False), ("已完成样本", "completed_samples", False),
    ("存续中样本", "still_running_samples", False), ("敲出且未敲入", "knocked_out_no_knock_in", False),
    ("未敲出且未敲入", "not_knocked_out_no_knock_in", False), ("敲入后敲出", "knocked_in_then_out", False),
    ("未敲出且敲入", "not_knocked_out_knocked_in", False), ("正收益概率", "positive_return_probability", True),
    ("首年敲出概率", "first_year_knock_out_probability", True),
    ("平均持有自然日", "average_holding_calendar_days", False),
    ("平均绝对收益率", "average_absolute_return", True), ("平均年化收益率", "average_annualized_return", True),
    ("平均派息次数", "average_dividend_count", False),
)


def _summary_page(plt, pdf, result, summary, section):
    completed = summary.get("completed_samples") or 0
    rows = []
    categories = {"knocked_out_no_knock_in", "not_knocked_out_no_knock_in",
                  "knocked_in_then_out", "not_knocked_out_knocked_in"}
    for label, key, percent in SUMMARY_FIELDS:
        if key in summary:
            value = summary.get(key)
            note = "（占已完成样本 {:.2%}）".format(value / completed) if key in categories and completed else ""
            rows.append([label, "%s%s" % (_format(value, percent), note)])
    _table_page(plt, pdf, result, section, rows,
                subtitle="概率与均值仅使用已完成样本；存续中样本单独列示。")


def _return_page(plt, pdf, result, charts, section):
    rows = charts.get("sample_returns") or []
    samples = [row for row in (result.get("samples") or []) if not row.get("still_running")]
    if len(samples) > 500:
        step = max(1, len(samples) // 500)
        samples = samples[::step][:500]
    fig = _new_page(plt, result, section, "左：建仓日标的点位；右：同一批已完成样本绝对收益率。")
    left = fig.add_axes([.07, .14, .40, .69])
    left.plot([row.get("entry_date") for row in samples], [row.get("entry_price") for row in samples],
              color=BLUE, linewidth=1.25)
    left.set_title("建仓日标的点位", fontsize=11, color=INK); left.set_ylabel("指数点位")
    right = fig.add_axes([.54, .14, .40, .69])
    right.plot([row.get("date") for row in rows], [row.get("value") for row in rows],
               color=TEAL, linewidth=1.25)
    right.axhline(0, color=MUTED, linewidth=.8)
    right.set_title("建仓日样本绝对收益率", fontsize=11, color=INK); right.set_ylabel("绝对收益率")
    for axis in (left, right):
        axis.grid(True, alpha=.16); axis.tick_params(axis="x", labelrotation=35, labelsize=6)
        labels = axis.get_xticklabels()
        if len(labels) > 16:
            every = max(1, len(labels) // 12)
            for index, label in enumerate(labels):
                label.set_visible(index % every == 0)
    _save(plt, pdf, fig)


def _distribution_stats(rows):
    values = []
    for row in rows:
        values.extend([float(row.get("month", 0))] * int(row.get("count", 1)))
    if not values:
        return []
    ordered = sorted(values); mean = sum(values) / len(values); median = ordered[len(ordered) // 2]
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return [["样本数", len(values)], ["最小值", min(values)], ["最大值", max(values)],
            ["均值", "%.2f" % mean], ["中位数", "%.2f" % median], ["标准差", "%.2f" % math.sqrt(variance)]]


def _distribution_page(plt, pdf, result, rows, section, x_label, color):
    fig = _new_page(plt, result, section, "仅统计已完成样本；存续中样本不进入分布。")
    axis = fig.add_axes([.08, .17, .63, .64])
    axis.bar([str(row.get("month")) for row in rows], [row.get("count") for row in rows], color=color)
    axis.set_xlabel(x_label); axis.set_ylabel("样本数"); axis.grid(axis="y", alpha=.18)
    stats_axis = fig.add_axes([.75, .24, .19, .50]); stats_axis.axis("off")
    table = stats_axis.table(cellText=_distribution_stats(rows), colLabels=("统计", "数值"),
                             cellLoc="left", colLoc="left", loc="upper center")
    _style_table(table, True, 8, 1.35)
    _save(plt, pdf, fig)


def _holding_page(plt, pdf, result, charts, section):
    _distribution_page(plt, pdf, result, charts.get("holding_months") or [], section, "退出观察月", BLUE)


def _dividend_page(plt, pdf, result, section):
    counts = {}
    for row in result.get("samples") or []:
        if not row.get("still_running") and row.get("dividend_count") is not None:
            key = int(row["dividend_count"]); counts[key] = counts.get(key, 0) + 1
    rows = [{"month": key, "count": counts[key]} for key in sorted(counts)]
    _distribution_page(plt, pdf, result, rows, section, "派息次数", TEAL)


def _component_section(plt, pdf, result, component, include_dividend):
    name = {"dcn": "DCN", "snowball": "雪球", "combo": "组合"}[component]
    block = (result.get("report_components") or {}).get(component) or {}
    _parameter_page(plt, pdf, result, name)
    _summary_page(plt, pdf, result, block.get("summary") or {}, "%s汇总" % name)
    _return_page(plt, pdf, result, block.get("charts") or {}, "%s收益图" % name)
    _holding_page(plt, pdf, result, block.get("charts") or {}, "%s存续期" % name)
    if include_dividend:
        _dividend_page(plt, pdf, result, "DCN派息分布")
