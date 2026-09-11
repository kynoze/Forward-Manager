"""Trending updater + cache reads. User path never calls TMDB."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from core.wroxen.trending import cache as tcache
from core.wroxen.trending.tmdb import fetch_trending_all_day

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_stop = False


def _interval() -> int:
    try:
        from config import Config
        return max(300, int(getattr(Config, "TRENDING_UPDATE_INTERVAL", 7200) or 7200))
    except Exception:
        return 7200


def _api_key() -> str:
    """First configured TMDB key (compat). Prefer ``_key_pool()`` for calls."""
    try:
        from config import Config
        keys = Config.tmdb_api_keys()
        return keys[0] if keys else ""
    except Exception:
        return ""


def _key_pool():
    from core.wroxen.trending.tmdb import get_key_pool
    try:
        from config import Config
        keys = Config.tmdb_api_keys()
    except Exception:
        keys = []
    return get_key_pool(keys)


async def _bulk_available_titles(
    owner_uid: int,
    wroxen_id: str,
    titles: List[str],
) -> Set[str]:
    from core.wroxen import db as wxdb

    col = wxdb._col(owner_uid)
    if col is None or not titles:
        return set()
    ors = []
    for t in titles[:40]:
        safe = re.escape(t.strip())
        if not safe:
            continue
        ors.append({
            "title": {
                "$regex": "(^|[^A-Za-z0-9])" + safe + "([^A-Za-z0-9]|$)",
                "$options": "i",
            }
        })
    if not ors:
        return set()
    try:
        cursor = col.find(
            {"wroxen_id": wroxen_id, "$or": ors},
            {"title": 1},
        ).limit(500)
        docs = await cursor.to_list(length=None)
    except Exception:
        logger.exception("trending availability query wroxen=%s", wroxen_id)
        return set()

    found_lower = {(d.get("title") or "").strip().lower() for d in docs}
    available: Set[str] = set()
    for t in titles:
        tl = t.strip().lower()
        if not tl:
            continue
        if tl in found_lower:
            available.add(t)
            continue
        for ft in found_lower:
            if tl in ft or ft in tl:
                available.add(t)
                break
    return available


async def _active_wroxen_targets() -> List[Tuple[int, str]]:
    """Enabled Wroxen configs that also have trending_enabled=True (default OFF)."""
    try:
        from database import list_all_enabled_wroxen
        configs = await list_all_enabled_wroxen()
    except Exception:
        logger.exception("list enabled wroxen for trending")
        return []
    seen = set()
    out: List[Tuple[int, str]] = []
    for c in configs or []:
        if not bool(c.get("trending_enabled", False)):
            continue
        wid = str(c.get("wroxen_id") or "")
        uid = c.get("user_id") or c.get("owner_id")
        if not wid or uid is None:
            continue
        key = (int(uid), wid)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


async def _refresh_one_region(region: str, *, force: bool = False) -> None:
    """Refresh a single region cache. Only ``global`` (TMDB) is supported."""
    region = "global"  # countries-wise trending removed
    interval = _interval()
    doc = await tcache.get_region_cache(region, "tmdb")
    if not force and not tcache.is_due(doc):
        await _rebuild_wx_for_region(region, doc)
        return

    pool = _key_pool()
    if not pool:
        logger.warning("Trending global skipped: TMDB_API_KEY(S) not set")
        if doc:
            await tcache.touch_region_next_update(region, "tmdb", interval)
        return

    source = "tmdb:/trending/all/day"
    logger.debug(
        "Trending update started region=global source=%s tmdb_keys=%s",
        source, len(pool),
    )
    try:
        items = await fetch_trending_all_day(pool=pool, limit=30)
        if not items:
            raise RuntimeError("empty titles for region=global")
        await tcache.save_region_cache(
            region, items, provider="tmdb", interval_sec=interval, source=source,
        )
        logger.debug(
            "Trending cache updated region=global · %s titles · via=tmdb",
            len(items),
        )
        doc = await tcache.get_region_cache(region, "tmdb")
        await _rebuild_wx_for_region(region, doc)
    except Exception as e:
        logger.warning(
            "Trending update failed region=global: %s — keeping previous cache", e
        )
        if doc and (doc.get("items") or []):
            await tcache.touch_region_next_update(region, "tmdb", interval)
        else:
            logger.warning("No Trending cache available for region=global")


async def _rebuild_wx_for_region(
    region: str,
    region_doc: Optional[Dict[str, Any]],
) -> None:
    if not region_doc:
        return
    raw = list(region_doc.get("items") or [])[:60]
    if not raw:
        return
    titles = [x.get("title") or "" for x in raw if x.get("title")]
    parent_at = region_doc.get("fetched_at")
    pairs = await _active_wroxen_targets()
    if not pairs:
        return
    for owner_uid, wroxen_id in pairs:
        try:
            from core.db_resolver import resolve_feature_db
            from core.wroxen import db as wxdb
            resolved = await resolve_feature_db(owner_uid, "wroxen")
            uri = resolved.get("uri")
            if not uri:
                continue
            ok, _ = await wxdb.ensure_connected(owner_uid, uri)
            if not ok:
                continue
            avail = await _bulk_available_titles(owner_uid, wroxen_id, titles)
            tagged = []
            for row in raw:
                title = row.get("title") or ""
                if not title:
                    continue
                tagged.append({
                    "title": title,
                    "media_type": row.get("media_type") or "movie",
                    "tmdb_id": row.get("tmdb_id"),
                    "year": row.get("year"),
                    "available": title in avail,
                })
            await tcache.save_wx_cache(
                owner_uid, wroxen_id, region, tagged,
                provider="tmdb", parent_fetched_at=parent_at,
            )
            n_ok = sum(1 for x in tagged if x.get("available"))
            logger.debug(
                "Trending wx cache owner=%s wx=%s region=%s: %s/%s available",
                owner_uid, wroxen_id, region, n_ok, len(tagged),
            )
        except Exception:
            logger.exception(
                "Trending wx filter failed owner=%s wx=%s region=%s",
                owner_uid, wroxen_id, region,
            )


async def refresh_trending(*, force: bool = False) -> None:
    """Refresh global TMDB trending cache (countries-wise removed)."""
    await tcache.ensure_indexes()
    try:
        await _refresh_one_region("global", force=force)
    except Exception:
        logger.exception("Trending global pass failed")


async def get_trending_now_items(
    owner_id: int,
    wroxen_id: str,
    region: str = "global",
    media_type: str = "all",
) -> List[Dict[str, Any]]:
    """Read-only Wroxen-scoped cache. Zero TMDB calls.

    Only global TMDB trending is supported (countries-wise removed).
    media_type: ``movie`` | ``tv`` | ``all`` — filter after cache read.
    """
    cache_region = "global"  # force global; country regions no longer maintained

    def _filter(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        mt = (media_type or "all").lower().strip()
        if mt in ("movie", "movies"):
            items = [x for x in items if (x.get("media_type") or "movie") == "movie"]
        elif mt in ("tv", "show", "shows"):
            items = [x for x in items if (x.get("media_type") or "") in ("tv", "show")]
        return list(items)[:30]

    doc = await tcache.get_wx_cache(int(owner_id), str(wroxen_id), cache_region, "tmdb")
    if doc and doc.get("items") is not None:
        return _filter(list(doc["items"]))
    region_doc = await tcache.get_region_cache(cache_region, "tmdb")
    if region_doc and region_doc.get("items"):
        await _rebuild_wx_for_region(cache_region, region_doc)
        doc = await tcache.get_wx_cache(int(owner_id), str(wroxen_id), cache_region, "tmdb")
        if doc and doc.get("items") is not None:
            return _filter(list(doc["items"]))
    return []


async def _purge_top_searches_once() -> None:
    """Drop Top Searches rows older than 30 days — bounds storage growth."""
    try:
        from core.wroxen.trending.analytics import purge_old_stats
        await purge_old_stats()
    except Exception:
        logger.debug("Top Searches purge skipped", exc_info=True)


async def _updater_loop() -> None:
    global _stop
    _stop = False
    await asyncio.sleep(5)
    while not _stop:
        try:
            await refresh_trending(force=False)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Trending updater pass failed")
        # After each region pass cycle — keep stats ≤ 30 days
        await _purge_top_searches_once()
        try:
            await asyncio.sleep(min(300, max(60, _interval() // 12)))
        except asyncio.CancelledError:
            break
    logger.debug("Trending updater stopped")


async def start_trending_updater() -> None:
    global _task, _stop
    _stop = False
    if _task and not _task.done():
        logger.debug("Trending updater already running")
        return
    await tcache.ensure_indexes()
    try:
        from core.wroxen.trending.analytics import ensure_indexes as ai
        await ai()
    except Exception:
        pass
    await _purge_top_searches_once()
    _task = asyncio.create_task(_updater_loop(), name="wroxen_trending_updater")
    logger.debug(
        "Trending updater scheduled (interval≈%ss, region=global TMDB only)",
        _interval(),
    )


async def stop_trending_updater() -> None:
    global _stop, _task
    _stop = True
    t = _task
    _task = None
    if t and not t.done():
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
