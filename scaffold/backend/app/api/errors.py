"""One error envelope, everywhere.

Every failure the client can see has the same shape and carries the request_id, so a
user-visible error is traceable to a log line without guessing.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, request_id_ctx

log = get_logger(__name__)


class AppError(Exception):
    """Domain-level error carrying an HTTP status and a stable machine code."""

    def __init__(self, code: str, message: str, http_status: int = 400, details=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.http_status, self.details = http_status, details or []


class NotFound(AppError):
    def __init__(self, what: str):
        super().__init__("NOT_FOUND", f"{what} not found", status.HTTP_404_NOT_FOUND)


class Conflict(AppError):
    def __init__(self, message: str, details=None):
        super().__init__("CONFLICT", message, status.HTTP_409_CONFLICT, details)


def _envelope(code: str, message: str, details=None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
            "request_id": request_id_ctx.get(),
        }
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_r: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, exc: RequestValidationError):
        # Flatten to field-level messages the frontend can attach to inputs directly.
        details = [
            {
                "field": ".".join(str(p) for p in e["loc"] if p not in ("body", "query")),
                "message": e["msg"],
            }
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope("VALIDATION_FAILED", "Request validation failed", details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_r: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("HTTP_ERROR", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_r: Request, exc: Exception):
        # Log the detail, return an opaque message — internals are not the client's business.
        log.exception("request.unhandled_error", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("INTERNAL_ERROR", "An unexpected error occurred"),
        )
