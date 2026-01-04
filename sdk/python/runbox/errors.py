"""Exceptions.

One base class so `except RunboxError` catches everything, and specific
subclasses so a caller who wants to retry a rate limit but not a bad request
can say so without inspecting status codes.
"""

from __future__ import annotations


class RunboxError(Exception):
    """Base for every error this library raises."""


class APIError(RunboxError):
    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(f"[{status} {code}] {message}")
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}


class AuthenticationError(APIError):
    """401. The key is wrong, revoked, or missing."""


class NotFoundError(APIError):
    """404. Also raised for a run belonging to another tenant — the API does
    not distinguish, and neither should this."""


class RateLimitError(APIError):
    """429. Carries retry_after when the server supplied one."""

    def __init__(self, status: int, code: str, message: str, detail: dict | None = None,
                 retry_after: float | None = None):
        super().__init__(status, code, message, detail)
        self.retry_after = retry_after


class InvalidRequestError(APIError):
    """400 or 422. Retrying without changing the request will not help."""


class ServerError(APIError):
    """5xx. Worth retrying."""


class ConnectionError(RunboxError):  # noqa: A001 — shadows a builtin deliberately
    """Could not reach the API at all."""


class StreamError(RunboxError):
    """The event stream broke and could not be resumed."""


class TimeoutError(RunboxError):  # noqa: A001 — shadows a builtin deliberately
    """A client-side wait expired. The run itself may still be going."""


def from_response(status: int, body: dict, retry_after: float | None = None) -> APIError:
    """Map an error response onto the right exception class."""
    # The control plane wraps errors in `detail` when raised via HTTPException,
    # and returns them bare from the validation handler. Accept both.
    payload = body.get("detail") if isinstance(body.get("detail"), dict) else body
    code = payload.get("error", "unknown_error")
    message = payload.get("message") or payload.get("detail") or "Request failed."
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else None

    if status == 401:
        return AuthenticationError(status, code, message, detail)
    if status == 404:
        return NotFoundError(status, code, message, detail)
    if status == 429:
        return RateLimitError(status, code, message, detail, retry_after)
    if status in (400, 422):
        return InvalidRequestError(status, code, message, detail)
    if status >= 500:
        return ServerError(status, code, message, detail)
    return APIError(status, code, message, detail)
