class HttpError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.cause = cause


class YahooError(HttpError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, status_code, cause)
        self.error_code = error_code
