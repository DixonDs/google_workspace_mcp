"""Tests for export_gmail_message, get_gmail_message_visual and their helpers."""

import base64
from pathlib import Path
from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError

import gmail.gmail_tools as gmail_tools
from core.utils import UserInputError
from gmail.gmail_tools import (
    _build_archival_html,
    _embed_inline_images,
    _export_filename,
    export_gmail_message,
    get_gmail_message_visual,
)


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _b64url(data: bytes) -> str:
    """Padded base64url as the Gmail API returns body-part data."""
    return base64.urlsafe_b64encode(data).decode()


def _b64url_unpadded(data: bytes) -> str:
    """Unpadded base64url, as the Gmail API returns ``raw`` message content."""
    return _b64url(data).rstrip("=")


PNG_BYTES = b"\x89PNG\r\n\x1a\n<fake-png-bytes>"
PNG_DATA_URI = f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode()}"


def _headers(**overrides):
    header_map = {
        "Subject": "Quarterly <report>",
        "From": "Alice <alice@example.com>",
        "To": "bob@example.com",
        "Cc": "carol@example.com",
        "Message-ID": "<msg-1@example.com>",
        "Date": "Fri, 28 Mar 2026 10:00:00 -0400",
    }
    header_map.update(overrides)
    return [
        {"name": name, "value": value}
        for name, value in header_map.items()
        if value is not None
    ]


def _metadata_response(message_id, headers=None, size_estimate=1024):
    return {
        "id": message_id,
        "sizeEstimate": size_estimate,
        "payload": {"headers": headers or _headers()},
    }


def _full_response(message_id, *, text=None, html=None, inline_images=(), headers=None):
    """Build a multipart/related payload with optional inline image parts."""
    alternative_parts = []
    if text is not None:
        alternative_parts.append(
            {"mimeType": "text/plain", "body": {"data": _b64url(text.encode())}}
        )
    if html is not None:
        alternative_parts.append(
            {"mimeType": "text/html", "body": {"data": _b64url(html.encode())}}
        )
    related_parts = [{"mimeType": "multipart/alternative", "parts": alternative_parts}]
    related_parts.extend(inline_images)
    return {
        "id": message_id,
        "payload": {
            "mimeType": "multipart/related",
            "headers": headers or _headers(),
            "parts": related_parts,
        },
    }


def _inline_image(cid, *, data=None, attachment_id=None, mime_type="image/png"):
    body = {"size": 42}
    if data is not None:
        body["data"] = data
    if attachment_id is not None:
        body["attachmentId"] = attachment_id
    return {
        "mimeType": mime_type,
        "filename": f"{cid}.png",
        "headers": [
            {"name": "Content-ID", "value": f"<{cid}>"},
            {"name": "Content-Disposition", "value": "inline"},
        ],
        "body": body,
    }


def _build_service(*, message_responses=None, attachment_responses=None):
    message_responses = message_responses or {}
    attachment_responses = attachment_responses or {}
    service = Mock()

    def message_get(**kwargs):
        request = Mock()
        request.execute.return_value = message_responses[
            (kwargs["id"], kwargs["format"])
        ]
        return request

    def attachment_get(**kwargs):
        request = Mock()
        response = attachment_responses[kwargs["id"]]
        if isinstance(response, Exception):
            request.execute.side_effect = response
        else:
            request.execute.return_value = response
        return request

    service.users().messages().get.side_effect = message_get
    service.users().messages().attachments().get.side_effect = attachment_get
    return service


def _http_error(status: int) -> HttpError:
    resp = Mock(status=status, reason="boom")
    return HttpError(resp, b"boom")


@pytest.fixture
def export_dir(monkeypatch, tmp_path):
    """Point the default export directory at a temp dir."""
    target = tmp_path / "exports"
    monkeypatch.setenv("WORKSPACE_MCP_EXPORT_DIR", str(target))
    return target


def _saved_path(result: str) -> Path:
    marker = "Successfully saved message to: "
    first = result.splitlines()[0]
    assert first.startswith(marker), result
    return Path(first[len(marker) :])


# --- _export_filename ------------------------------------------------------------


def test_export_filename_uses_message_date_and_subject():
    headers = {"Subject": "Hello world", "Date": "Fri, 28 Mar 2026 10:00:00 -0400"}
    assert _export_filename(headers, ".html") == "2026-03-28-Hello world.html"


def test_export_filename_falls_back_when_date_missing_or_invalid():
    for headers in ({"Subject": "x"}, {"Subject": "x", "Date": "not a date"}):
        name = _export_filename(headers, ".eml")
        assert name.endswith("-x.eml")
        assert len(name.split("-x.eml")[0]) == len("2026-01-01")


