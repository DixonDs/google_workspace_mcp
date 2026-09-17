"""
Shared visual rendering helpers: HTML -> PDF, PDF -> page images, image handling.

Used by the "visual" tools (``get_gmail_message_visual``, ``get_drive_file_visual``,
``get_local_file_visual``) and by ``export_gmail_message`` for its PDF/PNG formats.

The heavy dependencies (WeasyPrint, pdf2image/Poppler, Pillow) are imported lazily
so the server still starts when they - or their native libraries - are missing;
the tools then return a clear installation hint instead of failing at import time.
"""

import asyncio
import io
import logging
from pathlib import Path
from typing import List, Optional, Sequence, Union
from urllib.parse import urlsplit

from fastmcp.utilities.types import Image

logger = logging.getLogger(__name__)

PdfSource = Union[bytes, str, Path]

# Schemes WeasyPrint may fetch while rendering sender-controlled HTML. Anything
# else (notably file://) is refused so an email can't pull local files into a PDF
# that is then handed back to the model.
_ALLOWED_FETCH_SCHEMES = frozenset({"http", "https", "data"})

# Default render resolution for page images. 110 DPI keeps an A4 page around
# 900x1300 px: readable for a multimodal model without a huge payload.
DEFAULT_RENDER_DPI = 110


class RenderingDependencyError(RuntimeError):
    """A rendering dependency (Python package or native library) is unavailable."""


def _weasyprint():
    try:
        import weasyprint  # noqa: WPS433 (lazy import by design)
    except (ImportError, OSError) as exc:  # pragma: no cover - depends on host setup
        # WeasyPrint raises OSError (via cffi) when Pango/Cairo/GDK-PixBuf are missing.
        raise RenderingDependencyError(
            "PDF rendering requires WeasyPrint and its native libraries (Pango, "
            "Cairo, GDK-PixBuf). Install the Python package with "
            "`uv sync` and the libraries with e.g. `brew install pango` (macOS) or "
            f"`apt install libpango-1.0-0 libpangoft2-1.0-0` (Debian/Ubuntu). ({exc})"
        ) from exc
    return weasyprint


def _pdf2image():
    try:
        import pdf2image  # noqa: WPS433 (lazy import by design)
    except ImportError as exc:  # pragma: no cover - depends on host setup
        raise RenderingDependencyError(
            "PDF -> image conversion requires the pdf2image package. "
            f"Install it with `uv sync`. ({exc})"
        ) from exc
    return pdf2image


def _pil_image():
    try:
        from PIL import Image as PILImage  # noqa: WPS433 (lazy import by design)
    except ImportError as exc:  # pragma: no cover - depends on host setup
        raise RenderingDependencyError(
            f"Image handling requires Pillow. Install it with `uv sync`. ({exc})"
        ) from exc
    return PILImage


def _poppler_hint(exc: Exception) -> RenderingDependencyError:
    return RenderingDependencyError(
        "PDF -> image conversion requires Poppler (`pdftoppm`/`pdfinfo` on PATH). "
        "Install it with e.g. `brew install poppler` (macOS) or "
        f"`apt install poppler-utils` (Debian/Ubuntu). ({exc})"
    )


def _restricted_url_fetcher(url: str, *args, **kwargs):
    """Callable URL fetcher (WeasyPrint < 69) that only allows http(s) and data: URLs."""
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_FETCH_SCHEMES:
        raise ValueError(
            f"Refusing to fetch {scheme or 'relative'}: URL while rendering"
        )
    weasyprint = _weasyprint()
    return weasyprint.default_url_fetcher(url, *args, **kwargs)


def _make_url_fetcher():
    """Build the WeasyPrint fetcher that enforces the scheme allowlist.

    WeasyPrint >= 69 takes a ``URLFetcher`` object with ``allowed_protocols``;
    older versions take a plain callable.
    """
    weasyprint = _weasyprint()
    fetcher_cls = getattr(weasyprint, "URLFetcher", None)
    if fetcher_cls is not None:
        return fetcher_cls(allowed_protocols=sorted(_ALLOWED_FETCH_SCHEMES))
    return _restricted_url_fetcher


def html_to_pdf_bytes_sync(html_content: str) -> bytes:
    """Render an HTML document to PDF bytes with WeasyPrint (blocking)."""
    weasyprint = _weasyprint()
    document = weasyprint.HTML(
        string=html_content,
        url_fetcher=_make_url_fetcher(),
    )
    return document.write_pdf(presentational_hints=True)


async def html_to_pdf_bytes(html_content: str) -> bytes:
    """Render an HTML document to PDF bytes on a worker thread."""
    return await asyncio.to_thread(html_to_pdf_bytes_sync, html_content)


def pdf_page_count(pdf_input: PdfSource) -> int:
    """Return the number of pages in a PDF given as bytes or a file path."""
    pdf2image = _pdf2image()
    try:
        if isinstance(pdf_input, (str, Path)):
            info = pdf2image.pdfinfo_from_path(str(pdf_input))
        else:
            info = pdf2image.pdfinfo_from_bytes(pdf_input)
    except pdf2image.exceptions.PDFInfoNotInstalledError as exc:
        raise _poppler_hint(exc) from exc
    return int(info.get("Pages", 0))


