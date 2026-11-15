"""费用模型（契约第 4 节）。

规则摘要：
- walking / bicycling：0 元，breakdown 为空数组；
- transit：高德返回的票价，key = transit_fare；
- taxi：高德 route.taxi_cost，缺失时按「13 元含 3 公里 + 2.3 元/公里」估算并加 warning；
- driving：油费（距离 × 油耗 ÷ 100 × 油价）+ 高德过路费，key = fuel / toll；
- 金额一律先求和、再四舍五入到 0.01 元，且保证 ``sum(breakdown.amount) == total``。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .config import DEFAULTS, MODE_LABELS
from .errors import ApiError
from .util import as_float_opt, round2

ZERO_COST: dict[str, Any] = {"total": 0.0, "breakdown": []}

_KEY_LABELS = {
    "transit_fare": "公交/地铁票价",
    "taxi_fare": "打车费",
    "fuel": "油费",
    "toll": "过路费",
}


def build_cost(items: Iterable[tuple[str, str, Any]]) -> dict[str, Any]:
    """由费用明细构造 cost 对象，保证明细之和精确等于 total。

    ``items`` 为 ``(key, label, amount)``；金额 <= 0 或无法解析的条目会被丢弃
    （契约要求：油费为 0 时不加入明细、过路费为 0 时不加入明细）。
    """
    normalized: list[tuple[str, str, float]] = []
    for item in items:
        if not item:
            continue
        key, label, raw_amount = item
        amount = as_float_opt(raw_amount)
        if amount is None or amount <= 0:
            continue
        normalized.append((key, label or _KEY_LABELS.get(key, key), amount))
    if not normalized:
        return {"total": 0.0, "breakdown": []}

    total = round2(sum(amount for _, _, amount in normalized))
    breakdown = [
        {"key": key, "label": label, "amount": round2(amount)}
        for key, label, amount in normalized
    ]
    # 逐项取整后再求和可能比 round2(总和) 多/少 0.01，把残差补到最大一项上，
    # 从而保证 breakdown 之和严格等于 total。
    residual = round2(total - sum(item["amount"] for item in breakdown))
    if abs(residual) >= 0.005:
        index = max(range(len(breakdown)), key=lambda i: breakdown[i]["amount"])
        breakdown[index]["amount"] = round2(breakdown[index]["amount"] + residual)
        total = round2(sum(item["amount"] for item in breakdown))
    return {"total": total, "breakdown": breakdown}


def estimate_taxi_fee(distance_m: Any, options: dict[str, Any]) -> float:
    """打车费估算：起步价（含 base_km 公里）+ 超出部分 × 每公里单价。"""
    km = max(0.0, float(as_float_opt(distance_m) or 0.0)) / 1000.0
    base_fare = float(as_float_opt(options.get("taxi_base_fare")) or DEFAULTS["taxi_base_fare"])
    base_km = float(as_float_opt(options.get("taxi_base_km")) or DEFAULTS["taxi_base_km"])
    per_km = float(as_float_opt(options.get("taxi_per_km")) or DEFAULTS["taxi_per_km"])
    return base_fare + max(0.0, km - base_km) * per_km


def fuel_cost(distance_m: Any, options: dict[str, Any]) -> float:
    """油费 = 距离(km) × 油耗(L/100km) ÷ 100 × 油价(元/L)。"""
    km = max(0.0, float(as_float_opt(distance_m) or 0.0)) / 1000.0
    consumption = float(as_float_opt(options.get("fuel_consumption")) or DEFAULTS["fuel_consumption"])
    price = float(as_float_opt(options.get("fuel_price")) or DEFAULTS["fuel_price"])
    return km * consumption / 100.0 * price


def compute_cost(
    mode: str,
    *,
    distance_m: Any = 0,
    transit_fee: Any = None,
    taxi_cost: Any = None,
    tolls: Any = None,
    options: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """按交通方式计算一段的费用，返回 ``(cost, warnings)``。"""
    options = options or dict(DEFAULTS)
    warnings: list[str] = []
    if mode not in MODE_LABELS:
        raise ApiError("INVALID_INPUT", f"不支持的交通方式：{mode}", "")
    if mode in ("walking", "bicycling"):
        return dict(ZERO_COST), warnings
    if mode == "transit":
        fee = as_float_opt(transit_fee)
        if fee is None:
            warnings.append("未返回公交票价，按 0 元计")
            return dict(ZERO_COST), warnings
        return build_cost([("transit_fare", _KEY_LABELS["transit_fare"], fee)]), warnings
    if mode == "taxi":
        fee = as_float_opt(taxi_cost)
        if fee is None:
            warnings.append("打车费为估算值")
            fee = estimate_taxi_fee(distance_m, options)
        return build_cost([("taxi_fare", _KEY_LABELS["taxi_fare"], fee)]), warnings
    # driving
    items: list[tuple[str, str, float]] = []
    fuel = fuel_cost(distance_m, options)
    if fuel > 0:
        items.append(("fuel", _KEY_LABELS["fuel"], fuel))
    toll = as_float_opt(tolls)
    if toll is not None and toll > 0:
        items.append(("toll", _KEY_LABELS["toll"], toll))
    return build_cost(items), warnings


def merge_costs(costs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """全程合计：按 key 合并明细、金额累加后四舍五入。"""
    amounts: dict[str, float] = {}
    labels: dict[str, str] = {}
    for cost in costs:
        if not isinstance(cost, dict):
            continue
        for item in cost.get("breakdown") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if not key:
                continue
            value = as_float_opt(item.get("amount"))
            if value is None:
                continue
            amounts[key] = amounts.get(key, 0.0) + value
            labels.setdefault(key, str(item.get("label") or _KEY_LABELS.get(key, key)))
    return build_cost([(key, labels[key], amounts[key]) for key in amounts])