def test_export_filename_caps_subject_and_handles_blank_subject():
    assert _export_filename({"Subject": "s" * 500}, ".html").endswith(
        "-" + "s" * 80 + ".html"
    )
    assert _export_filename({"Subject": "   "}, ".html").endswith("-No_Subject.html")
    assert _export_filename({"Subject": 'Re: a/b:c*"d"?'}, ".pdf").endswith(
        "-Re abcd.pdf"
    )


# --- _build_archival_html --------------------------------------------------------


def test_archival_html_escapes_headers_and_preserves_body():
    headers = {
        "Subject": "Quarterly <report>",
        "From": "Alice <alice@example.com>",
        "To": "bob@example.com",
        "Date": "Fri, 28 Mar 2026 10:00:00 -0400",
        "Message-ID": "<msg-1@example.com>",
    }
    body = '<p style="color:red">Body <b>kept</b> verbatim</p>'
    doc = _build_archival_html(headers, body)

    assert doc.startswith("<!DOCTYPE html>")
    assert "<title>Quarterly &lt;report&gt;</title>" in doc
    assert "Alice &lt;alice@example.com&gt;" in doc
    assert "Message-ID: &lt;msg-1@example.com&gt;" in doc
    assert body in doc
    assert "Cc:" not in doc


# --- _embed_inline_images --------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_inline_images_uses_inline_data_and_fetches_attachments():
    html = (
        "<img src=\"cid:logo\"> <img src='cid:photo'> "
        '<div style="background:url(cid:logo)"></div>'
    )
    payload = _full_response(
        "m",
        html=html,
        inline_images=[
            _inline_image("logo", data=_b64url(PNG_BYTES)),
            _inline_image("photo", attachment_id="att-1", mime_type="image/jpeg"),
        ],
    )["payload"]
    service = _build_service(
        attachment_responses={"att-1": {"data": _b64url(b"jpeg-bytes")}}
    )

    rewritten, notes = await _embed_inline_images(service, "m", payload, html)

    assert notes == []
    assert "cid:" not in rewritten
    assert rewritten.count(PNG_DATA_URI) == 2
    jpeg_uri = f"data:image/jpeg;base64,{base64.b64encode(b'jpeg-bytes').decode()}"
    assert f"src='{jpeg_uri}'" in rewritten
    service.users().messages().attachments().get.assert_called_once_with(
        userId="me", messageId="m", id="att-1"
    )


@pytest.mark.asyncio
async def test_embed_inline_images_does_not_clobber_prefix_matching_cids():
    html = '<img src="cid:logo"><img src="cid:logo2">'
    payload = _full_response(
        "m",
        html=html,
        inline_images=[
            _inline_image("logo", data=_b64url(b"one")),
            _inline_image("logo2", data=_b64url(b"two")),
        ],
    )["payload"]

    rewritten, _ = await _embed_inline_images(Mock(), "m", payload, html)

    one = base64.b64encode(b"one").decode()
    two = base64.b64encode(b"two").decode()
    assert f'src="data:image/png;base64,{one}"' in rewritten
    assert f'src="data:image/png;base64,{two}"' in rewritten


@pytest.mark.asyncio
async def test_embed_inline_images_reports_failures_and_keeps_going():
    html = '<img src="cid:ok"><img src="cid:broken">'
    payload = _full_response(
        "m",
        html=html,
        inline_images=[
            _inline_image("ok", data=_b64url(PNG_BYTES)),
            _inline_image("broken", attachment_id="att-x"),
        ],
    )["payload"]
    service = _build_service(attachment_responses={"att-x": _http_error(404)})

    rewritten, notes = await _embed_inline_images(service, "m", payload, html)

    assert PNG_DATA_URI in rewritten
    assert 'src="cid:broken"' in rewritten
    assert notes == ["Inline image 'broken' could not be embedded."]


@pytest.mark.asyncio
async def test_embed_inline_images_skips_unreferenced_parts_without_fetching():
    html = "<p>no images here</p>"
    payload = _full_response(
        "m", html=html, inline_images=[_inline_image("unused", attachment_id="a")]
    )["payload"]
    service = _build_service()

    rewritten, notes = await _embed_inline_images(service, "m", payload, html)

    assert rewritten == html and notes == []
    service.users().messages().attachments().get.assert_not_called()


# --- export_gmail_message: html / eml (no rendering toolchain needed) -------------


