"""Operation-level filters (Job / Quick Forward) — independent of target settings.

Default media: video + document only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

DEFAULT_MEDIA_TYPES = ["video", "document"]

ALL_MEDIA_TYPES = [
    "video",
    "document",
    "photo",
    "audio",
    "animation",
    "voice",
    "video_note",
    "sticker",
    "text",
]


def default_op_filters() -> Dict[str, Any]:
    return {
        "media_types": list(DEFAULT_MEDIA_TYPES),
        "block_enabled": False,
        "block_words": [],
        "whitelist_enabled": False,
        "whitelist_words": [],
        "content_type": "all",
        "size_filter_enabled": False,
        "min_media_size": 0,
    }


def normalize_op_filters(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    from core.content_type import normalize_content_type

    base = default_op_filters()
    if not isinstance(raw, dict):
        return base
    mt = raw.get("media_types")
    if isinstance(mt, list) and mt:
        base["media_types"] = [str(x) for x in mt if x]
    else:
        base["media_types"] = list(DEFAULT_MEDIA_TYPES)
    base["block_enabled"] = bool(raw.get("block_enabled", False))
    base["block_words"] = [str(w) for w in (raw.get("block_words") or []) if w]
    base["whitelist_enabled"] = bool(
        raw.get("whitelist_enabled", raw.get("whitelist_mode", False))
    )
    wl = raw.get("whitelist_words")
    if wl is None:
        wl = raw.get("whitelist") or []
    base["whitelist_words"] = [str(w) for w in wl if w]
    # Missing field (legacy jobs) → all
    base["content_type"] = normalize_content_type(raw.get("content_type", "all"))
    base["size_filter_enabled"] = bool(raw.get("size_filter_enabled", False))
    try:
        base["min_media_size"] = max(0, int(raw.get("min_media_size") or 0))
    except (TypeError, ValueError):
        base["min_media_size"] = 0
    return base


def _merge_word_lists(*lists: List) -> List[str]:
    """Union of word lists, case-insensitive de-dupe, preserve first-seen order."""
    seen = set()
    out: List[str] = []
    for lst in lists:
        for w in lst or []:
            s = str(w).strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
    return out


def merge_settings_for_forward(
    target_settings: Optional[Dict[str, Any]],
    op_filters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Target settings as base; job/op filters combine on top.

    Always from target (never overridden by job filters):
      delay, forward_tag, anti_duplicate, caption_*, replace_*,
      remove_links, inline_buttons*, replacements

    Job/op when provided:
      content_type, size_filter_*  → from job
      media_types → INTERSECTION of job + target (both must allow the type)
        Target can further restrict Job Filters. To forward a type, enable it
        on BOTH Job Filters and Target Media Types.

    Block words / whitelist — BOTH target and job apply together:
      - enabled if target ON **or** job ON
      - word list = union of whichever sides are enabled
    """
    # Shallow copy + explicit preserve of target-only features so later
    # mutations never drop replacements / caption / buttons / delay.
    src = target_settings or {}
    settings: Dict[str, Any] = dict(src)
    for key in (
        "replace_enabled",
        "replacements",
        "caption_enabled",
        "caption_template",
        "rich_message_enabled",
        "remove_links",
        "inline_buttons_enabled",
        "inline_buttons",
        "forward_tag",
        "delay",
        "anti_duplicate",
    ):
        if key in src:
            settings[key] = src[key]

    # If user saved replacement rules but left the toggle OFF, still apply them
    reps = settings.get("replacements") or []
    if reps and not settings.get("replace_enabled"):
        settings["replace_enabled"] = True

    op = normalize_op_filters(op_filters)

    tgt_block_on = bool(settings.get("block_words_enabled", False))
    tgt_block_words = list(settings.get("block_words") or []) if tgt_block_on else []
    tgt_white_on = bool(settings.get("whitelist_mode", False))
    tgt_white_words = list(settings.get("whitelist") or []) if tgt_white_on else []
    tgt_media = [str(x).lower() for x in (settings.get("media_types") or []) if x]

    if op_filters is not None:
        job_media = [str(m).lower() for m in op["media_types"]]
        # Intersection: type forwarded only if BOTH Job Filters AND Target allow it.
        # Default target includes all types → Job Filters alone control the set.
        # Turning a type OFF on the Target further restricts jobs.
        if tgt_media:
            settings["media_types"] = [m for m in job_media if m in set(tgt_media)]
        else:
            settings["media_types"] = list(job_media)

        settings["content_type"] = op.get("content_type") or "all"
        settings["size_filter_enabled"] = bool(op.get("size_filter_enabled"))
        settings["min_media_size"] = int(op.get("min_media_size") or 0)

        job_block_on = bool(op.get("block_enabled"))
        job_block_words = list(op.get("block_words") or []) if job_block_on else []
        job_white_on = bool(op.get("whitelist_enabled"))
        job_white_words = list(op.get("whitelist_words") or []) if job_white_on else []

        settings["block_words_enabled"] = tgt_block_on or job_block_on
        settings["block_words"] = _merge_word_lists(tgt_block_words, job_block_words)

        settings["whitelist_mode"] = tgt_white_on or job_white_on
        settings["whitelist"] = _merge_word_lists(tgt_white_words, job_white_words)
    else:
        settings.setdefault("content_type", "all")
        settings.setdefault("size_filter_enabled", False)
        settings.setdefault("min_media_size", 0)
        if tgt_media:
            settings["media_types"] = tgt_media
    return settings
