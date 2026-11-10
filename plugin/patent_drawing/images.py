"""Bounded image decoding and static SVG acceptance. No model quality claims."""

import base64
import binascii
import io
import math
import re
import warnings
from dataclasses import dataclass
from hashlib import sha256

from defusedxml import ElementTree as SafeXML
from PIL import Image, ImageOps

from .errors import DrawingError

MIB = 1024 * 1024
MAX_REFERENCE = 10 * MIB
MAX_MASK = 4 * MIB - 1
MAX_OUTPUT = 15 * MIB
MAX_PIXELS = 16_000_000
PNG_END = b"\0\0\0\0IEND\xaeB`\x82"
SVG_NS = "http://www.w3.org/2000/svg"


@dataclass(frozen=True)
class PreparedImage:
    blob: bytes
    mime_type: str
    width: int
    height: int

    @property
    def sha256(self):
        return sha256(self.blob).hexdigest()

    @property
    def data_url(self):
        return f"data:{self.mime_type};base64,{base64.b64encode(self.blob).decode('ascii')}"


def check_dimensions(width, height):
    if not (
        0 < width <= 16384 and 0 < height <= 16384 and width * height <= MAX_PIXELS
    ):
        raise DrawingError("image_dimensions", "图片尺寸超限（最多1600万像素）")


def _validate_png_end(blob: bytes) -> None:
    """Pillow stops at the first IEND; require that chunk to end the file."""
    if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DrawingError("corrupted_image", "PNG文件头无效")
    offset = 8
    while offset + 12 <= len(blob):
        length = int.from_bytes(blob[offset : offset + 4], "big")
        chunk_type = blob[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(blob):
            break
        if chunk_type == b"IEND":
            if length == 0 and end == len(blob) and blob[offset:end] == PNG_END:
                return
            break
        offset = end
    raise DrawingError("corrupted_image", "PNG不完整或含尾随内容")


def _raster(blob: bytes, max_bytes: int, formats: set[str]):
    if not isinstance(blob, bytes) or not blob or len(blob) > max_bytes:
        raise DrawingError("image_size", "图片为空或超过字节限制")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(blob)) as image:
                fmt = image.format
                if fmt not in formats or getattr(image, "n_frames", 1) != 1:
                    raise DrawingError("image_format", "只支持单帧PNG/JPEG/WebP")
                check_dimensions(*image.size)
                if fmt == "PNG":
                    _validate_png_end(blob)
                if fmt == "JPEG" and not blob.endswith(b"\xff\xd9"):
                    raise DrawingError("corrupted_image", "JPEG不完整")
                image.verify()
            with Image.open(io.BytesIO(blob)) as image:
                image.load()
                return ImageOps.exif_transpose(image).copy(), fmt
    except DrawingError:
        raise
    except Exception:
        raise DrawingError("corrupted_image", "图片无法完整解码") from None


def _png(image: Image.Image, limit: int) -> PreparedImage:
    image = image.convert(
        "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    if buffer.tell() > limit:
        raise DrawingError("image_size", "规范化后的图片超过字节限制")
    return PreparedImage(buffer.getvalue(), "image/png", *image.size)


def prepare_reference(blob: bytes) -> PreparedImage:
    image, _ = _raster(blob, MAX_REFERENCE, {"PNG", "JPEG", "WEBP"})
    return _png(image, MAX_REFERENCE)


def prepare_mask(blob: bytes, reference: PreparedImage) -> PreparedImage:
    image, _ = _raster(blob, MAX_MASK, {"PNG"})
    if image.size != (reference.width, reference.height):
        raise DrawingError("mask_dimensions", "蒙版须与方向归一化后的参考图同尺寸")
    rgba = image.convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] != 0:
        raise DrawingError(
            "mask_transparency", "PNG蒙版必须含完全透明像素（可修改区域）"
        )
    return _png(rgba, MAX_MASK)


SVG_TAGS = {
    "svg",
    "g",
    "defs",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "text",
    "tspan",
    "title",
    "desc",
    "clipPath",
    "mask",
    "linearGradient",
    "radialGradient",
    "stop",
    "marker",
}
SVG_ATTRS = set(
    "id x y x1 y1 x2 y2 cx cy r rx ry dx dy width height viewBox d points fill stroke stroke-width stroke-linecap stroke-linejoin stroke-miterlimit stroke-dasharray stroke-dashoffset fill-rule clip-rule opacity fill-opacity stroke-opacity transform font-family font-size font-weight font-style text-anchor dominant-baseline alignment-baseline letter-spacing word-spacing clip-path mask marker-start marker-mid marker-end markerWidth markerHeight markerUnits refX refY orient preserveAspectRatio gradientUnits gradientTransform offset stop-color stop-opacity fx fy fr color vector-effect style version".split()
)
CSS_ATTRS = set(
    "fill stroke stroke-width stroke-linecap stroke-linejoin stroke-miterlimit stroke-dasharray stroke-dashoffset fill-rule clip-rule opacity fill-opacity stroke-opacity font-family font-size font-weight font-style text-anchor dominant-baseline alignment-baseline letter-spacing word-spacing clip-path mask marker-start marker-mid marker-end color vector-effect".split()
)
LOCAL_URL = re.compile(r"url\(\s*#([A-Za-z_][\w.-]*)\s*\)\Z")


