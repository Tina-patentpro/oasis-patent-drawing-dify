"""Official Image API transport. A paid POST is attempted at most once."""

import json
import re
from dataclasses import dataclass

import httpx

from .errors import DrawingError
from .file_input import bounded_body, close_http_resources, run_with_deadline
from .images import PreparedImage, prepare_output
from .prompts import DrawingRequest

MODEL = "gpt-image-2.6-flare"
API = "https://api.openai.com/v1/images/"
MAX_RESPONSE = 24 * 1024 * 1024
REQUEST_DEADLINE_SECONDS = 300


@dataclass(frozen=True)
class RenderResult:
    image: PreparedImage
    request_id: str | None
    usage: dict | None
    svg_usage: dict | None
    raw_response: bytes | None = None
    request_json: dict | None = None
    response_headers: dict | None = None
    usage_projection: dict | None = None


def safe_request_id(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"req_[A-Za-z0-9_-]{1,160}", value)
        else None
    )


def project_usage(value):
    """Typed projection; unknown fields never erase other valid token counts."""
    numeric={'input_tokens','output_tokens','total_tokens','text_tokens','image_tokens',
             'cached_tokens','cache_write_tokens','reasoning_tokens','audio_tokens'}
    nested={'input_tokens_details','output_tokens_details','cached_tokens_details'}
    omitted=0
    def project(obj,depth=0):
        nonlocal omitted
        if not isinstance(obj,dict)or depth>3:
            omitted+=1;return {}
        result={}
        for key,item in obj.items():
            if key in numeric and type(item)is int and 0<=item<=10**15:result[key]=item
            elif key in nested and isinstance(item,dict):
                child=project(item,depth+1)
                if child:result[key]=child
            elif key=='model'and isinstance(item,str)and re.fullmatch(r'gpt-[A-Za-z0-9_.-]{1,100}',item):result[key]=item
            else:omitted+=1
        return result
    summary=project(value)if isinstance(value,dict)and value else {}
    return {'summary':summary or None,'complete':bool(summary)and omitted==0,'omitted_count':omitted}


def safe_usage(value):
    return project_usage(value)['summary']


def _status_error(status: int, body: bytes, request_id: str | None):
    code = error_type = param = moderation_stage = None
    try:
        error = json.loads(body).get("error", {})
        if isinstance(error, dict):
            codes = {
                "invalid_value",
                "invalid_type",
                "invalid_request_error",
                "missing_required_parameter",
                "unsupported_parameter",
                "unsupported_value",
                "model_not_found",
                "content_policy_violation",
                "moderation_blocked",
                "image_generation_user_error",
                "invalid_api_key",
                "insufficient_quota",
                "rate_limit_exceeded",
                "permission_denied",
                "server_error",
            }
            types = {
                "image_generation_user_error",
                "invalid_request_error",
                "authentication_error",
                "permission_error",
                "rate_limit_error",
                "server_error",
            }
            value = error.get("code")
            code = value if isinstance(value, str) and value in codes else None
            value = error.get("type")
            error_type = value if isinstance(value, str) and value in types else None
            value = error.get("param")
            if isinstance(value, str) and (
                value
                in {
                    "images",
                    "image",
                    "image[]",
                    "mask",
                    "background",
                    "prompt",
                    "size",
                    "quality",
                    "complexity",
                    "model",
                    "output_format",
                    "n",
                }
                or re.fullmatch(
                    r"images\[(?:[0-9]|1[0-5])\]\.(?:image_url|file_id)", value
                )
            ):
                param = value
            details = error.get("moderation_details")
            if isinstance(details, dict):
                value = details.get("moderation_stage")
                moderation_stage = (
                    value
                    if isinstance(value, str)
                    and value in {"input", "output", "unknown"}
                    else None
                )
    except (ValueError, AttributeError, RecursionError):
        pass
    if status >= 500 or status == 408:
        category, message = (
            "result_unknown",
            "未取得最终结果；执行与费用状态未知，请先核对官方用量，不自动重试",
        )
    elif status == 401:
        category, message = "authorization_error", "OpenAI密钥无效或已失效"
    elif status == 403:
        category, message = "access_denied", "当前项目无权访问该模型或功能"
    elif status == 429:
        category, message = (
            ("quota_exceeded", "OpenAI项目额度不足")
            if code == "insufficient_quota"
            else ("rate_limited", "OpenAI请求被限流，请稍后手动处理")
        )
    elif code in {
        "content_policy_violation",
        "moderation_blocked",
    }:
        category = "content_refused"
        message = (
            "OpenAI输出图像审核未通过"
            if moderation_stage == "output"
            else "OpenAI拒绝处理此图像请求"
        )
    elif code == "image_generation_user_error" or (
        code is None and error_type == "image_generation_user_error"
    ):
        category, message = (
            "image_generation_error",
            "OpenAI未完成图像处理；请核对输入图像与修改要求，未自动重试",
        )
    elif status in {400, 404, 422}:
        category, message = (
            "invalid_request",
            "OpenAI不接受当前请求参数、模型或格式；未自动回退",
        )
    else:
        category, message = (
            "upstream_error",
            "OpenAI返回异常状态；未自动重试或跟随重定向",
        )
    return DrawingError(
        category,
        f"{message}（HTTP {status}）",
        request_id,
        upstream_code=code,
        upstream_type=error_type,
        upstream_param=param,
        moderation_stage=moderation_stage,
        http_status=status,response_body=body,
    )


