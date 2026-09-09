"""Persistent Trending cache in main MongoDB (global TMDB; region key kept for schema)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# One doc per (provider, region) — TMDB raw list (max 30)
COLL_REGION = "wroxen_trending_cache"
# Per Wroxen availability: (owner_id, wroxen_id, region, provider)
COLL_WX = "wroxen_trending_by_wx"
# Legacy collection name from first Trending version (provider-only unique)
COLL_GLOBAL_LEGACY = "wroxen_trending_global"

# Old index names that conflict with region-aware schema
_LEGACY_WX_INDEXES = (
    "trend_wx_provider",           # unique {wroxen_id, provider} — blocks multi-region
    "wroxen_id_1_provider_1",      # auto name variant
)
_LEGACY_REGION_INDEXES = (
    "trend_provider",              # unique {provider} only
    "provider_1",
)


def _db():
    from database import db
    return db.db


async def _drop_index_safe(col, name: str) -> None:
    try:
        await col.drop_index(name)
        logger.info("Dropped legacy trending index %s on %s", name, col.name)
    except Exception as e:
        # IndexNotFound is fine
        msg = str(e).lower()
        if "index not found" in msg or "not found" in msg:
            return
        logger.debug("drop_index %s: %s", name, e)


async def ensure_indexes() -> None:
    """Create region-aware indexes; drop legacy unique indexes that conflict."""
    try:
        d = _db()
        if d is None:
            return

        # --- region cache ---
        for name in _LEGACY_REGION_INDEXES:
            await _drop_index_safe(d[COLL_REGION], name)
        await _drop_index_safe(d[COLL_GLOBAL_LEGACY], "trend_provider")
        try:
            await d[COLL_REGION].create_index(
                [("provider", 1), ("region", 1)],
                unique=True,
                name="trend_provider_region",
            )
        except Exception as e:
            logger.debug("trend_provider_region: %s", e)
        try:
            await d[COLL_REGION].create_index("next_update_at", name="trend_region_next")
        except Exception:
            pass

        # --- per-wroxen cache: MUST drop old {wroxen_id, provider} unique ---
        for name in _LEGACY_WX_INDEXES:
            await _drop_index_safe(d[COLL_WX], name)
        # Also drop by key pattern if name differs
        try:
            info = await d[COLL_WX].index_information()
            for iname, meta in info.items():
                if iname == "_id_":
                    continue
                keys = list(meta.get("key") or [])
                # unique on exactly (wroxen_id, provider) without region/owner
                key_fields = [k[0] for k in keys]
                if (
                    meta.get("unique")
                    and key_fields == ["wroxen_id", "provider"]
                ):
                    await _drop_index_safe(d[COLL_WX], iname)
        except Exception:
            logger.debug("scan wx indexes", exc_info=True)

        try:
            await d[COLL_WX].create_index(
                [("owner_id", 1), ("wroxen_id", 1), ("region", 1), ("provider", 1)],
                unique=True,
                name="trend_wx_owner_region",
            )
        except Exception as e:
            logger.warning("trend_wx_owner_region index: %s", e)

        logger.info("Trending cache indexes ready (region-aware)")
    except Exception:
        logger.exception("trending cache ensure_indexes")


async def get_region_cache(region: str, provider: str = "tmdb") -> Optional[Dict[str, Any]]:
    d = _db()
    if d is None:
        return None
    return await d[COLL_REGION].find_one({
        "provider": provider,
        "region": (region or "global").lower(),
    })


async def save_region_cache(
    region: str,
    items: List[Dict[str, Any]],
    *,
    provider: str = "tmdb",
    interval_sec: int = 7200,
    source: str = "",
) -> None:
    """Save successful region list. Never call with empty on failure."""
    d = _db()
    if d is None:
        return
    now = datetime.now(timezone.utc)
    next_at = now + timedelta(seconds=max(60, int(interval_sec)))
    region = (region or "global").lower()
    await d[COLL_REGION].update_one(
        {"provider": provider, "region": region},
        {"$set": {
            "provider": provider,
            "region": region,
            "items": items[:60],
            "fetched_at": now,
            "updated_at": now,
            "next_update_at": next_at,
            "status": "success",
            "item_count": min(60, len(items)),
            "source": source,
        }},
        upsert=True,
    )


async def touch_region_next_update(region: str, provider: str, interval_sec: int) -> None:
    d = _db()
    if d is None:
        return
    now = datetime.now(timezone.utc)
    retry = now + timedelta(seconds=min(900, max(120, int(interval_sec) // 8)))
    await d[COLL_REGION].update_one(
        {"provider": provider, "region": (region or "global").lower()},
        {"$set": {"updated_at": now, "next_update_at": retry, "last_error_at": now}},
        upsert=False,
    )


async def get_wx_cache(
    owner_id: int,
    wroxen_id: str,
    region: str,
    provider: str = "tmdb",
) -> Optional[Dict[str, Any]]:
    d = _db()
    if d is None:
        return None
    return await d[COLL_WX].find_one({
        "owner_id": int(owner_id),
        "wroxen_id": str(wroxen_id),
        "region": (region or "global").lower(),
        "provider": provider,
    })


async def save_wx_cache(
    owner_id: int,
    wroxen_id: str,
    region: str,
    items: List[Dict[str, Any]],
    *,
    provider: str = "tmdb",
    parent_fetched_at: Optional[datetime] = None,
) -> None:
    d = _db()
    if d is None:
        return
    now = datetime.now(timezone.utc)
    filt = {
        "owner_id": int(owner_id),
        "wroxen_id": str(wroxen_id),
        "region": (region or "global").lower(),
        "provider": provider,
    }
    payload = {
        "owner_id": int(owner_id),
        "wroxen_id": str(wroxen_id),
        "region": (region or "global").lower(),
        "provider": provider,
        "items": items[:60],
        "updated_at": now,
        "parent_fetched_at": parent_fetched_at,
        "status": "success",
        "item_count": min(60, len(items)),
    }
    try:
        await d[COLL_WX].update_one(filt, {"$set": payload}, upsert=True)
    except Exception as e:
        # Legacy unique index {wroxen_id, provider} still present → drop & retry once
        if "duplicate key" in str(e).lower() or e.__class__.__name__ == "DuplicateKeyError":
            logger.warning(
                "Trending wx cache DuplicateKey (legacy index?) — repairing indexes and retrying"
            )
            await ensure_indexes()
            await d[COLL_WX].update_one(filt, {"$set": payload}, upsert=True)
        else:
            raise


def is_due(doc: Optional[Dict[str, Any]]) -> bool:
    if not doc:
        return True
    nxt = doc.get("next_update_at")
    if not nxt:
        return True
    if getattr(nxt, "tzinfo", None) is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= nxt
