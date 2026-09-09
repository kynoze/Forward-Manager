"""TMDB data fetch — background updater only. Never call from user callbacks.

Endpoints used
--------------
* Global:  GET /3/trending/all/day
  True TMDB short-window trending (no country param exists on this endpoint).

* Country (ISO 3166-1): discover popular by origin country
  GET /3/discover/movie?with_origin_country=XX&sort_by=popularity.desc
  GET /3/discover/tv?with_origin_country=XX&sort_by=popularity.desc
  Merge movie+TV, cap at ``limit``. This is popularity-by-origin, not
  global trending scores filtered by region (TMDB does not offer that).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

BASE = "https://api.themoviedb.org/3"

# Cooldown seconds after HTTP 429 / rate-limit signal per key
_RATE_COOLDOWN_SEC = 90.0


class TmdbRateLimitError(Exception):
    """TMDB rate limit (HTTP 429) for the current key."""


class TmdbKeyPool:
    """Round-robin TMDB API keys; skip keys cooling down after 429."""

    def __init__(self, keys: Sequence[str]) -> None:
        self._keys = [k.strip() for k in keys if (k or "").strip()]
        self._i = 0
        self._cooldown_until: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    def __bool__(self) -> bool:
        return bool(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def primary(self) -> str:
        return self._keys[0] if self._keys else ""

    async def acquire(self) -> str:
        """Next usable key (not in cooldown). Raises RuntimeError if none."""
        if not self._keys:
            raise RuntimeError("TMDB_API_KEY / TMDB_API_KEYS not configured")
        async with self._lock:
            now = time.monotonic()
            n = len(self._keys)
            for _ in range(n):
                key = self._keys[self._i % n]
                self._i = (self._i + 1) % n
                until = self._cooldown_until.get(key, 0.0)
                if now >= until:
                    return key
            # All cooling — pick soonest and wait is caller's choice; return that key
            best = min(self._keys, key=lambda k: self._cooldown_until.get(k, 0.0))
            wait = max(0.0, self._cooldown_until.get(best, 0.0) - now)
            if wait > 0:
                logger.warning(
                    "All %s TMDB keys rate-limited — using next in ~%.0fs",
                    n, wait,
                )
            return best

    async def mark_rate_limited(self, key: str) -> None:
        async with self._lock:
            self._cooldown_until[key] = time.monotonic() + _RATE_COOLDOWN_SEC
            logger.warning(
                "TMDB key …%s rate-limited — cooldown %ss",
                key[-6:] if len(key) >= 6 else key,
                int(_RATE_COOLDOWN_SEC),
            )


_pool: Optional[TmdbKeyPool] = None


def get_key_pool(keys: Optional[Sequence[str]] = None) -> TmdbKeyPool:
    """Module pool from explicit keys or Config."""
    global _pool
    if keys is not None:
        _pool = TmdbKeyPool(keys)
        return _pool
    if _pool is not None and _pool:
        return _pool
    try:
        from config import Config
        kl = Config.tmdb_api_keys()
    except Exception:
        kl = []
    _pool = TmdbKeyPool(kl)
    return _pool


def _year_from(date_str: Optional[str]) -> Optional[int]:
    if not date_str or len(str(date_str)) < 4:
        return None
    try:
        return int(str(date_str)[:4])
    except Exception:
        return None


def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "WinkiuCloner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        if e.code == 429 or e.code == 403 and "limit" in body.lower():
            raise TmdbRateLimitError(f"HTTP {e.code}: {body or e.reason}") from e
        raise


async def _get_json(url: str) -> dict:
    return await asyncio.to_thread(_http_get, url)


async def _get_json_pooled(
    path_and_query_builder,
    *,
    pool: Optional[TmdbKeyPool] = None,
    max_retries: int = 6,
) -> dict:
    """Call builder(api_key) -> url; rotate key on rate limit."""
    p = pool or get_key_pool()
    if not p:
        raise RuntimeError("TMDB_API_KEY / TMDB_API_KEYS not configured")
    last_err: Optional[Exception] = None
    tried = set()
    for attempt in range(max(1, max_retries)):
        key = await p.acquire()
        url = path_and_query_builder(key)
        try:
            return await _get_json(url)
        except TmdbRateLimitError as e:
            last_err = e
            await p.mark_rate_limited(key)
            tried.add(key)
            if len(tried) >= len(p) and attempt >= len(p) - 1:
                await asyncio.sleep(min(15.0, 2.0 * (attempt + 1)))
            continue
        except Exception as e:
            last_err = e
            raise
    raise RuntimeError(f"TMDB all keys exhausted: {last_err}") from last_err


def _norm_movie(row: dict) -> Optional[Dict[str, Any]]:
    title = (row.get("title") or "").strip()
    if not title:
        return None
    return {
        "title": title,
        "original_title": (row.get("original_title") or title).strip(),
        "media_type": "movie",
        "tmdb_id": row.get("id"),
        "year": _year_from(row.get("release_date")),
    }


def _norm_tv(row: dict) -> Optional[Dict[str, Any]]:
    title = (row.get("name") or "").strip()
    if not title:
        return None
    return {
        "title": title,
        "original_title": (row.get("original_name") or title).strip(),
        "media_type": "tv",
        "tmdb_id": row.get("id"),
        "year": _year_from(row.get("first_air_date")),
    }


async def fetch_trending_all_day(
    api_key: Optional[str] = None,
    *,
    limit: int = 30,
    pool: Optional[TmdbKeyPool] = None,
) -> List[Dict[str, Any]]:
    """Global TMDB trending (movies + TV). Uses key pool when available."""
    p = pool or (TmdbKeyPool([api_key]) if (api_key or "").strip() else get_key_pool())
    if not p:
        raise RuntimeError("TMDB_API_KEY is not configured")

    def _url(key: str) -> str:
        params = urllib.parse.urlencode({"api_key": key, "language": "en-US"})
        return f"{BASE}/trending/all/day?{params}"

    data = await _get_json_pooled(_url, pool=p)
    out: List[Dict[str, Any]] = []
    for row in data.get("results") or []:
        mtype = (row.get("media_type") or "").lower()
        if mtype == "movie":
            item = _norm_movie(row)
        elif mtype == "tv":
            item = _norm_tv(row)
        else:
            continue
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


async def fetch_popular_by_origin(
    api_key: Optional[str] = None,
    country_code: str = "",
    *,
    limit: int = 30,
    pool: Optional[TmdbKeyPool] = None,
) -> List[Dict[str, Any]]:
    """Popular movies+TV with origin country = ISO code. Not global trending."""
    p = pool or (TmdbKeyPool([api_key]) if (api_key or "").strip() else get_key_pool())
    if not p:
        raise RuntimeError("TMDB_API_KEY is not configured")
    cc = (country_code or "").upper().strip()
    if len(cc) != 2:
        raise ValueError(f"invalid country code: {country_code!r}")

    def _movie_url(key: str) -> str:
        q = urllib.parse.urlencode({
            "api_key": key,
            "language": "en-US",
            "sort_by": "popularity.desc",
            "with_origin_country": cc,
            "page": "1",
        })
        return f"{BASE}/discover/movie?{q}"

    def _tv_url(key: str) -> str:
        q = urllib.parse.urlencode({
            "api_key": key,
            "language": "en-US",
            "sort_by": "popularity.desc",
            "with_origin_country": cc,
            "page": "1",
        })
        return f"{BASE}/discover/tv?{q}"

    movie_data, tv_data = await asyncio.gather(
        _get_json_pooled(_movie_url, pool=p),
        _get_json_pooled(_tv_url, pool=p),
    )
    merged: List[Dict[str, Any]] = []
    seen = set()
    # Interleave by list order (each list already popularity-sorted)
    movies = [x for x in (movie_data.get("results") or []) if x]
    tvs = [x for x in (tv_data.get("results") or []) if x]
    i = j = 0
    while len(merged) < limit and (i < len(movies) or j < len(tvs)):
        # Prefer movie then tv alternating for variety
        pick_movie = (len(merged) % 2 == 0 and i < len(movies)) or j >= len(tvs)
        if pick_movie and i < len(movies):
            item = _norm_movie(movies[i])
            i += 1
        elif j < len(tvs):
            item = _norm_tv(tvs[j])
            j += 1
        else:
            break
        if not item:
            continue
        key = (item["media_type"], item.get("tmdb_id") or item["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:limit]


async def fetch_region_titles(
    api_key: str,
    region: str,
    *,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """Global → TMDB trending. Country regions are handled by JustWatch (see service).

    This function only serves Global. Country codes raise ValueError so the
    service layer can call JustWatch instead.
    """
    region = (region or "global").lower()
    if region in ("global", "all", ""):
        return await fetch_trending_all_day(api_key, limit=limit)
    raise ValueError(
        f"TMDB does not provide country trending for {region!r}; use JustWatch"
    )


# ── English display-title resolve (JustWatch non-Latin titles) ─────────────

# CJK + Hangul + Hiragana/Katakana + fullwidth — users struggle to read these
_SCRIPT_RANGES = (
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF66, 0xFF9D),  # Halfwidth Katakana
)


def needs_english_title(title: str) -> bool:
    """True if title is mostly East-Asian script (JP/KR/CN users need English)."""
    if not title or not str(title).strip():
        return False
    s = str(title).strip()
    letters = 0
    script = 0
    for ch in s:
        o = ord(ch)
        if ch.isascii() and ch.isalpha():
            letters += 1
            continue
        for a, b in _SCRIPT_RANGES:
            if a <= o <= b:
                script += 1
                letters += 1
                break
    if letters == 0:
        return False
    return (script / letters) >= 0.35


async def _tmdb_detail_en(
    api_key: Optional[str],
    media_type: str,
    tmdb_id: int,
    *,
    pool: Optional[TmdbKeyPool] = None,
) -> Optional[str]:
    """GET /movie|tv/{id}?language=en-US → English title/name."""
    mt = "tv" if (media_type or "").lower() in ("tv", "show", "series") else "movie"
    p = pool or (TmdbKeyPool([api_key]) if (api_key or "").strip() else get_key_pool())

    def _url(key: str) -> str:
        q = urllib.parse.urlencode({"api_key": key, "language": "en-US"})
        return f"{BASE}/{mt}/{int(tmdb_id)}?{q}"

    try:
        data = await _get_json_pooled(_url, pool=p)
    except Exception as e:
        logger.debug("TMDB detail %s/%s: %s", mt, tmdb_id, e)
        return None
    if mt == "tv":
        title = (data.get("name") or data.get("original_name") or "").strip()
    else:
        title = (data.get("title") or data.get("original_title") or "").strip()
    if title and not needs_english_title(title):
        return title
    if title:
        return title
    return None


async def _tmdb_search_en(
    api_key: Optional[str],
    query: str,
    *,
    media_type: Optional[str] = None,
    year: Optional[int] = None,
    pool: Optional[TmdbKeyPool] = None,
) -> Optional[tuple]:
    """Search multi/movie/tv; return (english_title, tmdb_id, media_type) or None."""
    p = pool or (TmdbKeyPool([api_key]) if (api_key or "").strip() else get_key_pool())
    path = "/search/multi"
    mt = (media_type or "").lower()
    if mt in ("movie", "tv"):
        path = f"/search/{mt}"

    def _url(key: str) -> str:
        qparams: Dict[str, str] = {
            "api_key": key,
            "language": "en-US",
            "query": query,
            "page": "1",
            "include_adult": "false",
        }
        if year:
            qparams["year"] = str(year)
        if mt == "movie" and year:
            qparams["primary_release_year"] = str(year)
        elif mt == "tv" and year:
            qparams["first_air_year"] = str(year)
        return f"{BASE}{path}?{urllib.parse.urlencode(qparams)}"

    try:
        data = await _get_json_pooled(_url, pool=p)
    except Exception as e:
        logger.debug("TMDB search %r: %s", query[:40], e)
        return None
    results = data.get("results") or []
    if not results:
        return None
    # Prefer matching media_type + year when possible
    def _score(row: dict) -> int:
        sc = 0
        rmt = (row.get("media_type") or mt or "").lower()
        if mt and rmt == mt:
            sc += 5
        if rmt in ("movie", "tv"):
            sc += 2
        y = _year_from(row.get("release_date") or row.get("first_air_date"))
        if year and y and abs(y - year) <= 1:
            sc += 3
        sc += int(row.get("popularity") or 0) // 10
        return sc

    results = sorted(results, key=_score, reverse=True)
    for row in results[:5]:
        rmt = (row.get("media_type") or mt or "movie").lower()
        if rmt not in ("movie", "tv"):
            continue
        if rmt == "tv":
            en = (row.get("name") or "").strip()
        else:
            en = (row.get("title") or "").strip()
        if not en:
            continue
        if needs_english_title(en):
            continue  # still script — skip
        tid = row.get("id")
        try:
            tid_i = int(tid) if tid is not None else None
        except Exception:
            tid_i = None
        return en, tid_i, rmt
    return None


async def enrich_titles_english(
    api_key: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    *,
    max_lookups: int = 20,
    pool: Optional[TmdbKeyPool] = None,
) -> List[Dict[str, Any]]:
    """Rewrite non-Latin ``title`` to English via TMDB when possible.

    - Prefer ``tmdb_id`` detail (accurate).
    - Else limited search by original title + year.
    - Always keeps ``original_title``; sets ``title`` to English display.
    - Soft-fails per item — never raises for network blips.
    - Uses multi-key pool: on 429 the next key is tried automatically.
    """
    items = items or []
    p = pool or (TmdbKeyPool([api_key]) if (api_key or "").strip() else get_key_pool())
    if not p or not items:
        return items

    lookups = 0
    out: List[Dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        title = (item.get("title") or "").strip()
        original = (item.get("original_title") or title).strip()
        if not title:
            out.append(item)
            continue
        if not needs_english_title(title):
            out.append(item)
            continue
        if lookups >= max_lookups:
            out.append(item)
            continue

        en: Optional[str] = None
        tid = item.get("tmdb_id")
        mt = item.get("media_type") or "movie"
        year = item.get("year")

        if tid is not None:
            try:
                tid_i = int(tid)
            except Exception:
                tid_i = None
            if tid_i:
                lookups += 1
                en = await _tmdb_detail_en(None, str(mt), tid_i, pool=p)
                await asyncio.sleep(0.08)

        if not en:
            lookups += 1
            found = await _tmdb_search_en(
                None,
                original or title,
                media_type=str(mt),
                year=year if isinstance(year, int) else None,
                pool=p,
            )
            await asyncio.sleep(0.08)
            if found:
                en, new_tid, new_mt = found
                if new_tid and not item.get("tmdb_id"):
                    item["tmdb_id"] = new_tid
                if new_mt:
                    item["media_type"] = new_mt

        if en and en.strip() and not needs_english_title(en):
            item["original_title"] = original or title
            item["title"] = en.strip()
            item["title_en_source"] = "tmdb"
            logger.debug(
                "EN title: %r → %r (tmdb_id=%s)",
                (original or title)[:40],
                en[:40],
                item.get("tmdb_id"),
            )
        out.append(item)
    return out