def prepare_image_payload(request, reference=None, mask=None, *, references=(), markup=None) -> bytes:
    from .images import prepare_reference, prepare_markup, prepare_mask
    if not isinstance(references, tuple) or len(references) > 15 or ((mask is not None or markup is not None or references) and reference is None):
        raise DrawingError('parameter_error', '首图/附参考数量或类型无效')
    if mask is not None and markup is not None: raise DrawingError('parameter_error', 'mask与markup互斥')
    payload = {'model': MODEL, 'prompt': request.prompt, 'size': request.size, 'quality': request.quality,
               'complexity': request.complexity, 'output_format': request.output_format, 'n': 1, 'background': 'opaque'}
    if reference is not None:
        if request.output_format != 'png': raise DrawingError('parameter_error', '修改工具只支持PNG输出')
        all_refs = (reference,) + references
        for ref in all_refs:
            if not isinstance(ref, PreparedImage): raise DrawingError('parameter_error', 'reference必须经过解码核验')
            normalized = prepare_reference(ref.blob)
            if (ref.width, ref.height, ref.mime_type, ref.sha256) != (normalized.width, normalized.height, normalized.mime_type, normalized.sha256):
                raise DrawingError('parameter_error', 'reference朝向或尺寸身份失配')
        payload['images'] = [{'image_url': r.data_url} for r in all_refs]
        if mask is not None:
            normalized = prepare_mask(mask.blob, reference)
            if (mask.width,mask.height)!=(normalized.width,normalized.height) or mask.sha256!=normalized.sha256: raise DrawingError('parameter_error','蒙版身份失配')
            payload['mask'] = {'image_url': mask.data_url}
        if markup is not None:
            normalized = prepare_markup(markup.blob, reference)
            if markup.sha256 != normalized.sha256: raise DrawingError('parameter_error','markup身份失配')
            payload['markup'] = {'image_url': markup.data_url}
    raw = json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
    if len(raw) > 64*1024*1024: raise DrawingError('parameter_error','累计图像请求超出容量')
    return raw


