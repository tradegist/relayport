from enum import StrEnum


class ErrorCode(StrEnum):
    # Yahoo Finance errors
    YAHOO_UNAUTHORIZED = "YAHOO_UNAUTHORIZED"
    YAHOO_ERROR = "YAHOO_ERROR"
    # Generic server errors
    FETCH_FAILED = "FETCH_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # User errors
    UNAUTHORIZED = "UNAUTHORIZED"
    VALIDATION_ERROR = "VALIDATION_ERROR"


# HTTP status overrides — only codes that differ from their class default.
# AppError default: 500. UserError default: 400.
_STATUS_OVERRIDES: dict[ErrorCode, int] = {
    ErrorCode.YAHOO_UNAUTHORIZED: 503,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.VALIDATION_ERROR: 422,
}

_DEFAULT_APP_STATUS = 500
_DEFAULT_USER_STATUS = 400


class AppError(Exception):
    """Server-side fault — maps to 5xx. Never expose internal details to callers."""

    def __init__(
        self,
        message: str,
        code: ErrorCode,
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.cause = cause

    @property
    def status_code(self) -> int:
        return _STATUS_OVERRIDES.get(self.code, _DEFAULT_APP_STATUS)

    def __str__(self) -> str:
        return f"{self.args[0]} [{self.code}]"


class UserError(AppError):
    """Client-side fault — maps to 4xx. Message is safe to surface to callers."""

    @property
    def status_code(self) -> int:
        return _STATUS_OVERRIDES.get(self.code, _DEFAULT_USER_STATUS)


class YahooError(AppError):
    """Yahoo Finance error — distinct class so retry logic can target it specifically."""