@pytest.mark.asyncio
async def test_export_html_default_path_writes_self_contained_document(export_dir):
    html = '<p>Hi <img src="cid:logo"></p>'
    service = _build_service(
        message_responses={
            ("msg-1", "metadata"): _metadata_response("msg-1"),
            ("msg-1", "full"): _full_response(
                "msg-1",
                text="Hi",
                html=html,
                inline_images=[_inline_image("logo", attachment_id="att-1")],
            ),
        },
        attachment_responses={"att-1": {"data": _b64url(PNG_BYTES)}},
    )

    result = await _unwrap(export_gmail_message)(
        service=service, message_id="msg-1", user_google_email="u@example.com"
    )

    saved = _saved_path(result)
    assert saved.parent == export_dir.resolve()
    assert saved.name == "2026-03-28-Quarterly report.html"
    assert "Format: html" in result
    assert "Subject: Quarterly <report>" in result
    assert "<img" not in result  # body never inlined in the response

    doc = saved.read_text(encoding="utf-8")
    assert doc.startswith("<!DOCTYPE html>")
    assert "Quarterly &lt;report&gt;" in doc
    assert f'<img src="{PNG_DATA_URI}">' in doc
    assert "cid:" not in doc


@pytest.mark.asyncio
async def test_export_output_path_variants(export_dir, tmp_path):
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m", headers=_headers(Subject="S")),
            ("m", "full"): _full_response("m", html="<p>x</p>"),
        }
    )
    run = _unwrap(export_gmail_message)
    common = dict(service=service, message_id="m", user_google_email="u@example.com")

    # Directory-like (trailing slash) -> that dir + default name
    target_dir = tmp_path / "archive"
    r = await run(**common, output_path=str(target_dir) + "/")
    assert _saved_path(r) == (target_dir / "2026-03-28-S.html").resolve()

    # Existing directory without trailing slash
    r = await run(**common, output_path=str(target_dir))
    assert _saved_path(r) == (target_dir / "2026-03-28-S.html").resolve()

    # Bare filename -> export dir
    r = await run(**common, output_path="custom.html")
    assert _saved_path(r) == (export_dir / "custom.html").resolve()

    # Bare filename without extension -> extension appended
    r = await run(**common, output_path="custom2")
    assert _saved_path(r) == (export_dir / "custom2.html").resolve()

    # Full path used exactly
    full = tmp_path / "deep" / "nested" / "exact.html"
    r = await run(**common, output_path=str(full))
    assert _saved_path(r) == full.resolve()
    assert full.exists()


@pytest.mark.asyncio
async def test_export_refuses_sensitive_output_paths(export_dir, tmp_path):
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m"),
            ("m", "full"): _full_response("m", html="<p>x</p>"),
        }
    )
    with pytest.raises(UserInputError, match="not allowed"):
        await _unwrap(export_gmail_message)(
            service=service,
            message_id="m",
            user_google_email="u@example.com",
            output_path=str(tmp_path / ".env"),
        )
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_export_html_falls_back_to_escaped_plaintext(export_dir):
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m"),
            ("m", "full"): _full_response("m", text="1 < 2 & <b>not bold</b>"),
        }
    )

    result = await _unwrap(export_gmail_message)(
        service=service, message_id="m", user_google_email="u@example.com"
    )

    assert "Note: No HTML body present; rendered the plaintext body instead." in result
    doc = _saved_path(result).read_text(encoding="utf-8")
    assert "<pre>1 &lt; 2 &amp; &lt;b&gt;not bold&lt;/b&gt;</pre>" in doc


@pytest.mark.asyncio
async def test_export_errors_on_empty_message_and_writes_nothing(export_dir):
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m"),
            ("m", "full"): _full_response("m"),
        }
    )
    result = await _unwrap(export_gmail_message)(
        service=service, message_id="m", user_google_email="u@example.com"
    )
    assert result == "Error: Message has no readable body content to export."
    assert not export_dir.exists() or list(export_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_export_eml_writes_raw_message(export_dir):
    raw_mime = "From: alice@example.com\r\nSubject: Quarterly <report>\r\n\r\nBody!"
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m"),
            ("m", "raw"): {"raw": _b64url_unpadded(raw_mime.encode())},
        }
    )

    result = await _unwrap(export_gmail_message)(
        service=service, message_id="m", user_google_email="u@example.com", format="eml"
    )

    saved = _saved_path(result)
    assert saved.suffix == ".eml"
    assert "Format: eml" in result
    assert "Body!" not in result
    assert saved.read_bytes().decode() == raw_mime
    formats = [
        c.kwargs["format"] for c in service.users().messages().get.call_args_list
    ]
    assert formats == ["metadata", "raw"]


