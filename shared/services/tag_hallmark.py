"""BIS hallmark mark for server-rendered tag HTML (embedded SVG above purity)."""

from __future__ import annotations

import base64
import os
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_HALLMARK_SVG_DEFAULT = _ASSETS / "BIS-Hallmark-Black.svg"
_HALLMARK_PNG_DEFAULT = _ASSETS / "bis_hallmark.png"
_HALLMARK_ADMIN_SVG = (
    Path(__file__).resolve().parents[2] / "jewel-admin-suite" / "public" / "BIS-Hallmark-Black.svg"
)

HALLMARK_FALLBACK_HTML = (
    '<svg class="hm-img hm-bis" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 32" aria-hidden="true">'
    '<path fill="#111" d="M14 2 L26 28 H2 Z"/>'
    '<circle cx="14" cy="19" r="4.2" fill="#111"/>'
    "</svg>"
)


def _hallmark_file_path() -> Path | None:
    custom = (os.getenv("TAG_HALLMARK_PATH") or "").strip()
    if custom:
        p = Path(custom)
        if p.is_file():
            return p
    for candidate in (_HALLMARK_SVG_DEFAULT, _HALLMARK_ADMIN_SVG, _HALLMARK_PNG_DEFAULT):
        if candidate.is_file():
            return candidate
    return None


def hallmark_mark_html(*, show_hallmark: bool = True) -> str:
    """Always print the BIS hallmark above purity (replaces the old triangle)."""
    del show_hallmark
    path = _hallmark_file_path()
    if path is None:
        return HALLMARK_FALLBACK_HTML
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    mime = "image/svg+xml" if suffix == ".svg" else "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return (
        f'<img class="hm-img hm-bis" src="data:{mime};base64,{b64}" '
        f'alt="" aria-hidden="true" />'
    )
