"""The two Dify tools share one bounded, file-first invocation path."""

from hashlib import sha256
from uuid import uuid4

from .client import MODEL, OpenAIImagesClient
from .errors import DrawingError, ToolInvokeError
from .file_input import FileSettings, read_file
from .images import MAX_MASK, MAX_REFERENCE, prepare_mask, prepare_reference
from .prompts import TEMPLATE_VERSION, build_prompt

REVIEW_ITEMS = [
    "核对部件、标号与引出线对应",
    "核对连接关系、流程方向和步骤",
    "核对附图与技术材料一致性",
    "核对修改是否影响未涉及区域",
    "核对线条、文字与最终打印清晰度",
]


def invoke(tool, parameters: dict, mode: str):
    try:
        request = build_prompt(parameters, mode)
        credentials = tool.runtime.credentials
        client = OpenAIImagesClient(credentials.get("openai_api_key"))
        settings = FileSettings(
            credentials.get("dify_file_origin") or "",
            credentials.get("dify_internal_files_url") or "",
        )
        reference, mask = None, None
        reference_hash, mask_hash = None, None
        if mode == "edit":
            raw = read_file(parameters.get("reference_image"), MAX_REFERENCE, settings)
            reference_hash = sha256(raw).hexdigest()
            reference = prepare_reference(raw)
            if parameters.get("mask") is not None:
                raw_mask = read_file(parameters["mask"], MAX_MASK, settings)
                mask_hash = sha256(raw_mask).hexdigest()
                mask = prepare_mask(raw_mask, reference)
        result = client.render(request, reference, mask)
        image = result.image
        filename = f"patent-{mode}-{uuid4().hex[:12]}.{request.output_format}"
        metadata = {
            "filename": filename,
            "file_name": filename,
            "mime_type": image.mime_type,
            "width": image.width,
            "height": image.height,
        }
        output = {
            "success": True,
            "mode": mode,
            "model": MODEL,
            "images": [
                {**metadata, "sha256": image.sha256, "byte_count": len(image.blob)}
            ],
            "count": 1,
            "parameters": {
                "drawing_type": request.drawing_type,
                "size": request.size,
                "quality": request.quality,
                "complexity": request.complexity,
                "output_format": request.output_format,
            },
            "template_version": TEMPLATE_VERSION,
            "prompt_sha256": request.prompt_sha256,
            "reference_sha256": reference_hash,
            "mask_sha256": mask_hash,
            "normalized_reference_sha256": reference.sha256 if reference else None,
            "normalized_mask_sha256": mask.sha256 if mask else None,
            "request_id": result.request_id,
            "usage": result.usage,
            "svg_usage": result.svg_usage,
            "file_validation_passed": True,
            "professional_review_status": "not_reviewed",
            "requires_human_review": True,
            "review_items": REVIEW_ITEMS,
        }
        # Build BOTH messages before yielding. A serialization failure cannot leave
        # an already-yielded file or a false success JSON behind.
        try:
            messages = [
                tool.create_blob_message(blob=image.blob, meta=metadata),
                tool.create_json_message(output),
            ]
        except Exception:
            raise DrawingError(
                "file_wrap_failed",
                "文件或元数据封装失败；未输出成功结果",
                result.request_id,
            ) from None
    except DrawingError as error:
        raise ToolInvokeError(str(error)) from None
    except Exception:
        raise ToolInvokeError(
            "[internal_error] 绘图处理失败；未输出成功结果，未自动重试"
        ) from None
    yield from messages
