import base64
import io

import pytest
from PIL import Image

from patent_drawing.errors import DrawingError
from patent_drawing.images import prepare_reference, prepare_mask, prepare_output


def picture(fmt="PNG", size=(32, 48), color="white", mode="RGB", **kwargs):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def b64(blob):
    return base64.b64encode(blob).decode()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_reference_formats(fmt):
    image = prepare_reference(picture(fmt))
    assert (image.width, image.height) == (32, 48)
    assert image.mime_type == "image/png"  # normalized losslessly after decode


def test_exif_orientation():
    exif = Image.Exif()
    exif[274] = 6
    image = prepare_reference(picture("JPEG", exif=exif))
    assert (image.width, image.height) == (48, 32)


@pytest.mark.parametrize(
    "blob",
    [
        b"bad",
        picture()[:-10],
        b"x" * (10 * 1024 * 1024 + 1),
        picture(size=(4100, 4100)),
        picture("GIF"),
    ],
)
def test_invalid_reference(blob):
    with pytest.raises(DrawingError):
        prepare_reference(blob)


def test_animated_webp_rejected():
    buf = io.BytesIO()
    Image.new("RGB", (32, 48), "white").save(
        buf,
        format="WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (32, 48), "black")],
        duration=100,
    )
    with pytest.raises(DrawingError):
        prepare_reference(buf.getvalue())


def test_mask_requires_exact_size_png_transparency():
    ref = prepare_reference(picture())
    good = picture(mode="RGBA", color=(0, 0, 0, 0))
    assert prepare_mask(good, ref).mime_type == "image/png"
    for blob in [
        picture(),
        picture(mode="RGBA", color=(0, 0, 0, 255)),
        picture("JPEG"),
        picture(size=(48, 32), mode="RGBA", color=(0, 0, 0, 0)),
        b"x" * (4 * 1024 * 1024),
    ]:
        with pytest.raises(DrawingError):
            prepare_mask(blob, ref)


def test_png_output_exact_format_and_base64():
    assert prepare_output(b64(picture()), "png").width == 32
    for value in ["!!!!", b64(picture("JPEG")), b64(picture()[:-8])]:
        with pytest.raises(DrawingError):
            prepare_output(value, "png")


def test_static_svg_and_local_definition():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 200"><defs><clipPath id="c"><rect width="20" height="20"/></clipPath></defs><path clip-path="url(#c)" d="M0 0L10 10" stroke="black"/><text x="10" y="20">1</text></svg>'
    image = prepare_output(b64(svg), "svg")
    assert (image.width, image.height, image.mime_type) == (100, 200, "image/svg+xml")


def test_svg_reports_actual_viewport_and_bounds_it():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" viewBox="0 0 100 100"/>'
    image = prepare_output(b64(svg.encode()), "svg")
    assert (image.width, image.height) == (800, 600)
    for width in ["999999999999999999999999999999", "0", "-1"]:
        with pytest.raises(DrawingError):
            prepare_output(
                b64(svg.replace('width="800"', f'width="{width}"').encode()), "svg"
            )


@pytest.mark.parametrize(
    "svg",
    [
        '<!DOCTYPE svg [<!ENTITY x "boom">]><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">&x;</svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" onload="alert(1)"/>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><image href="data:image/png;base64,AAAA"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><foreignObject/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path fill="url(https://evil.test/a)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><style>path {fill:red}</style></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><use href="https://evil.test/a"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><use href="#missing"/></svg>',
        '<svg xmlns="http://evil.test" viewBox="0 0 10 10"/>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 NaN 20"/>',
        '<?xml-stylesheet href="https://evil.test/a"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>',
    ],
)
def test_active_or_invalid_svg_rejected(svg):
    with pytest.raises(DrawingError):
        prepare_output(b64(svg.encode()), "svg")
