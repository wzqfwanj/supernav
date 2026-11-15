"""高德 Web 服务 API 客户端（地理编码 v3 + 路径规划 v5）。

解析原则（契约第 5 节）：字段可能是字符串或数字，结构缺失时降级而不是崩溃。
任何错误信息都会抹掉 Key 后再抛出，确保 Key 不进入响应或日志。
"""

from __future__ import annotations

from typing import Any

import requests

from .errors import ApiError
from .util import (
    as_float_opt,
    as_int,
    as_text,
    extend_polyline,
    format_location,
    limit_points,
    parse_location_text,
    redact,
)

BASE_URL = "https://restapi.amap.com"
TIMEOUT_S = 10
MAX_POLYLINE_POINTS = 500

#: 与 Key 相关的 infocode（高德文档中的鉴权/配额类错误），给出明确 hint
_KEY_INFOCODES = {
    "10001": "Key 无效或已过期",
    "10002": "Key 请求过于频繁（配额/并发超限）",
    "10003": "访问量超出 Key 配额",
    "10004": "Key 配额已用尽",
    "10005": "Key 被限制使用",
    "10006": "Key 绑定域名不匹配",
    "10007": "Key 绑定的服务类型不匹配",
    "10008": "Key 绑定的 IP 不匹配",
    "10009": "Key 与绑定平台不匹配",
    "10010": "Key 已被删除",
    "10011": "Key 已被禁用",
    "10012": "Key 未开通 Web 服务",
    "10013": "Key 签名校验失败",
}
_KEY_HINT = "请检查后端配置的 AMAP_KEY 是否为有效的「Web 服务」类型 Key"


def _polyline_points(raw: Any) -> list[list[float]]:
    points: list[list[float]] = []
    extend_polyline(points, raw)
    return points


def polyline_from_steps(steps: Any) -> list[list[float]]:
    """从 ``steps[].polyline`` 收集折线点（最多 500 个）。"""
    points: list[list[float]] = []
    if not isinstance(steps, list):
        return points
    for step in steps:
        if isinstance(step, dict):
            extend_polyline(points, step.get("polyline"))
    return limit_points(points, MAX_POLYLINE_POINTS)


def _route_polyline(steps: Any) -> list[list[float]]:
    """兼容少数返回 ``steps[].polyline`` 与直接 ``steps`` 结构的接口。"""
    points = polyline_from_steps(steps)
    if points:
        return points
    return limit_points(_polyline_points(steps), MAX_POLYLINE_POINTS)


