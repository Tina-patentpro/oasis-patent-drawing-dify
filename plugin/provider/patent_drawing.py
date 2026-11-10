from urllib.parse import urlsplit

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError


class PatentDrawingProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict) -> None:
        key = credentials.get("openai_api_key")
        if not isinstance(key, str) or not key.strip() or any(c.isspace() for c in key):
            raise ToolProviderCredentialValidationError("请填写OpenAI官方API密钥")
        for field in ("dify_file_origin", "dify_internal_files_url"):
            value = credentials.get(field)
            if not value:
                continue
            try:
                parsed = urlsplit(value)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.path not in {"", "/"}
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError()
                parsed.port
            except (ValueError, TypeError, AttributeError):
                raise ToolProviderCredentialValidationError(
                    "Dify文件地址仅可含http(s)协议、主机和端口"
                ) from None
        if credentials.get("dify_internal_files_url") and not credentials.get(
            "dify_file_origin"
        ):
            raise ToolProviderCredentialValidationError(
                "内部抓取基址须配合可信Dify文件origin"
            )
