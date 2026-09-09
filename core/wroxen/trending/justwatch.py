"""JustWatch country popular titles via simple-justwatch-python-api.

Fetches MOVIE or SHOW via object_types (no mix). Service may call twice
to fill both lists in cache; UI lets user pick Movies or Shows.

Global Trending does NOT use this module — use TMDB /trending/all/day.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


class JustWatchError(Exception):
    """Soft failure — safe to catch and keep previous cache."""


_LANG: Dict[str, str] = {
    "IN": "en", "US": "en", "GB": "en", "CA": "en", "AU": "en",
    "JP": "ja", "KR": "ko", "CN": "zh", "DE": "de", "FR": "fr",
    "ES": "es", "IT": "it", "BR": "pt", "MX": "es", "TR": "tr",
    "ID": "en", "PH": "en", "TH": "en", "NL": "nl", "RU": "ru",
    "SA": "ar", "AE": "en",
}

_COUNTRY_ALIAS: Dict[str, str] = {
    "CN": "HK",
}

_TYPE_MAP = {
    "movie": ["MOVIE"],
    "movies": ["MOVIE"],
    "tv": ["SHOW"],
    "show": ["SHOW"],
    "shows": ["SHOW"],
}


def _sync_popular(
    country: str,
    language: str,
    count: int,
    object_types: Optional[Sequence[str]] = None,
) -> List[Any]:
    try:
        from simplejustwatchapi import popular
    except ImportError as e:
        raise JustWatchError(
            "simple-justwatch-python-api is not installed. "
            "Add it to requirements.txt and reinstall."
        ) from e
    try:
        kwargs: Dict[str, Any] = {
            "country": country,
            "language": language,
            "count": count,
        }
        if object_types:
            kwargs["object_types"] = list(object_types)
        return list(popular(**kwargs) or [])
    except Exception as e:
        raise JustWatchError(f"library popular(): {e}") from e


def _norm_entry(entry: Any) -> Optional[Dict[str, Any]]:
    try:
        title = (getattr(entry, "title", None) or "").strip()
        if not title:
            return None
        otype = str(getattr(entry, "object_type", "") or "").upper()
        if "SHOW" in otype or "TV" in otype or "SERIES" in otype:
            media_type = "tv"
        else:
            media_type = "movie"
        year = getattr(entry, "release_year", None)
        try:
            year_i = int(year) if year is not None else None
        except Exception:
            year_i = None
        tmdb_raw = getattr(entry, "tmdb_id", None)
        tmdb_id = None
        if tmdb_raw is not None:
            try:
                tmdb_id = int(tmdb_raw)
            except Exception:
                tmdb_id = None
        jw_id = getattr(entry, "entry_id", None) or getattr(entry, "object_id", None)
        return {
            "title": title,
            "original_title": title,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "jw_id": jw_id,
            "year": year_i,
            "provider": "justwatch",
        }
    except Exception:
        return None


def _dedupe_norm(raw_list: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for entry in raw_list:
        row = _norm_entry(entry)
        if not row:
            continue
        key = (row["media_type"], row["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


async def fetch_popular_country(
    country_code: str,
    *,
    limit: int = 30,
    language: Optional[str] = None,
    media_type: str = "all",
) -> List[Dict[str, Any]]:
    """Fetch popular titles for ISO country.

    media_type:
      - ``movie`` → only MOVIE
      - ``tv``    → only SHOW
      - ``all``   → movies then shows (concat, not interleaved) up to limit each side
                    capped at ``limit`` total preferring balanced half/half storage
    """
    cc = (country_code or "").upper().strip()
    if len(cc) != 2:
        raise JustWatchError(f"invalid country code: {country_code!r}")

    jw_cc = _COUNTRY_ALIAS.get(cc, cc)
    lang = (language or _LANG.get(cc) or _LANG.get(jw_cc) or "en").lower()
    limit = max(1, min(int(limit or 30), 50))
    mt = (media_type or "all").lower().strip()

    async def _one(otypes: List[str], n: int) -> List[Dict[str, Any]]:
        try:
            raw = await asyncio.to_thread(
                _sync_popular, jw_cc, lang, n, otypes
            )
            return _dedupe_norm(raw)
        except JustWatchError:
            raise
        except Exception as e:
            raise JustWatchError(f"fetch {otypes}: {e}") from e

    if mt in _TYPE_MAP:
        items = await _one(_TYPE_MAP[mt], limit)
        if not items:
            raise JustWatchError(f"empty popular {mt} list for {cc}")
        logger.info("JustWatch %s · type=%s · %s titles", cc, mt, len(items))
        return items[:limit]

    # all → fetch both, store movies then shows (UI filters later)
    half = max(limit, 30)
    movies: List[Dict[str, Any]] = []
    shows: List[Dict[str, Any]] = []
    errors: List[str] = []
    try:
        movies = await _one(["MOVIE"], half)
    except JustWatchError as e:
        errors.append(str(e))
        logger.warning("JustWatch MOVIE %s: %s", cc, e)
    try:
        shows = await _one(["SHOW"], half)
    except JustWatchError as e:
        errors.append(str(e))
        logger.warning("JustWatch SHOW %s: %s", cc, e)

    out = movies + shows
    if not out:
        msg = "; ".join(errors) if errors else "empty"
        raise JustWatchError(f"empty popular list for {cc}: {msg}")
    logger.info(
        "JustWatch %s · movies=%s shows=%s total=%s",
        cc, len(movies), len(shows), len(out),
    )
    return out
