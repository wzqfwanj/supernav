"""FastAPI 应用：状态、规划接口与 web/ 静态托管。

启动时不依赖 web/ 目录存在（前端可能并行开发），静态文件在请求时动态查找，
因此即使先启动后端、后创建 web/ 也能正常工作。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, config, planner
from .errors import ApiError


class PlanRequest(BaseModel):
    """请求体校验保持宽松（内容校验在 planner 中完成，便于返回统一错误码）。"""

    model_config = ConfigDict(extra="ignore")

    points: list = Field(default_factory=list, description="2–10 个地点")
    modes: list = Field(default_factory=list, description="长度为 points-1 的交通方式")
    options: dict | None = Field(default=None, description="计价参数，全部可选")


def _web_file(relative: str) -> Path | None:
    """在 web/ 目录内安全查找文件（防目录穿越）；不存在返回 None。"""
    web_root = config.WEB_DIR
    try:
        root = web_root.resolve()
    except OSError:
        return None
    candidate = (root / relative).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        return None
    if candidate.is_file():
        return candidate
    return None


def _not_found(path: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "INVALID_INPUT", "message": f"未找到资源：/{path}", "hint": ""}},
        status_code=404,
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="SuperNav API",
        description="SuperNav 交通规划后端（高德 Web 服务 API / 演示模式）",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # ------------------------------------------------------------ 异常处理
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.to_dict(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        hints: list[str] = []
        for error in exc.errors()[:3]:
            location = ".".join(
                str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path")
            )
            message = str(error.get("msg") or "")
            hints.append(f"{location}: {message}" if location else message)
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "请求参数格式不正确",
                    "hint": "；".join(hints),
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 不记录异常详情，避免任何可能的 Key 泄露
        return JSONResponse(
            {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务器内部错误",
                    "hint": "请查看后端日志或稍后重试",
                }
            },
            status_code=500,
        )

    # ------------------------------------------------------------ 接口
    @app.get("/api/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    def status() -> dict:
        is_demo, reason = config.demo_state()
        return {
            "ok": True,
            "version": config.VERSION,
            "demo": is_demo,
            "demo_reason": reason,
            "defaults": dict(config.DEFAULTS),
            "modes": [dict(mode) for mode in config.MODES],
        }

    @app.post("/api/plan")
    def plan_endpoint(payload: PlanRequest) -> dict:
        return planner.plan(payload.model_dump())

    # ------------------------------------------------------------ 静态托管 web/
    @app.get("/", include_in_schema=False)
    def index():
        file_path = _web_file("index.html")
        if file_path is not None:
            return FileResponse(file_path)
        return JSONResponse(
            {
                "ok": True,
                "message": "后端已启动，但前端页面尚未就绪（web/index.html 不存在）。",
                "api": {"status": "/api/status", "plan": "/api/plan"},
            }
        )

    @app.get("/{path:path}", include_in_schema=False)
    def static_files(path: str):
        relative = path[4:] if path.startswith("web/") else path
        if not relative or relative.endswith("/"):
            relative = f"{relative}index.html"
        file_path = _web_file(relative)
        if file_path is None:
            return _not_found(path)
        return FileResponse(file_path)

    return app


app = create_app()
