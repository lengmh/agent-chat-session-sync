class PlatformAPIError(RuntimeError):
    """An API operation failed and may carry a platform-specific error code."""

    def __init__(self, code: int | None, message: str, http_status: int | None = None):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(f"platform API {code}: {message}")
