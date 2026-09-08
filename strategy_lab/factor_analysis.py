"""因子分析：相关性矩阵、VIF共线性与RankIC时序。

只读消费 pipeline 已构造的 factor_snapshot 行，输出结构化 JSON 供网页展示。
全部为纯函数，不依赖外部库（Python 3.7 兼容）。
"""
import math

from .config import FEATURE_NAMES


def _mean(values):
    return sum(values) / len(values) if values else None


def _std(values):
    if len(values) < 2:
        return None
    mean = _mean(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _pearson(xs, ys):
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = xs[:n], ys[:n]
    mx, my = _mean(xs), _mean(ys)
    sx, sy = _std(xs), _std(ys)
    if not sx or not sy:
        return None
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (n - 1)
    return cov / (sx * sy)


def _rank(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        tied = [index]
        while index + 1 < len(order) and values[order[index + 1]] == values[order[index]]:
            index += 1
            tied.append(index)
        average = sum(order[i] + 1 for i in tied) / len(tied)
        for i in tied:
            ranks[order[i]] = average
        index += 1
    return ranks


def spearman(xs, ys):
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    return _pearson(_rank(xs[:n]), _rank(ys[:n]))


def correlation_matrix(rows, method="pearson"):
    """因子间相关矩阵。method: pearson | spearman。"""
    func = _pearson if method == "pearson" else spearman
    names = list(FEATURE_NAMES)
    columns = {name: [float(r["features"][name]) for r in rows if r["features"].get(name) is not None]
               for name in names}
    matrix = {}
    for a in names:
        matrix[a] = {}
        for b in names:
            value = func(columns[a], columns[b]) if a != b else 1.0
            matrix[a][b] = round(value, 4) if value is not None else None
    return {"method": method, "names": names, "matrix": matrix,
            "samples": len(rows)}


def vif(rows):
    """方差膨胀因子：对每个因子用其余因子做不含截距的最小二乘拟合 R^2。

    VIF_j = 1 / (1 - R_j^2)。纯手写正规方程（与 model.py 相同的高斯消元思路）。
    """
    names = list(FEATURE_NAMES)
    data = [[float(r["features"][name]) for name in names] for r in rows]
    if len(data) < len(names) + 2:
        return {"names": names, "vif": {name: None for name in names}, "samples": len(data)}

    # 标准化以改善数值条件
    means = [_mean([row[j] for row in data]) for j in range(len(names))]
    stds = [_std([row[j] for row in data]) or 1.0 for j in range(len(names))]
    z = [[(row[j] - means[j]) / stds[j] for j in range(len(names))] for row in data]

    def solve(aug_rows, size):
        aug = [list(r) for r in aug_rows]
        for col in range(size):
            pivot = max(range(col, size), key=lambda r: abs(aug[r][col]))
            if abs(aug[pivot][col]) < 1e-12:
                return None
            aug[col], aug[pivot] = aug[pivot], aug[col]
            scale = aug[col][col]
            aug[col] = [v / scale for v in aug[col]]
            for r in range(size):
                if r != col:
                    factor = aug[r][col]
                    aug[r] = [a - factor * b for a, b in zip(aug[r], aug[col])]
        return [aug[r][-1] for r in range(size)]

    result = {}
    for j, name in enumerate(names):
        others = [k for k in range(len(names)) if k != j]
        size = len(others)
        matrix = [[sum(row[a] * row[b] for row in z) for b in others] for a in others]
        vector = [sum(row[j] * row[a] for row in z) for a in others]
        beta = solve([matrix[r] + [vector[r]] for r in range(size)], size)
        if beta is None:
            result[name] = None
            continue
        # R^2 of predicting z_j from others
        mean_j = 0.0  # z 已中心化
        ss_tot = sum((row[j] - mean_j) ** 2 for row in z)
        ss_res = sum((row[j] - sum(beta[i] * row[others[i]] for i in range(size))) ** 2 for row in z)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
        result[name] = round(1.0 / (1.0 - r2), 3) if r2 is not None and r2 < 1.0 - 1e-12 else None
    return {"names": names, "vif": result, "samples": len(data)}


def rank_ic_series(rows):
    """逐月 RankIC：因子截面值与下月目标超额收益的 Spearman 相关。

    只使用 target_end <= 该截面月的已知目标（防前视由 pipeline 的行构造保证）。
    """
    by_date = {}
    for row in rows:
        if row.get("target_excess_return") is not None:
            by_date.setdefault(row["date"], []).append(row)
    series = {name: [] for name in FEATURE_NAMES}
    dates = sorted(by_date)
    for day in dates:
        current = by_date[day]
        for name in FEATURE_NAMES:
            xs = [float(r["features"][name]) for r in current]
            ys = [float(r["target_excess_return"]) for r in current]
            ic = spearman(xs, ys)
            if ic is not None:
                series[name].append({"date": day, "ic": round(ic, 4)})
    summary = {}
    for name in FEATURE_NAMES:
        values = [point["ic"] for point in series[name]]
        mean = _mean(values)
        std = _std(values)
        summary[name] = {
            "mean_ic": round(mean, 4) if mean is not None else None,
            "ic_std": round(std, 4) if std is not None else None,
            "icir": round(mean / std, 4) if mean is not None and std else None,
            "positive_rate": round(sum(1 for v in values if v > 0) / len(values), 4) if values else None,
            "observations": len(values),
        }
    return {"series": series, "summary": summary}


def analyze(rows, methods=("pearson", "spearman")):
    """因子分析汇总入口。rows 为 pipeline 构造的 factor rows。"""
    payload = {"correlations": {}, "vif": vif(rows), "rank_ic": rank_ic_series(rows)}
    for method in methods:
        payload["correlations"][method] = correlation_matrix(rows, method)
    high_pairs = []
    corr = payload["correlations"].get("pearson", {}).get("matrix", {})
    names = payload["correlations"].get("pearson", {}).get("names", [])
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            value = (corr.get(a) or {}).get(b)
            if value is not None and abs(value) >= 0.7:
                high_pairs.append({"a": a, "b": b, "correlation": value})
    payload["high_correlation_pairs"] = high_pairs
    return payload
