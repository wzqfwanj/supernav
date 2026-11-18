"""SuperNav 交通规划 MVP — Lead 验收脚本（对运行中的服务做黑盒验收）。

用法：
    python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
    python scripts/acceptance.py            # 默认 http://127.0.0.1:8000
    $env:BASE_URL="http://127.0.0.1:8014"; python scripts/acceptance.py

无第三方依赖（除 requests，requirements 已含）。退出码 0 = 全部通过。
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback

import requests

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = 20
failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        failures.append(name)


def approx(a: float, b: float, tol: float = 0.011) -> bool:
    return abs(float(a) - float(b)) <= tol


def cost_sum(cost: dict) -> float:
    return round(sum(float(x.get("amount", 0)) for x in cost.get("breakdown", [])), 2)


def has_field(obj, name: str) -> bool:
    """递归检查 JSON 中是否存在指定字段名（用于确认 Key 字段未被下发）。"""
    if isinstance(obj, dict):
        return name in obj or any(has_field(v, name) for v in obj.values())
    if isinstance(obj, list):
        return any(has_field(v, name) for v in obj)
    return False


def main() -> int:
    s = requests.Session()

    # 1. status
    try:
        r = s.get(f"{BASE_URL}/api/status", timeout=TIMEOUT)
        status = r.json()
    except Exception:
        traceback.print_exc()
        print("\n无法连接服务，请先启动 uvicorn。")
        return 2
    check("GET /api/status 200", r.status_code == 200, f"status={r.status_code}")
    for key in ("ok", "version", "demo", "defaults", "modes"):
        check(f"status 包含 {key}", key in status)
    modes = {m.get("value") for m in status.get("modes", [])}
    check(
        "status modes 覆盖 5 种方式",
        {"walking", "bicycling", "transit", "taxi", "driving"} <= modes,
        f"modes={modes}",
    )
    demo = bool(status.get("demo"))
    check("无 Key 时 status.demo=true", demo is True, f"demo={demo}")
    raw_status = json.dumps(status, ensure_ascii=False)
    # demo_reason 允许出现「AMAP_KEY」这个环境变量名，但不能出现 Key 字段或真实 Key 值
    check("status 无 amap_key 字段", not has_field(status, "amap_key"))
    check(
        "status 不含疑似 32 位高德 Key",
        re.search(r"[0-9a-f]{32}", raw_status) is None,
        raw_status[:120],
    )

    # 2. healthz
    r = s.get(f"{BASE_URL}/api/healthz", timeout=TIMEOUT)
    check("GET /api/healthz 200", r.status_code == 200 and r.json().get("status") == "ok")

    defaults = status.get("defaults", {})
    fuel_price = float(defaults.get("fuel_price", 7.8))
    fuel_consumption = float(defaults.get("fuel_consumption", 8.0))

    # 3. 多段组合规划（演示模式）
    payload = {
        "points": [
            {"address": "北京南站"},
            {"address": "故宫博物院"},
            {"address": "北京西站"},
        ],
        "modes": ["transit", "walking"],
        "options": {"fuel_price": fuel_price, "fuel_consumption": fuel_consumption},
    }
    r = s.post(f"{BASE_URL}/api/plan", json=payload, timeout=TIMEOUT)
    check("POST /api/plan 多段 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        data = r.json()
        check("plan.mode=demo", data.get("mode") == "demo", f"mode={data.get('mode')}")
        legs = data.get("legs", [])
        check("plan 段数=2", len(legs) == 2, f"legs={len(legs)}")
        check("plan points 回显 3 个", len(data.get("points", [])) == 3)
        total_ok = True
        dist_sum = 0
        dur_sum = 0
        cost_by_key: dict[str, float] = {}
        for leg in legs:
            dist_sum += int(leg.get("distance_m", 0))
            dur_sum += int(leg.get("duration_s", 0))
            c = leg.get("cost", {})
            if not approx(cost_sum(c), c.get("total", -1)):
                total_ok = False
            for item in c.get("breakdown", []):
                cost_by_key[item["key"]] = cost_by_key.get(item["key"], 0.0) + float(
                    item.get("amount", 0)
                )
        totals = data.get("totals", {})
        check("totals 距离=逐段之和", int(totals.get("distance_m", -1)) == dist_sum)
        check("totals 时长=逐段之和", int(totals.get("duration_s", -1)) == dur_sum)
        check("每段 cost.total=明细之和", total_ok)
        tc = totals.get("cost", {})
        check("totals cost.total=明细之和", approx(cost_sum(tc), tc.get("total", -1)))
        expected_total = round(sum(cost_by_key.values()), 2)
        check(
            "totals cost.total=逐段明细汇总",
            approx(tc.get("total", -1), expected_total),
            f"expected={expected_total} got={tc.get('total')}",
        )
        walking = legs[1]
        check("步行段费用为 0", float(walking.get("cost", {}).get("total", -1)) == 0.0)
        check("公交段有票价明细", bool(legs[0].get("cost", {}).get("breakdown")))

    # 3.5 方式对比（/api/compare，一次返回 5 种方式）
    compare_body = {
        "points": [
            {"address": "北京南站"},
            {"address": "故宫博物院"},
            {"address": "北京西站"},
        ],
        "options": {"fuel_price": fuel_price, "fuel_consumption": fuel_consumption},
    }
    r = s.post(f"{BASE_URL}/api/compare", json=compare_body, timeout=TIMEOUT)
    check("POST /api/compare 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    if r.status_code == 200:
        cmp_data = r.json()
        check("compare.mode=demo", cmp_data.get("mode") == "demo", str(cmp_data.get("mode")))
        cmp_modes = cmp_data.get("modes", [])
        check("compare 返回 5 种方式", len(cmp_modes) == 5, str(len(cmp_modes)))
        check(
            "compare 方式顺序固定",
            [m.get("mode") for m in cmp_modes]
            == ["walking", "bicycling", "transit", "taxi", "driving"],
            str([m.get("mode") for m in cmp_modes]),
        )
        ok_shape = True
        ok_totals = True
        ok_cost = True
        zero_ok = True
        for entry in cmp_modes:
            ok_shape = ok_shape and set(entry) >= {
                "mode",
                "mode_label",
                "available",
                "error",
                "legs",
                "totals",
                "summary",
            }
            if not entry.get("available"):
                continue
            legs = entry.get("legs", [])
            totals = entry.get("totals", {})
            ok_shape = ok_shape and len(legs) == 2
            dist = sum(int(leg.get("distance_m", 0)) for leg in legs)
            dur = sum(int(leg.get("duration_s", 0)) for leg in legs)
            ok_totals = ok_totals and int(totals.get("distance_m", -1)) == dist
            ok_totals = ok_totals and int(totals.get("duration_s", -1)) == dur
            for leg in legs:
                c = leg.get("cost", {})
                ok_cost = ok_cost and approx(cost_sum(c), c.get("total", -1))
            tc = totals.get("cost", {})
            ok_cost = ok_cost and approx(cost_sum(tc), tc.get("total", -1))
            if entry.get("mode") in ("walking", "bicycling"):
                zero_ok = zero_ok and float(tc.get("total", -1)) == 0.0
        check("compare 每项结构齐全且 2 段", ok_shape)
        check("compare totals=逐段求和", ok_totals)
        check("compare 费用明细=total", ok_cost)
        check("compare 步行/骑行费用为 0", zero_ok)
        transit_entry = next((m for m in cmp_modes if m.get("mode") == "transit"), {})
        check("compare 公交有 summary", bool(transit_entry.get("summary")))
        check("compare 无 Key 泄露", "amap_key" not in json.dumps(cmp_data, ensure_ascii=False).lower())

    # 4. 各方式单段（步行/骑行/公交/打车/驾车）
    for mode in ("walking", "bicycling", "transit", "taxi", "driving"):
        p = {
            "points": [{"address": "北京南站"}, {"address": "故宫博物院"}],
            "modes": [mode],
            "options": {"fuel_price": fuel_price, "fuel_consumption": fuel_consumption},
        }
        r = s.post(f"{BASE_URL}/api/plan", json=p, timeout=TIMEOUT)
        ok = r.status_code == 200
        detail = f"{r.status_code} {r.text[:160]}"
        if ok:
            leg = r.json()["legs"][0]
            ok = leg.get("mode") == mode and int(leg.get("distance_m", -1)) >= 0
            c = leg.get("cost", {})
            ok = ok and approx(cost_sum(c), c.get("total", -1))
            if mode in ("walking", "bicycling"):
                ok = ok and float(c.get("total", -1)) == 0.0
            if mode == "driving":
                tol = float(c.get("total", 0)) - sum(
                    float(x["amount"]) for x in c.get("breakdown", []) if x["key"] == "toll"
                )
                expected_fuel = round(
                    int(leg["distance_m"]) / 1000 * fuel_consumption / 100 * fuel_price, 2
                )
                ok = ok and approx(tol, expected_fuel, 0.05)
                detail += f" fuel={tol} expected={expected_fuel}"
            if mode == "taxi":
                check(
                    "打车费为合理正值",
                    float(c.get("total", 0)) > 0,
                    f"total={c.get('total')}",
                )
        check(f"POST /api/plan 单段 {mode}", ok, detail)

    # 5. 参数校验
    bad_cases = [
        ("仅一个点", {"points": [{"address": "北京南站"}], "modes": []}),
        (
            "modes 数量不符",
            {"points": [{"address": "北京南站"}, {"address": "故宫"}], "modes": []},
        ),
        (
            "未知方式",
            {
                "points": [{"address": "北京南站"}, {"address": "故宫"}],
                "modes": ["flying"],
            },
        ),
        (
            "空地址",
            {"points": [{"address": ""}, {"address": "故宫"}], "modes": ["walking"]},
        ),
    ]
    for name, body in bad_cases:
        r = s.post(f"{BASE_URL}/api/plan", json=body, timeout=TIMEOUT)
        ok = 400 <= r.status_code < 500
        try:
            err = r.json()
            msg = err.get("error", {}).get("message") or err.get("detail")
        except Exception:
            msg = None
        check(f"参数校验：{name} 返回 4xx", ok, f"status={r.status_code}")
        check(f"参数校验：{name} 有可读信息", bool(str(msg or "").strip()), str(msg))

    # 6. 直接经纬度点
    body = {
        "points": [
            {"location": "116.378,39.865", "name": "北京南站"},
            {"location": "116.397,39.918", "name": "故宫"},
        ],
        "modes": ["walking"],
    }
    r = s.post(f"{BASE_URL}/api/plan", json=body, timeout=TIMEOUT)
    check("location 形式点位可用", r.status_code == 200, f"{r.status_code} {r.text[:160]}")

    # 7. 前端静态资源
    r = s.get(f"{BASE_URL}/", timeout=TIMEOUT)
    html = r.text
    check("GET / 返回 HTML", r.status_code == 200 and "<html" in html.lower())
    check("首页含三主题线索", any(k in html for k in ("明", "灰", "暗")) or "theme" in html.lower())
    assets = re.findall(r'(?:src|href)="([^"]+)"', html)
    local_assets = [
        a for a in assets if not a.startswith(("http://", "https://", "data:", "#", "mailto:"))
    ]
    if local_assets:
        for asset in local_assets[:10]:
            url = asset if asset.startswith("/") else f"{BASE_URL}/{asset}"
            ar = s.get(url, timeout=TIMEOUT)
            check(f"静态资源可访问 {asset}", ar.status_code == 200, f"status={ar.status_code}")
    else:
        check("首页引用了本地资源", False, "未找到 src/href 资源")

    # 8. 前端不得泄露 Key（静态文件粗查）
    for path in ("/app.js", "/styles.css"):
        rr = s.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        if rr.status_code == 200:
            check(f"{path} 不含 amap_key 字面量", "amap_key" not in rr.text.lower())

    print(f"\n共 {checks} 项检查，失败 {len(failures)} 项")
    if failures:
        print("失败清单：")
        for f in failures:
            print(" -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
