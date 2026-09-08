from collections import OrderedDict


BENCHMARK = "000300"
BROAD = OrderedDict([
    ("000300", "沪深300"), ("000905", "中证500"),
    ("000852", "中证1000"), ("000510", "中证A500"),
])
STYLE = OrderedDict([
    ("000918", "沪深300成长"), ("000919", "沪深300价值"),
    ("000922", "中证红利"), ("930939", "质量"), ("930782", "低波"),
])
INDUSTRY_PREFIX = "801"
ALLOWED_CODES = frozenset(list(BROAD) + list(STYLE))
FEATURE_NAMES = ("momentum_1m", "momentum_3m", "momentum_6m", "momentum_12m",
                 "volatility_60d", "relative_3m", "relative_6m",
                 "macro_state", "style_state", "sentiment_state")
RIDGE_ALPHA = 5.0
MIN_TRAIN_MONTHS = 36
TURNOVER_LIMIT = 0.30
COST_SCENARIOS_BP = (0, 10, 20)


def asset_class(code):
    if code in BROAD:
        return "broad"
    if code in STYLE:
        return "style"
    if str(code).startswith(INDUSTRY_PREFIX):
        return "industry"
    return None
