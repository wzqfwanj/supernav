#!/usr/bin/env python
"""SuperNav「5 种交通方式对比」——无头浏览器 DOM 交互验证（verify 角色）。

用 Playwright + 本机 Microsoft Edge / Google Chrome（无需下载浏览器）真实打开
``http://127.0.0.1:8014/`` 页面，模拟用户点击并读取 DOM，验证：

1. 默认「方式对比」模式：隐藏每段方式下拉、演示横幅可见；
2. 起点行「定位」按钮：授予 Playwright geolocation 后点击填入「我的位置」，提交 payload 为 `{location, name:"我的位置"}`，
   且定位后可手动改回地址（清除定位坐标）；无权限时错误提示路径可验证；
3. 提交后 5 张方式卡按 公交→驾车→打车→骑行→步行 排列，均显示 耗时/里程/费用；
4. 默认选中第一个可用方式，点击任一张卡切换详情（合计/逐段/路线随卡片变化，数值与 /api/compare 响应一致）；
5. 拦截 /api/compare 返回 available=false 的响应，点击不可用卡显示错误态、详情隐藏；
6. 切换到「组合行程」：每段方式下拉恢复，提交 /api/plan 正常出详情；
7. 表单错误展示、三主题切换 + localStorage 持久化。

只写 verify/**；不修改业务代码。用法::

    python verify/dom_check.py --auto-server          # 自行起停 8014
    python verify/dom_check.py                        # 复用已运行的服务
    $env:BASE_URL="http://127.0.0.1:8014"; python verify/dom_check.py

退出码 0 = 全部 PASS，1 = 有 FAIL / 运行异常。结果落盘 verify/artifacts/dom-check.json。
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# 复用 smoke.py 的服务管理与小工具（同目录）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke import ARTIFACT_DIR, PROJECT_ROOT, ServerHandle, is_number  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

COMPARE_ORDER = ["transit", "driving", "taxi", "bicycling", "walking"]
MODE_LABELS = {"walking": "步行", "bicycling": "骑行", "transit": "公交/地铁", "taxi": "打车", "driving": "驾车"}


# ---------------------------------------------------------------- 前端格式化复刻
def js_round(value: float) -> int:
    """JS Math.round（.5 向上）语义。"""
    return int(math.floor(float(value) + 0.5))


def fmt_money(value) -> str:
    if not is_number(value):
        return "—"
    return "¥" + f"{float(value):.2f}"


def fmt_distance(value) -> str:
    if not is_number(value) or float(value) < 0:
        return "—"
    number = float(value)
    if number < 1000:
        return f"{js_round(number)} 米"
    km = number / 1000.0
    return f"{km:.2f} 公里" if km < 10 else f"{km:.1f} 公里"


def fmt_duration(value) -> str:
    if not is_number(value) or float(value) < 0:
        return "—"
    seconds = js_round(float(value))
    if seconds < 60:
        return f"{seconds} 秒"
    total_minutes = js_round(seconds / 60.0)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0:
        return f"{hours} 小时 {minutes} 分钟" if minutes > 0 else f"{hours} 小时"
    return f"{minutes} 分钟"


# ---------------------------------------------------------------- 检查器
class DomChecker:
    def __init__(self) -> None:
        self.results: list[dict] = []
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        self.results.append({"name": name, "ok": ok, "detail": "" if ok else str(detail)[:1500]})
        if ok:
            self.passed += 1
            print(f"PASS  {name}")
        else:
            self.failed += 1
            print(f"FAIL  {name}  ::  {str(detail)[:600]}")
        return ok

    def eq(self, name: str, actual, expected) -> bool:
        return self.check(name, actual == expected, f"actual={actual!r} expected={expected!r}")


def find_browser() -> tuple[str | None, str]:
    for path in BROWSER_CANDIDATES:
        if Path(path).is_file():
            return path, Path(path).name
    return None, ""


def wait_app_ready(page) -> None:
    page.wait_for_selector("#point-input-0", timeout=20000)
    page.wait_for_function(
        "() => document.querySelectorAll('#points-list input[data-point-index]').length >= 2",
        timeout=20000,
    )


def fetch_compare_via_ui(page):
    with page.expect_response(lambda response: "/api/compare" in response.url and response.request.method == "POST") as info:
        page.click("#submit-btn")
    return info.value


def card_modes(page) -> list[str]:
    return page.eval_on_selector_all(
        "#compare-grid [data-compare-mode]",
        "elements => elements.map(element => element.getAttribute('data-compare-mode'))",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SuperNav 方式对比无头浏览器 DOM 验证")
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8014"))
    parser.add_argument("--auto-server", action="store_true", help="服务未运行时自动拉起 uvicorn 并在结束后关闭")
    parser.add_argument("--headful", action="store_true", help="有界面模式（调试用）")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    base_url = args.base_url.rstrip("/")
    print("=" * 78)
    print("SuperNav 方式对比 · 无头浏览器 DOM 验证（verify/dom_check.py）")
    print(f"时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    print(f"目标：{base_url}")
    print("=" * 78)

    ck = DomChecker()
    server = ServerHandle(base_url, auto=args.auto_server)
    samples: dict = {"base_url": base_url, "console_errors": [], "page_errors": []}
    artifact = ARTIFACT_DIR / "dom-check.json"
    exit_code = 1
    browser = None
    try:
        if sync_playwright is None:
            ck.check("env.playwright", False, "未安装 playwright（python -m pip install playwright）")
            return 1
        if not server.ensure():
            ck.check("env.server", False, f"无法连接 {base_url}")
            return 1
        executable, browser_name = find_browser()
        samples["browser"] = browser_name or "playwright-default"
        with sync_playwright() as playwright:
            launch_kwargs = {"headless": not args.headful, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            if executable:
                launch_kwargs["executable_path"] = executable
            try:
                browser = playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:  # noqa: BLE001
                ck.check("env.browser_launch", False, f"{type(exc).__name__}: {exc}")
                return 1
            ck.check("env.browser_launch", True, "")
            context = browser.new_context(
                viewport={"width": 1440, "height": 1100},
                geolocation={"latitude": 39.9, "longitude": 116.4},
                permissions=["geolocation"],
            )
            page = context.new_page()
            console_errors: list[str] = []
            page_errors: list[str] = []
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

            # ---------------------------------------------------------- 1. 加载与默认状态
            page.goto(base_url + "/", wait_until="load", timeout=30000)
            wait_app_ready(page)
            page.wait_for_function(
                "() => { const banner = document.querySelector('#demo-banner'); return banner && !banner.hidden; }",
                timeout=20000,
            )
            ck.eq("dom.title", page.title(), "SuperNav · 交通规划")
            ck.check("dom.demo_banner_visible", page.is_visible("#demo-banner"), "演示模式横幅应可见")
            ck.eq("dom.compare_mode_default", page.get_attribute('[data-plan-mode="compare"]', "aria-pressed"), "true")
            ck.eq("dom.combine_mode_default_off", page.get_attribute('[data-plan-mode="combine"]', "aria-pressed"), "false")
            ck.eq("dom.compare_hides_segment_selects", page.locator("#points-list select[data-mode-index]").count(), 0)
            ck.check(
                "dom.compare_hint",
                "一次对比 5 种方式" in page.inner_text("#mode-switch-hint"),
                page.inner_text("#mode-switch-hint"),
            )

            # ---------------------------------------------------------- 2. 起点定位（v0.3.0）
            ck.eq("dom.locate.start_button_count", page.locator('#points-list [data-locate-index="0"]').count(), 1)
            ck.eq("dom.locate.end_has_no_button", page.locator('#points-list [data-locate-index="1"]').count(), 0)
            ck.eq("dom.locate.button_text", page.inner_text('[data-locate-index="0"]').strip(), "定位")
            ck.check("dom.locate.secure_context", page.evaluate("() => window.isSecureContext") is True, "127.0.0.1 应为 secure context")
            page.click('[data-locate-index="0"]')
            page.wait_for_function("() => document.querySelector('#point-input-0').value === '我的位置'", timeout=15000)
            ck.eq("dom.locate.filled_value", page.input_value("#point-input-0"), "我的位置")
            ck.eq("dom.locate.button_text_after", page.inner_text('[data-locate-index="0"]').strip(), "重新定位")
            ck.check(
                "dom.locate.input_marked",
                page.eval_on_selector("#point-input-0", "element => element.classList.contains('is-located')"),
                "定位后输入框应有 is-located 样式",
            )
            located_title = page.get_attribute("#point-input-0", "title")
            ck.check("dom.locate.input_title", bool(located_title) and "116.400000" in located_title, located_title)
            samples["locate"] = {"value": "我的位置", "title": located_title, "button": "重新定位"}
            # 手动修改起点文本后应清除定位坐标/样式，避免「我的位置」与实际地址不一致
            page.fill("#point-input-0", "北京南站")
            page.wait_for_function(
                "() => document.querySelector('[data-locate-index=\"0\"]').textContent.trim() === '定位'",
                timeout=5000,
            )
            ck.check(
                "dom.locate.manual_edit_clears",
                page.eval_on_selector("#point-input-0", "element => !element.classList.contains('is-located')"),
                "手动输入后应移除定位样式",
            )
            # 再次定位，验证「定位起点 → 方式对比」端到端链路（真实请求体）
            page.click('[data-locate-index="0"]')
            page.wait_for_function("() => document.querySelector('#point-input-0').value === '我的位置'", timeout=15000)
            page.fill("#point-input-1", "故宫博物院")
            with page.expect_response(lambda response: "/api/compare" in response.url and response.request.method == "POST") as compare_info:
                page.click("#submit-btn")
            response = compare_info.value
            ck.eq("dom.compare_http_status", response.status, 200)
            request_payload = response.request.post_data_json or {}
            samples["compare_request"] = request_payload
            request_points = request_payload.get("points") or []
            start_point = request_points[0] if request_points else {}
            end_point = request_points[1] if len(request_points) > 1 else {}
            ck.eq("dom.locate.payload_location", start_point.get("location"), "116.400000,39.900000")
            ck.eq("dom.locate.payload_name", start_point.get("name"), "我的位置")
            ck.check("dom.locate.payload_no_start_address", "address" not in start_point, start_point)
            ck.eq("dom.locate.payload_end_address", end_point.get("address"), "故宫博物院")
            compare_data = response.json()
            samples["compare_response"] = compare_data
            page.wait_for_function(
                "() => document.querySelectorAll('#compare-grid [data-compare-mode]').length === 5",
                timeout=20000,
            )
            page.wait_for_function(
                "() => { const s = document.querySelector('#request-status'); return !s || s.hidden; }",
                timeout=20000,
            )
            ck.check("dom.compare_section_visible", page.is_visible("#compare-section"), "#compare-section 应可见")
            ck.eq("dom.compare_card_count", page.locator("#compare-grid [data-compare-mode]").count(), 5)
            order = card_modes(page)
            ck.eq("dom.compare_card_order", order, COMPARE_ORDER)
            names = page.eval_on_selector_all(
                "#compare-grid [data-compare-mode] .compare-name",
                "elements => elements.map(element => element.textContent)",
            )
            ck.eq("dom.compare_card_names", names, [MODE_LABELS[mode] for mode in COMPARE_ORDER])
            metrics = page.eval_on_selector_all(
                "#compare-grid [data-compare-mode] .compare-metric-label",
                "elements => elements.map(element => element.textContent)",
            )
            ck.eq("dom.compare_metrics_labels", metrics, ["耗时", "里程", "费用"] * 5)
            cards_have_values = page.evaluate(
                "() => Array.from(document.querySelectorAll('#compare-grid [data-compare-mode]')).every(card => {"
                "  if (card.classList.contains('is-unavailable')) return false;"
                "  const values = Array.from(card.querySelectorAll('.compare-metric-value')).map(el => el.textContent.trim());"
                "  return values.length === 3 && values.every(text => text && text !== '—');"
                "})"
            )
            ck.check("dom.compare_cards_have_values", cards_have_values, "可用方式卡应显示耗时/里程/费用数值")
            ck.check("dom.demo_source_badge", page.inner_text("#result-source").strip() == "演示数据", page.inner_text("#result-source"))
            page.screenshot(path=str(ARTIFACT_DIR / "dom-compare.png"), full_page=True)

            # ---------------------------------------------------------- 3. 点击切换详情
            expected_by_mode = {}
            for entry in compare_data.get("modes", []):
                totals = entry.get("totals") or {}
                expected_by_mode[entry.get("mode")] = {
                    "mode_label": MODE_LABELS.get(entry.get("mode"), entry.get("mode_label")),
                    "distance": fmt_distance(totals.get("distance_m")),
                    "duration": fmt_duration(totals.get("duration_s")),
                    "cost": fmt_money((totals.get("cost") or {}).get("total")),
                    "legs": len(entry.get("legs") or []),
                }
            detail_by_mode = {}
            for mode in COMPARE_ORDER:
                page.click(f'[data-compare-mode="{mode}"]')
                page.wait_for_function(
                    "mode => { const detail = document.querySelector('#detail-mode');"
                    " return detail && !detail.hidden && detail.getAttribute('data-mode') === mode; }",
                    arg=mode,
                    timeout=10000,
                )
                detail_label = page.inner_text("#detail-mode").strip()
                totals_text = {
                    "distance": page.inner_text("#totals-distance").strip(),
                    "duration": page.inner_text("#totals-duration").strip(),
                    "cost": page.inner_text("#totals-cost").strip(),
                }
                expected = expected_by_mode[mode]
                checked = {
                    "label": detail_label,
                    "expected_label": expected["mode_label"],
                    "distance": totals_text["distance"],
                    "expected_distance": expected["distance"],
                    "duration": totals_text["duration"],
                    "expected_duration": expected["duration"],
                    "cost": totals_text["cost"],
                    "expected_cost": expected["cost"],
                    "legs": page.locator("#legs-list .leg-card").count(),
                    "expected_legs": expected["legs"],
                    "selected": page.get_attribute(f'[data-compare-mode="{mode}"]', "aria-pressed"),
                }
                detail_by_mode[mode] = checked
                ck.eq(f"dom.detail.{mode}.label", checked["label"], checked["expected_label"])
                ck.eq(f"dom.detail.{mode}.distance", checked["distance"], checked["expected_distance"])
                ck.eq(f"dom.detail.{mode}.duration", checked["duration"], checked["expected_duration"])
                ck.eq(f"dom.detail.{mode}.cost", checked["cost"], checked["expected_cost"])
                ck.eq(f"dom.detail.{mode}.legs", checked["legs"], checked["expected_legs"])
                ck.eq(f"dom.detail.{mode}.selected", checked["selected"], "true")
                ck.eq(f"dom.detail.{mode}.route_legend", page.locator("#route-legend li").count(), expected["legs"])
                ck.check(f"dom.detail.{mode}.route_visible", page.is_visible("#route-section"), "路线示意应可见")
                if mode == "transit":
                    ck.check(
                        "dom.detail.transit.summary_shown",
                        page.is_visible("#detail-summary") and page.inner_text("#detail-summary").strip() != "",
                        page.inner_text("#detail-summary"),
                    )
            samples["detail_by_mode"] = detail_by_mode
            selected_cards = page.eval_on_selector_all(
                "#compare-grid [data-compare-mode].is-selected",
                "elements => elements.map(element => element.getAttribute('data-compare-mode'))",
            )
            ck.eq("dom.single_selected_card", len(selected_cards), 1)

            # ---------------------------------------------------------- 3b. 定位失败路径（权限拒绝 code=1）
            page.evaluate(
                "() => { navigator.geolocation.getCurrentPosition = function (success, error) {"
                " if (error) error({ code: 1, message: 'User denied Geolocation' }); }; }"
            )
            page.click('[data-locate-index="0"]')
            page.wait_for_function("() => { const box = document.querySelector('#alert'); return box && !box.hidden; }", timeout=10000)
            denied_message = page.inner_text("#alert-message")
            denied_hint = page.inner_text("#alert-hint")
            ck.check("dom.locate.denied_alert", "定位权限被拒绝" in denied_message, denied_message)
            ck.check("dom.locate.denied_hint", "允许本站定位" in denied_hint, denied_hint)
            ck.eq("dom.locate.denied_button_restored", page.inner_text('[data-locate-index="0"]').strip(), "重新定位")
            samples["locate_denied"] = {"message": denied_message, "hint": denied_hint}
            page.click("#alert-dismiss")

            # ---------------------------------------------------------- 4. available=false 错误态（拦截响应）
            unavailable = copy.deepcopy(compare_data)
            for entry in unavailable.get("modes", []):
                if entry.get("mode") == "transit":
                    entry["available"] = False
                    entry["error"] = {
                        "code": "AMAP_ERROR",
                        "message": "高德未返回可用路线，请更换交通方式或地点后重试",
                        "hint": "可以换一种方式或地点",
                    }
                    entry["legs"] = []
                    entry["totals"] = {"distance_m": 0, "duration_s": 0, "cost": {"total": 0.0, "breakdown": []}}
                    entry["summary"] = ""

            def fulfil_compare(route):
                route.fulfill(status=200, content_type="application/json", body=json.dumps(unavailable, ensure_ascii=False))

            page.route("**/api/compare", fulfil_compare)
            try:
                mocked = fetch_compare_via_ui(page)
                ck.eq("dom.unavailable.mock_status", mocked.status, 200)
                page.wait_for_function(
                    "() => document.querySelectorAll('#compare-grid [data-compare-mode]').length === 5"
                    " && document.querySelector('[data-compare-mode=\"transit\"]')"
                    " && document.querySelector('[data-compare-mode=\"transit\"]').classList.contains('is-unavailable')",
                    timeout=20000,
                )
                ck.check(
                    "dom.unavailable.card_badge",
                    "无可用路线" in page.inner_text('[data-compare-mode="transit"]'),
                    page.inner_text('[data-compare-mode="transit"]'),
                )
                ck.check("dom.unavailable.initial_detail_fallback", page.is_visible("#detail"), "默认应选中第一个可用方式并展示详情")
                card_state = page.evaluate(
                    "() => { const card = document.querySelector('[data-compare-mode=\"transit\"]');"
                    " return { aria_disabled: card.hasAttribute('aria-disabled'), native_disabled: card.disabled }; }"
                )
                samples["unavailable_card_state"] = card_state
                # L-compare-1 已修复：不可用卡不再带 aria-disabled，可被鼠标/键盘/读屏/自动化正常操作
                ck.eq("dom.unavailable.aria_disabled_absent", card_state.get("aria_disabled"), False)
                ck.eq("dom.unavailable.native_button_enabled", card_state.get("native_disabled"), False)
                page.click('[data-compare-mode="transit"]')
                page.wait_for_function(
                    "() => { const box = document.querySelector('#mode-error'); return box && !box.hidden; }",
                    timeout=10000,
                )
                ck.check("dom.unavailable.error_visible", page.is_visible("#mode-error"), "#mode-error 应可见")
                ck.eq("dom.unavailable.detail_hidden", page.is_visible("#detail"), False)
                error_text = page.inner_text("#mode-error")
                ck.check("dom.unavailable.error_mentions_mode", "公交/地铁" in error_text, error_text)
                ck.check("dom.unavailable.error_mentions_reason", "未返回可用路线" in error_text, error_text)
                ck.check("dom.unavailable.error_code_hint", "AMAP_ERROR" in error_text, error_text)
                page.click('[data-compare-mode="driving"]')
                page.wait_for_function(
                    "() => { const box = document.querySelector('#mode-error'); return box && box.hidden; }",
                    timeout=10000,
                )
                ck.check("dom.unavailable.recover_detail", page.is_visible("#detail"), "切回可用方式应恢复详情")
                ck.eq("dom.unavailable.recover_label", page.inner_text("#detail-mode").strip(), "驾车")
                samples["unavailable_state"] = {"error_text": error_text, "recovered_label": page.inner_text("#detail-mode").strip()}
            finally:
                page.unroute("**/api/compare", fulfil_compare)

            # ---------------------------------------------------------- 5. 组合行程模式
            page.click('[data-plan-mode="combine"]')
            page.wait_for_function("() => document.querySelector('#result').hidden === true", timeout=10000)
            ck.eq("dom.combine.result_hidden_after_switch", page.is_visible("#result"), False)
            ck.eq("dom.combine.segment_select_count", page.locator("#points-list select[data-mode-index]").count(), 1)
            page.select_option("#points-list select[data-mode-index='0']", "taxi")
            with page.expect_response(lambda response: "/api/plan" in response.url and response.request.method == "POST") as plan_info:
                page.click("#submit-btn")
            plan_response = plan_info.value
            ck.eq("dom.combine.plan_http_status", plan_response.status, 200)
            plan_data = plan_response.json()
            samples["plan_response"] = plan_data
            page.wait_for_function(
                "() => document.querySelectorAll('#legs-list .leg-card').length === 1",
                timeout=20000,
            )
            ck.check("dom.combine.compare_section_hidden", not page.is_visible("#compare-section"), "#compare-section 应隐藏")
            ck.eq("dom.combine.detail_mode_header_hidden", page.is_visible("#detail-mode"), False)
            plan_totals = plan_data.get("totals") or {}
            ck.eq("dom.combine.totals_distance", page.inner_text("#totals-distance").strip(), fmt_distance(plan_totals.get("distance_m")))
            ck.eq("dom.combine.totals_duration", page.inner_text("#totals-duration").strip(), fmt_duration(plan_totals.get("duration_s")))
            ck.eq(
                "dom.combine.totals_cost",
                page.inner_text("#totals-cost").strip(),
                fmt_money((plan_totals.get("cost") or {}).get("total")),
            )
            ck.check("dom.combine.legs_count", "共 1 段" in page.inner_text("#legs-count"), page.inner_text("#legs-count"))
            ck.eq("dom.combine.route_legend", page.locator("#route-legend li").count(), 1)
            combo_text = page.inner_text("#totals-combo")
            ck.check("dom.combine.combo_chips", "组合：" in combo_text and "打车" in combo_text, combo_text)
            samples["combine_mode"] = {
                "totals_distance": page.inner_text("#totals-distance").strip(),
                "totals_duration": page.inner_text("#totals-duration").strip(),
                "totals_cost": page.inner_text("#totals-cost").strip(),
                "combo": combo_text,
            }

            # ---------------------------------------------------------- 6. 表单错误展示
            page.click('[data-plan-mode="compare"]')
            page.wait_for_function("() => { const s = document.querySelector('#point-input-0'); return !!s; }", timeout=10000)
            page.fill("#point-input-0", "")
            page.click("#submit-btn")
            page.wait_for_function(
                "() => { const box = document.querySelector('#alert'); return box && !box.hidden; }",
                timeout=10000,
            )
            ck.check("dom.validation.alert_visible", page.is_visible("#alert"), "#alert 应可见")
            alert_message = page.inner_text("#alert-message")
            ck.check("dom.validation.alert_message", "请填写起点" in alert_message, alert_message)
            samples["validation_alert"] = alert_message
            page.fill("#point-input-0", "北京南站")
            page.click("#alert-dismiss")
            ck.eq("dom.validation.alert_dismissed", page.is_visible("#alert"), False)

            # ---------------------------------------------------------- 7. 主题切换 + localStorage
            page.click('[data-theme-choice="dark"]')
            page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'dark'", timeout=5000)
            stored = page.evaluate("() => window.localStorage.getItem('supernav-theme')")
            ck.eq("dom.theme.dark_applied", page.get_attribute("html", "data-theme"), "dark")
            ck.eq("dom.theme.dark_stored", stored, "dark")
            page.reload(wait_until="load")
            wait_app_ready(page)
            ck.eq("dom.theme.persisted_after_reload", page.get_attribute("html", "data-theme"), "dark")
            ck.eq("dom.theme.persisted_button", page.get_attribute('[data-theme-choice="dark"]', "aria-pressed"), "true")
            page.click('[data-theme-choice="light"]')
            page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'light'", timeout=5000)
            ck.eq("dom.theme.light_restored", page.evaluate("() => window.localStorage.getItem('supernav-theme')"), "light")
            samples["theme"] = {"persisted_after_reload": "dark"}

            # ---------------------------------------------------------- 8. 控制台错误
            samples["console_errors"] = console_errors
            samples["page_errors"] = page_errors
            ck.check("dom.console.no_errors", not console_errors, f"console errors={console_errors}")
            ck.check("dom.page.no_js_errors", not page_errors, f"page errors={page_errors}")
            context.close()
            exit_code = 0 if ck.failed == 0 else 1
    except Exception as exc:  # noqa: BLE001
        import traceback

        ck.check("harness.unexpected_exception", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "base_url": base_url,
            "browser": samples.get("browser"),
            "stats": {"passed": ck.passed, "failed": ck.failed},
            "results": ck.results,
            "samples": samples,
        }
        try:
            artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n[artifact] 结果已写入：{artifact}")
        except Exception as exc:  # noqa: BLE001
            print(f"[artifact] 写入失败：{exc}")
        server.stop()

    print("\n" + "=" * 78)
    print(f"统计：PASS={ck.passed}  FAIL={ck.failed}")
    print("结论：" + ("PASS（DOM 交互全部通过）" if ck.failed == 0 else "FAIL（详见上方 FAIL 行与 verify/artifacts/dom-check.json）"))
    print("=" * 78)
    return exit_code if ck.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
