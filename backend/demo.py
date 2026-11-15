"""演示模式：确定性模拟路线与费用（契约第 6 节）。

无高德 Key（或 DEMO_MODE=1）时使用。所有数据由输入地址/坐标稳定哈希得到，
同样的请求永远返回同样的路线与金额，便于前端联调与自动化测试。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from .costs import estimate_taxi_fee
from .errors import ApiError

#: 契约要求覆盖的内置示例地点（北京）
DEMO_PLACES: dict[str, dict[str, Any]] = {
    "北京南站": {
        "lng": 116.378,
        "lat": 39.865,
        "formatted_address": "北京市丰台区北京南站",
        "city": "北京市",
        "citycode": "010",
    },
    "故宫博物院": {
        "lng": 116.397,
        "lat": 39.918,
        "formatted_address": "北京市东城区景山前街4号",
        "city": "北京市",
        "citycode": "010",
    },
    "北京西站": {
        "lng": 116.322,
        "lat": 39.895,
        "formatted_address": "北京市丰台区北京西站",
        "city": "北京市",
        "citycode": "010",
    },
    "国贸": {
        "lng": 116.461,
        "lat": 39.909,
        "formatted_address": "北京市朝阳区建国门外大街1号",
        "city": "北京市",
        "citycode": "010",
    },
    "中关村": {
        "lng": 116.316,
        "lat": 39.984,
        "formatted_address": "北京市海淀区中关村",
        "city": "北京市",
        "citycode": "010",
    },
    "天安门": {
        "lng": 116.397,
        "lat": 39.908,
        "formatted_address": "北京市东城区天安门广场",
        "city": "北京市",
        "citycode": "010",
    },
    "王府井": {
        "lng": 116.411,
        "lat": 39.915,
        "formatted_address": "北京市东城区王府井大街",
        "city": "北京市",
        "citycode": "010",
    },
    "三里屯": {
        "lng": 116.455,
        "lat": 39.937,
        "formatted_address": "北京市朝阳区三里屯",
        "city": "北京市",
        "citycode": "010",
    },
    "首都机场": {
        "lng": 116.593,
        "lat": 39.943,
        "formatted_address": "北京市顺义区首都机场",
        "city": "北京市",
        "citycode": "010",
    },
    "北京大学": {
        "lng": 116.310,
        "lat": 39.992,
        "formatted_address": "北京市海淀区颐和园路5号",
        "city": "北京市",
        "citycode": "010",
    },
}

#: 常见别名 -> 内置地点
ALIASES: dict[str, str] = {
    "故宫": "故宫博物院",
    "紫禁城": "故宫博物院",
    "天安门广场": "天安门",
    "首都机场T3": "首都机场",
    "北京首都国际机场": "首都机场",
    "北大": "北京大学",
    "国贸站": "国贸",
}

MODE_FACTOR = {"walking": 1.15, "bicycling": 1.2, "driving": 1.3, "taxi": 1.3, "transit": 1.25}
MODE_SPEED_KMH = {"walking": 4.5, "bicycling": 12.0, "driving": 40.0, "taxi": 40.0, "transit": 25.0}

TRANSIT_WAIT_S = 300
TRANSIT_FEEDER_M = 1000
WALK_SPEED_MPS = 4.5 * 1000.0 / 3600.0
EARTH_RADIUS_M = 6371000.0

DEMO_LINES = ["地铁1号线", "地铁2号线", "地铁4号线", "地铁10号线", "公交300路"]


# --------------------------------------------------------------------- 工具函数
def _stable_ratio(text: str, start: int = 0, width: int = 4) -> float:
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    chunk = digest[start : start + width]
    return int(chunk, 16) / float(16**width)


def _hash_location(address: str) -> tuple[float, float]:
    """按稳定哈希在北京中心附近生成坐标（lng ±0.25°，lat ±0.18°）。"""
    lng = 116.397 + (_stable_ratio(address, 0) - 0.5) * 0.5
    lat = 39.908 + (_stable_ratio(address, 4) - 0.5) * 0.36
    return round(lng, 6), round(lat, 6)


def _lookup_place(address: str) -> tuple[str | None, dict[str, Any] | None]:
    name = address.strip()
    alias = ALIASES.get(name)
    if alias and alias in DEMO_PLACES:
        return alias, DEMO_PLACES[alias]
    if name in DEMO_PLACES:
        return name, DEMO_PLACES[name]
    for key, place in DEMO_PLACES.items():
        if key in name:
            return key, place
    return None, None


def haversine_m(start: tuple[float, float], end: tuple[float, float]) -> float:
    """两点间直线距离（米）。"""
    lng1, lat1 = start
    lng2, lat2 = end
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def transit_fare(distance_m: int) -> float:
    """公交票价阶梯：≤6km 3 元、≤12km 4 元、≤22km 5 元、≤32km 6 元，更远每 20km +1 元。"""
    km = max(0.0, float(distance_m)) / 1000.0
    if km <= 6:
        return 3.0
    if km <= 12:
        return 4.0
    if km <= 22:
        return 5.0
    if km <= 32:
        return 6.0
    return 6.0 + math.ceil((km - 32.0) / 20.0)


def _demo_polyline(start: tuple[float, float], end: tuple[float, float], samples: int = 24) -> list[list[float]]:
    """近似直线折线，带由稳定哈希决定的轻微弧度。"""
    bend = _stable_ratio(f"{start[0]:.6f},{start[1]:.6f};{end[0]:.6f},{end[1]:.6f}", 8) - 0.5
    amplitude = 0.0035 * bend
    points: list[list[float]] = []
    for index in range(samples + 1):
        ratio = index / samples
        lng = start[0] + (end[0] - start[0]) * ratio
        lat = start[1] + (end[1] - start[1]) * ratio
        offset = amplitude * math.sin(math.pi * ratio)
        points.append([round(lng + offset, 6), round(lat + offset * 0.5, 6)])
    return points


def _transit_detail(
    start: tuple[float, float],
    end: tuple[float, float],
    distance_m: int,
    duration_s: int,
) -> tuple[list[dict[str, Any]], str]:
    seed = f"{start[0]:.6f},{start[1]:.6f};{end[0]:.6f},{end[1]:.6f}"
    index = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(DEMO_LINES)
    line = DEMO_LINES[index]
    feeder = TRANSIT_FEEDER_M // 2
    walk_time = int(round(feeder / WALK_SPEED_MPS))
    ride_distance = max(0, int(distance_m) - TRANSIT_FEEDER_M)
    ride_time = max(0, int(duration_s) - 2 * walk_time - TRANSIT_WAIT_S)
    kind = "subway" if line.startswith("地铁") else "bus"
    steps = [
        {
            "type": "walking",
            "text": f"步行 {feeder} 米至站点",
            "distance_m": feeder,
            "duration_s": walk_time,
        },
        {
            "type": kind,
            "text": f"乘坐{line}",
            "distance_m": ride_distance,
            "duration_s": ride_time,
        },
        {
            "type": "walking",
            "text": f"步行 {feeder} 米到达终点",
            "distance_m": feeder,
            "duration_s": walk_time,
        },
    ]
    summary = f"步行 {feeder} 米 → {line} → 步行 {feeder} 米"
    return steps, summary


# --------------------------------------------------------------------- 对外接口
def resolve_point(spec: dict[str, Any]) -> dict[str, Any]:
    """把 planner 归一化后的地点解析成响应中的 point 结构（确定性）。"""
    if spec.get("location") is not None:
        lng, lat = spec["location"]
        name = spec.get("name") or ""
        return {
            "input": spec["input"],
            "name": name,
            "formatted_address": name,
            "city": "北京市",
            "citycode": "010",
            "location": {"lng": round(float(lng), 6), "lat": round(float(lat), 6)},
        }
    address = str(spec.get("address") or "").strip()
    key, place = _lookup_place(address)
    if place is not None:
        return {
            "input": spec["input"],
            "name": key or address,
            "formatted_address": place["formatted_address"],
            "city": place["city"],
            "citycode": place["citycode"],
            "location": {"lng": place["lng"], "lat": place["lat"]},
        }
    lng, lat = _hash_location(address)
    formatted = address if address.startswith("北京") else f"北京市{address}"
    return {
        "input": spec["input"],
        "name": address,
        "formatted_address": formatted,
        "city": "北京市",
        "citycode": "010",
        "location": {"lng": lng, "lat": lat},
    }


def route(
    mode: str,
    from_point: dict[str, Any],
    to_point: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    """生成一段确定性模拟路线（距离/耗时/费用输入/折线/steps）。"""
    if mode not in MODE_FACTOR:
        raise ApiError("INVALID_INPUT", f"不支持的交通方式：{mode}", "")
    start = (from_point["location"]["lng"], from_point["location"]["lat"])
    end = (to_point["location"]["lng"], to_point["location"]["lat"])
    straight = haversine_m(start, end)
    route_distance = straight * MODE_FACTOR[mode]

    if mode == "transit":
        distance = route_distance + TRANSIT_FEEDER_M
        duration = (
            route_distance / (MODE_SPEED_KMH["transit"] * 1000.0 / 3600.0)
            + TRANSIT_FEEDER_M / WALK_SPEED_MPS
            + TRANSIT_WAIT_S
        )
    else:
        distance = route_distance
        duration = route_distance / (MODE_SPEED_KMH[mode] * 1000.0 / 3600.0)

    distance_m = max(0, int(round(distance)))
    duration_s = max(0, int(round(duration)))

    taxi_cost: float | None = None
    tolls: float | None = None
    transit_fee: float | None = None
    steps: list[dict[str, Any]] = []
    summary = ""

    if mode == "taxi":
        taxi_cost = estimate_taxi_fee(distance_m, options)
    elif mode == "driving":
        tolls = max(0.0, distance_m / 1000.0 - 20.0) * 0.6
    elif mode == "transit":
        transit_fee = transit_fare(distance_m)
        steps, summary = _transit_detail(start, end, distance_m, duration_s)

    return {
        "distance_m": distance_m,
        "duration_s": duration_s,
        "taxi_cost": taxi_cost,
        "tolls": tolls,
        "transit_fee": transit_fee,
        "summary": summary,
        "steps": steps,
        "polyline": _demo_polyline(start, end),
        "warnings": [],
    }
