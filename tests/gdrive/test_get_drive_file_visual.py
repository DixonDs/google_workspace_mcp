"""Tests for get_drive_file_visual (rendering is mocked; routing logic is real)."""

from unittest.mock import Mock

import pytest

import gdrive.drive_tools as drive_tools
from core.file_limits import FileTooLargeError
from gdrive.drive_tools import get_drive_file_visual


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.fixture
def harness(monkeypatch):
    """Mock Drive metadata resolution, the download, and both renderers."""
    state = {"download_calls": [], "render_calls": []}

    async def fake_resolve(service, file_id, extra_fields=None):
        return "resolved-" + file_id, state["metadata"]

    async def fake_download(request_obj, **kwargs):
        state["download_calls"].append((request_obj, kwargs))
        if isinstance(state.get("download_result"), Exception):
            raise state["download_result"]
        return state.get("download_result", b"bytes")

    def fake_media_request(service, file_id, export_mime_type):
        return ("media", file_id, export_mime_type)

    async def fake_render_image(data, max_dimension):
        state["render_calls"].append(("image", data, max_dimension))
        return ["<image>"]

    async def fake_render_page(data, page_number, max_dimension):
        state["render_calls"].append(("page", data, page_number, max_dimension))
        return ["<image>", f"Displaying page {page_number} of 9."]

    monkeypatch.setattr(drive_tools, "resolve_drive_item", fake_resolve)
    monkeypatch.setattr(drive_tools, "download_media_bytes", fake_download)
    monkeypatch.setattr(drive_tools, "_media_request", fake_media_request)
    monkeypatch.setattr(drive_tools, "render_image", fake_render_image)
    monkeypatch.setattr(drive_tools, "render_document_page", fake_render_page)
    return state


async def _run(**kwargs):
    kwargs.setdefault("service", Mock())
    kwargs.setdefault("user_google_email", "u@example.com")
    return await _unwrap(get_drive_file_visual)(**kwargs)


@pytest.mark.asyncio
async def test_google_doc_is_exported_to_pdf_and_paged(harness):
    harness["metadata"] = {
        "name": "Design doc",
        "mimeType": "application/vnd.google-apps.document",
    }
    result = await _run(file_id="doc1", page_number=3, max_dimension=700)

    request_obj, kwargs = harness["download_calls"][0]
    assert request_obj == ("media", "resolved-doc1", "application/pdf")
    assert kwargs["file_name"] == "Design doc"
    assert harness["render_calls"] == [("page", b"bytes", 3, 700)]
    assert result == ["<image>", "Displaying page 3 of 9."]


@pytest.mark.asyncio
async def test_pdf_is_downloaded_as_is(harness):
    harness["metadata"] = {"name": "x.pdf", "mimeType": "application/pdf"}
    await _run(file_id="pdf1")
    assert harness["download_calls"][0][0] == ("media", "resolved-pdf1", None)
    assert harness["render_calls"][0][0] == "page"


@pytest.mark.asyncio
async def test_image_uses_image_renderer_and_ignores_page(harness):
    harness["metadata"] = {"name": "pic.jpg", "mimeType": "image/jpeg"}
    harness["download_result"] = b"jpeg"
    result = await _run(file_id="img1", page_number=5, max_dimension=300)
    assert harness["render_calls"] == [("image", b"jpeg", 300)]
    assert result == ["<image>"]


@pytest.mark.asyncio
async def test_unsupported_type_is_rejected_before_download(harness):
    harness["metadata"] = {"name": "data.zip", "mimeType": "application/zip"}
    (error,) = await _run(file_id="zip1")
    assert error.startswith("Error: 'data.zip' has type 'application/zip'")
    assert harness["download_calls"] == []


@pytest.mark.asyncio
async def test_size_limit_error_is_returned(harness):
    harness["metadata"] = {"name": "big.pdf", "mimeType": "application/pdf"}
    harness["download_result"] = FileTooLargeError("too big")
    assert await _run(file_id="big") == ["too big"]


@pytest.mark.asyncio
async def test_rendering_failure_is_reported(harness, monkeypatch):
    harness["metadata"] = {"name": "x.pdf", "mimeType": "application/pdf"}

    async def broken(*_args):
        raise ValueError("Could not read PDF: garbage")

    monkeypatch.setattr(drive_tools, "render_document_page", broken)
    (error,) = await _run(file_id="bad")
    assert error == "Error rendering document: Could not read PDF: garbage"
