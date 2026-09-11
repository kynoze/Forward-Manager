"""Group-level successful Wroxen search analytics (Top Searches)."""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

COLL = "wroxen_search_stats"
# Keep only this many calendar days of Top Searches stats (inclusive of today).
RETENTION_DAYS = 30


def normalize_search_title(title: str) -> str:
    """Light normalization — case/space/unicode; not aggressive fuzzy merge."""
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", str(title)).strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def _tz():
    try:
        from config import Config
        return ZoneInfo(getattr(Config, "BOT_TZ", None) or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _day_key(dt: Optional[datetime] = None) -> str:
    """Calendar day in BOT_TZ as YYYY-MM-DD."""
    tz = _tz()
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def _db():
    from database import db
    return db.db


async def ensure_indexes() -> None:
    try:
        d = _db()
        if d is None:
            return
        await d[COLL].create_index(
            [("group_id", 1), ("day", 1), ("normalized_title", 1)],
            unique=True,
            name="wx_stats_group_day_title",
        )
        await d[COLL].create_index(
            [("group_id", 1), ("day", 1)],
            name="wx_stats_group_day",
        )
        # Fast purge of days older than retention window
        await d[COLL].create_index([("day", 1)], name="wx_stats_day")
    except Exception:
        logger.debug("search stats index", exc_info=True)


def _retention_cutoff_day() -> str:
    """Oldest day key to keep (BOT_TZ). Docs with day < this are purged."""
    tz = _tz()
    today = datetime.now(timezone.utc).astimezone(tz).date()
    cutoff = today - timedelta(days=max(1, int(RETENTION_DAYS) - 1))
    return cutoff.strftime("%Y-%m-%d")


async def purge_old_stats(*, older_than_days: Optional[int] = None) -> int:
    """Delete Top Searches docs older than retention window. Returns deleted count.

    ``day`` is stored as YYYY-MM-DD so lexicographic ``$lt`` is correct.
    """
    days = int(older_than_days) if older_than_days is not None else RETENTION_DAYS
    days = max(1, days)
    tz = _tz()
    today = datetime.now(timezone.utc).astimezone(tz).date()
    # Keep last ``days`` calendar days including today → purge day < today-(days-1)
    cutoff = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    try:
        d = _db()
        if d is None:
            return 0
        result = await d[COLL].delete_many({"day": {"$lt": cutoff}})
        n = int(getattr(result, "deleted_count", 0) or 0)
        if n:
            logger.debug(
                "Top Searches purge: deleted %s docs with day < %s (keep %sd)",
                n, cutoff, days,
            )
        return n
    except Exception:
        logger.exception("purge_old_stats failed")
        return 0


async def record_successful_search(group_id: int, title: str) -> None:
    """+1 for a successful search in this group. No user_id stored."""
    norm = normalize_search_title(title)
    if not norm or not group_id:
        return
    display = re.sub(r"\s+", " ", str(title).strip())
    day = _day_key()
    try:
        d = _db()
        if d is None:
            return
        await d[COLL].update_one(
            {
                "group_id": int(group_id),
                "day": day,
                "normalized_title": norm,
            },
            {
                "$inc": {"count": 1},
                "$set": {
                    "display_title": display,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {
                    "group_id": int(group_id),
                    "day": day,
                    "normalized_title": norm,
                    "created_at": datetime.now(timezone.utc),
                },
            },
            upsert=True,
        )
        logger.debug(
            "Top Searches +1 group=%s day=%s title=%s",
            group_id, day, display[:80],
        )
    except Exception:
        logger.exception("record_successful_search group=%s", group_id)


def _day_range(period: str) -> List[str]:
    """Return list of YYYY-MM-DD day keys for the period in BOT_TZ."""
    tz = _tz()
    now_local = datetime.now(timezone.utc).astimezone(tz)
    today = now_local.date()
    period = (period or "7d").lower()
    if period in ("today", "1d", "1"):
        days = 1
    elif period in ("3d", "3"):
        days = 3
    elif period in ("7d", "7"):
        days = 7
    elif period in ("15d", "15"):
        days = 15
    elif period in ("30d", "30"):
        days = 30
    else:
        days = 7
    out = []
    for i in range(days):
        d = today - timedelta(days=i)
        out.append(d.strftime("%Y-%m-%d"))
    return out


async def top_searches(
    group_id: int,
    period: str = "7d",
    *,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Aggregate counts for group + period, sorted descending."""
    days = _day_range(period)
    d = _db()
    if d is None:
        return []
    try:
        pipeline = [
            {"$match": {"group_id": int(group_id), "day": {"$in": days}}},
            {"$group": {
                "_id": "$normalized_title",
                "count": {"$sum": "$count"},
                "display_title": {"$last": "$display_title"},
            }},
            {"$sort": {"count": -1}},
            {"$limit": int(limit)},
        ]
        # PyMongo Async: aggregate() is a coroutine → await, then to_list
        cursor = await d[COLL].aggregate(pipeline)
        rows = await cursor.to_list(length=limit)
        return [
            {
                "normalized_title": r["_id"],
                "display_title": r.get("display_title") or r["_id"],
                "count": int(r.get("count") or 0),
            }
            for r in rows
        ]
    except Exception:
        logger.exception("top_searches group=%s period=%s", group_id, period)
        return []
