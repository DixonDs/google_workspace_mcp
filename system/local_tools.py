"""
System/local tools: operate on files on the server's own filesystem.

These tools need no Google credentials. They exist so an agent can close the loop
on an export - e.g. run ``export_gmail_message(format="pdf", output_path=...)`` and
then *look at* the resulting file - without going back through Google.
"""

import logging
import mimetypes
from pathlib import Path
from typing import Annotated, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from core.server import server
from core.utils import ensure_path_not_sensitive
from core.visual_rendering import (
    describe_rendering_error,
    html_to_pdf_bytes,
    render_document_page,
    render_image,
)

logger = logging.getLogger(__name__)

# Extensions mimetypes may not know on a bare system.
_EXTRA_MIME_TYPES = {
    ".webp": "image/webp",
    ".eml": "message/rfc822",
}


def _guess_mime_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(path))
    if not mime_type:
        mime_type = _EXTRA_MIME_TYPES.get(
            path.suffix.lower(), "application/octet-stream"
        )
    return mime_type


def _resolve_local_file(path: str) -> Path:
    """Resolve and vet a user-supplied local path for reading.

    Any readable file is allowed except the sensitive locations shared with the
    rest of the server (``.env``, ``~/.ssh``, ``/etc/passwd``, ...): this tool runs
    on the user's own machine and has to be able to open whatever the export tools
    just wrote wherever the user asked for it.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File not found at {resolved}")
    if not resolved.is_file():
        raise ValueError(f"Path is not a file: {resolved}")
    ensure_path_not_sensitive(resolved)
    return resolved


@server.tool(
    title="Get Local File Visual",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def get_local_file_visual(
    path: Annotated[
        str,
        Field(
            description=(
                "Path to a local PDF, image, or HTML file (e.g. one produced by "
                "export_gmail_message). '~' is expanded."
            )
        ),
    ],
    page_number: Annotated[
        int,
        Field(description="1-based page to render. Ignored for images.", ge=1),
    ] = 1,
    max_dimension: Annotated[
        Optional[int],
        Field(
            description=(
                "Optional cap on the returned image's width and height in pixels "
                "(aspect ratio preserved)."
            ),
            ge=64,
        ),
    ] = None,
) -> list:
    """
    Visually inspect a local file by rendering it to an image.

    Supports PDF (one page at a time), images (PNG/JPEG/GIF/WebP/...), and HTML
    (rendered to PDF first, then paged). Typical use: check what an exported
    email or document actually looks like.

    Args:
        path (str): Path to the local file.
        page_number (int): Page number to render (default 1). Ignored for images.
        max_dimension (int): Optional maximum width or height of the returned image.

    Returns:
        list: [ImageContent, str] for documents (image + page footer), [ImageContent]
            for images, or a single "Error: ..." string.
    """
    logger.info(
        f"[get_local_file_visual] Path: {path!r}, Page: {page_number}, "
        f"Max Dim: {max_dimension}"
    )

    try:
        resolved = _resolve_local_file(path)
    except (FileNotFoundError, ValueError) as exc:
        return [f"Error: {exc}"]

    mime_type = _guess_mime_type(resolved)

    try:
        if mime_type.startswith("image/"):
            return await render_image(resolved, max_dimension)
        if mime_type == "application/pdf":
            return await render_document_page(resolved, page_number, max_dimension)
        if mime_type in ("text/html", "application/xhtml+xml"):
            html_content = resolved.read_text(encoding="utf-8", errors="replace")
            pdf_bytes = await html_to_pdf_bytes(html_content)
            return await render_document_page(pdf_bytes, page_number, max_dimension)
    except Exception as exc:
        logger.error(f"[get_local_file_visual] Rendering failed for {resolved}: {exc}")
        return [describe_rendering_error(exc)]

    return [
        f"Error: Unsupported file type '{mime_type}' for '{resolved.name}'. "
        "Only PDF, image, and HTML files can be inspected visually."
    ]
