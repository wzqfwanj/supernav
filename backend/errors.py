"""统一的业务错误类型。

契约要求非 2xx 响应体形如::

    {"error": {"code": "INVALID_INPUT", "message": "...", "hint": ""}}

``code`` 取值：INVALID_INPUT / GEOCODE_FAILED / AMAP_ERROR / KEY_MISSING / INTERNAL_ERROR。
"""

from __future__ import annotations

from typing import Any

_DEFAULT_STATUS = {
    "INVALID_INPUT": 400,
    "GEOCODE_FAILED": 422,
    "AMAP_ERROR": 502,
    "KEY_MISSING": 400,
    "INTERNAL_ERROR": 500,
}


class ApiError(Exception):
    """可安全返回给前端的业务错误（message 中不含高德 Key）。"""

    def __init__(
        self,
        code: str,
        message: str,
        hint: str = "",
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.hint = hint or ""
        self.status_code = int(status_code or _DEFAULT_STATUS.get(code, 400))
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "hint": self.hint,
            }
        }
