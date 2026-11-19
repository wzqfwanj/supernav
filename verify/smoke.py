#!/usr/bin/env python
"""SuperNav 交通规划 MVP —— 独立验证脚本（verify 角色，只读业务代码、不改业务代码）。

设计目标
--------
1. 黑盒验证：实际启动/连接 ``http://127.0.0.1:8014`` 上的 FastAPI 服务，调用真实接口。
2. 独立复算：不 import 业务实现来算钱，而是用响应里的 ``distance_m`` 与契约公式**重新算一遍**，
   再和接口返回的金额逐项比对（driving 油费/过路费、taxi 估算、transit 票价阶梯、合计）。
3. 自带断言与退出码：全部 PASS 退出码 0；任一 FAIL 退出码 1；结果落盘到
   ``verify/artifacts/smoke-run.json`` 供写报告引用。
4. 覆盖：契约字段、参数校验、10 点上限、直接经纬度、费用公式、``/api/compare`` 5 方式对比、
   单方式失败隔离（代码审查 + mock 单测）、起点定位/regeo（代码审查 + mock 单测）、
   安全（Key 不泄漏 / .gitignore）、前端静态资源、前端关键逻辑代码审查（含对比/组合/定位）、
   ``node --check``、后端 pytest。

用法
----
::

    # 方式 A（推荐）：先手动启动服务，再跑 smoke
    python -m uvicorn backend.app:app --host 127.0.0.1 --port 8014
    python verify/smoke.py

    # 方式 B：脚本自己拉起 8014 服务，跑完自动关掉（可重复运行）
    python verify/smoke.py --auto-server

    # 指定别的地址（例如 Lead 在 8000 上起的服务）
    $env:BASE_URL = "http://127.0.0.1:8000"; python verify/smoke.py

退出码：0 = 全部通过；1 = 存在 FAIL / 运行异常。
不访问真实高德接口：脚本强制在没有 AMAP_KEY 的环境下以演示模式验证；若检测到真实模式，
依赖演示数据的检查会 FAIL 并说明原因（不会联网）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import requests

# --------------------------------------------------------------------------- 常量
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = PROJECT_ROOT / "verify" / "artifacts"
DEFAULT_BASE_URL = "http://127.0.0.1:8014"
HTTP_TIMEOUT = 30

MODES = ["walking", "bicycling", "transit", "taxi", "driving"]
MODE_LABELS = {
    "walking": "步行",
    "bicycling": "骑行",
    "transit": "公交/地铁",
    "taxi": "打车",
    "driving": "驾车",
}
DEFAULTS = {
    "fuel_price": 7.8,
    "fuel_consumption": 8.0,
    "transit_strategy": 0,
    "taxi_base_fare": 13.0,
    "taxi_base_km": 3.0,
    "taxi_per_km": 2.3,
}
COST_LABELS = {
    "transit_fare": "公交/地铁票价",
    "taxi_fare": "打车费",
    "fuel": "油费",
    "toll": "过路费",
}
MONEY_TOL = 0.011  # 1 分钱容差（金额四舍五入到 0.01）

# 内置示例地点（演示模式覆盖，docs/API-CONTRACT.md 第 6 节）
PLACES = ["北京南站", "故宫博物院", "北京西站", "国贸", "中关村", "天安门", "王府井", "三里屯", "首都机场", "北京大学"]

# 当前契约实现版本（backend/__init__.py 的 __version__；v0.5.0 起前端为黑白灰单主题）
EXPECTED_VERSION = "0.5.0"


# --------------------------------------------------------------------------- 小工具
def round2(value) -> float:
    """契约口径：金额先求和再四舍五入到 0.01（ROUND_HALF_UP），独立实现。"""
    if value is None:
        return 0.0
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (ArithmeticError, ValueError):
        return 0.0


def cents(value) -> int:
    """金额换成整数「分」，避免浮点比较。"""
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return -10**9


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def money_2dp(value) -> bool:
    """金额保留 2 位小数（接口可能返回 3.0 这种 1 位小数的 float，语义上仍是 2 位精度）。"""
    if not is_number(value):
        return False
    return cents(value) == int(round(float(value) * 100)) and abs(round2(value) - float(value)) < 1e-9


def iso_with_tz(text) -> bool:
    if not isinstance(text, str) or not text:
        return False
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def transit_fare(distance_m: int) -> float:
    """独立复算演示模式公交票价阶梯（docs/API-CONTRACT.md 第 6 节）。"""
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


def taxi_estimate(distance_m: int, options: dict) -> float:
    """独立复算打车估算：起步价（含 base_km 公里）+ 超出部分 × 每公里单价。"""
    km = max(0.0, float(distance_m)) / 1000.0
    base_fare = float(options["taxi_base_fare"])
    base_km = float(options["taxi_base_km"])
    per_km = float(options["taxi_per_km"])
    return base_fare + max(0.0, km - base_km) * per_km


def driving_expected(distance_m: int, options: dict) -> tuple[float, float]:
    """独立复算 driving：油费 = km×油耗/100×油价；过路费 = max(0, km-20)×0.6。"""
    km = max(0.0, float(distance_m)) / 1000.0
    fuel = km * float(options["fuel_consumption"]) / 100.0 * float(options["fuel_price"])
    toll = max(0.0, km - 20.0) * 0.6
    return fuel, toll


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- 检查器
class Checker:
    def __init__(self) -> None:
        self.results: list[dict] = []
        self.passed = 0
        self.failed = 0
        self.warnings: list[dict] = []

    # -- 基础记录 -----------------------------------------------------------
    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        entry = {"name": name, "ok": ok, "detail": "" if ok else str(detail)[:1500]}
        self.results.append(entry)
        if ok:
            self.passed += 1
            print(f"PASS  {name}")
        else:
            self.failed += 1
            print(f"FAIL  {name}  ::  {str(detail)[:600]}")
        return ok

    def warn(self, name: str, detail: str) -> None:
        self.warnings.append({"name": name, "detail": str(detail)[:1500]})
        print(f"WARN  {name}  ::  {str(detail)[:400]}")

    def eq(self, name: str, actual, expected) -> bool:
        return self.check(name, actual == expected, f"actual={actual!r} expected={expected!r}")

    def close(self, name: str, actual, expected, tol: float = MONEY_TOL) -> bool:
        ok = is_number(actual) and is_number(expected) and abs(float(actual) - float(expected)) <= tol + 1e-9
        return self.check(name, ok, f"actual={actual!r} expected≈{expected!r} (tol={tol})")

    # -- HTTP ---------------------------------------------------------------
    def http(self, session: requests.Session, name: str, method: str, url: str, **kwargs):
        """返回 (status, text, data)；传输失败返回 (None, "", None) 并记一条 FAIL。"""
        try:
            response = session.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            self.check(f"{name}.transport", False, f"{type(exc).__name__}: {exc}")
            return None, "", None
        text = response.text
        data = None
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        return response.status_code, text, data

    # -- 通用结构 -----------------------------------------------------------
    def check_cost(self, prefix: str, cost, expected_keys: set[str] | None = None):
        """校验 cost 结构：total/breakdown、2 位小数、明细和等于 total。返回 (amounts, labels)。"""
        amounts: dict[str, float] = {}
        labels: dict[str, str] = {}
        if not isinstance(cost, dict):
            self.check(f"{prefix}.object", False, f"cost 不是对象：{cost!r}")
            return amounts, labels
        total = cost.get("total")
        breakdown = cost.get("breakdown")
        self.check(f"{prefix}.total_number", is_number(total) and float(total) >= 0, f"total={total!r}")
        self.check(f"{prefix}.total_2dp", money_2dp(total), f"total={total!r}")
        if not isinstance(breakdown, list):
            self.check(f"{prefix}.breakdown_list", False, f"breakdown={breakdown!r}")
            return amounts, labels
        items_ok = True
        detail = ""
        for index, item in enumerate(breakdown):
            if not isinstance(item, dict):
                items_ok = False
                detail = f"[{index}] 不是对象"
                break
            if set(item.keys()) != {"key", "label", "amount"}:
                items_ok = False
                detail = f"[{index}] 字段={sorted(item.keys())}"
                break
            if not (isinstance(item["key"], str) and item["key"]):
                items_ok = False
                detail = f"[{index}] key 非法"
                break
            if not (isinstance(item["label"], str) and item["label"]):
                items_ok = False
                detail = f"[{index}] label 非法"
                break
            if not (is_number(item["amount"]) and float(item["amount"]) > 0):
                items_ok = False
                detail = f"[{index}] amount={item['amount']!r}（应为正数，0/负值不应入明细）"
                break
            if not money_2dp(item["amount"]):
                items_ok = False
                detail = f"[{index}] amount={item['amount']!r} 不是 2 位小数"
                break
            amounts[item["key"]] = float(item["amount"])
            labels[item["key"]] = item["label"]
        self.check(f"{prefix}.breakdown_items", items_ok, detail)
        duplicate = len(breakdown) != len(amounts)
        self.check(f"{prefix}.breakdown_unique_keys", not duplicate, "breakdown 中存在重复 key")
        self.check(
            f"{prefix}.sum_equals_total",
            cents(sum(amounts.values())) == cents(total),
            f"sum(breakdown)={cents(sum(amounts.values()))} 分, total={cents(total)} 分",
        )
        if expected_keys is not None:
            self.eq(f"{prefix}.keys", set(amounts.keys()), set(expected_keys))
        for key, label in labels.items():
            if key in COST_LABELS:
                self.eq(f"{prefix}.label.{key}", label, COST_LABELS[key])
        return amounts, labels

    def check_polyline(self, prefix: str, polyline) -> None:
        if not isinstance(polyline, list):
            self.check(f"{prefix}.polyline_list", False, f"polyline={type(polyline).__name__}")
            return
        self.check(f"{prefix}.polyline_len", len(polyline) <= 500, f"len={len(polyline)}")
        valid = True
        detail = ""
        for index, pair in enumerate(polyline):
            if not isinstance(pair, list) or len(pair) != 2:
                valid = False
                detail = f"[{index}] 不是 [lng, lat]"
                break
            lng, lat = pair
            if not (is_number(lng) and is_number(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
                valid = False
                detail = f"[{index}] 经纬度越界：{pair!r}"
                break
        self.check(f"{prefix}.polyline_pairs", valid, detail)

    def check_steps(self, prefix: str, steps, require_nonempty: bool) -> None:
        if not isinstance(steps, list):
            self.check(f"{prefix}.steps_list", False, f"steps={type(steps).__name__}")
            return
        if require_nonempty:
            self.check(f"{prefix}.steps_nonempty", len(steps) > 0, "steps 为空")
        valid = True
        detail = ""
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                valid = False
                detail = f"[{index}] 不是对象"
                break
            if not (isinstance(step.get("type"), str) and step["type"]):
                valid = False
                detail = f"[{index}] type 非法"
                break
            if "text" in step and not isinstance(step["text"], str):
                valid = False
                detail = f"[{index}] text 非法"
                break
            for field in ("distance_m", "duration_s"):
                if field in step and not (is_int(step[field]) and step[field] >= 0):
                    valid = False
                    detail = f"[{index}].{field}={step[field]!r}"
                    break
            if not valid:
                break
        self.check(f"{prefix}.steps_items", valid, detail)

    def check_points(self, prefix: str, points, expected_count: int) -> None:
        self.check(f"{prefix}.count", isinstance(points, list) and len(points) == expected_count, f"len={len(points) if isinstance(points, list) else points!r}")
        if not isinstance(points, list):
            return
        valid = True
        detail = ""
        for index, point in enumerate(points):
            if not isinstance(point, dict):
                valid = False
                detail = f"[{index}] 不是对象"
                break
            location = point.get("location")
            checks = [
                isinstance(point.get("input"), str),
                isinstance(point.get("name"), str),
                isinstance(point.get("formatted_address"), str),
                isinstance(point.get("city"), str),
                isinstance(point.get("citycode"), str),
                isinstance(location, dict),
            ]
            if not all(checks):
                valid = False
                detail = f"[{index}] 字段类型不符：{ {k: type(point.get(k)).__name__ for k in ('input','name','formatted_address','city','citycode','location')} }"
                break
            if not (is_number(location.get("lng")) and is_number(location.get("lat"))):
                valid = False
                detail = f"[{index}] location 非数字：{location!r}"
                break
            if not (-180 <= location["lng"] <= 180 and -90 <= location["lat"] <= 90):
                valid = False
                detail = f"[{index}] location 越界：{location!r}"
                break
        self.check(f"{prefix}.items", valid, detail)

    # -- 逐段与费用独立复算 --------------------------------------------------
    def check_leg(
        self,
        prefix: str,
        leg,
        position: int,
        expected_mode: str,
        options: dict,
        point_count: int,
    ) -> None:
        if not isinstance(leg, dict):
            self.check(f"{prefix}.object", False, f"leg 不是对象：{leg!r}")
            return
        self.eq(f"{prefix}.index", leg.get("index"), position)
        self.eq(f"{prefix}.mode", leg.get("mode"), expected_mode)
        self.eq(f"{prefix}.mode_label", leg.get("mode_label"), MODE_LABELS[expected_mode])
        for side in ("from", "to"):
            endpoint = leg.get(side)
            ok = (
                isinstance(endpoint, dict)
                and isinstance(endpoint.get("name"), str)
                and endpoint["name"] != ""
                and isinstance(endpoint.get("location"), dict)
                and is_number(endpoint["location"].get("lng"))
                and is_number(endpoint["location"].get("lat"))
            )
            self.check(f"{prefix}.{side}", ok, f"{side}={endpoint!r}")
        distance = leg.get("distance_m")
        duration = leg.get("duration_s")
        self.check(f"{prefix}.distance_int", is_int(distance) and distance >= 0, f"distance_m={distance!r}")
        self.check(f"{prefix}.duration_int", is_int(duration) and duration >= 0, f"duration_s={duration!r}")
        self.check(f"{prefix}.summary_str", isinstance(leg.get("summary"), str), f"summary={leg.get('summary')!r}")
        steps = leg.get("steps")
        self.check_steps(prefix, steps, require_nonempty=(expected_mode == "transit"))
        self.check_polyline(prefix, leg.get("polyline"))
        warnings = leg.get("warnings")
        self.check(
            f"{prefix}.warnings",
            isinstance(warnings, list) and all(isinstance(item, str) and item for item in warnings),
            f"warnings={warnings!r}",
        )
        cost = leg.get("cost")
        amounts, _labels = self.check_cost(f"{prefix}.cost", cost)
        if isinstance(distance, int) and not isinstance(distance, bool) and distance >= 0:
            self.check_recomputed_cost(prefix, expected_mode, distance, amounts, cost, options)

    def check_recomputed_cost(self, prefix: str, mode: str, distance_m: int, amounts: dict, cost, options: dict) -> None:
        """用响应 distance_m 独立复算费用，与接口返回逐项比对。"""
        total = cost.get("total") if isinstance(cost, dict) else None
        if mode in ("walking", "bicycling"):
            self.check(f"{prefix}.recompute.free_breakdown", amounts == {}, f"breakdown={amounts!r}")
            self.check(f"{prefix}.recompute.free_total", is_number(total) and float(total) == 0.0, f"total={total!r}")
            return
        if mode == "transit":
            expected = transit_fare(distance_m)
            self.eq(f"{prefix}.recompute.transit_keys", set(amounts.keys()), {"transit_fare"})
            self.close(f"{prefix}.recompute.transit_fare", amounts.get("transit_fare"), expected)
            self.close(f"{prefix}.recompute.transit_total", total, expected)
            return
        if mode == "taxi":
            expected = taxi_estimate(distance_m, options)
            self.eq(f"{prefix}.recompute.taxi_keys", set(amounts.keys()), {"taxi_fare"})
            # 契约主公式：13 + max(0, km-3)×2.3（用响应 distance_m）。演示模式传入的是已取整的
            # distance_m，理论误差应为 0；仍按任务要求给出 ≤1 元容差的二次判定，避免误报。
            self.close(f"{prefix}.recompute.taxi_fare_strict", amounts.get("taxi_fare"), expected, 0.011)
            self.check(
                f"{prefix}.recompute.taxi_fare_tol1",
                is_number(amounts.get("taxi_fare")) and abs(float(amounts["taxi_fare"]) - expected) <= 1.0 + 1e-9,
                f"actual={amounts.get('taxi_fare')!r} expected≈{expected!r} (≤1 元容差)",
            )
            self.close(f"{prefix}.recompute.taxi_total", total, round2(expected))
            return
        # driving
        fuel, toll = driving_expected(distance_m, options)
        expected_keys = set()
        if fuel > 0:
            expected_keys.add("fuel")
        if toll > 0:
            expected_keys.add("toll")
        self.eq(f"{prefix}.recompute.driving_keys", set(amounts.keys()), expected_keys)
        if "fuel" in expected_keys:
            self.close(f"{prefix}.recompute.fuel", amounts.get("fuel"), round2(fuel))
        if toll > 0:
            self.close(f"{prefix}.recompute.toll", amounts.get("toll"), round2(toll))
        else:
            self.check(f"{prefix}.recompute.toll_absent", "toll" not in amounts, f"过路费=0 时不应出现在明细：{amounts!r}")
        expected_total = round2(round2(fuel) + round2(toll))
        self.close(f"{prefix}.recompute.total", total, expected_total)
        # 交叉验证响应距离与公式同源：过路费是否按 max(0,km-20)×0.6 触发
        self.check(
            f"{prefix}.recompute.toll_threshold",
            (toll > 0) == ("toll" in expected_keys),
            f"km={distance_m / 1000.0:.3f}, toll={toll:.4f}",
        )

    def check_totals(self, prefix: str, data: dict, options: dict) -> None:
        legs = data.get("legs")
        totals = data.get("totals")
        if not isinstance(legs, list) or not isinstance(totals, dict):
            self.check(f"{prefix}.structure", False, f"legs={type(legs).__name__}, totals={type(totals).__name__}")
            return
        expected_distance = sum(int(leg.get("distance_m") or 0) for leg in legs)
        expected_duration = sum(int(leg.get("duration_s") or 0) for leg in legs)
        self.eq(f"{prefix}.distance_m", totals.get("distance_m"), expected_distance)
        self.eq(f"{prefix}.duration_s", totals.get("duration_s"), expected_duration)
        totals_cost = totals.get("cost")
        amounts, _labels = self.check_cost(f"{prefix}.cost", totals_cost)
        # 按 key 合并 = 逐段明细之和
        merged: dict[str, float] = {}
        leg_total_cents = 0
        for leg in legs:
            leg_cost = leg.get("cost") if isinstance(leg, dict) else None
            if not isinstance(leg_cost, dict):
                continue
            leg_total_cents += cents(leg_cost.get("total"))
            breakdown = leg_cost.get("breakdown")
            if not isinstance(breakdown, list):
                continue
            for item in breakdown:
                if isinstance(item, dict) and isinstance(item.get("key"), str):
                    merged[item["key"]] = merged.get(item["key"], 0.0) + float(item.get("amount") or 0)
        self.eq(f"{prefix}.merged_keys", set(amounts.keys()), set(merged.keys()))
        merged_ok = True
        merged_detail = ""
        for key in set(amounts.keys()) | set(merged.keys()):
            expected_amount = round2(merged.get(key, 0))
            if cents(amounts.get(key, 0)) != cents(expected_amount):
                merged_ok = False
                merged_detail = f"{key}: totals={amounts.get(key)!r}, 逐段求和={expected_amount!r}"
                break
        self.check(f"{prefix}.merged_amounts", merged_ok, merged_detail)
        totals_total = totals_cost.get("total") if isinstance(totals_cost, dict) else 0
        self.check(
            f"{prefix}.total_equals_leg_sum",
            cents(totals_total) == leg_total_cents,
            f"totals={cents(totals_total)} 分, 逐段 total 之和={leg_total_cents} 分",
        )

    # -- 整份 /api/plan 响应 -------------------------------------------------
    def check_plan_response(self, prefix: str, data, expected_modes: list[str], options: dict, expected_points: int = 0) -> None:
        if not isinstance(data, dict):
            self.check(f"{prefix}.object", False, f"响应不是 JSON 对象：{data!r}")
            return
        self.check(f"{prefix}.mode_demo", data.get("mode") == "demo", f"mode={data.get('mode')!r}")
        self.check(f"{prefix}.generated_at", iso_with_tz(data.get("generated_at")), f"generated_at={data.get('generated_at')!r}")
        points = data.get("points")
        if expected_points:
            self.check_points(prefix, points, expected_points)
        legs = data.get("legs")
        self.check(f"{prefix}.legs_count", isinstance(legs, list) and len(legs) == len(expected_modes), f"legs={legs if not isinstance(legs, list) else len(legs)}")
        if not isinstance(legs, list):
            return
        for index, (leg, mode) in enumerate(zip(legs, expected_modes)):
            self.check_leg(f"{prefix}.leg{index}", leg, index, mode, options, len(expected_modes) + 1)
        self.check_totals(prefix, data, options)


# --------------------------------------------------------------------------- 请求构造
def plan_payload(addresses, modes, options=None):
    payload = {"points": [{"address": name} for name in addresses], "modes": list(modes)}
    if options is not None:
        payload["options"] = options
    return payload


# --------------------------------------------------------------------------- 服务进程管理
class ServerHandle:
    def __init__(self, base_url: str, auto: bool) -> None:
        self.base_url = base_url.rstrip("/")
        self.auto = auto
        self.proc: subprocess.Popen | None = None
        self.log_path: Path | None = None
        self.owned = False
        self.alive_before = False

    def health_ok(self, timeout: float = 1.0) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/healthz", timeout=timeout)
            return response.status_code == 200
        except requests.RequestException:
            return False

    def ensure(self) -> bool:
        if self.health_ok():
            self.alive_before = True
            print(f"[server] 使用已运行的服务：{self.base_url}")
            return True
        if not self.auto:
            print(f"[server] 无法连接 {self.base_url}，且未启用 --auto-server。请先启动服务。")
            return False
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = ARTIFACT_DIR / "server-8014.log"
        port = re.search(r":(\d+)$", self.base_url)
        port_number = port.group(1) if port else "8014"
        env = os.environ.copy()
        # 强制「无 Key + 演示模式」路径：不让本机环境变量干扰验证
        env.pop("AMAP_KEY", None)
        env.pop("DEMO_MODE", None)
        env["PYTHONUNBUFFERED"] = "1"
        log = self.log_path.open("w", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", port_number],
                cwd=str(PROJECT_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
            )
        finally:
            log.close()
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.proc.poll() is not None:
                print(f"[server] uvicorn 启动失败，退出码 {self.proc.returncode}，日志：{self.log_path}")
                return False
            if self.health_ok(0.5):
                self.owned = True
                print(f"[server] 已自动启动 {self.base_url}（日志：{self.log_path}）")
                return True
            time.sleep(0.3)
        print("[server] 等待 uvicorn 就绪超时")
        return False

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self.owned:
            print(f"[server] 已关闭本次启动的服务（日志：{self.log_path}）")


# --------------------------------------------------------------------------- 验证套件
class Suite:
    def __init__(self, ck: Checker, session: requests.Session, base_url: str) -> None:
        self.ck = ck
        self.session = session
        self.base_url = base_url
        self.samples: dict = {}

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    last_content_type: str = ""

    def fetch(self, name: str, path: str):
        """GET 一个路径，并记录 Content-Type（静态资源检查用）。"""
        try:
            response = self.session.get(self.url(path), timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            self.ck.check(f"{name}.transport", False, f"{type(exc).__name__}: {exc}")
            self.last_content_type = ""
            return None, "", None
        self.last_content_type = (response.headers.get("Content-Type") or "").lower()
        text = response.text
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        return response.status_code, text, data

    def get_json(self, name: str, path: str):
        return self.fetch(name, path)

    def post_plan(self, name: str, payload):
        return self.ck.http(
            self.session,
            name,
            "POST",
            self.url("/api/plan"),
            json=payload,
            headers={"Accept": "application/json"},
        )

    def post_compare(self, name: str, payload):
        return self.ck.http(
            self.session,
            name,
            "POST",
            self.url("/api/compare"),
            json=payload,
            headers={"Accept": "application/json"},
        )

    # -- 1. 探活 / 状态 ------------------------------------------------------
    def check_health_and_status(self) -> dict:
        print("\n== 1. GET /api/healthz, GET /api/status ==")
        status_code, text, data = self.get_json("healthz", "/api/healthz")
        self.ck.eq("healthz.status_code", status_code, 200)
        self.ck.eq("healthz.body", data, {"status": "ok"})

        status_code, text, data = self.get_json("status", "/api/status")
        self.ck.eq("status.status_code", status_code, 200)
        if not isinstance(data, dict):
            self.ck.check("status.object", False, f"响应不是 JSON 对象：{text[:200]!r}")
            return {}
        self.samples["status"] = data
        self.ck.eq("status.ok", data.get("ok"), True)
        self.ck.eq("status.version", data.get("version"), EXPECTED_VERSION)
        self.ck.check("status.demo_bool", isinstance(data.get("demo"), bool), f"demo={data.get('demo')!r}")
        self.ck.check("status.demo_true", data.get("demo") is True, "无 Key 环境下应进入演示模式（demo=true）")
        self.ck.check(
            "status.demo_reason",
            isinstance(data.get("demo_reason"), str) and len(data.get("demo_reason", "").strip()) > 0,
            f"demo_reason={data.get('demo_reason')!r}",
        )
        defaults = data.get("defaults")
        self.ck.check("status.defaults_object", isinstance(defaults, dict), f"defaults={defaults!r}")
        if isinstance(defaults, dict):
            self.ck.eq("status.defaults_keys", set(defaults.keys()), set(DEFAULTS.keys()))
            for key, expected in DEFAULTS.items():
                self.ck.eq(f"status.defaults.{key}", defaults.get(key), expected)
        modes = data.get("modes")
        expected_modes = [{"value": value, "label": MODE_LABELS[value]} for value in MODES]
        self.ck.eq("status.modes", modes, expected_modes)
        return data

    # -- 2. 演示模式规划（2–5 段混合）----------------------------------------
    def check_plan_matrix(self) -> None:
        print("\n== 2. POST /api/plan（演示模式，2–5 段混合方式）==")
        cases = [
            ("plan.mixed.1leg", [PLACES[0], PLACES[1]], ["walking"]),
            ("plan.mixed.2legs", [PLACES[0], PLACES[1], PLACES[2]], ["transit", "bicycling"]),
            ("plan.mixed.3legs", [PLACES[0], PLACES[1], PLACES[2], PLACES[3]], ["taxi", "walking", "driving"]),
            ("plan.mixed.4legs", [PLACES[0], PLACES[1], PLACES[2], PLACES[3], PLACES[4]], ["bicycling", "transit", "driving", "taxi"]),
            ("plan.mixed.5legs", [PLACES[0], PLACES[1], PLACES[2], PLACES[3], PLACES[4], PLACES[8]], MODES),
        ]
        options = dict(DEFAULTS)
        for name, addresses, modes in cases:
            status_code, text, data = self.post_plan(name, plan_payload(addresses, modes))
            self.ck.eq(f"{name}.status_code", status_code, 200)
            if status_code == 200:
                self.ck.check_plan_response(name, data, modes, options, expected_points=len(addresses))
                if "mixed.5legs" in name:
                    self.samples["plan_mixed_5legs"] = data
            else:
                self.ck.check(f"{name}.body", False, text[:500])

    # -- 3a. 参数校验（4xx）--------------------------------------------------
    def check_validation_errors(self) -> None:
        print("\n== 3a. 参数校验（全部应 4xx，且 message 可读）==")
        two = [{"address": "北京南站"}, {"address": "故宫博物院"}]
        valid_cases = [
            ("validation.missing_body", {}),
            ("validation.points_empty", {"points": [], "modes": []}),
            ("validation.one_point", {"points": [{"address": "只有起点"}], "modes": []}),
            ("validation.eleven_points", {"points": [{"address": f"点{i}"} for i in range(11)], "modes": ["walking"] * 10}),
            ("validation.points_not_list", {"points": "北京南站", "modes": []}),
            ("validation.modes_missing", {"points": two}),
            ("validation.modes_too_few", {"points": two, "modes": []}),
            ("validation.modes_too_many", {"points": two, "modes": ["walking", "walking"]}),
            ("validation.unknown_mode", {"points": two, "modes": ["flight"]}),
            ("validation.mode_not_string", {"points": two, "modes": [1]}),
            ("validation.empty_address", {"points": [{"address": ""}, {"address": "B"}], "modes": ["walking"]}),
            ("validation.point_without_address", {"points": [{"name": "无地址"}, {"address": "B"}], "modes": ["walking"]}),
            ("validation.point_not_object", {"points": ["北京南站", {"address": "B"}], "modes": ["walking"]}),
            ("validation.address_not_string", {"points": [{"address": 123}, {"address": "B"}], "modes": ["walking"]}),
            ("validation.location_bad_format", {"points": [{"location": "abc"}, {"address": "B"}], "modes": ["walking"]}),
            ("validation.location_one_part", {"points": [{"location": "116.3"}, {"address": "B"}], "modes": ["walking"]}),
            ("validation.location_too_many_decimals", {"points": [{"location": "116.1234567,39.9"}, {"address": "B"}], "modes": ["walking"]}),
            ("validation.location_out_of_range", {"points": [{"location": "200,39.9"}, {"address": "B"}], "modes": ["walking"]}),
            ("validation.location_not_string", {"points": [{"location": 123}, {"address": "B"}], "modes": ["walking"]}),
            ("validation.options_not_object", {"points": two, "modes": ["walking"], "options": "x"}),
            ("validation.transit_strategy_9", {"points": two, "modes": ["walking"], "options": {"transit_strategy": 9}}),
            ("validation.transit_strategy_fraction", {"points": two, "modes": ["walking"], "options": {"transit_strategy": 2.5}}),
            ("validation.fuel_price_negative", {"points": two, "modes": ["walking"], "options": {"fuel_price": -1}}),
            ("validation.fuel_price_text", {"points": two, "modes": ["walking"], "options": {"fuel_price": "abc"}}),
            ("validation.fuel_price_bool", {"points": two, "modes": ["walking"], "options": {"fuel_price": True}}),
        ]
        readable_samples = {}
        for name, payload in valid_cases:
            status_code, text, data = self.post_plan(name, payload)
            ok = isinstance(status_code, int) and 400 <= status_code < 500
            self.ck.check(f"{name}.4xx", ok, f"status={status_code} body={text[:300]!r}")
            if not ok:
                continue
            error = data.get("error") if isinstance(data, dict) else None
            self.ck.check(f"{name}.error_object", isinstance(error, dict), f"body={text[:300]!r}")
            if not isinstance(error, dict):
                continue
            self.ck.check(
                f"{name}.error_code",
                isinstance(error.get("code"), str) and error["code"] in {"INVALID_INPUT", "GEOCODE_FAILED", "AMAP_ERROR", "KEY_MISSING", "INTERNAL_ERROR"},
                f"code={error.get('code')!r}",
            )
            message = error.get("message")
            self.ck.check(
                f"{name}.error_message_readable",
                isinstance(message, str) and len(message.strip()) >= 2 and "Traceback" not in message,
                f"message={message!r}",
            )
            self.ck.check(f"{name}.error_hint_str", isinstance(error.get("hint"), str), f"hint={error.get('hint')!r}")
            if name in {"validation.one_point", "validation.eleven_points", "validation.modes_too_few", "validation.unknown_mode", "validation.empty_address", "validation.location_too_many_decimals", "validation.transit_strategy_9", "validation.fuel_price_negative"}:
                readable_samples[name] = {"status": status_code, "body": data}
        self.samples["validation"] = readable_samples

        # 畸形 JSON（FastAPI 自动 422，但必须被包装成可读错误）
        status_code, text, data = self.ck.http(
            self.session,
            "validation.malformed_json",
            "POST",
            self.url("/api/plan"),
            data="{not-json",
            headers={"Content-Type": "application/json"},
        )
        self.ck.check("validation.malformed_json.4xx", isinstance(status_code, int) and 400 <= status_code < 500, f"status={status_code} body={text[:300]!r}")
        self.ck.check(
            "validation.malformed_json.error_object",
            isinstance(data, dict) and isinstance(data.get("error"), dict) and isinstance(data["error"].get("message"), str) and data["error"]["message"],
            f"body={text[:300]!r}",
        )

    # -- 3b. location 直接经纬度 / 10 点上限（成功路径）-----------------------
    def check_location_and_limits(self) -> None:
        print("\n== 3b. location 直接经纬度、10 点上限 ==")
        # 直接经纬度（location）成功路径：坐标原样解析、可规划、地址+坐标混用
        payload = {
            "points": [
                {"location": "116.378,39.865", "name": "起点坐标"},
                {"location": "116.397,39.918"},
            ],
            "modes": ["walking"],
        }
        status_code, text, data = self.post_plan("plan.location.only", payload)
        self.ck.eq("plan.location.only.status_code", status_code, 200)
        if status_code == 200:
            self.ck.check_plan_response("plan.location.only", data, ["walking"], dict(DEFAULTS), expected_points=2)
            point_0 = data["points"][0]
            point_1 = data["points"][1]
            self.ck.eq("plan.location.only.input_echo", point_0.get("input"), "116.378,39.865")
            self.ck.eq("plan.location.only.name", point_0.get("name"), "起点坐标")
            self.ck.eq("plan.location.only.lnglat", point_1.get("location"), {"lng": 116.397, "lat": 39.918})
            self.ck.eq("plan.location.only.from_name", data["legs"][0]["from"]["name"], "起点坐标")
            self.samples["plan_location"] = data

        payload = {
            "points": [
                {"location": "116.123456,39.123456"},
                {"address": "故宫博物院"},
                {"location": "116.5,39.95", "name": "终点坐标"},
            ],
            "modes": ["driving", "taxi"],
        }
        status_code, text, data = self.post_plan("plan.location.mixed", payload)
        self.ck.eq("plan.location.mixed.status_code", status_code, 200)
        if status_code == 200:
            self.ck.check_plan_response("plan.location.mixed", data, ["driving", "taxi"], dict(DEFAULTS), expected_points=3)
            self.ck.eq("plan.location.mixed.point0", data["points"][0].get("location"), {"lng": 116.123456, "lat": 39.123456})
            self.ck.eq("plan.location.mixed.point2", data["points"][2].get("location"), {"lng": 116.5, "lat": 39.95})
            self.ck.eq("plan.location.mixed.point1.name", data["points"][1].get("name"), "故宫博物院")

        # 10 点上限（成功）与 11 点（拒绝，前面已测 4xx）
        payload = plan_payload(PLACES[:10], ["walking", "bicycling", "transit", "taxi", "driving", "walking", "bicycling", "transit", "taxi"])
        status_code, text, data = self.post_plan("plan.ten_points", payload)
        self.ck.eq("plan.ten_points.status_code", status_code, 200)
        if status_code == 200:
            self.ck.check_plan_response("plan.ten_points", data, payload["modes"], dict(DEFAULTS), expected_points=10)
            self.ck.eq("plan.ten_points.legs", len(data.get("legs", [])), 9)

    # -- 4. options 计价参数 --------------------------------------------------
    def check_options(self) -> None:
        print("\n== 4. options 计价参数生效（独立复算）==")
        two = [PLACES[0], PLACES[8]]  # 北京南站 -> 首都机场（约 30km 级，触发过路费）
        options = {"fuel_price": 8.5, "fuel_consumption": 6.7}
        merged_options = dict(DEFAULTS)
        merged_options.update(options)
        status_code, text, data = self.post_plan("plan.options.driving", plan_payload(two, ["driving"], options))
        self.ck.eq("plan.options.driving.status_code", status_code, 200)
        if status_code == 200:
            self.ck.check_plan_response("plan.options.driving", data, ["driving"], merged_options, expected_points=2)
            leg = data["legs"][0]
            km = leg["distance_m"] / 1000.0
            fuel, toll = driving_expected(leg["distance_m"], merged_options)
            self.ck.check("plan.options.driving.toll_triggered", toll > 0, f"km={km:.2f}（应 >20km 才收过路费）")
            self.samples["plan_options_driving"] = data

        taxi_options = {"taxi_base_fare": 15.0, "taxi_base_km": 2.0, "taxi_per_km": 3.0}
        merged_taxi = dict(DEFAULTS)
        merged_taxi.update(taxi_options)
        status_code, text, data = self.post_plan("plan.options.taxi", plan_payload(two, ["taxi"], taxi_options))
        self.ck.eq("plan.options.taxi.status_code", status_code, 200)
        if status_code == 200:
            self.ck.check_plan_response("plan.options.taxi", data, ["taxi"], merged_taxi, expected_points=2)

        # 部分 options、transit_strategy 合法值
        status_code, _, _ = self.post_plan("plan.options.partial", plan_payload(two, ["walking"], {"fuel_price": 6.0}))
        self.ck.eq("plan.options.partial.status_code", status_code, 200)
        status_code, _, _ = self.post_plan(
            "plan.options.transit_strategy8",
            plan_payload([PLACES[0], PLACES[1]], ["transit"], {"transit_strategy": 8}),
        )
        self.ck.eq("plan.options.transit_strategy8.status_code", status_code, 200)

    # -- 4b. /api/compare（契约 3.4：5 种交通方式对比）-----------------------
    def check_compare_entry(self, prefix: str, entry, position: int, options: dict, expected_legs: int) -> None:
        if not isinstance(entry, dict):
            self.ck.check(f"{prefix}.object", False, f"entry 不是对象：{entry!r}")
            return
        expected_mode = MODES[position]
        self.ck.eq(f"{prefix}.keys", set(entry.keys()), {"mode", "mode_label", "available", "error", "legs", "totals", "summary"})
        self.ck.eq(f"{prefix}.mode", entry.get("mode"), expected_mode)
        self.ck.eq(f"{prefix}.mode_label", entry.get("mode_label"), MODE_LABELS[expected_mode])
        self.ck.eq(f"{prefix}.available", entry.get("available"), True)
        self.ck.eq(f"{prefix}.error", entry.get("error"), None)
        self.ck.check(f"{prefix}.summary_str", isinstance(entry.get("summary"), str), f"summary={entry.get('summary')!r}")
        legs = entry.get("legs")
        actual_legs = len(legs) if isinstance(legs, list) else legs
        self.ck.eq(f"{prefix}.legs_count", actual_legs, expected_legs)
        if not isinstance(legs, list):
            return
        for index, leg in enumerate(legs):
            self.ck.check_leg(f"{prefix}.leg{index}", leg, index, expected_mode, options, expected_legs + 1)
        self.ck.check_totals(prefix, {"legs": legs, "totals": entry.get("totals")}, options)
        # 全方式对比时，每段的 mode 必须都是该方式
        if all(isinstance(leg, dict) for leg in legs):
            self.ck.eq(f"{prefix}.leg_modes", [leg.get("mode") for leg in legs], [expected_mode] * len(legs))

    def check_compare_response(self, prefix: str, data, expected_points: int, options: dict) -> None:
        if not isinstance(data, dict):
            self.ck.check(f"{prefix}.object", False, f"响应不是 JSON 对象：{data!r}")
            return
        self.ck.eq(f"{prefix}.keys", set(data.keys()), {"mode", "generated_at", "points", "modes"})
        self.ck.check(f"{prefix}.mode_demo", data.get("mode") == "demo", f"mode={data.get('mode')!r}")
        self.ck.check(f"{prefix}.generated_at", iso_with_tz(data.get("generated_at")), f"generated_at={data.get('generated_at')!r}")
        self.ck.check_points(prefix, data.get("points"), expected_points)
        entries = data.get("modes")
        self.ck.check(f"{prefix}.modes_list", isinstance(entries, list), f"modes={entries if not isinstance(entries, list) else len(entries)}")
        if not isinstance(entries, list):
            return
        self.ck.eq(f"{prefix}.modes_count", len(entries), 5)
        self.ck.eq(f"{prefix}.modes_order", [item.get("mode") for item in entries if isinstance(item, dict)], MODES)
        for position, entry in enumerate(entries):
            self.check_compare_entry(f"{prefix}.entry{position}", entry, position, options, expected_points - 1)
        # 费用语义交叉检查：步行/骑行免费；公交/打车/驾车在有距离时应 > 0
        by_mode = {entry.get("mode"): entry for entry in entries if isinstance(entry, dict)}
        for mode in ("walking", "bicycling"):
            entry = by_mode.get(mode) or {}
            cost = (entry.get("totals") or {}).get("cost") or {}
            self.ck.check(f"{prefix}.{mode}_free", cost.get("total") == 0 and cost.get("breakdown") == [], f"cost={cost!r}")
        if expected_points >= 2:
            distances = {
                mode: ((by_mode.get(mode) or {}).get("totals") or {}).get("distance_m") or 0
                for mode in ("transit", "taxi", "driving")
            }
            if all(value > 0 for value in distances.values()):
                for mode in ("transit", "taxi", "driving"):
                    cost = ((by_mode.get(mode) or {}).get("totals") or {}).get("cost") or {}
                    self.ck.check(f"{prefix}.{mode}_paid", is_number(cost.get("total")) and cost["total"] > 0, f"cost={cost!r}")

    def check_compare(self) -> None:
        print("\n== 4b. POST /api/compare（演示模式，2–5 点；契约 3.4）==")
        options = dict(DEFAULTS)
        cases = [
            ("compare.2points", [PLACES[0], PLACES[1]], 1),
            ("compare.3points", [PLACES[0], PLACES[1], PLACES[2]], 2),
            ("compare.4points", [PLACES[0], PLACES[1], PLACES[2], PLACES[3]], 3),
            ("compare.5points", [PLACES[0], PLACES[1], PLACES[2], PLACES[3], PLACES[4]], 4),
            ("compare.10points", PLACES[:10], 9),  # 契约与 /api/plan 同为 2–10，覆盖上限
        ]
        for name, addresses, expected_legs in cases:
            payload = {"points": [{"address": name} for name in addresses]}
            status_code, text, data = self.post_compare(name, payload)
            self.ck.eq(f"{name}.status_code", status_code, 200)
            if status_code == 200:
                self.check_compare_response(name, data, len(addresses), options)
                self.ck.eq(f"{name}.legs_per_mode", [len(item.get("legs", [])) for item in data.get("modes", [])], [expected_legs] * 5)
                if expected_legs == 2:
                    self.samples["compare_3points"] = data
            else:
                self.ck.check(f"{name}.body", False, text[:500])

        # options 生效（driving 油费/过路费复算在 check_leg 内完成）
        custom = {"fuel_price": 8.5, "fuel_consumption": 6.7}
        merged = dict(DEFAULTS)
        merged.update(custom)
        payload = {"points": [{"address": PLACES[0]}, {"address": PLACES[8]}], "options": custom}
        status_code, text, data = self.post_compare("compare.options", payload)
        self.ck.eq("compare.options.status_code", status_code, 200)
        if status_code == 200:
            self.check_compare_response("compare.options", data, 2, merged)
            driving = next((item for item in data["modes"] if item.get("mode") == "driving"), {})
            leg = (driving.get("legs") or [{}])[0]
            fuel, toll = driving_expected(leg.get("distance_m") or 0, merged)
            self.ck.check("compare.options.toll_triggered", toll > 0, f"km={(leg.get('distance_m') or 0) / 1000.0:.2f}")
            self.samples["compare_options_driving"] = driving

        # 支持 location 直接坐标（与 /api/plan 规则一致）
        payload = {
            "points": [
                {"location": "116.378,39.865", "name": "起点坐标"},
                {"location": "116.397,39.918"},
            ],
        }
        status_code, text, data = self.post_compare("compare.location", payload)
        self.ck.eq("compare.location.status_code", status_code, 200)
        if status_code == 200:
            self.check_compare_response("compare.location", data, 2, options)
            self.ck.eq("compare.location.point0_name", data["points"][0].get("name"), "起点坐标")
            self.ck.eq("compare.location.point1_lnglat", data["points"][1].get("location"), {"lng": 116.397, "lat": 39.918})
            self.samples["compare_location"] = data

        # compare 不需要 modes；传入 modes（甚至非法值）与额外字段应被忽略
        payload = {"points": [{"address": PLACES[0]}, {"address": PLACES[1]}], "modes": ["flying"], "unknown_field": 1}
        status_code, text, data = self.post_compare("compare.ignores_modes", payload)
        self.ck.eq("compare.ignores_modes.status_code", status_code, 200)
        if status_code == 200:
            self.ck.eq("compare.ignores_modes.count", len(data.get("modes", [])), 5)

    def check_compare_validation(self) -> None:
        print("\n== 4c. /api/compare 参数校验（4xx、错误格式）==")
        two = [{"address": "北京南站"}, {"address": "故宫博物院"}]
        payloads = [
            ("compare.validation.missing_body", {}),
            ("compare.validation.one_point", {"points": [{"address": "只有起点"}]}),
            ("compare.validation.eleven_points", {"points": [{"address": f"点{i}"} for i in range(11)]}),
            ("compare.validation.points_not_list", {"points": "北京南站"}),
            ("compare.validation.empty_address", {"points": [{"address": ""}, {"address": "B"}]}),
            ("compare.validation.point_without_address", {"points": [{"name": "无地址"}, {"address": "B"}]}),
            ("compare.validation.location_bad_format", {"points": [{"location": "abc"}, {"address": "B"}]}),
            ("compare.validation.options_not_object", {"points": two, "options": "x"}),
            ("compare.validation.transit_strategy_9", {"points": two, "options": {"transit_strategy": 9}}),
            ("compare.validation.fuel_price_negative", {"points": two, "options": {"fuel_price": -1}}),
        ]
        samples = {}
        for name, payload in payloads:
            status_code, text, data = self.post_compare(name, payload)
            ok = isinstance(status_code, int) and 400 <= status_code < 500
            self.ck.check(f"{name}.4xx", ok, f"status={status_code} body={text[:300]!r}")
            if not ok:
                continue
            error = data.get("error") if isinstance(data, dict) else None
            self.ck.check(f"{name}.error_object", isinstance(error, dict), f"body={text[:300]!r}")
            if isinstance(error, dict):
                self.ck.check(
                    f"{name}.error_code",
                    isinstance(error.get("code"), str) and error["code"] in {"INVALID_INPUT", "GEOCODE_FAILED", "AMAP_ERROR", "KEY_MISSING", "INTERNAL_ERROR"},
                    f"code={error.get('code')!r}",
                )
                self.ck.check(
                    f"{name}.error_message_readable",
                    isinstance(error.get("message"), str) and len(error["message"].strip()) >= 2 and "Traceback" not in error["message"],
                    f"message={error.get('message')!r}",
                )
                self.ck.check(f"{name}.error_hint_str", isinstance(error.get("hint"), str), f"hint={error.get('hint')!r}")
                if name in {"compare.validation.one_point", "compare.validation.eleven_points", "compare.validation.transit_strategy_9"}:
                    samples[name] = {"status": status_code, "body": data}
        self.samples["compare_validation"] = samples

        status_code, text, data = self.ck.http(
            self.session,
            "compare.validation.malformed_json",
            "POST",
            self.url("/api/compare"),
            data="{not-json",
            headers={"Content-Type": "application/json"},
        )
        self.ck.check("compare.validation.malformed_json.4xx", isinstance(status_code, int) and 400 <= status_code < 500, f"status={status_code} body={text[:300]!r}")
        self.ck.check(
            "compare.validation.malformed_json.error_object",
            isinstance(data, dict) and isinstance(data.get("error"), dict) and isinstance(data["error"].get("message"), str) and data["error"]["message"],
            f"body={text[:300]!r}",
        )

    def _run_pytest_selector(self, selector: str, name: str) -> str:
        env = os.environ.copy()
        env.pop("AMAP_KEY", None)
        env.pop("DEMO_MODE", None)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-k", selector],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.ck.check(f"{name}.exit_code", False, "pytest 超时（300s）")
            return ""
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        tail = output.splitlines()[-4:] if output else []
        self.samples.setdefault("pytest_selectors", {})[selector] = tail
        self.ck.check(f"{name}.exit_code", proc.returncode == 0, f"rc={proc.returncode} tail={tail}")
        passed = re.search(r"(\d+) passed", output)
        self.ck.check(f"{name}.passed_count", passed is not None and int(passed.group(1)) > 0, f"tail={tail}")
        return output

    def check_compare_failure_isolation(self) -> None:
        print("\n== 4d. 单方式失败隔离（代码审查 + 定向 mock 单测）==")
        planner_src = read_text(PROJECT_ROOT / "backend" / "planner.py")
        compare_match = re.search(r"def compare\(.*?(?=\ndef |\Z)", planner_src, re.S)
        body = compare_match.group(0) if compare_match else ""
        unavailable_match = re.search(r"def _unavailable_mode\(.*?(?=\ndef |\Z)", planner_src, re.S)
        unavailable_src = unavailable_match.group(0) if unavailable_match else ""
        empty_match = re.search(r"def _empty_totals\(.*?(?=\ndef |\Z)", planner_src, re.S)
        empty_src = empty_match.group(0) if empty_match else ""
        self.ck.check("compare.code.parse_compare_fn", bool(body), "未找到 planner.compare 函数")
        checks = [
            ("loop_five_modes", "for mode in config.VALID_MODES" in body),
            ("per_mode_try", "try:" in body and "_build_legs(" in body),
            ("catch_api_error", "except ApiError" in body),
            ("catch_unexpected", "except Exception" in body),
            ("unavailable_helper", bool(unavailable_src)),
            ("unavailable_available_false", '"available": False' in unavailable_src),
            ("unavailable_legs_empty", '"legs": []' in unavailable_src),
            ("unavailable_zero_totals", "_empty_totals()" in unavailable_src),
            ("unavailable_error_shape", 'error.to_dict()["error"]' in unavailable_src),
            ("zero_totals_shape", '"distance_m": 0' in empty_src and '"duration_s": 0' in empty_src and '"total": 0.0' in empty_src),
            ("available_true_entry", '"available": True' in body and '"error": None' in body),
            ("points_resolved_before_loop", body.find("resolve_points_") != -1 and body.find("resolve_points_") < body.find("for mode in config.VALID_MODES")),
        ]
        for label, ok in checks:
            self.ck.check(f"compare.code.{label}", ok, label)
        docs = read_text(PROJECT_ROOT / "docs" / "API-CONTRACT.md")
        self.ck.check(
            "compare.docs.isolation_rule",
            "单一方式失败不影响整体" in docs and "available=false" in docs,
            "契约 3.4 缺少失败隔离说明",
        )
        # 定向跑后端 mock 单测：单方式失败隔离 / 真实模式 compare
        self._run_pytest_selector("compare_isolates", "compare.pytest.isolation")
        self._run_pytest_selector("TestCompareLive", "compare.pytest.live")

    # -- 4e. 起点定位 / 逆地理编码（v0.3.0→v0.4.0，代码审查 + 定向 mock 单测）--
    def check_regeo_location(self) -> None:
        print("\n== 4e. 起点定位（regeo 逆地理编码 / 详细地址回显）代码审查 + 定向 mock 单测 ==")
        amap_src = read_text(PROJECT_ROOT / "backend" / "amap.py")
        planner_src = read_text(PROJECT_ROOT / "backend" / "planner.py")
        app_src = read_text(PROJECT_ROOT / "backend" / "app.py")
        docs = read_text(PROJECT_ROOT / "docs" / "API-CONTRACT.md")
        checks = [
            ("amap.regeo_method", "def regeo(" in amap_src),
            ("amap.regeo_endpoint", '"/v3/geocode/regeo"' in amap_src),
            ("amap.regeo_address_component", "addressComponent" in amap_src),
            (
                "amap.regeo_city_province_fallback",
                re.search(
                    r'as_text\(component\.get\("city"\)\)\s+or\s+as_text\(component\.get\("province"\)\)',
                    amap_src,
                )
                is not None,
            ),
            ("amap.regeo_formatted_address", 'regeocode.get("formatted_address")' in amap_src),
            ("amap.regeo_citycode", 'component.get("citycode")' in amap_src),
            ("planner.location_calls_regeo", "client.regeo(" in planner_src),
            (
                "planner.regeo_failure_tolerant",
                re.search(
                    r"try:\s*\n\s*geo = client\.regeo\([^\n]*\)\s*\n\s*except ApiError:\s*\n\s*geo = \{\}",
                    planner_src,
                )
                is not None,
            ),
            ("planner.regeo_fields_used", all(key in planner_src for key in ('geo.get("formatted_address")', 'geo.get("city")', 'geo.get("citycode")'))),
            ("app.regeo_endpoint", '@app.get("/api/regeo")' in app_src),
            ("app.regeo_demo_text", "当前位置（" in app_src),
            ("app.regeo_live_branch", "client.regeo(format_location" in app_src and '"mode": "live"' in app_src),
            ("app.regeo_invalid_4xx", 'raise ApiError(\n                "INVALID_INPUT"' in app_src),
            ("docs.regeo_documented", "逆地理编码" in docs and "/v3/geocode/regeo" in docs),
            ("docs.regeo_endpoint_section", "### 3.5 `GET /api/regeo`" in docs),
            ("docs.regeo_demo_note", "演示模式返回" in docs and "坐标文本" in docs),
            (f"version.{EXPECTED_VERSION}", EXPECTED_VERSION in read_text(PROJECT_ROOT / "backend" / "__init__.py")),
        ]
        for label, ok in checks:
            self.ck.check(f"location.code.{label}", ok, label)
        # 定向跑后端 mock 单测：regeo 补全 / 失败降级 / /api/regeo 端点（demo/live mock/非法坐标/Key 不泄漏）
        self._run_pytest_selector("TestRegeoEndpoint", "location.pytest.regeo_endpoint")
        self._run_pytest_selector("regeo", "location.pytest.regeo")

    # -- 4f. GET /api/regeo（v0.4.0，演示模式 HTTP 契约）---------------------
    def check_regeo_endpoint(self) -> None:
        print("\n== 4f. GET /api/regeo（演示模式：详细地址回显接口）==")
        status_code, text, data = self.fetch("regeo.valid", "/api/regeo?location=116.397,39.908")
        valid_text = text or ""
        self.ck.eq("regeo.valid.status_code", status_code, 200)
        if isinstance(data, dict):
            self.ck.eq("regeo.valid.keys", set(data.keys()), {"mode", "formatted_address", "city", "citycode", "location"})
            self.ck.eq("regeo.valid.mode", data.get("mode"), "demo")
            self.ck.eq("regeo.valid.formatted_address", data.get("formatted_address"), "当前位置（116.397000, 39.908000）")
            self.ck.eq("regeo.valid.city", data.get("city"), "北京市")
            self.ck.eq("regeo.valid.citycode", data.get("citycode"), "010")
            self.ck.eq("regeo.valid.location", data.get("location"), {"lng": 116.397, "lat": 39.908})
            self.samples["regeo_demo"] = data
        else:
            self.ck.check("regeo.valid.object", False, valid_text[:300])

        # 6 位小数坐标同样正常（前端定位固定发 6 位）
        status_code, text, data = self.fetch("regeo.six_decimals", "/api/regeo?location=116.123456,39.123456")
        self.ck.eq("regeo.six_decimals.status_code", status_code, 200)
        if isinstance(data, dict):
            self.ck.eq("regeo.six_decimals.location", data.get("location"), {"lng": 116.123456, "lat": 39.123456})

        # 非法坐标 → 4xx + 统一错误结构
        invalid_cases = [
            ("regeo.invalid.missing", "/api/regeo"),
            ("regeo.invalid.bad", "/api/regeo?location=bad"),
            ("regeo.invalid.one_part", "/api/regeo?location=116.3"),
            ("regeo.invalid.out_of_range", "/api/regeo?location=200,39.9"),
            ("regeo.invalid.empty", "/api/regeo?location="),
            ("regeo.invalid.text", "/api/regeo?location=abc,def"),
        ]
        samples = {}
        for name, path in invalid_cases:
            status_code, text, data = self.fetch(name, path)
            ok = isinstance(status_code, int) and 400 <= status_code < 500
            self.ck.check(f"{name}.4xx", ok, f"status={status_code} body={text[:300]!r}")
            error = data.get("error") if isinstance(data, dict) else None
            self.ck.check(f"{name}.error_object", isinstance(error, dict), f"body={text[:300]!r}")
            if isinstance(error, dict):
                self.ck.eq(f"{name}.error_code", error.get("code"), "INVALID_INPUT")
                self.ck.check(
                    f"{name}.error_message_readable",
                    isinstance(error.get("message"), str) and len(error["message"].strip()) >= 2 and "Traceback" not in error["message"],
                    f"message={error.get('message')!r}",
                )
                self.ck.check(f"{name}.error_hint_str", isinstance(error.get("hint"), str), f"hint={error.get('hint')!r}")
                if name in {"regeo.invalid.bad", "regeo.invalid.out_of_range"}:
                    samples[name] = {"status": status_code, "body": data}
        self.samples["regeo_validation"] = samples

        # >6 位小数：v0.4.0 最终版已与 /api/plan 对齐 → 400（L-v040-1 已修复）
        status_code, text, data = self.fetch("regeo.too_many_decimals", "/api/regeo?location=116.1234567,39.9")
        ok_4xx = isinstance(status_code, int) and 400 <= status_code < 500
        self.ck.check("regeo.too_many_decimals.4xx", ok_4xx, f"status={status_code} body={text[:300]!r}")
        error = data.get("error") if isinstance(data, dict) else None
        self.ck.check(
            "regeo.too_many_decimals.error_code",
            isinstance(error, dict) and error.get("code") == "INVALID_INPUT",
            f"error={error!r}",
        )
        self.ck.check(
            "regeo.too_many_decimals.message_6_digits",
            isinstance(error, dict) and isinstance(error.get("message"), str) and "6 位" in error["message"],
            f"message={(error or {}).get('message')!r}",
        )
        if not ok_4xx:
            self.ck.warn("regeo.too_many_decimals.contract", "location 超过 6 位小数应返回 400（与 /api/plan 一致）")

        # 演示响应不含 Key 值形态
        leak_patterns = [re.compile(r"\b[0-9a-fA-F]{32}\b"), re.compile(r"restapi\.amap\.com"), re.compile(r'"amap_key"\s*:'), re.compile(r"[?&]key=(?!\*)")]
        leaked = next((pattern.pattern for pattern in leak_patterns if pattern.search(valid_text)), None)
        self.ck.check("regeo.no_key_material", leaked is None, f"命中 {leaked!r}")

    # -- 5. 安全（Key 不泄漏 / .gitignore）-----------------------------------
    def check_security(self) -> None:
        print("\n== 5. 安全：响应与前端静态文件不含 Key；.gitignore 覆盖 ==")
        texts = {}
        for name, path, kwargs in [
            ("status", "/api/status", {}),
            ("openapi", "/api/openapi.json", {}),
            ("index", "/", {}),
            ("app_js", "/app.js", {}),
            ("styles_css", "/styles.css", {}),
            ("favicon", "/favicon.svg", {}),
        ]:
            status_code, text, data = self.ck.http(self.session, f"security.fetch.{name}", "GET", self.url(path), **kwargs)
            self.ck.eq(f"security.fetch.{name}.status_code", status_code, 200)
            texts[name] = text

        status_code, text, data = self.post_plan("security.plan", plan_payload(PLACES[:2], ["transit"]))
        self.ck.eq("security.plan.status_code", status_code, 200)
        texts["plan"] = text

        status_code, text, data = self.post_compare(
            "security.compare",
            {"points": [{"address": PLACES[0]}, {"address": PLACES[1]}]},
        )
        self.ck.eq("security.compare.status_code", status_code, 200)
        texts["compare"] = text

        status_code, text, data = self.fetch("security.regeo", "/api/regeo?location=116.397,39.908")
        self.ck.eq("security.regeo.status_code", status_code, 200)
        texts["regeo"] = text

        # 响应/静态文件里不得出现「Key 值」或 Key 的请求形态。
        # 注意：demo_reason 按契约会写「未配置 AMAP_KEY」，那是环境变量名而非 Key 值，不能误报，
        # 因此这里只扫描 Key 值形态（32 位十六进制）、高德域名、JSON 字段 amap_key、query 参数 key=。
        leak_patterns = [
            re.compile(r"\b[0-9a-fA-F]{32}\b"),
            re.compile(r"restapi\.amap\.com"),
            re.compile(r'"amap_key"\s*:'),
            re.compile(r"[?&]key=(?!\*)"),
        ]
        for name, text in texts.items():
            lowered = text or ""
            leaked = next((pattern.pattern for pattern in leak_patterns if pattern.search(lowered)), None)
            self.ck.check(f"security.{name}.no_key_material", leaked is None, f"命中 {leaked!r}")
        # 错误响应也不能带 Env/Key 细节
        status_code, text, data = self.post_plan("security.error_body", {"points": [], "modes": []})
        self.ck.check("security.error.no_key_material", not any(pattern.search(text or "") for pattern in leak_patterns), text[:300])
        # 演示原因允许（且应当）说明是「未配置 Key」，但不得出现 Key 值本身
        status_text = texts.get("status", "")
        self.ck.check(
            "security.status.demo_reason_is_reason_not_key",
            "AMAP_KEY" in status_text and not any(pattern.search(status_text) for pattern in leak_patterns),
            "demo_reason 应说明未配置 Key（环境变量名可出现），但不得包含 Key 值",
        )

        # 前端源码中不得出现 AMAP / Key 相关字符串
        web_dir = PROJECT_ROOT / "web"
        for file_name in ("index.html", "app.js", "styles.css"):
            source = read_text(web_dir / file_name)
            self.ck.check(
                f"security.web.{file_name}.no_amap_key",
                "AMAP_KEY" not in source and "amap_key" not in source and "restapi.amap.com" not in source,
                f"{file_name} 出现了 AMAP/Key 相关字符串",
            )
        frontend_hex = [name for name in ("index.html", "app.js", "styles.css") if re.search(r"\b[0-9a-fA-F]{32}\b", read_text(web_dir / name))]
        self.ck.eq("security.web.no_hardcoded_hex_key", frontend_hex, [])

        # backend 源码里不得有硬编码 Key（只允许从环境变量/本地配置读取）
        backend_hex = []
        for path in sorted((PROJECT_ROOT / "backend").glob("*.py")):
            if re.search(r"\b[0-9a-fA-F]{32}\b", read_text(path)):
                backend_hex.append(path.name)
        self.ck.eq("security.backend.no_hardcoded_hex_key", backend_hex, [])

        # config.local.json 必须被 .gitignore 覆盖，且当前工作区不应存在真实 Key 文件
        gitignore = read_text(PROJECT_ROOT / ".gitignore")
        lines = {line.strip() for line in gitignore.splitlines()}
        self.ck.check("security.gitignore.config_local", "config.local.json" in lines, f".gitignore 未包含 config.local.json：{sorted(lines)}")
        self.ck.check("security.gitignore.env", ".env" in lines, ".gitignore 未包含 .env")
        config_local = PROJECT_ROOT / "config.local.json"
        config_local_exists = config_local.exists()
        self.ck.check(
            "security.config_local.absent_or_placeholder",
            not config_local_exists,
            f"工作区存在 config.local.json（含真实 Key 的文件不应提交/出现在验证环境）：{config_local}",
        )
        example = read_text(PROJECT_ROOT / "config.local.example.json")
        example_data = json.loads(example)
        self.ck.check(
            "security.config_example.placeholder",
            isinstance(example_data.get("amap_key"), str) and "你的" in example_data["amap_key"],
            f"config.local.example.json 的 amap_key 应是占位符：{example_data.get('amap_key')!r}",
        )
        # status/plan 响应中也没有非契约字段泄露实现细节
        status_data = json.loads(texts["status"])
        self.ck.eq("security.status.keys", set(status_data.keys()), {"ok", "version", "demo", "demo_reason", "defaults", "modes"})

    # -- 6. 静态资源 ----------------------------------------------------------
    def check_static(self) -> None:
        print("\n== 6. 前端静态资源（同源托管）==")
        status_code, text, data = self.fetch("static.index", "/")
        self.ck.eq("static.index.status_code", status_code, 200)
        self.ck.check("static.index.is_html", "text/html" in self.last_content_type, self.last_content_type)
        self.ck.check("static.index.title", "<title>SuperNav</title>" in (text or ""), "浏览器标签应固定为 SuperNav")
        self.ck.check("static.index.has_demo_banner", 'id="demo-banner"' in (text or ""), "缺少演示模式横幅")
        self.ck.check(
            "static.index.single_theme_no_switch",
            "theme-switch" not in (text or "") and "theme-option" not in (text or "") and "data-theme" not in (text or ""),
            "单主题模板不应有主题切换控件/data-theme",
        )
        self.ck.check(
            "static.index.template_structure",
            'class="head"' in (text or "") and "<h1>SuperNav</h1>" in (text or "") and 'class="sub"' in (text or "") and 'class="content"' in (text or ""),
            "模板结构应为 .head(h1/.sub) + .content",
        )
        self.ck.check("static.index.local_assets", 'href="styles.css"' in (text or "") and 'src="app.js"' in (text or ""), "应引用同源 styles.css / app.js")
        self.samples["index"] = text

        for name, path, marker, expected_type in [
            ("styles", "/styles.css", "--bg: #3a3f47", "css"),
            ("app", "/app.js", "const SAVED_LOCATION_COOKIE", "javascript"),
            ("favicon", "/favicon.svg", "<svg", "svg"),
        ]:
            status_code, text, data = self.fetch(f"static.{name}", path)
            self.ck.eq(f"static.{name}.status_code", status_code, 200)
            self.ck.check(f"static.{name}.content_type", expected_type in self.last_content_type, self.last_content_type)
            self.ck.check(f"static.{name}.content", marker in (text or ""), f"未找到标记 {marker!r}")

        # web/ 前缀兼容路径
        status_code, _, _ = self.ck.http(self.session, "static.web_prefix", "GET", self.url("/web/app.js"))
        self.ck.eq("static.web_prefix.status_code", status_code, 200)

        # 未知路径：404 + 统一错误结构
        status_code, text, data = self.ck.http(self.session, "static.unknown", "GET", self.url("/no-such-file-xyz"))
        self.ck.eq("static.unknown.status_code", status_code, 404)
        self.ck.check(
            "static.unknown.error_body",
            isinstance(data, dict) and isinstance(data.get("error"), dict) and isinstance(data["error"].get("message"), str),
            f"body={text[:300]!r}",
        )

        # 目录穿越防护
        status_code, text, _ = self.ck.http(self.session, "static.traversal", "GET", self.url("/..%2f..%2fconfig.local.example.json"))
        self.ck.check("static.traversal.blocked", status_code in (400, 404), f"status={status_code}")

    # -- 7. 前端关键逻辑代码审查 ---------------------------------------------
    def check_frontend_source(self) -> None:
        print("\n== 7. 前端代码审查（单灰主题/模板/功能/定位 Cookie/无外链）==")
        index = read_text(PROJECT_ROOT / "web" / "index.html")
        app = read_text(PROJECT_ROOT / "web" / "app.js")
        styles = read_text(PROJECT_ROOT / "web" / "styles.css")

        def present(label: str, condition: bool, detail: str = "") -> None:
            self.ck.check(f"frontend.{label}", condition, detail)

        # ===== 单灰主题 + 模板结构（v0.5.0 前端约束）=====
        present("theme.single_no_data_theme", "data-theme" not in index and "data-theme" not in app and "[data-theme" not in styles)
        present("theme.no_switch_dom", "theme-switch" not in index and "theme-option" not in index and "themeButtons" not in app)
        present("theme.no_localstorage", "localStorage" not in app and "localStorage" not in index)
        present("theme.no_theme_query", "?theme=" not in index and "?theme=" not in app)
        present("theme.template_head", 'class="head"' in index and "<h1>SuperNav</h1>" in index)
        present("theme.h1_title", "<title>SuperNav</title>" in index and "<h1>SuperNav</h1>" in index)
        present("theme.sub", 'class="sub"' in index)
        present("theme.content", 'class="content"' in index)
        present("theme.color_scheme_dark", 'name="color-scheme"' in index and "color-scheme: dark" in styles)
        present("theme.560_breakpoint", "@media (max-width: 560px)" in styles)
        present("theme.no_box_shadow", "box-shadow" not in styles)
        present("theme.no_external_css", "@import" not in styles and re.search(r"https?://", styles) is None)
        present(
            "theme.root_five_vars",
            all(
                f"--{name}: {value}" in styles
                for name, value in (
                    ("bg", "#3a3f47"),
                    ("fg", "#f2f3f5"),
                    ("muted", "#b3b9c2"),
                    ("line", "#4b525c"),
                    ("hover", "#454c56"),
                )
            ),
        )
        hex_values = set(re.findall(r"#[0-9a-fA-F]{6}\b", styles))
        present(
            "theme.five_literal_colors",
            hex_values == {"#3a3f47", "#f2f3f5", "#b3b9c2", "#4b525c", "#454c56"},
            f"literal colors={sorted(hex_values)}",
        )
        present("theme.no_rgb_hsl_colors", re.search(r"rgba?\(|hsla?\(", styles) is None)
        non_var_part = re.sub(r":root\s*\{.*?\}", "", styles, flags=re.S)
        present("theme.colors_via_vars_only", re.search(r"#[0-9a-fA-F]{3,8}", non_var_part) is None, "变量定义之外不应出现字面色值")
        present("theme.cookie_no_localstorage", "document.cookie" in app and "localStorage" not in app)

        # 添加 / 删除途经点、10 点上限
        present("points.add_button", 'id="add-point"' in index and "添加途经点" in index)
        present("points.add_handler", "addWaypoint" in app and "dom.addPoint.addEventListener" in app)
        present("points.remove_button", "data-remove-index" in app and "removeWaypoint" in app)
        present("points.max_const", re.search(r"MAX_POINTS\s*=\s*10", app) is not None)
        present("points.min_const", re.search(r"MIN_POINTS\s*=\s*2", app) is not None)
        present("points.dynamic_rows", "renderForm" in app and "points-list" in index)
        present("points.insert_before_end", "state.points.length - 1" in app and "state.modes.splice" in app)
        present("points.remove_guard", "index <= 0" in app and "index >= state.points.length - 1" in app)

        # 演示模式横幅 / 来源标识
        present("demo.banner_dom", 'id="demo-banner"' in index and "演示模式" in index)
        present("demo.banner_reason_dom", 'id="demo-banner-reason"' in index)
        present("demo.banner_fn", "setDemoBanner" in app and "dom.demoBanner.hidden" in app)
        present("demo.banner_from_status", "status.demo === true" in app and "demo_reason" in app)
        present("demo.result_badge", "updateSourceBadge" in app and "mode === 'demo'" in app and "演示数据" in app)

        # 错误展示 / 请求体
        present("errors.alert_dom", 'id="alert"' in index and 'id="alert-message"' in index and 'id="alert-hint"' in index)
        present("errors.show_alert", "function showAlert" in app and "function clearAlert" in app)
        present("errors.extract_error", "function extractError" in app and "data.error.message" in app)
        present("errors.submit_failure", "catch (error)" in app and "showAlert(" in app)
        present("errors.timeout", "AbortController" in app and "PLAN_TIMEOUT_MS" in app and "COMPARE_TIMEOUT_MS" in app)
        present("api.status_fetch", re.search(r"fetch\(\s*['\"]/?api/status", app) is not None)
        present("api.plan_fetch", "requestEndpoint('api/plan'" in app and "method: 'POST'" in app)
        present("api.compare_fetch", "requestEndpoint('api/compare'" in app)
        present("api.status_retry", "handleStatusRetry" in app and 'id="status-retry"' in index)
        present("payload.address_shape", "function buildPointsPayload" in app and "points: buildPointsPayload()" in app)
        compare_payload_match = re.search(r"function buildComparePayload\(.*?\n  \}", app, re.S)
        compare_payload_src = compare_payload_match.group(0) if compare_payload_match else ""
        present("payload.compare_no_modes", "function buildComparePayload" in app and "points:" in compare_payload_src and "modes:" not in compare_payload_src, "对比请求不应携带 modes")

        # ===== 方式对比（默认）与组合行程 =====
        present("compare.default_plan_mode", re.search(r"planMode:\s*PLAN_MODE_COMPARE", app) is not None)
        present("compare.mode_switch_dom", 'data-plan-mode="compare"' in index and 'data-plan-mode="combine"' in index)
        present("compare.mode_switch_default", re.search(r'data-plan-mode="compare"[^>]*aria-pressed="true"', index) is not None)
        present("compare.mode_switch_bind", "dom.planModeButtons.forEach" in app and "setPlanMode(" in app)
        present("compare.combine_form_keeps_selects", "state.planMode === PLAN_MODE_COMBINE && index < state.points.length - 1" in app, "组合行程模式应保留每段方式下拉")
        order_match = re.search(r"COMPARE_ORDER\s*=\s*\[(.*?)\]", app, re.S)
        order_values = re.findall(r"'([a-z]+)'", order_match.group(1)) if order_match else []
        present(
            "compare.card_order",
            order_values == ["transit", "driving", "taxi", "bicycling", "walking"],
            f"卡片顺序应为 公交→驾车→打车→骑行→步行，实际 COMPARE_ORDER={order_values}",
        )
        present("compare.grid_dom", 'id="compare-section"' in index and 'id="compare-grid"' in index)
        present("compare.card_dom", "function renderCompareCards" in app and "data-compare-mode" in app)
        present("compare.card_metrics", "buildCompareMetric('耗时'" in app and "'里程'" in app and "'费用'" in app)
        present("compare.click_switch", "selectCompareMode(" in app and "addEventListener('click'" in app)
        present(
            "compare.detail_reuse",
            "function renderCompareDetail" in app
            and "renderTotals(detailData" in app
            and "renderLegs(detailData" in app
            and "renderRoute(entry.legs" in app,
            "详情应复用合计/逐段/路线渲染",
        )
        present("compare.initial_selection", "pickInitialCompareMode" in app)
        present("compare.unavailable_card", "is-unavailable" in app and "无可用路线" in app)
        present("compare.unavailable_detail", "function showModeError" in app and "dom.detail.hidden = true" in app and 'id="mode-error"' in index)
        present("compare.combine_mode_kept", "function renderPlanResult" in app and "function buildPlanPayload" in app and "requestEndpoint('api/plan'" in app)
        present("compare.mode_switch_rerender", "function setPlanMode" in app and "renderForm()" in app)
        present(
            "compare.no_innerhtml",
            not re.search(r"\.innerHTML\s*=", app)
            and "insertAdjacentHTML" not in app
            and "document.write" not in app
            and "eval(" not in app,
            "前端不应使用 innerHTML/eval 等不安全 DOM 写法",
        )

        # ===== 起点定位（v0.3.0）=====
        present("locate.button_dynamic", "data-locate-index" in app and "btn-locate" in app)
        present(
            "locate.start_only",
            re.search(r"if \(pointKind\(index\) === 'start'\) \{\s*\n\s*const locate", app) is not None
            and "btn-locate" in styles,
            "定位按钮应只出现在起点行",
        )
        present("locate.geolocation_api", "navigator.geolocation" in app and "getCurrentPosition" in app)
        present("locate.secure_context_guard", "window.isSecureContext" in app)
        present("locate.button_busy", "setLocateButtonBusy" in app and "定位中" in app)
        present("locate.point_locations_state", app.count("pointLocations") >= 3 and "pointLocations: [null, null]" in app)
        present(
            "locate.payload_location",
            re.search(r"location:\s*located\.lng\.toFixed\(6\)", app) is not None
            and re.search(r"name:\s*String\(address \|\| ''\)\.trim\(\) \|\| '我的位置'", app) is not None,
            "定位点提交时应为 {location, name:'我的位置'}",
        )
        present("locate.fills_detail", "state.points[index] = detail" in app and "locateFallbackLabel(" in app)
        present("locate.error_paths", all(f"code === {code}" in app for code in (1, 2, 3)))
        present("locate.manual_edit_clears", "state.pointLocations[index] = null" in app and "不再使用浏览器定位坐标" in app)
        present("locate.css", ".btn-locate" in styles and ".point-input.is-located" in styles)

        # ===== 上次定位保存/恢复（v0.5.0：Cookie，无 localStorage）=====
        present("locate.saved_cookie_key", "supernav_saved_location" in app and "SAVED_LOCATION_COOKIE" in app)
        present("locate.cookie_helpers", "function setCookie" in app and "function getCookie" in app and app.count("document.cookie") >= 2)
        present("locate.cookie_attributes", "Max-Age=31536000" in app and "Path=/" in app and "SameSite=Lax" in app)
        present("locate.saved_json", "JSON.stringify(location)" in app and "JSON.parse(raw)" in app)
        present("locate.saved_validation", "lng < -180" in app and "lat < -90" in app and "updatedAt" in app)
        present(
            "locate.restore_on_init",
            "function init()" in app and "restoreSavedLocation();" in app and app.index("restoreSavedLocation();") > app.index("function init()"),
            "init() 中应恢复上次定位（Cookie）",
        )
        present("locate.no_localstorage", "localStorage" not in app)
        present("locate.regeo_fetch", "api/regeo?location=" in app and "encodeURIComponent(" in app)
        present("locate.regeo_timeout", "fetchRegeoAddress" in app and "10000" in app and "controller.abort()" in app)
        present("locate.fallback_label", "function locateFallbackLabel" in app and "当前位置（" in app)
        present("locate.saves_after_regeo", "saveSavedLocation({" in app and "address: detail" in app)
        present(
            "locate.keeps_busy_until_regeo",
            "fetchRegeoAddress(located.lng, located.lat).then" in app and "state.pointLocations[index] !== located" in app,
            "逆地理返回前应保持「定位中…」并防止用户编辑被覆盖",
        )
        present("locate.title_saved", "'已定位并保存：'" in app)

        # 无外部 CDN / 无外链资源
        external_refs = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', index)
        present("offline.index_no_external_refs", not external_refs, f"外链：{external_refs}")
        app_urls = re.findall(r"https?://[^\s'\"]+", app)
        # 允许 SVG 命名空间与「本地可用 http://127.0.0.1/localhost」提示文案；其余外链视为违规
        allowed_url_prefixes = ("http://www.w3.org/", "http://127.0.0.1", "http://localhost", "https://localhost")
        non_svg_urls = [url for url in app_urls if not url.startswith(allowed_url_prefixes)]
        present("offline.app_no_external_urls", not non_svg_urls, f"外链：{non_svg_urls}")
        present("offline.no_cdn", not re.search(r"(cdn\.|unpkg|jsdelivr|googleapis|bootstrap)", index + app, re.I), "出现 CDN 引用")
        present("offline.css_no_import", "@import" not in styles and not re.search(r"url\(\s*['\"]?https?://", styles), "CSS 引用外部资源")
        present("offline.index_script_local", 'src="app.js"' in index and 'href="styles.css"' in index)
        present("offline.single_js", 'src="app.js"' in index and len(re.findall(r"<script\b", index)) == 1, "只应加载同源 app.js 一个脚本")

    # -- 8. node --check / 内联脚本语法 --------------------------------------
    def check_node_syntax(self) -> None:
        print("\n== 8. node --check（web/app.js + index.html 内联脚本）==")
        node = shutil.which("node")
        if not node:
            self.ck.check("node.available", False, "PATH 中找不到 node，无法执行 node --check")
            return
        self.ck.check("node.available", True, "")
        target = PROJECT_ROOT / "web" / "app.js"
        proc = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, timeout=60)
        self.ck.check("node.check_app_js", proc.returncode == 0, (proc.stderr or proc.stdout)[:500])
        # 抽取 index.html 内联 <script>（无 src）做语法检查
        index = read_text(PROJECT_ROOT / "web" / "index.html")
        inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index, re.S)
        if inline:
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            inline_path = ARTIFACT_DIR / "index-inline.js"
            inline_path.write_text("\n".join(inline), encoding="utf-8")
            proc = subprocess.run([node, "--check", str(inline_path)], capture_output=True, text=True, timeout=60)
            self.ck.check("node.check_index_inline", proc.returncode == 0, (proc.stderr or proc.stdout)[:500])
        else:
            # v0.5.0 单主题模板不再需要内联主题引导脚本（无 data-theme/localStorage）
            self.ck.check("node.no_inline_script", True, "index.html 无内联脚本（符合单主题模板）")

    # -- 9. 后端 pytest（离线 mock 单测）-------------------------------------
    def check_pytest(self) -> None:
        print("\n== 9. 后端离线单测：python -m pytest -q ==")
        env = os.environ.copy()
        env.pop("AMAP_KEY", None)
        env.pop("DEMO_MODE", None)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=600,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.ck.check("pytest.exit_code", False, "pytest 超时（600s）")
            return
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        tail = output.splitlines()[-8:] if output else []
        self.samples["pytest_tail"] = tail
        self.ck.check("pytest.exit_code", proc.returncode == 0, f"rc={proc.returncode} tail={tail}")
        passed_match = re.search(r"(\d+) passed", output)
        self.ck.check("pytest.passed_count", passed_match is not None and int(passed_match.group(1)) > 0, f"tail={tail}")

    # -- 结果落盘 ------------------------------------------------------------
    def save_artifacts(self, server: ServerHandle, checks: Checker) -> None:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "base_url": self.base_url,
            "server_owned_by_smoke": server.owned,
            "server_log": str(server.log_path) if server.log_path else None,
            "stats": {"passed": checks.passed, "failed": checks.failed, "warnings": len(checks.warnings)},
            "warnings": checks.warnings,
            "results": checks.results,
            "samples": self.samples,
        }
        target = ARTIFACT_DIR / "smoke-run.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[artifact] 结果已写入：{target}")


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SuperNav 交通规划 MVP 独立验证（verify/smoke.py）")
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", DEFAULT_BASE_URL), help="服务地址，默认 http://127.0.0.1:8014")
    parser.add_argument("--auto-server", action="store_true", help="服务未运行时自动拉起 uvicorn（8014）并在结束后关闭")
    parser.add_argument("--no-auto-server", action="store_true", help="不自动拉起服务（默认行为同此；保留 --auto-server 显式开启）")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    base_url = args.base_url.rstrip("/")
    print("=" * 78)
    print("SuperNav 交通规划 MVP · 独立验证（verify/smoke.py）")
    print(f"时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    print(f"目标：{base_url}")
    print(f"工作区：{PROJECT_ROOT}")
    print("=" * 78)

    ck = Checker()
    session = requests.Session()
    server = ServerHandle(base_url, auto=args.auto_server and not args.no_auto_server)
    suite = Suite(ck, session, base_url)
    exit_code = 0
    try:
        if not server.ensure():
            ck.check("server.reachable", False, f"无法连接 {base_url}")
            server.stop()
            suite.save_artifacts(server, ck)
            return 1
        ck.check("server.reachable", True, "")

        status_data = suite.check_health_and_status()
        is_demo = isinstance(status_data, dict) and status_data.get("demo") is True

        if is_demo:
            suite.check_plan_matrix()
            suite.check_validation_errors()
            suite.check_location_and_limits()
            suite.check_options()
            suite.check_compare()
            suite.check_regeo_endpoint()
        else:
            ck.check(
                "environment.demo_mode",
                False,
                "当前服务不是演示模式（检测到 AMAP_KEY / DEMO_MODE 配置）。为保证离线可重复验证，"
                "请在无 Key 环境或用 DEMO_MODE=1 启动服务后重跑；本次跳过依赖演示数据的规划/费用用例。",
            )
            # 参数校验不依赖数据源，仍可验证；成功路径会联网，跳过
            suite.check_validation_errors()
        # compare 的参数校验 / 失败隔离代码审查 / 定向 mock 单测不依赖演示数据，始终执行
        suite.check_compare_validation()
        suite.check_compare_failure_isolation()
        # 起点定位（regeo）代码审查 + 定向 mock 单测，同样不依赖演示数据
        suite.check_regeo_location()

        suite.check_security()
        suite.check_static()
        suite.check_frontend_source()
        suite.check_node_syntax()
        suite.check_pytest()
    except Exception as exc:  # noqa: BLE001  —— 验证脚本自身异常必须落盘并返回失败
        import traceback

        ck.check("harness.unexpected_exception", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        try:
            suite.save_artifacts(server, ck)
        except Exception as exc:  # noqa: BLE001
            print(f"[artifact] 写入失败：{exc}")
        server.stop()
        session.close()

    print("\n" + "=" * 78)
    print(f"统计：PASS={ck.passed}  FAIL={ck.failed}  WARN={len(ck.warnings)}")
    if ck.warnings:
        for warning in ck.warnings:
            print(f"  WARN {warning['name']}: {warning['detail'][:300]}")
    print("结论：" + ("PASS（全部检查通过）" if ck.failed == 0 else f"FAIL（{ck.failed} 项未通过，详见上方 FAIL 行与 verify/artifacts/smoke-run.json）"))
    print("=" * 78)
    exit_code = 0 if ck.failed == 0 else 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