@pytest.mark.asyncio
async def test_export_rejects_oversized_message_before_fetching_body(
    monkeypatch, export_dir
):
    monkeypatch.setenv("WORKSPACE_MCP_MAX_FILE_BYTES", "100")
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m", size_estimate=500),
            ("m", "full"): _full_response("m", html="<p>should not fetch</p>"),
        }
    )

    result = await _unwrap(export_gmail_message)(
        service=service, message_id="m", user_google_email="u@example.com"
    )

    assert result.startswith("Error:")
    formats = [
        c.kwargs["format"] for c in service.users().messages().get.call_args_list
    ]
    assert formats == ["metadata"]


@pytest.mark.asyncio
async def test_export_pdf_reports_missing_toolchain_cleanly(monkeypatch, export_dir):
    """Without WeasyPrint the tool returns an install hint rather than a traceback."""
    from core.visual_rendering import RenderingDependencyError

    async def _boom(_html):
        raise RenderingDependencyError("PDF rendering requires WeasyPrint ...")

    monkeypatch.setattr(gmail_tools, "html_to_pdf_bytes", _boom)
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m"),
            ("m", "full"): _full_response("m", html="<p>x</p>"),
        }
    )

    result = await _unwrap(export_gmail_message)(
        service=service, message_id="m", user_google_email="u@example.com", format="pdf"
    )

    assert result.startswith("Error: PDF rendering requires WeasyPrint")
    assert not export_dir.exists() or list(export_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_export_pdf_and_png_use_rendering_pipeline(monkeypatch, export_dir):
    """PDF/PNG formats feed the archival HTML into the shared renderers."""
    seen = {}

    async def fake_pdf(document):
        seen["pdf_html"] = document
        return b"%PDF-1.7 fake"

    async def fake_png(document):
        seen["png_html"] = document
        return PNG_BYTES

    monkeypatch.setattr(gmail_tools, "html_to_pdf_bytes", fake_pdf)
    monkeypatch.setattr(gmail_tools, "html_to_png_bytes", fake_png)
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m"),
            ("m", "full"): _full_response("m", html="<p>Rendered</p>"),
        }
    )
    run = _unwrap(export_gmail_message)
    common = dict(service=service, message_id="m", user_google_email="u@example.com")

    pdf_result = await run(**common, format="pdf")
    pdf_path = _saved_path(pdf_result)
    assert pdf_path.suffix == ".pdf" and pdf_path.read_bytes() == b"%PDF-1.7 fake"
    assert "<p>Rendered</p>" in seen["pdf_html"] and "Quarterly" in seen["pdf_html"]

    png_result = await run(**common, format="png")
    png_path = _saved_path(png_result)
    assert png_path.suffix == ".png" and png_path.read_bytes() == PNG_BYTES
    assert "<p>Rendered</p>" in seen["png_html"]


# --- get_gmail_message_visual ----------------------------------------------------


@pytest.mark.asyncio
async def test_visual_renders_requested_page(monkeypatch):
    calls = {}

    async def fake_pdf(document):
        calls["html"] = document
        return b"%PDF fake"

    async def fake_render(pdf_bytes, page_number, max_dimension):
        calls["render"] = (pdf_bytes, page_number, max_dimension)
        return ["<image>", "Displaying page 2 of 3."]

    monkeypatch.setattr(gmail_tools, "html_to_pdf_bytes", fake_pdf)
    monkeypatch.setattr(gmail_tools, "render_document_page", fake_render)
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m"),
            ("m", "full"): _full_response("m", text="plain only"),
        }
    )

    result = await _unwrap(get_gmail_message_visual)(
        service=service,
        user_google_email="u@example.com",
        message_id="m",
        page_number=2,
        max_dimension=800,
    )

    assert calls["render"] == (b"%PDF fake", 2, 800)
    assert "<pre>plain only</pre>" in calls["html"]
    assert result[0] == "<image>"
    assert result[1].startswith("Displaying page 2 of 3.")
    assert "Note: No HTML body present" in result[1]


@pytest.mark.asyncio
async def test_visual_returns_error_list_for_empty_message():
    service = _build_service(
        message_responses={
            ("m", "metadata"): _metadata_response("m"),
            ("m", "full"): _full_response("m"),
        }
    )
    result = await _unwrap(get_gmail_message_visual)(
        service=service, user_google_email="u@example.com", message_id="m"
    )
    assert result == ["Error: Message has no readable body content to export."]