class AmapClient:
    """同一次规划请求复用一个实例（复用 TCP 连接），Key 仅保存在实例内部。"""

    def __init__(self, key: str, session: requests.Session | None = None) -> None:
        self._key = (key or "").strip()
        self._session = session or requests.Session()

    # ---------------------------------------------------------------- 基础请求
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._key:
            raise ApiError("KEY_MISSING", "未配置高德 Key", "请设置环境变量 AMAP_KEY 或 config.local.json")
        query = {key: value for key, value in params.items() if value is not None}
        query["key"] = self._key
        try:
            response = self._session.get(BASE_URL + path, params=query, timeout=TIMEOUT_S)
            response.raise_for_status()
            data = response.json()
        except requests.Timeout:
            raise ApiError("AMAP_ERROR", "高德接口请求超时（10 秒）", "请稍后重试或检查网络")
        except requests.RequestException as exc:
            raise ApiError(
                "AMAP_ERROR",
                f"高德接口请求失败：{redact(exc, self._key)}",
                "请检查网络连通性",
            )
        except ValueError:
            raise ApiError("AMAP_ERROR", "高德接口返回了无法解析的数据", "")
        if not isinstance(data, dict):
            raise ApiError("AMAP_ERROR", "高德接口返回结构异常", "")
        if str(data.get("status")) != "1":
            info = as_text(data.get("info")) or "未知错误"
            infocode = as_text(data.get("infocode"))
            hint = ""
            key_problem = infocode in _KEY_INFOCODES or "KEY" in info.upper()
            if infocode in _KEY_INFOCODES:
                hint = f"{_KEY_INFOCODES[infocode]}；{_KEY_HINT}"
            elif key_problem:
                hint = _KEY_HINT
            message = f"高德接口返回错误：{info}（infocode={infocode or '未知'}）"
            if key_problem:
                message += "——疑似高德 Key 配置问题"
            raise ApiError("AMAP_ERROR", message, hint)
        return data

    # ---------------------------------------------------------------- 地理编码
    def geocode(self, address: str) -> dict[str, Any]:
        data = self._get("/v3/geocode/geo", {"address": address})
        geocodes = data.get("geocodes")
        if not isinstance(geocodes, list) or not geocodes:
            raise ApiError(
                "GEOCODE_FAILED",
                f"无法解析地址：{address}",
                '请换一个更具体的地址，或直接输入 "lng,lat" 经纬度',
            )
        first = geocodes[0]
        if not isinstance(first, dict):
            raise ApiError("GEOCODE_FAILED", f"无法解析地址：{address}", "")
        location = parse_location_text(first.get("location"))
        if location is None:
            raise ApiError("GEOCODE_FAILED", f"地址缺少有效经纬度：{address}", "")
        formatted = as_text(first.get("formatted_address")) or address
        return {
            "name": formatted,
            "formatted_address": formatted,
            "city": as_text(first.get("city")) or as_text(first.get("province")),
            "citycode": as_text(first.get("citycode")),
            "location": {"lng": round(location[0], 6), "lat": round(location[1], 6)},
        }

    # ---------------------------------------------------------------- 路径规划
    def driving(self, origin: str, destination: str) -> dict[str, Any]:
        data = self._get(
            "/v5/direction/driving",
            {
                "origin": origin,
                "destination": destination,
                "show_fields": "cost,navi,polyline",
                "strategy": "32",
            },
        )
        return self._parse_simple_route(data, with_cost_fields=True)

    def walking(self, origin: str, destination: str) -> dict[str, Any]:
        data = self._get(
            "/v5/direction/walking",
            {"origin": origin, "destination": destination, "show_fields": "cost,navi,polyline"},
        )
        return self._parse_simple_route(data)

    def bicycling(self, origin: str, destination: str) -> dict[str, Any]:
        data = self._get(
            "/v5/direction/bicycling",
            {"origin": origin, "destination": destination, "show_fields": "cost,navi,polyline"},
        )
        return self._parse_simple_route(data)

    def transit(
        self,
        origin: str,
        destination: str,
        city1: str = "",
        city2: str = "",
        strategy: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "origin": origin,
            "destination": destination,
            "strategy": str(as_int(strategy, 0)),
            "show_fields": "cost,navi,polyline",
        }
        city1 = as_text(city1)
        city2 = as_text(city2) or city1
        if city1:
            params["city1"] = city1
        if city2:
            params["city2"] = city2
        data = self._get("/v5/direction/transit/integrated", params)
        return self._parse_transit_route(data)

    # ---------------------------------------------------------------- 解析实现
    def _parse_simple_route(
        self, data: dict[str, Any], with_cost_fields: bool = False
    ) -> dict[str, Any]:
        route = data.get("route") if isinstance(data.get("route"), dict) else {}
        paths = route.get("paths") if isinstance(route.get("paths"), list) else []
        warnings: list[str] = []
        path = paths[0] if paths else None
        if not isinstance(path, dict):
            path = {}
            warnings.append("高德未返回可用路线，距离/耗时按 0 计")
        distance = max(0, as_int(path.get("distance")))
        cost = path.get("cost") if isinstance(path.get("cost"), dict) else {}
        duration = max(0, as_int(path.get("duration")))
        if duration == 0:
            duration = max(0, as_int(cost.get("duration")))
        taxi_cost = as_float_opt(route.get("taxi_cost")) if with_cost_fields else None
        if with_cost_fields and taxi_cost is None:
            taxi_cost = as_float_opt(cost.get("taxi_cost"))
        tolls = as_float_opt(cost.get("tolls")) if with_cost_fields else None
        return {
            "distance_m": distance,
            "duration_s": duration,
            "taxi_cost": taxi_cost,
            "tolls": tolls,
            "transit_fee": None,
            "summary": "",
            "steps": [],
            "polyline": _route_polyline(path.get("steps")),
            "warnings": warnings,
        }

    def _parse_transit_route(self, data: dict[str, Any]) -> dict[str, Any]:
        route = data.get("route") if isinstance(data.get("route"), dict) else {}
        transits = route.get("transits") if isinstance(route.get("transits"), list) else []
        warnings: list[str] = []
        transit = transits[0] if transits else None
        if not isinstance(transit, dict):
            transit = {}
            warnings.append("高德未返回可用公交方案，距离/耗时按 0 计")
        distance = max(0, as_int(transit.get("distance")))
        cost = transit.get("cost") if isinstance(transit.get("cost"), dict) else {}
        duration = max(0, as_int(transit.get("duration")))
        if duration == 0:
            duration = max(0, as_int(cost.get("duration")))
        fee = as_float_opt(cost.get("transit_fee"))
        if fee is None:
            fee = _fare_from_segments(transit)
        steps, summary = _transit_steps(transit)
        return {
            "distance_m": distance,
            "duration_s": duration,
            "taxi_cost": None,
            "tolls": None,
            "transit_fee": fee,
            "summary": summary,
            "steps": steps,
            "polyline": _transit_polyline(transit),
            "warnings": warnings,
        }