def pdf_to_images(
    pdf_input: PdfSource,
    first_page: Optional[int] = None,
    last_page: Optional[int] = None,
    dpi: int = DEFAULT_RENDER_DPI,
) -> list:
    """Rasterise PDF pages (bytes or path) to a list of PIL images."""
    pdf2image = _pdf2image()
    kwargs = {"dpi": dpi, "fmt": "png"}
    if first_page:
        kwargs["first_page"] = first_page
    if last_page:
        kwargs["last_page"] = last_page
    try:
        if isinstance(pdf_input, (str, Path)):
            return pdf2image.convert_from_path(str(pdf_input), **kwargs)
        return pdf2image.convert_from_bytes(pdf_input, **kwargs)
    except pdf2image.exceptions.PDFInfoNotInstalledError as exc:
        raise _poppler_hint(exc) from exc
    except pdf2image.exceptions.PDFPageCountError as exc:
        raise ValueError(f"Could not read PDF: {exc}") from exc


def stitch_images_vertically(images: Sequence):
    """Stack PIL images top-to-bottom on a white canvas (single image is returned as-is)."""
    if not images:
        raise ValueError("No images to stitch.")
    if len(images) == 1:
        return images[0]
    PILImage = _pil_image()
    width = max(img.width for img in images)
    height = sum(img.height for img in images)
    canvas = PILImage.new("RGB", (width, height), "white")
    y = 0
    for img in images:
        canvas.paste(img, (0, y))
        y += img.height
    return canvas


def resize_to_fit(image, max_dimension: Optional[int]):
    """Shrink a PIL image in place so neither side exceeds ``max_dimension``."""
    if not max_dimension or max(image.size) <= max_dimension:
        return image
    PILImage = _pil_image()
    image.thumbnail((max_dimension, max_dimension), PILImage.Resampling.LANCZOS)
    return image


def pil_to_png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        image = image.convert("RGB")
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def image_content(png_bytes: bytes):
    """Wrap PNG bytes as an MCP ImageContent block."""
    return Image(data=png_bytes, format="png").to_image_content()


def render_image_sync(
    image_input: Union[bytes, str, Path], max_dimension: Optional[int] = None
) -> list:
    """Return an image (bytes or path) as ``[ImageContent]``, resized if requested.

    The image is always re-encoded as PNG so the client receives one predictable
    format regardless of the source (JPEG, GIF, WebP, ...).
    """
    PILImage = _pil_image()
    source = (
        str(image_input)
        if isinstance(image_input, (str, Path))
        else io.BytesIO(image_input)
    )
    with PILImage.open(source) as img:
        img.load()
        img = resize_to_fit(img, max_dimension)
        return [image_content(pil_to_png_bytes(img))]


def render_document_page_sync(
    pdf_input: PdfSource,
    page_number: int = 1,
    max_dimension: Optional[int] = None,
    dpi: int = DEFAULT_RENDER_DPI,
) -> list:
    """
    Render one page of a PDF as ``[ImageContent, footer_text]``.

    The footer tells the model which page it is looking at and how to get the
    next one. Out-of-range pages return a single error string instead.
    """
    if page_number < 1:
        return [f"Error: page_number must be >= 1 (got {page_number})."]

    total_pages = pdf_page_count(pdf_input)
    if total_pages == 0:
        return ["Error: the document has no pages or could not be read."]
    if page_number > total_pages:
        return [
            f"Error: Page {page_number} does not exist. "
            f"The document has {total_pages} page(s)."
        ]

    images = pdf_to_images(
        pdf_input, first_page=page_number, last_page=page_number, dpi=dpi
    )
    if not images:
        return [f"Error: Could not render page {page_number}."]

    image = resize_to_fit(images[0], max_dimension)
    footer = f"Displaying page {page_number} of {total_pages}."
    if page_number < total_pages:
        footer += (
            f" Call this tool again with page_number={page_number + 1} "
            "to see the next page."
        )
    return [image_content(pil_to_png_bytes(image)), footer]


async def render_document_page(
    pdf_input: PdfSource,
    page_number: int = 1,
    max_dimension: Optional[int] = None,
) -> list:
    """Async wrapper for :func:`render_document_page_sync`."""
    return await asyncio.to_thread(
        render_document_page_sync, pdf_input, page_number, max_dimension
    )


async def render_image(
    image_input: Union[bytes, str, Path], max_dimension: Optional[int] = None
) -> list:
    """Async wrapper for :func:`render_image_sync`."""
    return await asyncio.to_thread(render_image_sync, image_input, max_dimension)


async def html_to_png_bytes(html_content: str, dpi: int = DEFAULT_RENDER_DPI) -> bytes:
    """Render HTML to a single tall PNG (all PDF pages stitched vertically)."""
    pdf_bytes = await html_to_pdf_bytes(html_content)

    def _rasterise() -> bytes:
        images = pdf_to_images(pdf_bytes, dpi=dpi)
        if not images:
            raise ValueError("No pages were rendered from the document.")
        return pil_to_png_bytes(stitch_images_vertically(images))

    return await asyncio.to_thread(_rasterise)


def describe_rendering_error(exc: Exception) -> str:
    """Turn a rendering failure into a user-facing error string."""
    if isinstance(exc, RenderingDependencyError):
        return f"Error: {exc}"
    return f"Error rendering document: {exc}"


__all__: List[str] = [
    "DEFAULT_RENDER_DPI",
    "RenderingDependencyError",
    "describe_rendering_error",
    "html_to_pdf_bytes",
    "html_to_pdf_bytes_sync",
    "html_to_png_bytes",
    "image_content",
    "pdf_page_count",
    "pdf_to_images",
    "pil_to_png_bytes",
    "render_document_page",
    "render_document_page_sync",
    "render_image",
    "render_image_sync",
    "resize_to_fit",
    "stitch_images_vertically",
]
