"""统一业务异常与错误响应。

所有业务错误抛出 :class:`AppError`，由 FastAPI 异常处理器转成::

    {"error": {"code": "...", "message": "...", "detail": {...}}}
"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail or {}


def not_found(message: str = "资源不存在", **detail: Any) -> AppError:
    return AppError(404, "not_found", message, detail)


def conflict(message: str, **detail: Any) -> AppError:
    return AppError(409, "conflict", message, detail)


def unprocessable(message: str, **detail: Any) -> AppError:
    return AppError(422, "unprocessable_entity", message, detail)


def register_exception_handlers(app) -> None:  # noqa: ANN001
    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        body = {"error": {"code": exc.code, "message": exc.message}}
        if exc.detail:
            body["error"]["detail"] = exc.detail
        return JSONResponse(status_code=exc.status_code, content=body)
