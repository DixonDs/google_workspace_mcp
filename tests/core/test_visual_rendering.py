"""Tests for core.visual_rendering.

Pure-Python parts (resizing, stitching, PNG encoding, ImageContent wrapping, error
mapping) always run. Tests that need WeasyPrint's native libraries or Poppler are
skipped when the toolchain is not installed on the host.
"""

import shutil

import pytest
from mcp.types import ImageContent

from core import visual_rendering as vr

PIL = pytest.importorskip("PIL")
from PIL import Image as PILImage  # noqa: E402


def _weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:
        return False
    return True


def _poppler_available() -> bool:
    return shutil.which("pdftoppm") is not None and shutil.which("pdfinfo") is not None


needs_weasyprint = pytest.mark.skipif(
    not _weasyprint_available(), reason="WeasyPrint (Pango/Cairo) not installed"
)
needs_poppler = pytest.mark.skipif(
    not _poppler_available(), reason="Poppler not installed"
)


def _png(width, height, color="red") -> bytes:
    return vr.pil_to_png_bytes(PILImage.new("RGB", (width, height), color))


# --- pure helpers ---------------------------------------------------------------


def test_resize_to_fit_only_shrinks():
    img = PILImage.new("RGB", (400, 200))
    assert vr.resize_to_fit(img, None).size == (400, 200)
    assert vr.resize_to_fit(img, 1000).size == (400, 200)
    assert vr.resize_to_fit(img, 100).size == (100, 50)


def test_stitch_images_vertically():
    a = PILImage.new("RGB", (100, 20), "red")
    b = PILImage.new("RGB", (80, 30), "blue")
    assert vr.stitch_images_vertically([a]) is a
    stitched = vr.stitch_images_vertically([a, b])
    assert stitched.size == (100, 50)
    assert stitched.getpixel((5, 5)) == (255, 0, 0)
    assert stitched.getpixel((5, 25)) == (0, 0, 255)
    assert stitched.getpixel((95, 25)) == (255, 255, 255)  # padding is white
    with pytest.raises(ValueError):
        vr.stitch_images_vertically([])


def test_pil_to_png_bytes_normalises_mode():
    cmyk = PILImage.new("CMYK", (4, 4))
    png = vr.pil_to_png_bytes(cmyk)
    assert png.startswith(b"\x89PNG")
    with PILImage.open(__import__("io").BytesIO(png)) as reopened:
        assert reopened.mode == "RGB"


def test_image_content_wraps_png():
    block = vr.image_content(_png(2, 2))
    assert isinstance(block, ImageContent)
    assert block.mimeType == "image/png"
    assert block.data


def test_render_image_sync_from_bytes_and_path(tmp_path):
    png = _png(300, 100, "green")
    (result,) = vr.render_image_sync(png, max_dimension=150)
    assert isinstance(result, ImageContent)

    jpg_path = tmp_path / "pic.jpg"
    PILImage.new("RGB", (60, 40), "blue").save(jpg_path, format="JPEG")
    (result,) = vr.render_image_sync(jpg_path)
    assert result.mimeType == "image/png"  # always re-encoded as PNG


@pytest.mark.asyncio
async def test_render_image_async_wrapper():
    (result,) = await vr.render_image(_png(10, 10))
    assert isinstance(result, ImageContent)


def test_describe_rendering_error():
    assert vr.describe_rendering_error(vr.RenderingDependencyError("need X")) == (
        "Error: need X"
    )
    assert vr.describe_rendering_error(ValueError("bad pdf")) == (
        "Error rendering document: bad pdf"
    )


def test_restricted_url_fetcher_blocks_file_urls():
    with pytest.raises(ValueError, match="Refusing to fetch file"):
        vr._restricted_url_fetcher("file:///etc/passwd")
    with pytest.raises(ValueError, match="Refusing to fetch relative"):
        vr._restricted_url_fetcher("relative/path.png")


@needs_weasyprint
def test_url_fetcher_refuses_file_scheme_when_rendering(tmp_path):
    fetcher = vr._make_url_fetcher()
    with pytest.raises(ValueError, match="disallowed protocol"):
        fetcher.fetch("file:///etc/hosts")
    # data: URLs (embedded inline images) are still allowed.
    response = fetcher.fetch("data:text/plain;base64,aGk=")
    assert response.read() == b"hi"


# --- real toolchain --------------------------------------------------------------


@needs_weasyprint
def test_html_to_pdf_bytes_sync_produces_pdf():
    pdf = vr.html_to_pdf_bytes_sync("<html><body><h1>Hello</h1></body></html>")
    assert pdf.startswith(b"%PDF")


@needs_weasyprint
@needs_poppler
def test_render_document_page_pages_and_footer():
    # Two pages via a forced page break.
    html = (
        "<html><body><h1>Page one</h1>"
        '<div style="page-break-before: always"></div><h1>Page two</h1></body></html>'
    )
    pdf = vr.html_to_pdf_bytes_sync(html)
    assert vr.pdf_page_count(pdf) == 2

    image, footer = vr.render_document_page_sync(pdf, page_number=1, max_dimension=400)
    assert isinstance(image, ImageContent)
    assert footer == (
        "Displaying page 1 of 2. Call this tool again with page_number=2 "
        "to see the next page."
    )
    _, footer2 = vr.render_document_page_sync(pdf, page_number=2)
    assert footer2 == "Displaying page 2 of 2."
    (error,) = vr.render_document_page_sync(pdf, page_number=3)
    assert error.startswith("Error: Page 3 does not exist")


@needs_weasyprint
@needs_poppler
@pytest.mark.asyncio
async def test_html_to_png_bytes_stitches_pages():
    html = (
        "<html><body><p>one</p>"
        '<div style="page-break-before: always"></div><p>two</p></body></html>'
    )
    png = await vr.html_to_png_bytes(html)
    with PILImage.open(__import__("io").BytesIO(png)) as img:
        # Two stacked A4 pages are much taller than wide.
        assert img.height > img.width * 2
