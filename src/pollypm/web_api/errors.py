"""Error response helpers.

Every 4xx / 5xx response body in the Web API conforms to the
`docs/web-api-spec.md` §6 shape:

    {"error": {"code": "snake_case", "message": "...", "hint": "..."}}

Plus an optional ``details[]`` array for ``validation_error``.

This module owns the canonical exception class
(:class:`APIError`) and the FastAPI exception handler that renders
the body. Endpoint code raises ``APIError(...)``; the handler
formats the JSON response so every 4xx/5xx path uses one shape.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class APIError(Exception):
    """Raised by endpoint code to short-circuit with a typed error response.

    Carries the spec's ``(code, message, hint?)`` triple plus the HTTP
    status code. ``details`` is optional and only surfaces on
    ``validation_error`` (per §6).
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        hint: str | None = None,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
            }
        }
        if self.hint is not None:
            body["error"]["hint"] = self.hint
        if self.details is not None:
            body["details"] = self.details
        return body


def error_response(error: APIError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.to_body())


# Convenience constructors so endpoint code reads naturally:
#
#     raise not_found("Project not registered", hint="...")
#
# rather than threading the status code through every callsite.
def not_found(message: str, *, hint: str | None = None) -> APIError:
    return APIError(status_code=404, code="not_found", message=message, hint=hint)


def invalid_request(message: str, *, hint: str | None = None) -> APIError:
    return APIError(status_code=400, code="invalid_request", message=message, hint=hint)


def invalid_state(message: str, *, hint: str | None = None) -> APIError:
    return APIError(status_code=400, code="invalid_state", message=message, hint=hint)


def unauthorized(message: str = "Bearer token required") -> APIError:
    return APIError(status_code=401, code="unauthorized", message=message)


def invalid_token(message: str = "Bearer token rejected") -> APIError:
    return APIError(status_code=401, code="invalid_token", message=message)


def conflict(message: str, *, hint: str | None = None) -> APIError:
    return APIError(status_code=409, code="conflict", message=message, hint=hint)


def validation_error(
    message: str,
    *,
    details: list[dict[str, str]] | None = None,
    hint: str | None = None,
) -> APIError:
    return APIError(
        status_code=422,
        code="validation_error",
        message=message,
        hint=hint,
        details=details,
    )


def internal_error(message: str = "Unhandled server exception") -> APIError:
    return APIError(status_code=500, code="internal_error", message=message)


# ---------------------------------------------------------------------------
# Handlers wired into the FastAPI app
# ---------------------------------------------------------------------------


async def handle_api_error(_request: Request, exc: APIError) -> JSONResponse:
    return error_response(exc)


async def handle_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Reshape FastAPI validation failures into the spec's shape.

    FastAPI's default 422 body is ``{"detail": [...]}``. The spec
    requires ``{"error": {...}, "details": [...]}`` with ``field`` /
    ``message`` keys per row. Map FastAPI's ``loc`` chain to the
    final field name (last entry) so the row reads naturally.
    """
    details: list[dict[str, str]] = []
    for err in exc.errors():
        loc = err.get("loc") or ()
        field = ".".join(str(part) for part in loc if part not in {"body", "query", "path", "header"})
        if not field:
            field = ".".join(str(part) for part in loc)
        details.append({
            "field": field,
            "message": str(err.get("msg") or "invalid value"),
        })
    api_err = APIError(
        status_code=422,
        code="validation_error",
        message="Request body failed validation",
        details=details,
    )
    return error_response(api_err)


async def handle_unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
    """Last-resort catch — never let a stack trace leak."""
    api_err = APIError(
        status_code=500,
        code="internal_error",
        message=f"Unhandled server exception: {exc.__class__.__name__}",
    )
    return error_response(api_err)


__all__ = [
    "APIError",
    "conflict",
    "error_response",
    "handle_api_error",
    "handle_unhandled_exception",
    "handle_validation_error",
    "internal_error",
    "invalid_request",
    "invalid_state",
    "invalid_token",
    "not_found",
    "unauthorized",
    "validation_error",
]
