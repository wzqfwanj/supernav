"""SuperNav 后端单元测试。

完全离线运行：所有高德 HTTP 请求都被 monkeypatch 成假响应，不访问真实网络。
环境里没有 httpx（FastAPI TestClient 依赖它），这里用一个极简 ASGI 调用器代替。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
import requests

from backend import amap, config, costs, demo, planner
from backend.app import app
from backend.errors import ApiError
from backend.util import round2


# ============================================================ 测试基础设施
@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    """隔离真实环境变量与 config.local.json，保证测试确定性。"""
    config_file = tmp_path / "config.local.json"
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    monkeypatch.delenv("AMAP_KEY", raising=False)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    return config_file


def call_api(method: str, path: str, payload=None):
    """直接以 ASGI 方式调用应用，返回 (status, headers, body_bytes)。"""
    body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [(b"host", b"testserver")]
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "app": app,
        "root_path": "",
    }
    result: dict = {}
    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        # 请求体已读完：真实服务器会阻塞等待 disconnect，这里用永不完成的等待模拟，
        # 以便 FileResponse 的取消逻辑正常退出。
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
            result["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            result["body"] = result.get("body", b"") + message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return result["status"], result.get("headers", []), result.get("body", b"")


def get_json(method: str, path: str, payload=None):
    status, _headers, body = call_api(method, path, payload)
    return status, json.loads(body.decode("utf-8"))


def cents(value: float) -> int:
    return int(round(float(value) * 100))


def assert_cost_consistent(cost: dict, expected_keys: set[str] | None = None) -> None:
    """金额保留 2 位小数，breakdown 之和等于 total。"""
    assert isinstance(cost, dict)
    assert isinstance(cost["total"], (int, float))
    assert cost["total"] >= 0
    breakdown = cost["breakdown"]
    assert isinstance(breakdown, list)
    keys = set()
    for item in breakdown:
        assert set(item) == {"key", "label", "amount"}
        assert isinstance(item["key"], str) and item["key"]
        assert isinstance(item["label"], str) and item["label"]
        assert round(item["amount"], 2) == item["amount"]  # 金额保留 2 位小数
        keys.add(item["key"])
    assert cents(sum(item["amount"] for item in breakdown)) == cents(cost["total"])
    if expected_keys is not None:
        assert keys == expected_keys


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def install_amap(monkeypatch, routes: dict[str, object], call_log: list | None = None):
    """把 requests.Session.get 替换成按 URL 后缀分发的假实现。

    ``routes``: {路径后缀: payload 或 callable(params) -> payload}
    """
    log = call_log if call_log is not None else []

    def fake_get(self, url, params=None, timeout=None):
        params = dict(params or {})
        log.append({"url": url, "params": params, "timeout": timeout})
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return FakeResponse(payload(params) if callable(payload) else payload)
        raise AssertionError(f"测试发出了未预期的请求：{url}")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    return log


def geocode_payload(name: str, lng: float, lat: float, city: str = "北京市", citycode: str = "010"):
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "geocodes": [
            {
                "formatted_address": name,
                "location": f"{lng},{lat}",
                "city": city,
                "citycode": citycode,
                "province": city,
                "district": "",
            }
        ],
    }


GEOCODE_BY_ADDRESS = {
    "北京南站": geocode_payload("北京市丰台区北京南站", 116.378, 39.865),
    "故宫博物院": geocode_payload("北京市东城区景山前街4号", 116.397, 39.918),
}

DRIVING_TAXI_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "route": {
        "taxi_cost": "45",
        "paths": [
            {
                "distance": "12000",
                "duration": "1800",
                "cost": {"duration": "1800", "tolls": "0"},
                "steps": [{"polyline": "116.378,39.865;116.39,39.87;116.397,39.918"}],
            }
        ],
    },
}

DRIVING_FUEL_PAYLOAD = {
    "status": "1",
    "route": {
        "paths": [
            {
                # 故意混用 int / str
                "distance": 30000,
                "cost": {"duration": "2400", "tolls": "15"},
                "steps": [{"polyline": "116.378,39.865;116.39,39.87"}],
            }
        ]
    },
}

TRANSIT_PAYLOAD = {
    "status": "1",
    "route": {
        "transits": [
            {
                "distance": "8000",
                "cost": {"duration": "2400", "transit_fee": "5"},
                "segments": [
                    {
                        # 真实 v5：同一个 segment 同时含 walking 与 bus；
                        # 折线为 {"polyline": "..."} 嵌套对象；耗时在 cost.duration
                        "walking": {
                            "distance": "185",
                            "cost": {"duration": "237"},
                            "steps": [{"polyline": {"polyline": "116.378,39.865;116.379,39.866"}}],
                        },
                        "bus": {
                            "buslines": [
                                {
                                    "name": "地铁4号线大兴线(天宫院--安河桥北)",
                                    "type": "地铁线路",
                                    "distance": "4834",
                                    "cost": {"duration": "780"},
                                    "departure_stop": {"name": "北京南站"},
                                    "arrival_stop": {"name": "西单"},
                                    "polyline": {
                                        "polyline": "116.379,39.866;116.390,39.900;116.397,39.918"
                                    },
                                }
                            ]
                        },
                    },
                    {
                        "walking": {
                            "distance": "130",
                            "cost": {"duration": "100"},
                            "steps": [{"polyline": {"polyline": "116.397,39.918;116.398,39.919"}}],
                        },
                        "bus": {
                            "buslines": [
                                {
                                    "name": "地铁1号线八通线(苹果园--环球度假区)",
                                    "type": "地铁线路",
                                    "distance": "1300",
                                    "cost": {"duration": "200"},
                                    "departure_stop": {"name": "西单"},
                                    "arrival_stop": {"name": "天安门西"},
                                    "polyline": {"polyline": "116.398,39.919;116.397,39.918"},
                                }
                            ]
                        },
                    },
                    {
                        "walking": {
                            "distance": "200",
                            "cost": {"duration": "160"},
                            "steps": [{"polyline": {"polyline": "116.397,39.918;116.397,39.919"}}],
                        }
                    },
                ],
            }
        ]
    },
}

WALKING_PAYLOAD = {
    "status": "1",
    "route": {
        "paths": [
            {
                "distance": "1200",
                "duration": "900",
                "steps": [{"polyline": "116.378,39.865;116.397,39.918"}],
            }
        ]
    },
}


# ============================================================ 金额与费用模型
class TestMoneyAndCost:
    def test_round2_is_half_up(self):
        assert round2(1.005) == 1.01
        assert round2(2.675) == 2.68
        assert round2("3.14159") == 3.14
        assert round2(None) == 0.0
        assert round2("abc") == 0.0

    def test_build_cost_filters_non_positive_and_keeps_sum(self):
        cost = costs.build_cost(
            [("fuel", "油费", 18.72), ("toll", "过路费", 0), ("toll2", "零费", -3), ("x", "坏值", "abc")]
        )
        assert cost["total"] == 18.72
        assert [item["key"] for item in cost["breakdown"]] == ["fuel"]
        assert_cost_consistent(cost, {"fuel"})

    def test_build_cost_rounds_sum_first(self):
        cost = costs.build_cost([("a", "甲", 0.004), ("b", "乙", 0.004)])
        assert_cost_consistent(cost)
        assert cost["total"] == 0.01

    def test_walking_and_bicycling_are_free(self):
        for mode in ("walking", "bicycling"):
            cost, warnings = costs.compute_cost(mode, distance_m=12345, options=dict(config.DEFAULTS))
            assert cost == {"total": 0.0, "breakdown": []}
            assert warnings == []

    def test_driving_fuel_and_toll(self):
        cost, warnings = costs.compute_cost(
            "driving", distance_m=30000, tolls="15", options=dict(config.DEFAULTS)
        )
        # 30km × 8L/100km ÷ 100 × 7.8 元/L = 18.72
        assert cost["total"] == 33.72
        amounts = {item["key"]: item["amount"] for item in cost["breakdown"]}
        assert amounts == {"fuel": 18.72, "toll": 15.0}
        assert warnings == []
        assert_cost_consistent(cost, {"fuel", "toll"})

    def test_driving_without_toll_has_only_fuel(self):
        cost, _ = costs.compute_cost("driving", distance_m=10000, tolls="0", options=dict(config.DEFAULTS))
        assert [item["key"] for item in cost["breakdown"]] == ["fuel"]
        assert cost["total"] == 6.24

    def test_driving_uses_custom_options(self):
        options = dict(config.DEFAULTS)
        options.update({"fuel_price": 10.0, "fuel_consumption": 5.0})
        cost, _ = costs.compute_cost("driving", distance_m=10000, options=options)
        assert cost["total"] == 5.0

    def test_transit_uses_returned_fare(self):
        cost, warnings = costs.compute_cost("transit", transit_fee="5", options=dict(config.DEFAULTS))
        assert cost == {"total": 5.0, "breakdown": [{"key": "transit_fare", "label": "公交/地铁票价", "amount": 5.0}]}
        assert warnings == []

    def test_transit_missing_fare_warns_and_counts_zero(self):
        cost, warnings = costs.compute_cost("transit", transit_fee=None, options=dict(config.DEFAULTS))
        assert cost == {"total": 0.0, "breakdown": []}
        assert warnings == ["未返回公交票价，按 0 元计"]

    def test_taxi_prefers_amap_cost(self):
        cost, warnings = costs.compute_cost("taxi", distance_m=5000, taxi_cost=42.5, options=dict(config.DEFAULTS))
        assert cost["total"] == 42.5
        assert warnings == []

    def test_taxi_estimates_when_missing(self):
        cost, warnings = costs.compute_cost("taxi", distance_m=5000, taxi_cost=None, options=dict(config.DEFAULTS))
        assert cost["total"] == 17.6  # 13 + (5-3) × 2.3
        assert warnings == ["打车费为估算值"]
        assert_cost_consistent(cost, {"taxi_fare"})

    def test_merge_costs_merges_by_key(self):
        merged = costs.merge_costs(
            [
                {"total": 5.0, "breakdown": [{"key": "transit_fare", "label": "公交/地铁票价", "amount": 5.0}]},
                {"total": 4.0, "breakdown": [{"key": "transit_fare", "label": "公交/地铁票价", "amount": 4.0}]},
            ]
        )
        assert merged == {
            "total": 9.0,
            "breakdown": [{"key": "transit_fare", "label": "公交/地铁票价", "amount": 9.0}],
        }
        assert_cost_consistent(merged, {"transit_fare"})


# ============================================================ 演示模式
class TestDemoPlan:
    def test_demo_two_legs_mixed_modes(self):
        payload = {
            "points": [{"address": "北京南站"}, {"address": "故宫博物院"}, {"address": "北京西站"}],
            "modes": ["transit", "walking"],
        }
        status, data = get_json("POST", "/api/plan", payload)
        assert status == 200
        assert data["mode"] == "demo"
        assert len(data["points"]) == 3
        assert len(data["legs"]) == 2
        assert [leg["mode"] for leg in data["legs"]] == ["transit", "walking"]
        assert [leg["mode_label"] for leg in data["legs"]] == ["公交/地铁", "步行"]

        transit, walking = data["legs"]
        assert transit["index"] == 0 and walking["index"] == 1
        assert transit["summary"]
        assert [step["type"] for step in transit["steps"]] == ["walking", "subway", "walking"]
        assert transit["steps"][0]["distance_m"] > 0
        assert transit["cost"]["breakdown"][0]["key"] == "transit_fare"
        assert walking["cost"] == {"total": 0.0, "breakdown": []}
        assert walking["polyline"] and len(walking["polyline"]) <= 500

        for leg in data["legs"]:
            assert isinstance(leg["distance_m"], int) and leg["distance_m"] >= 0
            assert isinstance(leg["duration_s"], int) and leg["duration_s"] >= 0
            assert_cost_consistent(leg["cost"])
            assert isinstance(leg["warnings"], list)
            assert isinstance(leg["steps"], list)
            assert len(leg["polyline"]) <= 500

        totals = data["totals"]
        assert totals["distance_m"] == sum(leg["distance_m"] for leg in data["legs"])
        assert totals["duration_s"] == sum(leg["duration_s"] for leg in data["legs"])
        assert_cost_consistent(totals["cost"], {"transit_fare"})
        assert cents(totals["cost"]["total"]) == cents(sum(leg["cost"]["total"] for leg in data["legs"]))

        generated_at = datetime.fromisoformat(data["generated_at"])
        assert generated_at.tzinfo is not None

    @pytest.mark.parametrize("mode", ["walking", "bicycling", "transit", "taxi", "driving"])
    def test_demo_each_mode(self, mode):
        payload = {"points": [{"address": "北京南站"}, {"address": "国贸"}], "modes": [mode]}
        result = planner.plan(payload)
        assert result["mode"] == "demo"
        leg = result["legs"][0]
        assert leg["mode"] == mode
        assert leg["mode_label"] == config.MODE_LABELS[mode]
        assert_cost_consistent(leg["cost"])
        if mode in ("walking", "bicycling"):
            assert leg["cost"] == {"total": 0.0, "breakdown": []}
            assert leg["steps"] == []
            assert leg["summary"] == ""
        if mode == "transit":
            assert {item["key"] for item in leg["cost"]["breakdown"]} <= {"transit_fare"}
            assert leg["summary"]
        if mode == "taxi":
            assert {item["key"] for item in leg["cost"]["breakdown"]} == {"taxi_fare"}
        if mode == "driving":
            assert {item["key"] for item in leg["cost"]["breakdown"]} <= {"fuel", "toll"}
            assert "fuel" in {item["key"] for item in leg["cost"]["breakdown"]}

    def test_demo_is_deterministic(self):
        payload = {
            "points": [{"address": "中关村"}, {"address": "三里屯"}, {"address": "某个不存在的地方"}],
            "modes": ["taxi", "bicycling"],
        }
        first = planner.plan(payload)
        second = planner.plan(payload)
        assert first["points"] == second["points"]
        assert first["legs"] == second["legs"]
        assert first["totals"] == second["totals"]
        assert first["mode"] == second["mode"] == "demo"

    def test_demo_known_places_and_hash_fallback(self):
        known = demo.resolve_point({"input": "北京南站", "address": "北京南站", "location": None, "name": ""})
        assert known["location"]["lng"] == pytest.approx(116.378)
        assert known["location"]["lat"] == pytest.approx(39.865)
        assert known["citycode"] == "010"

        alias = demo.resolve_point({"input": "故宫", "address": "故宫", "location": None, "name": ""})
        assert alias["name"] == "故宫博物院"

        spec = {"input": "火星基地", "address": "火星基地", "location": None, "name": ""}
        first = demo.resolve_point(spec)
        second = demo.resolve_point(spec)
        assert first == second
        assert 115.5 < first["location"]["lng"] < 117.5
        assert 39.0 < first["location"]["lat"] < 40.5
        other = demo.resolve_point({"input": "月球基地", "address": "月球基地", "location": None, "name": ""})
        assert other["location"] != first["location"]

    def test_demo_location_input_echo(self):
        payload = {
            "points": [{"location": "116.378,39.865", "name": "起点"}, {"location": "116.397,39.918"}],
            "modes": ["walking"],
        }
        result = planner.plan(payload)
        assert result["points"][0]["input"] == "116.378,39.865"
        assert result["points"][0]["name"] == "起点"
        assert result["points"][1]["location"] == {"lng": 116.397, "lat": 39.918}
        assert result["legs"][0]["from"]["name"] == "起点"

    def test_demo_transit_fare_tiers(self):
        assert demo.transit_fare(5000) == 3.0
        assert demo.transit_fare(6000) == 3.0
        assert demo.transit_fare(6001) == 4.0
        assert demo.transit_fare(12000) == 4.0
        assert demo.transit_fare(12001) == 5.0
        assert demo.transit_fare(22000) == 5.0
        assert demo.transit_fare(22001) == 6.0
        assert demo.transit_fare(32000) == 6.0
        assert demo.transit_fare(32001) == 7.0
        assert demo.transit_fare(52001) == 8.0

    def test_demo_driving_toll_rule(self):
        payload = {"points": [{"address": "北京南站"}, {"address": "首都机场"}], "modes": ["driving"]}
        result = planner.plan(payload)
        leg = result["legs"][0]
        km = leg["distance_m"] / 1000.0
        toll = max(0.0, km - 20.0) * 0.6
        amounts = {item["key"]: item["amount"] for item in leg["cost"]["breakdown"]}
        assert amounts.get("toll", 0.0) == pytest.approx(round2(toll), abs=0.011)
        assert "fuel" in amounts

    def test_demo_mode_forced_by_env_even_with_key(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY")
        monkeypatch.setenv("DEMO_MODE", "1")

        def fail_get(*args, **kwargs):
            raise AssertionError("演示模式不应发起任何高德请求")

        monkeypatch.setattr(requests.Session, "get", fail_get)
        payload = {"points": [{"address": "天安门"}, {"address": "王府井"}], "modes": ["walking"]}
        result = planner.plan(payload)
        assert result["mode"] == "demo"


# ============================================================ 参数校验
class TestValidation:
    @pytest.mark.parametrize(
        "payload,expected_fragment",
        [
            ({"points": [{"address": "只有起点"}], "modes": []}, "至少需要起点和终点"),
            (
                {"points": [{"address": f"点{i}"} for i in range(11)], "modes": ["walking"] * 10},
                "最多支持 10 个地点",
            ),
            (
                {"points": [{"address": "A"}, {"address": "B"}, {"address": "C"}], "modes": ["walking"]},
                "modes 数量",
            ),
            ({"points": [{"address": "A"}, {"address": "B"}], "modes": ["flight"]}, "不支持的交通方式"),
            ({"points": [{"address": ""}, {"address": "B"}], "modes": ["walking"]}, "缺少地址"),
            ({"points": [{"name": "无地址"}, {"address": "B"}], "modes": ["walking"]}, "缺少地址"),
            ({"points": [{"location": "116.3"}, {"address": "B"}], "modes": ["walking"]}, "location"),
            (
                {"points": [{"location": "116.1234567,39.9"}, {"address": "B"}], "modes": ["walking"]},
                "6 位",
            ),
            ({"points": [{"address": "A"}, {"address": "B"}], "modes": ["walking"], "options": {"transit_strategy": 9}}, "transit_strategy"),
        ],
    )
    def test_invalid_inputs(self, payload, expected_fragment):
        status, data = get_json("POST", "/api/plan", payload)
        assert status == 400
        assert data["error"]["code"] == "INVALID_INPUT"
        assert expected_fragment in data["error"]["message"]
        assert isinstance(data["error"]["hint"], str)

    def test_points_not_list_returns_wrapped_422(self):
        status, data = get_json("POST", "/api/plan", {"points": "北京南站", "modes": []})
        assert status == 422
        assert data["error"]["code"] == "INVALID_INPUT"
        assert data["error"]["message"]

    def test_planner_raises_api_error(self):
        from backend.errors import ApiError

        with pytest.raises(ApiError) as excinfo:
            planner.plan({"points": [{"address": "A"}], "modes": []})
        assert excinfo.value.code == "INVALID_INPUT"
        assert excinfo.value.status_code == 400

    def test_options_can_be_partial(self):
        payload = {
            "points": [{"address": "北京南站"}, {"address": "国贸"}],
            "modes": ["driving"],
            "options": {"fuel_price": 8.5},
        }
        result = planner.plan(payload)
        leg = result["legs"][0]
        expected = round2(leg["distance_m"] / 1000.0 * 8.0 / 100.0 * 8.5)
        amounts = {item["key"]: item["amount"] for item in leg["cost"]["breakdown"]}
        assert amounts.get("fuel", 0.0) == pytest.approx(expected, abs=0.011)


# ============================================================ 真实模式（mock 高德）
class TestLiveMode:
    def _live_payload(self, modes):
        return {
            "points": [{"address": "北京南站"}, {"address": "故宫博物院"}],
            "modes": modes,
        }

    def test_live_taxi_uses_route_taxi_cost(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_TAXI")
        log = install_amap(
            monkeypatch,
            {"/v3/geocode/geo": lambda params: GEOCODE_BY_ADDRESS[params["address"]], "/v5/direction/driving": DRIVING_TAXI_PAYLOAD},
        )
        payload = self._live_payload(["taxi"])
        status, data = get_json("POST", "/api/plan", payload)
        assert status == 200
        assert data["mode"] == "live"
        assert data["points"][0]["citycode"] == "010"
        leg = data["legs"][0]
        assert leg["mode"] == "taxi"
        assert leg["distance_m"] == 12000
        assert leg["duration_s"] == 1800
        assert leg["cost"]["total"] == 45.0
        assert leg["warnings"] == []
        assert data["totals"]["cost"]["total"] == 45.0
        # 2 个不同地址 → 2 次地理编码，加 1 次驾车路径规划
        assert sum(1 for item in log if item["url"].endswith("/v3/geocode/geo")) == 2
        assert sum(1 for item in log if item["url"].endswith("/v5/direction/driving")) == 1
        assert all(item["timeout"] == amap.TIMEOUT_S for item in log)

    def test_live_driving_fuel_and_toll_with_str_int_mix(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_DRIVE")
        install_amap(
            monkeypatch,
            {
                "/v3/geocode/geo": lambda params: GEOCODE_BY_ADDRESS[params["address"]],
                "/v5/direction/driving": DRIVING_FUEL_PAYLOAD,
            },
        )
        status, data = get_json("POST", "/api/plan", self._live_payload(["driving"]))
        assert status == 200
        leg = data["legs"][0]
        assert leg["distance_m"] == 30000
        assert leg["duration_s"] == 2400  # duration 来自 paths[0].cost.duration
        amounts = {item["key"]: item["amount"] for item in leg["cost"]["breakdown"]}
        assert amounts == {"fuel": 18.72, "toll": 15.0}
        assert leg["cost"]["total"] == 33.72
        assert_cost_consistent(leg["cost"], {"fuel", "toll"})

    def test_live_transit_parses_steps_and_fare(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_TRANSIT")
        log = install_amap(
            monkeypatch,
            {
                "/v3/geocode/geo": lambda params: GEOCODE_BY_ADDRESS[params["address"]],
                "/v5/direction/transit/integrated": TRANSIT_PAYLOAD,
            },
        )
        status, data = get_json("POST", "/api/plan", self._live_payload(["transit"]))
        assert status == 200
        leg = data["legs"][0]
        assert leg["distance_m"] == 8000
        assert leg["duration_s"] == 2400
        assert leg["cost"] == {
            "total": 5.0,
            "breakdown": [{"key": "transit_fare", "label": "公交/地铁票价", "amount": 5.0}],
        }
        # 真实 v5 同一 segment 同时含 walking + bus，两类步骤都要保留
        assert [step["type"] for step in leg["steps"]] == [
            "walking",
            "subway",
            "walking",
            "subway",
            "walking",
        ]
        assert [step["distance_m"] for step in leg["steps"]] == [185, 4834, 130, 1300, 200]
        assert all(step["duration_s"] > 0 for step in leg["steps"])
        assert "步行 185 米" in leg["summary"]
        assert "地铁4号线" in leg["summary"]
        assert "地铁1号线" in leg["summary"]
        assert leg["polyline"]
        transit_call = [item for item in log if item["url"].endswith("/transit/integrated")][0]
        assert transit_call["params"]["city1"] == "010"
        assert transit_call["params"]["city2"] == "010"
        assert transit_call["params"]["strategy"] == "0"

    def test_live_taxi_estimate_when_taxi_cost_missing(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_EST")
        missing_taxi_cost = {
            "status": "1",
            "route": {
                "paths": [
                    {"distance": "5000", "duration": "1200", "steps": []},
                ]
            },
        }
        install_amap(
            monkeypatch,
            {
                "/v3/geocode/geo": lambda params: GEOCODE_BY_ADDRESS[params["address"]],
                "/v5/direction/driving": missing_taxi_cost,
            },
        )
        status, data = get_json("POST", "/api/plan", self._live_payload(["taxi"]))
        assert status == 200
        leg = data["legs"][0]
        assert leg["cost"]["total"] == 17.6
        assert leg["cost"]["breakdown"][0]["key"] == "taxi_fare"
        assert "打车费为估算值" in leg["warnings"]

    def test_live_transit_missing_fare_degrades(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_DEGRADE")
        partial = {
            "status": "1",
            "route": {
                "transits": [
                    {
                        "distance": "6000",
                        "segments": [
                            {"walking": {"distance": "200", "duration": "160"}},
                        ],
                    }
                ]
            },
        }
        install_amap(
            monkeypatch,
            {
                "/v3/geocode/geo": lambda params: GEOCODE_BY_ADDRESS[params["address"]],
                "/v5/direction/transit/integrated": partial,
            },
        )
        status, data = get_json("POST", "/api/plan", self._live_payload(["transit"]))
        assert status == 200
        leg = data["legs"][0]
        assert leg["cost"] == {"total": 0.0, "breakdown": []}
        assert "未返回公交票价，按 0 元计" in leg["warnings"]
        assert leg["distance_m"] == 6000
        assert data["totals"]["cost"]["total"] == 0.0

    def test_live_missing_route_structure_degrades(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_EMPTY")
        install_amap(
            monkeypatch,
            {
                "/v3/geocode/geo": lambda params: GEOCODE_BY_ADDRESS[params["address"]],
                "/v5/direction/walking": {"status": "1", "route": {}},
            },
        )
        status, data = get_json("POST", "/api/plan", self._live_payload(["walking"]))
        assert status == 200
        leg = data["legs"][0]
        assert leg["distance_m"] == 0
        assert leg["duration_s"] == 0
        assert leg["cost"] == {"total": 0.0, "breakdown": []}
        assert leg["polyline"] == []
        assert leg["warnings"]

    def test_geocode_deduplicates_same_address(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_DEDUPE")
        log = install_amap(
            monkeypatch,
            {
                "/v3/geocode/geo": lambda params: GEOCODE_BY_ADDRESS[params["address"]],
                "/v5/direction/walking": WALKING_PAYLOAD,
            },
        )
        payload = {
            "points": [{"address": "北京南站"}, {"address": "北京南站"}, {"address": "故宫博物院"}],
            "modes": ["walking", "walking"],
        }
        status, data = get_json("POST", "/api/plan", payload)
        assert status == 200
        geocode_calls = [item for item in log if item["url"].endswith("/v3/geocode/geo")]
        assert len(geocode_calls) == 2  # 重复地址只解析一次
        assert len(data["legs"]) == 2

    def test_location_points_skip_geocode(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_LOC")
        log = install_amap(monkeypatch, {"/v5/direction/walking": WALKING_PAYLOAD})
        payload = {
            "points": [{"location": "116.378,39.865"}, {"location": "116.397,39.918"}],
            "modes": ["walking"],
        }
        status, data = get_json("POST", "/api/plan", payload)
        assert status == 200
        assert not any(item["url"].endswith("/v3/geocode/geo") for item in log)
        assert data["points"][0]["citycode"] == ""
        assert data["points"][0]["name"] == "116.378,39.865"

    def test_amap_api_error_maps_to_amap_error(self, monkeypatch):
        secret = "SECRET_KEY_SHOULD_NOT_LEAK"
        monkeypatch.setenv("AMAP_KEY", secret)
        install_amap(
            monkeypatch,
            {
                "/v3/geocode/geo": {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"},
            },
        )
        status, _headers, body = call_api("POST", "/api/plan", self._live_payload(["walking"]))
        assert status == 502
        text = body.decode("utf-8")
        data = json.loads(text)
        assert data["error"]["code"] == "AMAP_ERROR"
        assert "INVALID_USER_KEY" in data["error"]["message"]
        assert "10001" in data["error"]["message"]
        assert secret not in text

    def test_geocode_failure_returns_geocode_failed(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_GEOFAIL")
        install_amap(monkeypatch, {"/v3/geocode/geo": {"status": "1", "geocodes": []}})
        status, data = get_json("POST", "/api/plan", self._live_payload(["walking"]))
        assert status == 422
        assert data["error"]["code"] == "GEOCODE_FAILED"
        assert "无法解析地址" in data["error"]["message"]

    def test_amap_request_error_maps_to_amap_error(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_REQERR")

        def raise_connection(self, url, params=None, timeout=None):
            raise requests.ConnectionError("连接失败")

        monkeypatch.setattr(requests.Session, "get", raise_connection)
        status, data = get_json("POST", "/api/plan", self._live_payload(["walking"]))
        assert status == 502
        assert data["error"]["code"] == "AMAP_ERROR"
        assert "请求失败" in data["error"]["message"]

    def test_key_missing_raises_key_missing(self):
        client = amap.AmapClient("")
        from backend.errors import ApiError

        with pytest.raises(ApiError) as excinfo:
            client.geocode("北京南站")
        assert excinfo.value.code == "KEY_MISSING"


# ============================================================ 状态/静态资源
class TestStatusAndStatic:
    def test_healthz(self):
        status, data = get_json("GET", "/api/healthz")
        assert status == 200
        assert data == {"status": "ok"}

    def test_status_demo_contract(self):
        status, data = get_json("GET", "/api/status")
        assert status == 200
        assert data["ok"] is True
        assert data["version"] == config.VERSION
        assert data["demo"] is True
        assert data["demo_reason"]
        assert data["defaults"] == config.DEFAULTS
        assert [item["value"] for item in data["modes"]] == [
            "walking",
            "bicycling",
            "transit",
            "taxi",
            "driving",
        ]
        assert all(item["label"] for item in data["modes"])

    def test_status_hides_amap_key(self, monkeypatch):
        secret = "SECRET_AMAP_KEY_123"
        monkeypatch.setenv("AMAP_KEY", secret)
        status, _headers, body = call_api("GET", "/api/status")
        assert status == 200
        text = body.decode("utf-8")
        data = json.loads(text)
        assert data["demo"] is False
        assert data["demo_reason"] == ""
        assert secret not in text

    def test_key_from_local_config_file(self, isolated_config, monkeypatch):
        isolated_config.write_text(json.dumps({"amap_key": "LOCAL_FILE_KEY"}), encoding="utf-8")
        assert config.get_amap_key() == "LOCAL_FILE_KEY"
        status, _headers, body = call_api("GET", "/api/status")
        assert status == 200
        data = json.loads(body.decode("utf-8"))
        assert data["demo"] is False
        assert "LOCAL_FILE_KEY" not in body.decode("utf-8")

    def test_env_key_overrides_local_config(self, isolated_config, monkeypatch):
        isolated_config.write_text(json.dumps({"amap_key": "LOCAL_FILE_KEY"}), encoding="utf-8")
        monkeypatch.setenv("AMAP_KEY", "ENV_KEY")
        assert config.get_amap_key() == "ENV_KEY"

    def test_demo_flag_from_local_config(self, isolated_config, monkeypatch):
        isolated_config.write_text(
            json.dumps({"amap_key": "LOCAL_FILE_KEY", "demo_mode": "auto"}), encoding="utf-8"
        )
        is_demo, _reason = config.demo_state()
        assert is_demo is False

    def test_root_and_static_dynamic_web_dir(self, monkeypatch, tmp_path):
        web_dir = tmp_path / "web"
        monkeypatch.setattr(config, "WEB_DIR", web_dir)

        status, _headers, body = call_api("GET", "/")
        assert status == 200
        assert json.loads(body.decode("utf-8"))["ok"] is True

        (web_dir / "styles.css").parent.mkdir(parents=True)
        (web_dir / "index.html").write_text("<html><body>SuperNav 测试页</body></html>", encoding="utf-8")
        (web_dir / "styles.css").write_text("body{color:#000}", encoding="utf-8")

        status, headers, body = call_api("GET", "/")
        assert status == 200
        assert "SuperNav 测试页" in body.decode("utf-8")
        content_type = dict(headers).get(b"content-type", b"").decode("utf-8")
        assert "text/html" in content_type

        status, headers, body = call_api("GET", "/styles.css")
        assert status == 200
        assert "color" in body.decode("utf-8")

        # /web/ 前缀同样可访问
        status, _headers, body = call_api("GET", "/web/index.html")
        assert status == 200
        assert "SuperNav 测试页" in body.decode("utf-8")

    def test_unknown_static_path_returns_404(self):
        status, data = get_json("GET", "/definitely-missing.css")
        assert status == 404
        assert data["error"]["code"] == "INVALID_INPUT"

    def test_plan_via_http_matches_contract_shape(self):
        payload = {
            "points": [{"address": "北京南站"}, {"address": "故宫博物院"}],
            "modes": ["taxi"],
        }
        status, data = get_json("POST", "/api/plan", payload)
        assert status == 200
        assert set(data) == {"mode", "generated_at", "points", "legs", "totals"}
        point = data["points"][0]
        assert set(point) == {"input", "name", "formatted_address", "city", "citycode", "location"}
        leg = data["legs"][0]
        assert set(leg) == {
            "index",
            "mode",
            "mode_label",
            "from",
            "to",
            "distance_m",
            "duration_s",
            "cost",
            "summary",
            "steps",
            "polyline",
            "warnings",
        }
        assert set(leg["cost"]) == {"total", "breakdown"}
        assert set(data["totals"]) == {"distance_m", "duration_s", "cost"}


class TestAmapErrorHandling:
    def test_retries_qps_error_then_succeeds(self, monkeypatch):
        """10021（QPS 超限）应退避重试一次而不是直接失败。"""
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_QPS")
        calls = {"n": 0}

        def fake_get(self, url, params=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(
                    {
                        "status": "0",
                        "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT",
                        "infocode": "10021",
                    }
                )
            return FakeResponse(GEOCODE_BY_ADDRESS["北京南站"])

        monkeypatch.setattr(requests.Session, "get", fake_get)
        monkeypatch.setattr(amap.time, "sleep", lambda _seconds: None)
        client = amap.AmapClient("TEST_KEY_QPS")
        result = client.geocode("北京南站")
        assert result["citycode"] == "010"
        assert calls["n"] == 2

    def test_no_route_error_has_friendly_message_and_hint(self, monkeypatch):
        """30007（无可用路线）应给出可读提示，而不是原始英文错误。"""
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_NOROUTE")
        install_amap(
            monkeypatch,
            {
                "/v5/direction/bicycling": {
                    "status": "0",
                    "info": "RESULTS_ARE_EMPTY",
                    "infocode": "30007",
                }
            },
        )
        client = amap.AmapClient("TEST_KEY_NOROUTE")
        with pytest.raises(ApiError) as exc:
            client.bicycling("116.378,39.865", "116.397,39.918")
        assert "未返回可用路线" in exc.value.message
        assert "可换一种方式" in exc.value.hint


# ============================================================ /api/compare（契约 3.4）
#: 步行：含 instruction / road_name / distance / cost.duration / step_distance / 嵌套 polyline
WALKING_STEPS_PAYLOAD = {
    "status": "1",
    "route": {
        "paths": [
            {
                "distance": "1500",
                "duration": "1080",
                "steps": [
                    {
                        "instruction": "向西步行 100 米",
                        "road_name": "南站路",
                        "distance": "100",
                        "duration": "72",
                        "polyline": {"polyline": "116.378,39.865;116.379,39.865"},
                    },
                    {
                        "instruction": "沿长安街步行",
                        "road_name": "长安街",
                        "step_distance": "1400",
                        "cost": {"duration": "1008"},
                        "polyline": [{"polyline": "116.379,39.865;116.397,39.918"}],
                    },
                ],
            }
        ]
    },
}

BICYCLING_STEPS_PAYLOAD = {
    "status": "1",
    "route": {
        "paths": [
            {
                "distance": "1600",
                "duration": "480",
                "steps": [
                    {
                        "instruction": "沿南二环骑行",
                        "road_name": "南二环",
                        "distance": 1600,
                        "duration": 480,
                        "polyline": "116.378,39.865;116.397,39.918",
                    }
                ],
            }
        ]
    },
}

DRIVING_STEPS_PAYLOAD = {
    "status": "1",
    "route": {
        "taxi_cost": "38",
        "paths": [
            {
                "distance": "9000",
                "duration": "1200",
                "cost": {"duration": "1200", "tolls": "5"},
                "steps": [
                    {
                        "instruction": "沿南二环行驶",
                        "road_name": "南二环",
                        "distance": 3000,
                        "duration": 400,
                        "polyline": "116.378,39.865;116.39,39.87",
                    },
                    {
                        "instruction": "进入长安街",
                        "road_name": "长安街",
                        "step_distance": "6000",
                        "cost": {"duration": "800"},
                        "polyline": {"polyline": "116.39,39.87;116.397,39.918"},
                    },
                ],
            }
        ],
    },
}

COMPARE_ROUTES = {
    "/v3/geocode/geo": lambda params: GEOCODE_BY_ADDRESS[params["address"]],
    "/v5/direction/walking": WALKING_STEPS_PAYLOAD,
    "/v5/direction/bicycling": BICYCLING_STEPS_PAYLOAD,
    "/v5/direction/driving": DRIVING_STEPS_PAYLOAD,
    "/v5/direction/transit/integrated": TRANSIT_PAYLOAD,
}
ZERO_TOTALS = {"distance_m": 0, "duration_s": 0, "cost": {"total": 0.0, "breakdown": []}}


def assert_mode_entry_shape(item: dict) -> None:
    """校验 compare.modes[] 单项结构（契约 3.4）。"""
    assert set(item) == {"mode", "mode_label", "available", "error", "legs", "totals", "summary"}
    assert item["mode"] in config.VALID_MODES
    assert item["mode_label"] == config.MODE_LABELS[item["mode"]]
    assert isinstance(item["summary"], str)
    if item["available"]:
        assert item["error"] is None
        assert isinstance(item["legs"], list)
        assert set(item["totals"]) == {"distance_m", "duration_s", "cost"}
    else:
        assert set(item["error"]) == {"code", "message", "hint"}
        assert item["error"]["code"] and item["error"]["message"]
        assert item["legs"] == []
        assert item["totals"] == ZERO_TOTALS


class TestCompareDemo:
    def test_compare_returns_five_modes(self):
        payload = {
            "points": [{"address": "北京南站"}, {"address": "故宫博物院"}, {"address": "北京西站"}],
        }
        status, data = get_json("POST", "/api/compare", payload)
        assert status == 200
        assert set(data) == {"mode", "generated_at", "points", "modes"}
        assert data["mode"] == "demo"
        assert len(data["points"]) == 3
        assert [item["mode"] for item in data["modes"]] == list(config.VALID_MODES)

        for item in data["modes"]:
            assert_mode_entry_shape(item)
            assert item["available"] is True
            assert len(item["legs"]) == 2
            assert all(leg["mode"] == item["mode"] for leg in item["legs"])
            assert item["totals"]["distance_m"] == sum(leg["distance_m"] for leg in item["legs"])
            assert item["totals"]["duration_s"] == sum(leg["duration_s"] for leg in item["legs"])
            assert cents(item["totals"]["cost"]["total"]) == cents(
                sum(leg["cost"]["total"] for leg in item["legs"])
            )
            assert_cost_consistent(item["totals"]["cost"])
            for leg in item["legs"]:
                assert_cost_consistent(leg["cost"])
                assert len(leg["polyline"]) <= 500
                assert isinstance(leg["steps"], list)

        by_mode = {item["mode"]: item for item in data["modes"]}
        assert by_mode["walking"]["totals"]["cost"]["total"] == 0.0
        assert by_mode["bicycling"]["totals"]["cost"]["total"] == 0.0
        for mode in ("transit", "taxi", "driving"):
            assert by_mode[mode]["totals"]["cost"]["total"] > 0
        assert by_mode["transit"]["summary"]
        generated_at = datetime.fromisoformat(data["generated_at"])
        assert generated_at.tzinfo is not None

    @pytest.mark.parametrize("mode", ["walking", "bicycling", "transit", "taxi", "driving"])
    def test_compare_single_leg_each_mode(self, mode):
        payload = {"points": [{"address": "北京南站"}, {"address": "国贸"}]}
        status, data = get_json("POST", "/api/compare", payload)
        assert status == 200
        item = next(entry for entry in data["modes"] if entry["mode"] == mode)
        assert_mode_entry_shape(item)
        assert item["available"] is True
        assert len(item["legs"]) == 1
        leg = item["legs"][0]
        assert leg["mode"] == mode
        assert item["totals"]["distance_m"] == leg["distance_m"]
        assert item["totals"]["duration_s"] == leg["duration_s"]
        assert item["totals"]["cost"] == leg["cost"]

    def test_compare_demo_supports_max_points(self):
        points = [{"address": name} for name in list(demo.DEMO_PLACES)[:10]]
        status, data = get_json("POST", "/api/compare", {"points": points})
        assert status == 200
        for item in data["modes"]:
            assert item["available"] is True
            assert len(item["legs"]) == 9

    def test_compare_ignores_modes_field(self):
        payload = {
            "points": [{"address": "天安门"}, {"address": "王府井"}],
            "modes": ["flying"],
            "unknown_field": 1,
        }
        status, data = get_json("POST", "/api/compare", payload)
        assert status == 200
        assert len(data["modes"]) == 5

    def test_compare_validation(self):
        status, data = get_json("POST", "/api/compare", {"points": [{"address": "北京南站"}]})
        assert status == 400
        assert data["error"]["code"] == "INVALID_INPUT"
        assert "至少需要起点和终点" in data["error"]["message"]

        too_many = {"points": [{"address": f"点{i}"} for i in range(11)]}
        status, data = get_json("POST", "/api/compare", too_many)
        assert status == 400
        assert data["error"]["code"] == "INVALID_INPUT"


class TestCompareLive:
    def _install(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_COMPARE")
        return install_amap(monkeypatch, COMPARE_ROUTES)

    def test_compare_geocodes_once_shared_by_all_modes(self, monkeypatch):
        log = self._install(monkeypatch)
        payload = {
            "points": [
                {"address": "北京南站"},
                {"address": "故宫博物院"},
                {"address": "北京南站"},
            ]
        }
        status, data = get_json("POST", "/api/compare", payload)
        assert status == 200
        assert data["mode"] == "live"
        geocode_calls = [item for item in log if item["url"].endswith("/v3/geocode/geo")]
        assert len(geocode_calls) == 2  # 3 个点只有 2 个不同地址，5 种方式共享
        assert all(item["timeout"] == amap.TIMEOUT_S for item in log)
        for item in data["modes"]:
            assert item["available"] is True
            assert len(item["legs"]) == 2
        # 3 点 → 每方式 2 段：walking/bicycling/transit 各 2 次 + driving 端点 4 次（taxi+driving）
        driving_calls = [item for item in log if item["url"].endswith("/v5/direction/driving")]
        assert len(driving_calls) == 4

    def test_compare_live_steps_and_summary(self, monkeypatch):
        self._install(monkeypatch)
        payload = {"points": [{"address": "北京南站"}, {"address": "故宫博物院"}]}
        status, data = get_json("POST", "/api/compare", payload)
        assert status == 200
        by_mode = {item["mode"]: item for item in data["modes"]}

        walking = by_mode["walking"]["legs"][0]
        assert [step["type"] for step in walking["steps"]] == ["walking", "walking"]
        assert len(walking["steps"]) <= 10
        first, second = walking["steps"]
        assert first["text"] == "向西步行 100 米"
        assert first["distance_m"] == 100
        assert first["duration_s"] == 72
        assert second["distance_m"] == 1400  # step_distance 容错
        assert second["duration_s"] == 1008  # cost.duration 容错
        assert "南站路" in walking["summary"] and "长安街" in walking["summary"]
        assert walking["polyline"]  # 嵌套 polyline 也要解析出来

        bicycling = by_mode["bicycling"]["legs"][0]
        assert [step["type"] for step in bicycling["steps"]] == ["bicycling"]
        assert bicycling["summary"] == "南二环"

        taxi = by_mode["taxi"]["legs"][0]
        assert [step["type"] for step in taxi["steps"]] == ["taxi", "taxi"]
        assert "长安街" in taxi["summary"]
        assert taxi["cost"]["breakdown"][0]["key"] == "taxi_fare"

        driving = by_mode["driving"]["legs"][0]
        assert [step["type"] for step in driving["steps"]] == ["driving", "driving"]
        assert "南二环" in driving["summary"]

        # 公交仍为乘车步骤（walking/subway 顺序），不受简单路线 steps 改动影响
        transit_steps = by_mode["transit"]["legs"][0]["steps"]
        assert [step["type"] for step in transit_steps][:2] == ["walking", "subway"]

    def test_plan_also_gets_simple_route_steps(self, monkeypatch):
        """步骤增强同样作用于 /api/plan（结构不变，steps 更丰富）。"""
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_PLAN_STEPS")
        install_amap(
            monkeypatch,
            {
                "/v3/geocode/geo": lambda params: GEOCODE_BY_ADDRESS[params["address"]],
                "/v5/direction/walking": WALKING_STEPS_PAYLOAD,
            },
        )
        status, data = get_json(
            "POST",
            "/api/plan",
            {"points": [{"address": "北京南站"}, {"address": "故宫博物院"}], "modes": ["walking"]},
        )
        assert status == 200
        leg = data["legs"][0]
        assert set(data) == {"mode", "generated_at", "points", "legs", "totals"}
        assert [step["type"] for step in leg["steps"]] == ["walking", "walking"]
        assert "南站路" in leg["summary"]

    @pytest.mark.parametrize(
        "bad_mode,bad_path",
        [
            ("transit", "/v5/direction/transit/integrated"),
            ("bicycling", "/v5/direction/bicycling"),
        ],
    )
    def test_compare_isolates_single_mode_failure(self, monkeypatch, bad_mode, bad_path):
        routes = dict(COMPARE_ROUTES)
        routes[bad_path] = {
            "status": "0",
            "info": "RESULTS_ARE_EMPTY",
            "infocode": "30007",
        }
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_COMPARE_FAIL")
        install_amap(monkeypatch, routes)
        payload = {"points": [{"address": "北京南站"}, {"address": "故宫博物院"}]}
        status, data = get_json("POST", "/api/compare", payload)
        assert status == 200
        by_mode = {item["mode"]: item for item in data["modes"]}
        bad = by_mode[bad_mode]
        assert bad["available"] is False
        assert bad["error"]["code"] == "AMAP_ERROR"
        assert "未返回可用路线" in bad["error"]["message"]
        assert bad["error"]["hint"]
        assert bad["legs"] == []
        assert bad["totals"] == ZERO_TOTALS
        for mode in config.VALID_MODES:
            if mode == bad_mode:
                continue
            assert by_mode[mode]["available"] is True, mode
            assert by_mode[mode]["legs"], mode

    def test_compare_isolates_unexpected_exception(self, monkeypatch):
        """非 ApiError 的意外异常也只影响该方式，整体仍 200。"""
        real_build_legs = planner._build_legs

        def flaky_build_legs(modes, points, options, source, client=None):
            if modes and modes[0] == "walking":
                raise RuntimeError("boom")
            return real_build_legs(modes, points, options, source, client)

        monkeypatch.setattr(planner, "_build_legs", flaky_build_legs)
        status, data = get_json(
            "POST", "/api/compare", {"points": [{"address": "北京南站"}, {"address": "故宫博物院"}]}
        )
        assert status == 200
        by_mode = {item["mode"]: item for item in data["modes"]}
        assert by_mode["walking"]["available"] is False
        assert by_mode["walking"]["error"]["code"] == "INTERNAL_ERROR"
        assert by_mode["walking"]["legs"] == []
        assert by_mode["walking"]["totals"] == ZERO_TOTALS
        assert by_mode["driving"]["available"] is True

    def test_compare_geocode_failure_is_4xx(self, monkeypatch):
        monkeypatch.setenv("AMAP_KEY", "TEST_KEY_COMPARE_GEOFAIL")
        install_amap(monkeypatch, {"/v3/geocode/geo": {"status": "1", "geocodes": []}})
        status, data = get_json(
            "POST", "/api/compare", {"points": [{"address": "北京南站"}, {"address": "故宫博物院"}]}
        )
        assert status == 422
        assert data["error"]["code"] == "GEOCODE_FAILED"
        assert "无法解析地址" in data["error"]["message"]

