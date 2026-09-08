from .config import BROAD, TURNOVER_LIMIT, asset_class


CAPS = {"broad": .35, "style": .15, "industry": .10}
GROUP_CAPS = {"style": .30, "industry": .30}


def _capped(items, target, cap):
    result = {code: 0.0 for code, _ in items}
    remaining = list(items)
    left = target
    while remaining and left > 1e-12:
        total = sum(max(value, 1e-9) for _, value in remaining)
        changed = False
        for code, value in list(remaining):
            allocation = left * max(value, 1e-9) / total
            if allocation >= cap - 1e-12:
                result[code] = cap
                left -= cap
                remaining.remove((code, value))
                changed = True
        if not changed:
            for code, value in remaining:
                result[code] = left * max(value, 1e-9) / total
            left = 0.0
    return result


def target_weights(rows, scores):
    candidates = []
    for row in rows:
        code, group = row["code"], asset_class(row["code"])
        score = float(scores.get(code, {}).get("score", 0.0))
        if group == "broad" or score > 0:
            volatility = max(float(row["features"]["volatility_60d"]), .01)
            candidates.append((code, group, max(score, .01) / volatility))
    if not candidates:
        return {}
    grouped = {group: [(code, value) for code, own, value in candidates if own == group]
               for group in ("broad", "style", "industry")}
    if not grouped["broad"]:
        return {}
    raw = {group: sum(value for _, value in items) for group, items in grouped.items()}
    total = sum(raw.values()) or 1.0
    style_target = min(GROUP_CAPS["style"], raw["style"] / total)
    industry_target = min(GROUP_CAPS["industry"], raw["industry"] / total)
    if style_target + industry_target > .50:
        scale = .50 / (style_target + industry_target)
        style_target, industry_target = style_target * scale, industry_target * scale
    targets = {"broad": 1.0 - style_target - industry_target,
               "style": style_target, "industry": industry_target}
    weights = {}
    for group, items in grouped.items():
        weights.update(_capped(items, targets[group], CAPS[group]))
    # Rounding and sparse groups can leave a small remainder; fill broad names
    # by score order without exceeding the explicit 35% cap.
    left = 1.0 - sum(weights.values())
    for code, _ in sorted(grouped["broad"], key=lambda item: -item[1]):
        room = CAPS["broad"] - weights.get(code, 0.0)
        add = min(max(left, 0.0), max(room, 0.0))
        weights[code] = weights.get(code, 0.0) + add
        left -= add
    return {code: value for code, value in weights.items() if value > 1e-10}


def limit_turnover(previous, target, limit=TURNOVER_LIMIT):
    keys = set(previous) | set(target)
    turnover = .5 * sum(abs(target.get(code, 0.0) - previous.get(code, 0.0)) for code in keys)
    if previous and turnover > limit:
        alpha = limit / turnover
        target = {code: previous.get(code, 0.0) + alpha * (target.get(code, 0.0) - previous.get(code, 0.0))
                  for code in keys}
        turnover = limit
    return {code: value for code, value in target.items() if value > 1e-10}, turnover


def validate(weights):
    total = sum(weights.values())
    broad = sum(value for code, value in weights.items() if code in BROAD)
    return {"fully_invested": abs(total - 1.0) < 1e-8, "long_only": all(value >= 0 for value in weights.values()),
            "broad_minimum": broad >= .50 - 1e-8,
            "individual_caps": all(value <= CAPS[asset_class(code)] + 1e-8 for code, value in weights.items())}
