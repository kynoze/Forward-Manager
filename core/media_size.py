"""Minimum media size filter helpers for Jobs (and shared op_filters).

Convention: binary units
  1 MB = 1024 * 1024 bytes
  1 GB = 1024 ** 3 bytes
  1 TB = 1024 ** 4 bytes

Comparison (when enabled): media_size_bytes >= min_media_size_bytes → allow
Exact equality is allowed. Unknown / missing size does not crash; treated as pass
(so text-only / size-less messages are not bulk-skipped).
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

# Presets shown in UI (bytes)
SIZE_PRESETS = [
    ("10 MB", 10 * 1024 * 1024),
    ("30 MB", 30 * 1024 * 1024),
    ("50 MB", 50 * 1024 * 1024),
    ("100 MB", 100 * 1024 * 1024),
    ("200 MB", 200 * 1024 * 1024),
    ("500 MB", 500 * 1024 * 1024),
    ("1 GB", 1 * 1024 ** 3),
    ("1.5 GB", int(1.5 * 1024 ** 3)),
    ("2 GB", 2 * 1024 ** 3),
    ("4 GB", 4 * 1024 ** 3),
    ("5 GB", 5 * 1024 ** 3),
    ("10 GB", 10 * 1024 ** 3),
]

_SIZE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(tb|t|gb|g|mb|m|kb|k|b|bytes?)?\s*$",
    re.IGNORECASE,
)


def mb(n: float) -> int:
    return int(n * 1024 * 1024)


def gb(n: float) -> int:
    return int(n * (1024 ** 3))


def format_bytes(n: Optional[int]) -> str:
    """Human label for UI (binary)."""
    if n is None or n <= 0:
        return "Not Set"
    n = int(n)
    tb = 1024 ** 4
    g = 1024 ** 3
    m = 1024 ** 2
    if n >= tb and n % tb == 0:
        return f"{n // tb} TB"
    if n >= g:
        if n % g == 0:
            return f"{n // g} GB"
        # one decimal if clean half
        val = n / g
        if abs(val * 2 - round(val * 2)) < 1e-9:
            return f"{val:.1f} GB".replace(".0 ", " ")
        return f"{val:.2f} GB".rstrip("0").rstrip(".") + " GB"
    if n >= m:
        if n % m == 0:
            return f"{n // m} MB"
        return f"{n / m:.1f} MB"
    if n >= 1024:
        return f"{n // 1024} KB"
    return f"{n} B"


def parse_size_input(text: str) -> Tuple[Optional[int], Optional[str]]:
    """Parse user input into bytes.

    Returns (bytes, None) on success, or (None, error_message).
    Rejects zero, negative, and unit-less ambiguous values.
    """
    raw = (text or "").strip()
    if not raw:
        return None, "Empty value."
    m = _SIZE_RE.match(raw)
    if not m:
        return None, (
            "Invalid size.\n\n"
            "Examples:\n`100 MB`\n`500 MB`\n`1.5 GB`\n`2 GB`"
        )
    try:
        num = float(m.group(1))
    except ValueError:
        return None, "Invalid number."
    if num <= 0:
        return None, "Size must be greater than 0."
    unit = (m.group(2) or "").lower()
    if not unit:
        return None, (
            "Unit required (MB / GB / TB).\n\n"
            "Examples:\n`100 MB`\n`1.5 GB`"
        )
    if unit in ("tb", "t"):
        mult = 1024 ** 4
    elif unit in ("gb", "g"):
        mult = 1024 ** 3
    elif unit in ("mb", "m"):
        mult = 1024 ** 2
    elif unit in ("kb", "k"):
        mult = 1024
    else:
        mult = 1
    total = int(num * mult)
    if total <= 0:
        return None, "Size must be greater than 0."
    # Hard sanity ceiling (Telegram practical limit ~4GB docs historically; allow up to 20GB)
    if total > 20 * (1024 ** 3):
        return None, "Maximum allowed is 20 GB."
    return total, None


def get_message_file_size(message: Any) -> Optional[int]:
    """Telegram metadata size in bytes. None if unknown / not applicable."""
    if not message or getattr(message, "empty", False):
        return None

    def _sz(obj) -> Optional[int]:
        if obj is None:
            return None
        for attr in ("file_size", "size"):
            v = getattr(obj, attr, None)
            if v is not None:
                try:
                    n = int(v)
                    if n >= 0:
                        return n
                except (TypeError, ValueError):
                    pass
        return None

    for attr in (
        "document",
        "video",
        "audio",
        "animation",
        "voice",
        "video_note",
        "sticker",
    ):
        n = _sz(getattr(message, attr, None))
        if n is not None:
            return n

    # photo: largest PhotoSize
    photo = getattr(message, "photo", None)
    if photo is not None:
        n = _sz(photo)
        if n is not None:
            return n
        sizes = getattr(photo, "sizes", None)
        if sizes:
            best = None
            for item in sizes:
                s = _sz(item)
                if s is not None and (best is None or s > best):
                    best = s
            if best is not None:
                return best
    return None


def passes_size_filter(message: Any, settings: dict) -> Tuple[bool, str]:
    """Return (ok, reason). reason starts with media_size_ when filtered."""
    if not settings or not bool(settings.get("size_filter_enabled")):
        return True, "ok"
    try:
        minimum = int(settings.get("min_media_size") or 0)
    except (TypeError, ValueError):
        minimum = 0
    if minimum <= 0:
        return True, "ok"

    size = get_message_file_size(message)
    if size is None:
        # No reliable size (text / unknown) — do not invent or bulk-skip
        return True, "ok"
    if size < minimum:
        return (
            False,
            f"media_size_too_small:{size}<{minimum}",
        )
    return True, "ok"
