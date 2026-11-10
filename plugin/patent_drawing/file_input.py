"""Read bytes or trusted Dify file references without implicit SDK blob fetches."""

import base64
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
from gevent import Timeout, spawn

from .errors import DrawingError

UUID_PATH = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
DIFY_IMAGE_PATH = re.compile(
    rf"/files/(?:{UUID_PATH}/file-preview|tools/{UUID_PATH}\.(?:png|jpg|jpeg|webp))"
)
FILE_DEADLINE_SECONDS = 60
HTTP_CLEANUP_SECONDS = 0.1


def run_with_deadline(operation, seconds: float):
    """Bound the caller independently of cancellation-time transport cleanup."""
    deadline = time.monotonic() + seconds

    def execute():
        try:
            return True, operation(deadline)
        except BaseException as error:
            # Do not let gevent log raw transport exceptions before redaction.
            return False, error

    worker = spawn(execute)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DrawingError("response_timeout", "HTTP请求超过总时长限制；未自动重试")
        try:
            success, value = worker.get(timeout=remaining)
        except Timeout:
            raise DrawingError(
                "response_timeout", "HTTP请求超过总时长限制；未自动重试"
            ) from None
        if not success:
            raise value
        if time.monotonic() >= deadline:
            raise DrawingError("response_timeout", "HTTP请求超过总时长限制；未自动重试")
        return value
    finally:
        if not worker.ready():
            worker.kill(block=False)


def close_http_resources(response, client):
    """Best-effort, bounded cleanup cannot extend the caller's HTTP deadline."""

    def cleanup():
        for resource in (response, client):
            if resource is None:
                continue
            try:
                with Timeout(HTTP_CLEANUP_SECONDS, False):
                    resource.close()
            except Exception:
                # Cleanup must never echo raw transport failures or retry a POST.
                pass

    spawn(cleanup)


@contextmanager
def response_deadline(seconds: float, expires_at: float | None = None):
    """Cancel cooperative I/O in the Dify/gevent runtime, including EOF waits."""
    now = time.monotonic()
    deadline = (
        min(now + seconds, expires_at) if expires_at is not None else now + seconds
    )
    remaining = deadline - now
    if remaining <= 0:
        raise DrawingError("response_timeout", "HTTP请求超过总时长限制；未自动重试")
    with Timeout(
        remaining,
        DrawingError("response_timeout", "HTTP请求超过总时长限制；未自动重试"),
    ):
        yield deadline
        if time.monotonic() >= deadline:
            raise DrawingError("response_timeout", "HTTP请求超过总时长限制；未自动重试")


@dataclass(frozen=True)
class FileSettings:
    origin: str = ""
    internal_base: str = ""


def _origin(url: str):
    try:
        p = urlsplit(url)
        if (
            p.scheme not in {"http", "https"}
            or not p.hostname
            or p.username
            or p.password
            or p.fragment
        ):
            raise ValueError()
        return p, (
            p.scheme,
            p.hostname.lower(),
            p.port or (443 if p.scheme == "https" else 80),
        )
    except ValueError:
        raise DrawingError("file_origin", "文件来源地址不受信任或配置无效") from None


def trusted_url(url: str, settings: FileSettings) -> str:
    if not settings.origin:
        raise DrawingError("file_origin", "请在插件凭据中配置可信Dify文件origin")
    source, source_origin = _origin(url)
    configured, allowed = _origin(settings.origin)
    origins = {allowed}
    target = None
    if settings.internal_base:
        target, internal_origin = _origin(settings.internal_base)
        if target.path not in {"", "/"} or target.query:
            raise DrawingError("file_origin", "Dify内部抓取基址仅可含协议、主机和端口")
        origins.add(internal_origin)
    if configured.path not in {"", "/"} or configured.query or source_origin not in origins:
        raise DrawingError("file_origin", "文件URL不属于配置的Dify文件origin")
    if not DIFY_IMAGE_PATH.fullmatch(source.path):
        raise DrawingError("file_path", "仅支持Dify签名图片预览或工具图片文件路径")
    query = parse_qs(source.query, keep_blank_values=True)
    if (
        any(
            len(query.get(k, [])) != 1 or not query[k][0]
            for k in ("timestamp", "nonce", "sign")
        )
        or not query["timestamp"][0].isdigit()
    ):
        raise DrawingError("file_signature", "Dify文件预览URL缺少签名字段")
    if target is not None:
        return urlunsplit((target.scheme, target.netloc, source.path, source.query, ""))
    return url


