"""Tests for core.export_paths (local export path resolution)."""

import os
from pathlib import Path

import pytest

from core.export_paths import (
    get_export_dir,
    resolve_output_path,
    sanitize_export_filename,
    write_export,
)


@pytest.fixture
def export_dir(monkeypatch, tmp_path):
    target = tmp_path / "exports"
    monkeypatch.setenv("WORKSPACE_MCP_EXPORT_DIR", str(target))
    return target.resolve()


def test_get_export_dir_defaults_to_tempdir(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKSPACE_MCP_EXPORT_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    tempfile.tempdir = None  # force tempfile to re-read TMPDIR
    try:
        assert get_export_dir() == tmp_path.resolve()
    finally:
        tempfile.tempdir = None


def test_get_export_dir_honours_env_and_expands_user(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_EXPORT_DIR", "~/mcp-exports")
    assert get_export_dir() == (Path.home() / "mcp-exports").resolve()


def test_sanitize_export_filename():
    assert sanitize_export_filename('a<b>:"c/d\\e|f?g*h') == "abcdefgh"
    assert sanitize_export_filename("  spaced   out  ") == "spaced out"
    assert sanitize_export_filename("") == "export"
    assert sanitize_export_filename("...") == "export"
    assert len(sanitize_export_filename("x" * 500)) == 100


def test_resolve_none_uses_export_dir_and_default_name(export_dir):
    assert resolve_output_path(None, "default.pdf") == export_dir / "default.pdf"
    assert resolve_output_path("   ", "default.pdf") == export_dir / "default.pdf"


def test_resolve_directory_like_paths(export_dir, tmp_path):
    existing = tmp_path / "out"
    existing.mkdir()
    assert resolve_output_path(str(existing), "d.pdf") == existing.resolve() / "d.pdf"
    assert (
        resolve_output_path(str(tmp_path / "new") + "/", "d.pdf")
        == (tmp_path / "new" / "d.pdf").resolve()
    )
    assert (
        resolve_output_path(str(tmp_path / "new") + os.sep, "d.pdf")
        == (tmp_path / "new" / "d.pdf").resolve()
    )


def test_resolve_bare_filename_goes_to_export_dir(export_dir):
    assert resolve_output_path("mine.pdf", "d.pdf") == export_dir / "mine.pdf"
    # Missing extension -> default extension appended
    assert resolve_output_path("mine", "d.pdf") == export_dir / "mine.pdf"
    # Different extension is respected
    assert resolve_output_path("mine.html", "d.pdf") == export_dir / "mine.html"


def test_resolve_full_path_used_verbatim(export_dir, tmp_path):
    target = tmp_path / "a" / "b.pdf"
    assert resolve_output_path(str(target), "d.pdf") == target.resolve()
    assert (
        resolve_output_path(str(tmp_path / "a" / "b"), "d.pdf")
        == (tmp_path / "a" / "b.pdf").resolve()
    )


def test_resolve_expands_user(export_dir):
    assert (
        resolve_output_path("~/x/y.pdf", "d.pdf")
        == (Path.home() / "x" / "y.pdf").resolve()
    )


def test_resolve_rejects_sensitive_locations(export_dir, tmp_path):
    with pytest.raises(ValueError, match="not allowed"):
        resolve_output_path(str(tmp_path / ".env"), "d.pdf")
    with pytest.raises(ValueError, match="not allowed"):
        resolve_output_path(str(tmp_path / ".ssh" / "id_rsa.pdf"), "d.pdf")
    with pytest.raises(ValueError, match="not allowed"):
        resolve_output_path("credentials.json", "d.pdf")


def test_write_export_creates_parents_and_overwrites(tmp_path):
    target = tmp_path / "deep" / "er" / "f.bin"
    write_export(target, b"one")
    write_export(target, b"two")
    assert target.read_bytes() == b"two"
