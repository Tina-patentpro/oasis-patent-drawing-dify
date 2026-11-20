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


class PlannerResultUnknown(DrawingError):
    def __init__(self, operation_id, model_calls, request_id=None):
        super().__init__('result_unknown', '规划结果待核对。可按本次操作标识检查原始回执，系统不会重复发送。', request_id)
        self.operation_id = operation_id
        self.model_calls = model_calls


class SourceCoverageIncomplete(ValueError):
    def __init__(self, findings, message):
        self.findings = findings
        super().__init__(message)


class ImageDispatchNotStarted(DrawingError):
    """An audited image preflight rejected before the transport claim."""

    def __init__(self, message):
        super().__init__('local_preflight_rejected', message)


class SourceLineageMismatch(ImageDispatchNotStarted):
    def __init__(self):
        super().__init__('原始来源与已登记的任务谱系不一致，未发送图像请求。请由运营方核对原始记录。')


class DuplicateImageRequest(ImageDispatchNotStarted):
    reason = 'duplicate_image_request'

    def __init__(self):
        super().__init__('本批次已有相同图像请求，本次未发送图像请求。请核对原请求及已保存的图件。')


class ToolInvokeError(RuntimeError):
    """Invocation failure propagated by Dify SDK 0.4.x as a failed tool run."""