class OpenAIImagesClient:
    def __init__(self, api_key: str, *, transport=None):
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or any(c.isspace() for c in api_key)
        ):
            raise DrawingError("authorization_error", "请配置有效的OpenAI API密钥")
        self._api_key = api_key
        from .operator_policy import configured_transport
        self._transport = transport if transport is not None else configured_transport()

    def bind_lineage(self, source_sha, family_sha):
        if any(not isinstance(x,str) or not re.fullmatch(r'[a-f0-9]{64}',x) for x in [source_sha,family_sha]):raise ValueError('invalid private lineage')
        self._lineage=(source_sha,family_sha)

    def bind_recovery(self, receipt):
        self._recovery=receipt

    def bind_plan(self,sha,required_images=1):
        if not isinstance(sha,str)or not re.fullmatch(r'[a-f0-9]{64}',sha):raise ValueError('invalid internal plan identity')
        if type(required_images) is not int or not 1 <= required_images <= 24:
            raise ValueError('invalid internal remaining image count')
        self._plan_sha256=sha
        self._plan_required_images=required_images

    def validate_dispatch_plan(self, plan_sha, required_images):
        from .operator_policy import AuditedTransport
        if isinstance(self._transport, AuditedTransport):
            self._transport.validate_image_plan(plan_sha, required_images)

    def render(
        self,
        request: DrawingRequest,
        reference: PreparedImage | None = None,
        mask: PreparedImage | None = None,
        evidence_sink=None,
        *, references=(), markup=None,
    ) -> RenderResult:
        raw_payload = prepare_image_payload(request, reference, mask, references=references, markup=markup)
        endpoint = 'edits' if reference is not None else 'generations'
        request_id = None
        response_headers = {}
        if evidence_sink:
            evidence_sink('request', raw_payload)

        def fetch(deadline):
            nonlocal request_id, response_headers
            client = httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(300, connect=20),
                follow_redirects=False,
            )
            response = None
            try:
                outgoing = client.build_request(
                    "POST",
                    API + endpoint,
                    content=raw_payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        **({"x-patent-recovery-id":self._recovery["authorization_id"],"x-patent-recovery-evidence-sha256":self._recovery["evidence_sha256"]} if getattr(self,"_recovery",None) else {}),
                        **({"x-patent-source-sha256":self._lineage[0],"x-patent-lineage-sha256":self._lineage[1]} if getattr(self,"_lineage",None) else {}),
                        **({"x-patent-plan-sha256":self._plan_sha256,
                            "x-patent-plan-remaining":str(self._plan_required_images)}
                           if getattr(self,"_plan_sha256",None)else{}),
                    },
                )
                response = client.send(outgoing, stream=True)
                request_id = safe_request_id(response.headers.get("x-request-id"))
                response_headers = {k: response.headers[k] for k in ('x-request-id', 'content-type', 'date') if k in response.headers}
                limit = MAX_RESPONSE if response.status_code == 200 else 1024 * 1024
                try:
                    body = bounded_body(
                        response,
                        limit,
                        max_seconds=REQUEST_DEADLINE_SECONDS,
                        deadline=deadline,
                    )
                except DrawingError:
                    raise DrawingError(
                        "result_unknown",
                        "未取得可用响应，执行与费用状态未知；未自动重试，请核对官方用量",
                        request_id,
                    ) from None
                if evidence_sink:
                    evidence_sink('response', body)
                    evidence_sink('headers', json.dumps({'status': response.status_code, 'headers': response_headers}).encode())
                if response.status_code != 200:
                    raise _status_error(response.status_code, body, request_id)
                return body
            finally:
                close_http_resources(response, client)

        try:
            body = run_with_deadline(fetch, REQUEST_DEADLINE_SECONDS)
        except DrawingError as error:
            if error.category == "response_timeout":
                raise DrawingError(
                    "result_unknown",
                    "请求超过总时限，执行与费用状态未知；未自动重试，请核对官方用量",
                    request_id,
                ) from None
            raise
        except httpx.HTTPError:
            raise DrawingError(
                "result_unknown",
                "连接中断或超时，执行与费用状态未知；未自动重试，请先核对官方用量",
                request_id,
            ) from None
        try:
            result = json.loads(body)
            data = result.get("data")
            if (
                not isinstance(data, list)
                or len(data) != 1
                or not isinstance(data[0], dict)
                or not data[0].get("b64_json")
            ):
                raise DrawingError(
                    "upstream_no_image",
                    "OpenAI未返回一张有效图像；未下载任何输出URL",
                    request_id,
                )
            image = prepare_output(data[0]["b64_json"], request.output_format)
            usage = result.get("usage")
            svg_usage = result.get("svg_usage")
            return RenderResult(
                image, request_id, safe_usage(usage), safe_usage(svg_usage), body, json.loads(raw_payload), response_headers, project_usage(usage)
            )
        except DrawingError as error:
            if error.request_id is None and request_id is not None:
                raise DrawingError(
                    error.category, error.public_message, request_id,http_status=200,response_body=body
                ) from None
            error.http_status=200;error._response_body=body
            raise
        except (ValueError, TypeError, AttributeError):
            raise DrawingError(
                "upstream_invalid_response",
                "OpenAI返回无效响应；未取得可用图像",
                request_id,http_status=200,response_body=body,
            ) from None
