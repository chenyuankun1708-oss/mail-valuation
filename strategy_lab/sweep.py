"""参数网格扫描：ridge_alpha、min_train_months、成本bp 的稳定性检验。

复用 pipeline 的行构造与 backtest 的走步逻辑，只替换参数；
每个组合输出与主结果可对比的年化指标。防止组合爆炸，
默认网格为小规模（alpha 4档 × min_months 3档），可通过参数扩展。
"""
from .backtest import walk_forward


DEFAULT_ALPHA_GRID = (1.0, 5.0, 10.0, 50.0)
DEFAULT_MIN_MONTHS_GRID = (36, 48, 60)


def sweep_alpha(rows, series, alphas=DEFAULT_ALPHA_GRID):
    """固定 min_train_months，扫描 ridge alpha。"""
    results = []
    for alpha in alphas:
        try:
            backtest = walk_forward(rows, series, alpha=alpha)
        except TypeError:  # 兼容旧签名
            backtest = walk_forward(rows, series)
        metrics = (backtest.get("metrics") or {}).get("ridge", {}).get("10") or {}
        results.append({"alpha": alpha,
                        "annual_return": metrics.get("annual_return"),
                        "annual_volatility": metrics.get("annual_volatility"),
                        "sharpe": metrics.get("sharpe"),
                        "max_drawdown": metrics.get("max_drawdown"),
                        "periods": len(backtest.get("periods") or [])})
    return {"dimension": "ridge_alpha", "results": results}


def sweep_min_months(rows, series, grid=DEFAULT_MIN_MONTHS_GRID):
    """固定 alpha，扫描最低训练月数。"""
    results = []
    for months in grid:
        try:
            backtest = walk_forward(rows, series, min_train_months=months)
        except TypeError:
            backtest = walk_forward(rows, series)
        metrics = (backtest.get("metrics") or {}).get("ridge", {}).get("10") or {}
        results.append({"min_train_months": months,
                        "annual_return": metrics.get("annual_return"),
                        "annual_volatility": metrics.get("annual_volatility"),
                        "sharpe": metrics.get("sharpe"),
                        "max_drawdown": metrics.get("max_drawdown"),
                        "periods": len(backtest.get("periods") or [])})
    return {"dimension": "min_train_months", "results": results}


def run_sweep(rows, series, alphas=DEFAULT_ALPHA_GRID, month_grid=DEFAULT_MIN_MONTHS_GRID):
    """扫描汇总入口。rows/series 与 pipeline 相同。"""
    return {"ridge_alpha": sweep_alpha(rows, series, alphas),
            "min_train_months": sweep_min_months(rows, series, month_grid)}
