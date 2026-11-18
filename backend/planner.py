"""规划编排：参数校验 → 地点解析（真实/演示）→ 逐段路线与费用 → 全程合计。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import amap, config, costs, demo
from .errors import ApiError
from .util import format_location, limit_points, parse_location_text

MAX_POINTS = 10
MIN_POINTS = 2


# --------------------------------------------------------------------- 参数校验
def _invalid(message: str, hint: str = "") -> ApiError:
    return ApiError("INVALID_INPUT", message, hint)


def _validate_location_input(text: str, label: str) -> tuple[float, float]:
    parts = text.strip().split(",")
    if len(parts) != 2:
        raise _invalid(f"{label} location 格式应为 \"lng,lat\"", '例如 {"location": "116.378,39.865"}')
    for part in parts:
        if "." in part and len(part.split(".", 1)[1]) > 6:
            raise _invalid(f"{label} location 小数点后最多 6 位", "")
    location = parse_location_text(text)
    if location is None:
        raise _invalid(f"{label} location 必须是合法的 \"lng,lat\" 经纬度", "")
    return location


def _as_point_spec(raw: Any, index: int) -> dict[str, Any]:
    label = f"第 {index + 1} 个地点"
    if not isinstance(raw, dict):
        raise _invalid(f"{label}格式不正确", "每个地点应为对象")

    raw_location = raw.get("location")
    raw_address = raw.get("address")
    raw_name = raw.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""

    location: tuple[float, float] | None = None
    if raw_location is not None:
        if not isinstance(raw_location, str) or not raw_location.strip():
            raise _invalid(f"{label} location 必须是非空字符串", '例如 "116.378,39.865"')
        location = _validate_location_input(raw_location, label)

    address = ""
    if raw_address is not None:
        if not isinstance(raw_address, str):
            raise _invalid(f"{label} address 必须是字符串", "")
        address = raw_address.strip()

    if location is None and not address:
        raise _invalid(f"{label}缺少地址", f"请提供 address 或 location 字段")

    if location is not None:
        input_text = raw_address.strip() if isinstance(raw_address, str) and raw_address.strip() else raw_location.strip()
        return {
            "input": input_text,
            "address": None,
            "location": location,
            "name": name or address or input_text,
        }
    return {
        "input": raw_address if isinstance(raw_address, str) else address,
        "address": address,
        "location": None,
        "name": name,
    }


def _validate_points(payload: Any) -> list[dict[str, Any]]:
    """校验 points 字段并归一化成 point spec 列表（plan / compare 共用）。"""
    if not isinstance(payload, dict):
        raise _invalid("请求体必须是 JSON 对象", "")

    raw_points = payload.get("points")
    if not isinstance(raw_points, list):
        raise _invalid("points 必须是数组", "示例见 docs/API-CONTRACT.md")
    if len(raw_points) < MIN_POINTS:
        raise _invalid("至少需要起点和终点", "points 需要 2–10 个地点")
    if len(raw_points) > MAX_POINTS:
        raise _invalid(f"最多支持 {MAX_POINTS} 个地点", f"当前提供 {len(raw_points)} 个")

    return [_as_point_spec(raw, index) for index, raw in enumerate(raw_points)]


def validate_payload(payload: Any) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """校验 /api/plan 请求体，返回 (points_spec, modes, options)；不合法抛 ApiError。"""
    points_spec = _validate_points(payload)

    raw_modes = payload.get("modes")
    if not isinstance(raw_modes, list):
        raise _invalid("modes 必须是数组", "每个相邻地点之间需要一个交通方式")
    expected = len(points_spec) - 1
    if len(raw_modes) != expected:
        raise _invalid(
            "modes 数量必须等于地点数减一",
            f"{len(points_spec)} 个地点需要 {expected} 个交通方式，当前 {len(raw_modes)} 个",
        )
    modes: list[str] = []
    for index, raw_mode in enumerate(raw_modes):
        if not isinstance(raw_mode, str) or raw_mode.strip() not in config.VALID_MODES:
            raise _invalid(
                f"不支持的交通方式：{raw_mode}",
                "可用方式：" + "、".join(config.VALID_MODES),
            )
        modes.append(raw_mode.strip())

    options = config.resolve_options(payload.get("options"))
    return points_spec, modes, options


def validate_compare_payload(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """校验 /api/compare 请求体：不需要 modes（传入则忽略），返回 (points_spec, options)。"""
    points_spec = _validate_points(payload)
    options = config.resolve_options(payload.get("options"))
    return points_spec, options


# --------------------------------------------------------------------- 地点解析
def _display_name(point: dict[str, Any]) -> str:
    return str(point.get("name") or point.get("formatted_address") or point.get("input") or "")


def resolve_points_demo(points_spec: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [demo.resolve_point(spec) for spec in points_spec]


def resolve_points_live(points_spec: list[dict[str, Any]], client: amap.AmapClient) -> list[dict[str, Any]]:
    """真实模式：address 走地理编码（同一请求内去重）；location 逆地理编码补全城市信息。"""
    cache: dict[str, dict[str, Any]] = {}
    resolved: list[dict[str, Any]] = []
    for spec in points_spec:
        if spec["location"] is not None:
            lng, lat = spec["location"]
            name = spec.get("name") or ""
            location_text = format_location(lng, lat)
            # 浏览器定位只给经纬度：逆地理编码补全地址/城市/citycode（公交规划需要 citycode）
            geo: dict[str, Any] = {}
            try:
                geo = client.regeo(location_text)
            except ApiError:
                geo = {}
            # 调用方未显式命名（name 为空或回显坐标）时，用逆地理编码地址展示
            explicit_name = name if name and name != spec.get("input") else ""
            display_name = explicit_name or geo.get("formatted_address") or name
            resolved.append(
                {
                    "input": spec["input"],
                    "name": display_name,
                    "formatted_address": geo.get("formatted_address") or display_name,
                    "city": geo.get("city") or "",
                    "citycode": geo.get("citycode") or "",
                    "location": {"lng": round(float(lng), 6), "lat": round(float(lat), 6)},
                }
            )
            continue
        cache_key = spec["address"]
        if cache_key not in cache:
            cache[cache_key] = client.geocode(cache_key)
        geocoded = cache[cache_key]
        resolved.append(
            {
                "input": spec["input"],
                "name": spec.get("name") or geocoded["name"],
                "formatted_address": geocoded["formatted_address"],
                "city": geocoded["city"],
                "citycode": geocoded["citycode"],
                "location": dict(geocoded["location"]),
            }
        )
    return resolved


# --------------------------------------------------------------------- 组装单段
def _assemble_leg(
    index: int,
    mode: str,
    from_point: dict[str, Any],
    to_point: dict[str, Any],
    route_data: dict[str, Any],
    cost: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    polyline = [
        [round(float(point[0]), 6), round(float(point[1]), 6)]
        for point in (route_data.get("polyline") or [])
        if isinstance(point, (list, tuple)) and len(point) == 2
    ]
    return {
        "index": index,
        "mode": mode,
        "mode_label": config.MODE_LABELS[mode],
        "from": {
            "name": _display_name(from_point),
            "location": {
                "lng": float(from_point["location"]["lng"]),
                "lat": float(from_point["location"]["lat"]),
            },
        },
        "to": {
            "name": _display_name(to_point),
            "location": {
                "lng": float(to_point["location"]["lng"]),
                "lat": float(to_point["location"]["lat"]),
            },
        },
        "distance_m": max(0, int(route_data.get("distance_m") or 0)),
        "duration_s": max(0, int(route_data.get("duration_s") or 0)),
        "cost": cost,
        "summary": str(route_data.get("summary") or ""),
        "steps": list(route_data.get("steps") or []),
        "polyline": limit_points(polyline, 500),
        "warnings": list(warnings),
    }


def _cost_for(mode: str, route_data: dict[str, Any], options: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    return costs.compute_cost(
        mode,
        distance_m=route_data.get("distance_m") or 0,
        transit_fee=route_data.get("transit_fee"),
        taxi_cost=route_data.get("taxi_cost"),
        tolls=route_data.get("tolls"),
        options=options,
    )


# --------------------------------------------------------------------- 单段构建
def _live_route(
    client: amap.AmapClient,
    mode: str,
    from_point: dict[str, Any],
    to_point: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    origin = format_location(from_point["location"]["lng"], from_point["location"]["lat"])
    destination = format_location(to_point["location"]["lng"], to_point["location"]["lat"])
    if mode == "walking":
        return client.walking(origin, destination)
    if mode == "bicycling":
        return client.bicycling(origin, destination)
    if mode in ("taxi", "driving"):
        return client.driving(origin, destination, mode=mode)
    return client.transit(
        origin,
        destination,
        from_point.get("citycode") or "",
        to_point.get("citycode") or "",
        options.get("transit_strategy", 0),
    )


def _route_data(
    mode: str,
    from_point: dict[str, Any],
    to_point: dict[str, Any],
    options: dict[str, Any],
    source: str,
    client: amap.AmapClient | None = None,
) -> dict[str, Any]:
    if source == "demo":
        return demo.route(mode, from_point, to_point, options)
    if client is None:
        raise ApiError("INTERNAL_ERROR", "内部错误：缺少高德客户端", "")
    return _live_route(client, mode, from_point, to_point, options)


def _build_leg(
    index: int,
    mode: str,
    points: list[dict[str, Any]],
    options: dict[str, Any],
    source: str,
    client: amap.AmapClient | None = None,
) -> dict[str, Any]:
    from_point, to_point = points[index], points[index + 1]
    route_data = _route_data(mode, from_point, to_point, options, source, client)
    cost, cost_warnings = _cost_for(mode, route_data, options)
    warnings = list(route_data.get("warnings") or []) + cost_warnings
    return _assemble_leg(index, mode, from_point, to_point, route_data, cost, warnings)


def _build_legs(
    modes: list[str],
    points: list[dict[str, Any]],
    options: dict[str, Any],
    source: str,
    client: amap.AmapClient | None = None,
) -> list[dict[str, Any]]:
    return [
        _build_leg(index, mode, points, options, source, client)
        for index, mode in enumerate(modes)
    ]


# --------------------------------------------------------------------- 主流程
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def summarize_totals(legs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "distance_m": sum(int(leg.get("distance_m") or 0) for leg in legs),
        "duration_s": sum(int(leg.get("duration_s") or 0) for leg in legs),
        "cost": costs.merge_costs([leg.get("cost") or {} for leg in legs]),
    }


def _empty_totals() -> dict[str, Any]:
    return {"distance_m": 0, "duration_s": 0, "cost": {"total": 0.0, "breakdown": []}}


def _mode_summary(legs: list[dict[str, Any]]) -> str:
    """整程可读摘要：把各段摘要（换乘/道路）按顺序拼接。"""
    parts = [str(leg.get("summary") or "").strip() for leg in legs]
    return " → ".join(part for part in parts if part)[:200]


def _unavailable_mode(mode: str, error: ApiError) -> dict[str, Any]:
    return {
        "mode": mode,
        "mode_label": config.MODE_LABELS[mode],
        "available": False,
        "error": error.to_dict()["error"],
        "legs": [],
        "totals": _empty_totals(),
        "summary": "",
    }


def plan(payload: Any) -> dict[str, Any]:
    points_spec, modes, options = validate_payload(payload)
    is_demo, _reason = config.demo_state()

    if is_demo:
        points = resolve_points_demo(points_spec)
        legs = _build_legs(modes, points, options, "demo")
        source = "demo"
    else:
        client = amap.AmapClient(config.get_amap_key())
        points = resolve_points_live(points_spec, client)
        legs = _build_legs(modes, points, options, "live", client)
        source = "live"

    return {
        "mode": source,
        "generated_at": _now_iso(),
        "points": points,
        "legs": legs,
        "totals": summarize_totals(legs),
    }


def compare(payload: Any) -> dict[str, Any]:
    """一次对比 5 种交通方式（契约 3.4）。

    地点只解析一次供 5 种方式共享；某种方式失败时隔离为 available=false，
    整体仍返回 200。
    """
    points_spec, options = validate_compare_payload(payload)
    is_demo, _reason = config.demo_state()

    if is_demo:
        points = resolve_points_demo(points_spec)
        source = "demo"
        client: amap.AmapClient | None = None
    else:
        client = amap.AmapClient(config.get_amap_key())
        points = resolve_points_live(points_spec, client)
        source = "live"

    legs_per_mode = len(points) - 1
    modes_result: list[dict[str, Any]] = []
    for mode in config.VALID_MODES:
        try:
            legs = _build_legs([mode] * legs_per_mode, points, options, source, client)
        except ApiError as exc:
            modes_result.append(_unavailable_mode(mode, exc))
            continue
        except Exception:  # 单一方式异常也要隔离，避免影响整体对比
            modes_result.append(
                _unavailable_mode(
                    mode,
                    ApiError("INTERNAL_ERROR", "该交通方式规划失败", "可尝试其他交通方式"),
                )
            )
            continue
        modes_result.append(
            {
                "mode": mode,
                "mode_label": config.MODE_LABELS[mode],
                "available": True,
                "error": None,
                "legs": legs,
                "totals": summarize_totals(legs),
                "summary": _mode_summary(legs),
            }
        )

    return {
        "mode": source,
        "generated_at": _now_iso(),
        "points": points,
        "modes": modes_result,
    }
