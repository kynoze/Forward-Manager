"""Wroxen Trending: global TMDB cache + group Top Searches.

Countries-wise trending has been removed; only TMDB /trending/all/day.
"""
from core.wroxen.trending.service import (
    start_trending_updater,
    stop_trending_updater,
    get_trending_now_items,
)
from core.wroxen.trending.analytics import (
    record_successful_search,
    top_searches,
    normalize_search_title,
    purge_old_stats,
)

__all__ = [
    "start_trending_updater",
    "stop_trending_updater",
    "get_trending_now_items",
    "record_successful_search",
    "top_searches",
    "normalize_search_title",
    "purge_old_stats",
]
