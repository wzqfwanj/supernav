"""运行配置：版本、交通方式、默认计价参数、高德 Key 与演示模式判定。

Key 只从环境变量 ``AMAP_KEY`` 或项目根目录 ``config.local.json`` 读取，
永远不会写入响应、日志或代码默认值。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import __version__
from .errors import ApiError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.local.json"
WEB_DIR = PROJECT_ROOT / "web"

VERSION = __version__

#: 交通方式（顺序与契约一致）
MODES: list[dict[str, str]] = [
    {"value": "walking", "label": "步行"},
    {"value": "bicycling", "label": "骑行"},
    {"value": "transit", "label": "公交/地铁"},
    {"value": "taxi", "label": "打车"},
    {"value": "driving", "label": "驾车"},
]
MODE_LABELS: dict[str, str] = {item["value"]: item["label"] for item in MODES}
VALID_MODES: tuple[str, ...] = tuple(MODE_LABELS)

#: 契约 3.1 defaults
DEFAULTS: dict[str, float | int] = {
    "fuel_price": 7.8,
    "fuel_consumption": 8.0,
    "transit_strategy": 0,
    "taxi_base_fare": 13.0,
    "taxi_base_km": 3.0,
    "taxi_per_km": 2.3,
}

_TRUTHY = {"1", "true", "yes", "on", "y"}
_FALSY = {"0", "false", "no", "off", "auto", "n", ""}

#: options 中各数值字段的合理上界，防止荒谬输入
_OPTION_LIMITS = {
    "fuel_price": 1000.0,
    "fuel_consumption": 1000.0,
    "taxi_base_fare": 1000.0,
    "taxi_base_km": 1000.0,
    "taxi_per_km": 1000.0,
}


def read_local_config() -> dict[str, Any]:
    """读取 config.local.json（可选，不存在/损坏时视为空配置）。"""
    try:
        raw = CONFIG_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_amap_key() -> str:
    """环境变量优先，其次 config.local.json；未配置返回空字符串。"""
    env_key = os.environ.get("AMAP_KEY", "").strip()
    if env_key:
        return env_key
    value = read_local_config().get("amap_key")
    return value.strip() if isinstance(value, str) else ""


def _demo_flag(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUTHY:
            return True
        if text in _FALSY:
            return False
    return None


def demo_state() -> tuple[bool, str]:
    """返回 ``(是否演示模式, 演示原因)``。"""
    forced = _demo_flag(os.environ.get("DEMO_MODE"))
    if forced is None:
        forced = _demo_flag(read_local_config().get("demo_mode"))
    if forced:
        return True, "DEMO_MODE 已开启，使用演示数据"
    if not get_amap_key():
        return True, "未配置 AMAP_KEY，使用演示数据"
    return False, ""


def resolve_options(overrides: Any = None) -> dict[str, Any]:
    """把请求里的 options 与默认值合并并校验，返回完整配置。"""
    options: dict[str, Any] = dict(DEFAULTS)
    if overrides is None:
        return options
    if not isinstance(overrides, dict):
        raise ApiError("INVALID_INPUT", "options 必须是对象", '例如 {"fuel_price": 7.8}')
    for key in DEFAULTS:
        raw = overrides.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool):
            raise ApiError("INVALID_INPUT", f"options.{key} 必须是数字", "")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ApiError("INVALID_INPUT", f"options.{key} 必须是数字", "")
        if key == "transit_strategy":
            if int(value) != value or not (0 <= value <= 8):
                raise ApiError(
                    "INVALID_INPUT",
                    "options.transit_strategy 必须是 0–8 的整数",
                    "高德公交换乘策略，默认 0（推荐）",
                )
            options[key] = int(value)
            continue
        limit = _OPTION_LIMITS.get(key, 1000.0)
        if not (0 <= value <= limit):
            raise ApiError("INVALID_INPUT", f"options.{key} 必须在 0–{limit:g} 之间", "")
        options[key] = value
    return options