def _safe_svg(blob: bytes) -> PreparedImage:
    if len(blob) > 2 * MIB:
        raise DrawingError("svg_size", "SVG超过2MiB上限")
    try:
        text = blob.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError("NUL is invalid UTF-8 XML; reject UTF-16/32 autodetection")
        declaration = re.match(r"^\s*<\?xml\s+[^?]*\?>", text)
        if declaration:
            encoding = re.search(
                r"\bencoding\s*=\s*(['\"])(.*?)\1", declaration.group()
            )
            if encoding and encoding.group(2).lower() not in {"utf-8", "utf8"}:
                raise ValueError("SVG must declare UTF-8")
        without_decl = re.sub(r"^\s*<\?xml\s+[^?]*\?>", "", text, count=1)
        if "<?" in without_decl:
            raise ValueError("processing instruction")
        root = SafeXML.fromstring(
            blob, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
        if root.tag != f"{{{SVG_NS}}}svg":
            raise ValueError("SVG namespace")
        nodes = list(root.iter())
        if (
            len(nodes) > 20000
            or sum(len(n.text or "") + len(n.tail or "") for n in nodes) > 100000
        ):
            raise ValueError("complexity")
        ids, refs = set(), set()
        for node in nodes:
            if (
                not isinstance(node.tag, str)
                or not node.tag.startswith(f"{{{SVG_NS}}}")
                or node.tag.split("}", 1)[1] not in SVG_TAGS
            ):
                raise ValueError("tag")
            for name, value in node.attrib.items():
                if name == "{http://www.w3.org/XML/1998/namespace}space" and value in {
                    "default",
                    "preserve",
                }:
                    continue
                if name not in SVG_ATTRS or len(value) > 100000:
                    raise ValueError("attribute")
                if name == "id":
                    if not re.fullmatch(r"[A-Za-z_][\w.-]*", value) or value in ids:
                        raise ValueError("id")
                    ids.add(value)
                pairs = [(name, value)]
                if name == "style":
                    pairs = []
                    for declaration in value.split(";"):
                        if not declaration.strip():
                            continue
                        key, sep, val = declaration.partition(":")
                        if not sep or key.strip() not in CSS_ATTRS:
                            raise ValueError("CSS")
                        pairs.append((key.strip(), val.strip()))
                for key, val in pairs:
                    if (
                        any(c in val for c in ("\\", "@", "&", "<", ">"))
                        or "/*" in val
                        or "expression" in val.lower()
                        or ":" in val
                    ):
                        raise ValueError("active value")
                    if "url" in val.lower() or "(" in val and key in CSS_ATTRS:
                        m = LOCAL_URL.fullmatch(val)
                        if not m:
                            raise ValueError("external or unsupported CSS function")
                        refs.add(m.group(1))
        if not refs.issubset(ids):
            raise ValueError("undefined reference")
        # Depth cap also prevents expensive nested transforms in downstream viewers.
        stack = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            if depth > 64:
                raise ValueError("depth")
            stack.extend((child, depth + 1) for child in node)

        def length(v):
            m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(px|pt|mm|cm|in)?", v)
            if not m:
                raise ValueError("length")
            return (
                float(m.group(1))
                * {
                    None: 1,
                    "px": 1,
                    "pt": 96 / 72,
                    "mm": 96 / 25.4,
                    "cm": 96 / 2.54,
                    "in": 96,
                }[m.group(2)]
            )

        if "viewBox" in root.attrib:
            values = [
                float(v) for v in re.split(r"[\s,]+", root.attrib["viewBox"].strip())
            ]
            if len(values) != 4 or not all(math.isfinite(v) for v in values):
                raise ValueError("viewBox")
            width, height = values[2:]
            check_dimensions(width, height)
            aspect = width / height
            if "width" in root.attrib and "height" in root.attrib:
                width, height = (
                    length(root.attrib["width"]),
                    length(root.attrib["height"]),
                )
            elif "width" in root.attrib:
                width = length(root.attrib["width"])
                height = width / aspect
            elif "height" in root.attrib:
                height = length(root.attrib["height"])
                width = height * aspect
        else:
            width, height = (
                length(root.attrib.get("width", "")),
                length(root.attrib.get("height", "")),
            )
        check_dimensions(width, height)
        # Root physical dimensions, when present, must also be finite and bounded.
        for key in ("width", "height"):
            if key in root.attrib and not re.fullmatch(
                r"[0-9]+(?:\.[0-9]+)?(?:px|pt|mm|cm|in)?", root.attrib[key]
            ):
                raise ValueError("root dimensions")
        return PreparedImage(blob, "image/svg+xml", math.ceil(width), math.ceil(height))
    except DrawingError:
        raise
    except Exception:
        raise DrawingError(
            "unsafe_svg", "SVG不是可接受的静态矢量图，或包含不支持的内容"
        ) from None


def prepare_output(encoded: str, output_format: str) -> PreparedImage:
    if (
        not isinstance(encoded, str)
        or not encoded
        or len(encoded) > 4 * math.ceil(MAX_OUTPUT / 3)
    ):
        raise DrawingError("output_size", "输出图片为空或超过容量限制")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise DrawingError("decode_failed", "输出不是合法base64图片") from None
    if len(blob) > MAX_OUTPUT:
        raise DrawingError("output_size", "输出图片超过15MiB")
    if output_format == "svg":
        return _safe_svg(blob)
    if output_format != "png":
        raise DrawingError("parameter_error", "不支持的输出格式")
    image, _ = _raster(blob, MAX_OUTPUT, {"PNG"})
    return PreparedImage(blob, "image/png", *image.size)


def prepare_markup(blob: bytes, primary: PreparedImage) -> PreparedImage:
    """A separate transparent PNG in normalized first-input coordinates."""
    image, fmt = _raster(blob, MAX_MASK, {'PNG'})
    if fmt != 'PNG' or image.size != (primary.width, primary.height):
        raise DrawingError('parameter_error', 'markup必须匹配朝向规范化后的首图尺寸')
    if 'A' not in image.getbands() or image.getchannel('A').getextrema()[0] == 255:
        raise DrawingError('parameter_error', 'markup必须含透明像素')
    return _png(image, MAX_MASK)
