"""Tests for the system service's get_local_file_visual tool."""

import pytest

import system.local_tools as local_tools
from system.local_tools import get_local_file_visual


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.fixture
def renderers(monkeypatch):
    calls = []

    async def fake_render_image(path, max_dimension):
        calls.append(("image", str(path), max_dimension))
        return ["<image>"]

    async def fake_render_page(source, page_number, max_dimension):
        calls.append(("page", source, page_number, max_dimension))
        return ["<image>", f"Displaying page {page_number} of 2."]

    async def fake_html_to_pdf(html):
        calls.append(("html_to_pdf", html))
        return b"%PDF fake"

    monkeypatch.setattr(local_tools, "render_image", fake_render_image)
    monkeypatch.setattr(local_tools, "render_document_page", fake_render_page)
    monkeypatch.setattr(local_tools, "html_to_pdf_bytes", fake_html_to_pdf)
    return calls


run = _unwrap(get_local_file_visual)


@pytest.mark.asyncio
async def test_image_file(renderers, tmp_path):
    img = tmp_path / "shot.webp"
    img.write_bytes(b"RIFF....WEBP")
    result = await run(path=str(img), max_dimension=500)
    assert result == ["<image>"]
    assert renderers == [("image", str(img.resolve()), 500)]


@pytest.mark.asyncio
async def test_pdf_file_is_rendered_from_path(renderers, tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = await run(path=str(pdf), page_number=2)
    assert result[1] == "Displaying page 2 of 2."
    kind, source, page, max_dim = renderers[0]
    assert (kind, page, max_dim) == ("page", 2, None)
    assert source == pdf.resolve()  # path, not bytes: no full read into memory


@pytest.mark.asyncio
async def test_html_file_is_converted_then_paged(renderers, tmp_path):
    page = tmp_path / "mail.html"
    page.write_text("<html><body>Hi</body></html>", encoding="utf-8")
    result = await run(path=str(page))
    assert result[0] == "<image>"
    assert renderers[0] == ("html_to_pdf", "<html><body>Hi</body></html>")
    assert renderers[1] == ("page", b"%PDF fake", 1, None)


@pytest.mark.asyncio
async def test_tilde_is_expanded(renderers, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "p.png").write_bytes(b"\x89PNG")
    result = await run(path="~/p.png")
    assert result == ["<image>"]


@pytest.mark.asyncio
async def test_missing_and_directory_paths(renderers, tmp_path):
    (missing,) = await run(path=str(tmp_path / "nope.pdf"))
    assert missing.startswith("Error: File not found")
    (is_dir,) = await run(path=str(tmp_path))
    assert is_dir.startswith("Error: Path is not a file")
    assert renderers == []


@pytest.mark.asyncio
async def test_sensitive_paths_are_refused(renderers, tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=1")
    (error,) = await run(path=str(secret))
    assert "not allowed" in error
    assert renderers == []


@pytest.mark.asyncio
async def test_unsupported_type(renderers, tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    (error,) = await run(path=str(other))
    assert error.startswith("Error: Unsupported file type 'text/plain'")


@pytest.mark.asyncio
async def test_toolchain_error_is_reported(tmp_path, monkeypatch):
    from core.visual_rendering import RenderingDependencyError

    async def boom(*_a):
        raise RenderingDependencyError("PDF -> image conversion requires Poppler")

    monkeypatch.setattr(local_tools, "render_document_page", boom)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    (error,) = await run(path=str(pdf))
    assert error == "Error: PDF -> image conversion requires Poppler"
