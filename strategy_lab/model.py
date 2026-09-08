import math

from .config import FEATURE_NAMES, RIDGE_ALPHA


def _solve(matrix, vector):
    size = len(vector)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            continue
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[column])]
    return [augmented[row][-1] for row in range(size)]


def fit_ridge(rows, alpha=RIDGE_ALPHA):
    if not rows:
        raise ValueError("训练样本为空")
    means, scales = {}, {}
    for name in FEATURE_NAMES:
        values = [float(row["features"][name]) for row in rows]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / max(len(values) - 1, 1)
        means[name], scales[name] = mean, math.sqrt(variance) or 1.0
    x = [[1.0] + [(float(row["features"][name]) - means[name]) / scales[name] for name in FEATURE_NAMES]
         for row in rows]
    y = [float(row["target_excess_return"]) for row in rows]
    size = len(FEATURE_NAMES) + 1
    matrix = [[sum(sample[i] * sample[j] for sample in x) for j in range(size)] for i in range(size)]
    for index in range(1, size):
        matrix[index][index] += alpha
    vector = [sum(sample[index] * target for sample, target in zip(x, y)) for index in range(size)]
    coefficients = _solve(matrix, vector)
    return {"intercept": coefficients[0], "coefficients": dict(zip(FEATURE_NAMES, coefficients[1:])),
            "means": means, "scales": scales, "alpha": alpha, "samples": len(rows),
            "training_start": min(row["date"] for row in rows), "training_end": max(row["target_end"] for row in rows)}


def predict(model, row):
    contributions = {}
    value = model["intercept"]
    for name in FEATURE_NAMES:
        contribution = model["coefficients"][name] * (row["features"][name] - model["means"][name]) / model["scales"][name]
        contributions[name] = contribution
        value += contribution
    return value, contributions


def zscores(values):
    mean = sum(values) / len(values) if values else 0.0
    variance = sum((value - mean) ** 2 for value in values) / max(len(values) - 1, 1)
    scale = math.sqrt(variance) or 1.0
    return [max(-3.0, min(3.0, (value - mean) / scale)) for value in values]


def baseline_scores(rows):
    fields = {name: zscores([row["features"][name] for row in rows]) for name in FEATURE_NAMES}
    result = {}
    for index, row in enumerate(rows):
        trend = sum(fields[name][index] for name in ("momentum_1m", "momentum_3m", "momentum_6m", "momentum_12m")) / 4
        relative = (fields["relative_3m"][index] + fields["relative_6m"][index]) / 2
        components = {"trend": .35 * trend, "macro": .20 * fields["macro_state"][index],
                      "relative_strength": .20 * relative,
                      "sentiment_volatility": .15 * ((fields["sentiment_state"][index] - fields["volatility_60d"][index]) / 2),
                      "style_valuation": .10 * fields["style_state"][index]}
        result[row["code"]] = {"score": sum(components.values()), "components": components}
    return result