def bounded_body(
    response: httpx.Response,
    maximum: int,
    max_seconds: float = 60,
    *,
    deadline: float | None = None,
) -> bytes:
    if response.headers.get("content-encoding", "identity").lower() not in {
        "",
        "identity",
    }:
        raise DrawingError("response_encoding", "不接受压缩传输响应")
    raw_length = response.headers.get("content-length")
    if raw_length and (not raw_length.isdecimal() or int(raw_length) > maximum):
        raise DrawingError("response_size", "响应大小无效或超过容量限制")
    result = bytearray()
    with response_deadline(max_seconds, deadline) as expires:
        for chunk in response.iter_bytes():
            if time.monotonic() >= expires:
                raise DrawingError(
                    "response_timeout", "响应读取超过总时长限制；未自动重试"
                )
            if len(result) + len(chunk) > maximum:
                raise DrawingError("response_size", "响应超过容量限制")
            result.extend(chunk)
    return bytes(result)


def read_file(
    value: object, max_bytes: int, settings: FileSettings, *, transport=None
) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        blob = bytes(value)
    else:
        # File.blob performs an unbounded, unvalidated GET. Never access it here.
        if not isinstance(value, dict) and callable(getattr(value, "model_dump", None)):
            value = value.model_dump()
        if isinstance(value, str):
            value = {"url": value}
        if not isinstance(value, dict):
            raise DrawingError("source_image_missing", "请提供Dify图片文件")
        raw = value.get("blob")
        if isinstance(raw, (bytes, bytearray)):
            blob = bytes(raw)
        else:
            url = value.get("url")
            if not isinstance(url, str):
                raise DrawingError("source_image_missing", "未能读取图片文件内容")
            if url.startswith("data:image/"):
                _, sep, encoded = url.partition(";base64,")
                if not sep or len(encoded) > 4 * ((max_bytes + 2) // 3):
                    raise DrawingError("source_image_size", "图片数据格式或容量无效")
                try:
                    blob = base64.b64decode(encoded, validate=True)
                except Exception:
                    raise DrawingError(
                        "source_image_decode", "图片数据无法解码"
                    ) from None
            else:
                url = trusted_url(url, settings)

                def fetch(deadline):
                    client = httpx.Client(
                        transport=transport,
                        timeout=httpx.Timeout(30, connect=10),
                        follow_redirects=False,
                        trust_env=False,
                    )
                    response = None
                    try:
                        request = client.build_request(
                            "GET",
                            url,
                            headers={
                                "Accept": "image/*",
                                "Accept-Encoding": "identity",
                            },
                        )
                        response = client.send(request, stream=True)
                        if response.status_code != 200:
                            raise DrawingError(
                                "source_image_fetch", "Dify文件获取失败或发生重定向"
                            )
                        return bounded_body(response, max_bytes, deadline=deadline)
                    finally:
                        close_http_resources(response, client)

                try:
                    blob = run_with_deadline(fetch, FILE_DEADLINE_SECONDS)
                except httpx.HTTPError:
                    raise DrawingError(
                        "source_image_fetch", "Dify文件获取失败"
                    ) from None
    if not blob or len(blob) > max_bytes:
        raise DrawingError("source_image_size", "图片文件为空或超过容量限制")
    return blob
