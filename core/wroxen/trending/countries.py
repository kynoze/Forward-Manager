"""Region helpers for Wroxen Trending (global TMDB only).

Country-wise trending has been removed. Only global TMDB
``/trending/all/day`` is supported.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# key -> (flag_or_emoji, display name, ISO-3166-1 or None for global)
COUNTRIES: Dict[str, Tuple[str, str, str | None]] = {
    "global": ("🌍", "Global", None),
}

# Stable order for UI / updater
COUNTRY_ORDER: List[str] = list(COUNTRIES.keys())


def _norm_region(code: str) -> str:
    """Normalize region key — only 'global' is valid now."""
    c = (code or "global").strip()
    if not c or c.lower() in ("global", "g"):
        return "global"
    return c.upper()


def region_label(code: str) -> str:
    meta = COUNTRIES.get(_norm_region(code)) or COUNTRIES["global"]
    return f"{meta[0]} {meta[1]}"


def is_valid_region(code: str) -> bool:
    return _norm_region(code) in COUNTRIES
