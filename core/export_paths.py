"""
Local-filesystem export paths for tools that save files where the user asks.

Unlike attachment storage (managed, auto-expiring, meant for hand-off to the
client), exports are the user's own files: they go where the user points and are
never cleaned up. The default directory is ``WORKSPACE_MCP_EXPORT_DIR`` when set,
otherwise the OS temporary directory.
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from core.utils import ensure_path_not_sensitive

logger = logging.getLogger(__name__)

EXPORT_DIR_ENV = "WORKSPACE_MCP_EXPORT_DIR"

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_FILENAME_STEM = 100


def get_export_dir() -> Path:
    """Directory used when the caller gives no path (or only a bare filename)."""
    configured = os.environ.get(EXPORT_DIR_ENV, "").strip()
    base = Path(configured).expanduser() if configured else Path(tempfile.gettempdir())
    return base.resolve()


def sanitize_export_filename(name: str, fallback: str = "export") -> str:
    """Make a string safe to use as a filename on Windows, macOS and Linux."""
    cleaned = _INVALID_FILENAME_CHARS.sub("", name or "")
    cleaned = " ".join(cleaned.split()).strip(" .")
    cleaned = cleaned[:_MAX_FILENAME_STEM].rstrip(" .")
    return cleaned or fallback


def resolve_output_path(output_path: Optional[str], default_filename: str) -> Path:
    """
    Turn the user's ``output_path`` argument into a concrete file path.

    Strategy (unchanged from the original tool so existing clients keep working):

    1. ``None``                              -> export dir / default filename
    2. Directory-like (trailing separator,
       or an existing directory)             -> that directory / default filename
    3. Bare filename (no path separator)     -> export dir / that filename
    4. Anything else                         -> exactly that path

    The final path is canonicalised and checked against the sensitive-location
    denylist (``.env``, ``~/.ssh``, ``/etc/passwd``, ...). If the caller omits an
    extension in cases 3/4, the default filename's extension is appended so the
    file opens with the right application.
    """
    default_ext = Path(default_filename).suffix

    if output_path is None or not output_path.strip():
        target = get_export_dir() / default_filename
    else:
        raw = output_path.strip()
        expanded = Path(raw).expanduser()
        looks_like_dir = raw.endswith(("/", os.sep)) or expanded.is_dir()
        has_separator = "/" in raw or os.sep in raw

        if looks_like_dir:
            target = expanded / default_filename
        elif not has_separator:
            target = get_export_dir() / raw
        else:
            target = expanded

        if not target.suffix and default_ext:
            target = target.with_name(target.name + default_ext)

    resolved = target.resolve()
    if resolved.is_dir():
        raise ValueError(f"Output path '{resolved}' is an existing directory.")
    ensure_path_not_sensitive(resolved)
    return resolved


def write_export(path: Path, content: bytes) -> Path:
    """Create parent directories and write ``content`` to ``path`` (overwriting)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    logger.info(f"Exported {len(content)} bytes to {path}")
    return path
