class DrawingError(ValueError):
    """Safe, deliberately public error; never include upstream bodies or secrets."""

    def __init__(
        self,
        category: str,
        message: str,
        request_id: str | None = None,
        *,
        upstream_code: str | None = None,
        upstream_type: str | None = None,
        upstream_param: str | None = None,
        moderation_stage: str | None = None,
        http_status: int | None = None,
        response_body: bytes | None = None,
    ):
        self.http_status = http_status
        self._response_body = response_body
        self.category = category
        self.public_message = message
        self.request_id = request_id
        self.upstream_code = upstream_code
        self.upstream_type = upstream_type
        self.upstream_param = upstream_param
        self.moderation_stage = moderation_stage
        # These fields are allowlisted by the transport, never raw API text.
        details = "".join(
            f"；{name}={value}"
            for name, value in [
                ("upstream_code", upstream_code),
                ("upstream_type", upstream_type),
                ("upstream_param", upstream_param),
                ("moderation_stage", moderation_stage),
            ]
            if value is not None
        )
        suffix = f"；request_id={request_id}" if request_id else ""
        super().__init__(f"[{category}] {message}{details}{suffix}")


class ImageDispatchNotStarted(DrawingError):
    """An audited image budget check rejected before the transport claim."""

    def __init__(self, message):
        super().__init__('local_preflight_rejected', message)


class ToolInvokeError(RuntimeError):
    """Invocation failure propagated by Dify SDK 0.4.x as a failed tool run."""
