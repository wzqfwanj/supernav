"""与业务无关的通用小工具：数字/文本/经纬度容错解析、金额取整等。

高德接口的字段可能是字符串或数字，也可能缺失；这里的工具函数统一做容错。
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_CENT = Decimal("0.01")
MAX_POLYLINE_POINTS = 500


def as_int(value: Any, default: int = 0) -> int:
    """把高德返回的数字/数字字符串转成 int，失败或缺省时返回 default。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else default
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return default
    return default


def as_float_opt(value: Any) -> float | None:
    """把数字/数字字符串转成 float；无法转换时返回 None（区分「缺失」与 0）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
    return None


def as_text(value: Any) -> str:
    """高德偶尔把城市名返回成 [] 或数字，统一降级成字符串。"""
    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            text = as_text(item)
            if text:
                return text
    return ""


def parse_location_text(value: Any) -> tuple[float, float] | None:
    """解析 ``"116.378,39.865"`` 形式的经纬度（也容忍 [lng, lat] 数组）。"""
    if isinstance(value, str):
        parts: list[Any] = value.strip().replace(";", ",").split(",")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        parts = list(value)
    else:
        return None
    if len(parts) != 2:
        return None
    lng = as_float_opt(parts[0])
    lat = as_float_opt(parts[1])
    if lng is None or lat is None:
        return None
    if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    return (lng, lat)


def format_location(lng: float, lat: float) -> str:
    """高德接口要求的 ``lng,lat`` 文本（最多 6 位小数）。"""
    return f"{float(lng):.6f},{float(lat):.6f}"


def round2(value: Any) -> float:
    """金额四舍五入到 0.01 元（ROUND_HALF_UP，避免二进制浮点的银行家舍入）。"""
    number = as_float_opt(value)
    if number is None:
        return 0.0
    try:
        return float(Decimal(str(number)).quantize(_CENT, rounding=ROUND_HALF_UP))
    except (ArithmeticError, ValueError):
        return 0.0


def limit_points(points: list[list[float]], limit: int = MAX_POLYLINE_POINTS) -> list[list[float]]:
    """折线点过多时等距抽稀，保留首尾点。"""
    if limit <= 0:
        return []
    if len(points) <= limit:
        return points
    if limit == 1:
        return points[:1]
    last = len(points) - 1
    return [points[round(i * last / (limit - 1))] for i in range(limit)]


def extend_polyline(points: list[list[float]], raw: Any) -> None:
    """把高德 ``"lng,lat;lng,lat;..."`` 折线字符串追加到 points。"""
    if not isinstance(raw, str) or not raw.strip():
        return
    for chunk in raw.split(";"):
        location = parse_location_text(chunk)
        if location is not None:
            points.append([round(location[0], 6), round(location[1], 6)])


def redact(text: Any, secret: str) -> str:
    """从错误信息里抹掉 Key，确保绝不泄露到响应/日志。"""
    message = str(text)
    if secret:
        message = message.replace(secret, "***")
    return message