def _fare_from_segments(transit: dict[str, Any]) -> float | None:
    """票价兜底：segments[].cost 或 buslines[].ticket_price，累加后返回。"""
    segments = transit.get("segments")
    if not isinstance(segments, list):
        return None
    total: float | None = None
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        found: float | None = None
        segment_cost = segment.get("cost")
        if isinstance(segment_cost, dict):
            for key in ("transit_fee", "ticket_price", "fare", "price", "value"):
                found = as_float_opt(segment_cost.get(key))
                if found is not None:
                    break
        else:
            found = as_float_opt(segment_cost)
        if found is None:
            bus = segment.get("bus") if isinstance(segment.get("bus"), dict) else {}
            buslines = bus.get("buslines") if isinstance(bus.get("buslines"), list) else []
            for line in buslines:
                if isinstance(line, dict):
                    found = as_float_opt(line.get("ticket_price"))
                    if found is not None:
                        break
        if found is not None:
            total = (total or 0.0) + found
    return total


def _stop_name(value: Any) -> str:
    if isinstance(value, dict):
        return as_text(value.get("name"))
    return as_text(value)


def _transit_steps(transit: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """把 segments 按顺序转成简化步骤与可读摘要（步行 / 公交线路 / 地铁 / 火车）。"""
    segments = transit.get("segments")
    steps: list[dict[str, Any]] = []
    parts: list[str] = []
    if not isinstance(segments, list):
        return steps, ""
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        walking = segment.get("walking")
        if isinstance(walking, dict):
            distance = max(0, as_int(walking.get("distance")))
            duration = max(0, as_int(walking.get("duration")))
            if distance or duration:
                steps.append(
                    {
                        "type": "walking",
                        "text": f"步行 {distance} 米",
                        "distance_m": distance,
                        "duration_s": duration,
                    }
                )
                parts.append(f"步行 {distance} 米")
            continue
        bus = segment.get("bus")
        if isinstance(bus, dict):
            buslines = bus.get("buslines") if isinstance(bus.get("buslines"), list) else []
            line = buslines[0] if buslines and isinstance(buslines[0], dict) else {}
            name = as_text(line.get("name")) or "公交线路"
            # 高德把地铁线路也放在 buslines 里，用 type（如「地铁线路」）或线路名区分
            line_type = as_text(line.get("type"))
            kind = "subway" if ("地铁" in line_type or name.startswith("地铁")) else "bus"
            departure = _stop_name(line.get("departure_stop"))
            arrival = _stop_name(line.get("arrival_stop"))
            distance = max(0, as_int(line.get("distance")) or as_int(bus.get("distance")))
            duration = max(0, as_int(line.get("duration")) or as_int(bus.get("duration")))
            text = name
            if departure and arrival:
                text = f"{name}（{departure} → {arrival}）"
            steps.append(
                {
                    "type": kind,
                    "text": text,
                    "distance_m": distance,
                    "duration_s": duration,
                }
            )
            parts.append(text)
            continue
        railway = segment.get("railway")
        if isinstance(railway, dict):
            name = as_text(railway.get("name")) or "火车"
            departure = _stop_name(railway.get("departure_stop"))
            arrival = _stop_name(railway.get("arrival_stop"))
            distance = max(0, as_int(railway.get("distance")))
            duration = max(0, as_int(railway.get("time")) or as_int(railway.get("duration")))
            text = name
            if departure and arrival:
                text = f"{name}（{departure} → {arrival}）"
            steps.append(
                {
                    "type": "railway",
                    "text": text,
                    "distance_m": distance,
                    "duration_s": duration,
                }
            )
            parts.append(text)
            continue
        taxi = segment.get("taxi")
        if isinstance(taxi, dict):
            distance = max(0, as_int(taxi.get("distance")))
            duration = max(0, as_int(taxi.get("duration")))
            steps.append(
                {
                    "type": "taxi",
                    "text": "打车接驳",
                    "distance_m": distance,
                    "duration_s": duration,
                }
            )
            parts.append("打车接驳")
    return steps, " → ".join(parts)


def _transit_polyline(transit: dict[str, Any]) -> list[list[float]]:
    segments = transit.get("segments")
    if not isinstance(segments, list):
        return []
    points: list[list[float]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        extend_polyline(points, segment.get("polyline"))
        walking = segment.get("walking")
        if isinstance(walking, dict):
            for step in walking.get("steps") or []:
                if isinstance(step, dict):
                    extend_polyline(points, step.get("polyline"))
        bus = segment.get("bus")
        if isinstance(bus, dict):
            for line in bus.get("buslines") or []:
                if isinstance(line, dict):
                    extend_polyline(points, line.get("polyline"))
        railway = segment.get("railway")
        if isinstance(railway, dict):
            extend_polyline(points, railway.get("polyline"))
    return limit_points(points, MAX_POLYLINE_POINTS)


__all__ = [
    "AmapClient",
    "BASE_URL",
    "TIMEOUT_S",
    "MAX_POLYLINE_POINTS",
    "polyline_from_steps",
    "format_location",
]
