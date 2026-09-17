from typing import Any, Dict, Optional

from pyrogram.types import Message


def _normalize_media_type(raw) -> str:
    """Map Pyrogram/Kurigram media enum/value to settings keys (photo, video, …)."""
    if raw is None:
        return ""
    s = getattr(raw, "value", None) or str(raw)
    s = str(s).strip()
    if "." in s:
        s = s.split(".")[-1]
    return s.lower()


def should_process_message(message: Message, settings: Dict[str, Any]) -> tuple[bool, str]:
    if getattr(message, "empty", False):
        return False, "deleted"

    allowed_media = [_normalize_media_type(x) for x in (settings.get("media_types") or [])]
    allowed_set = set(allowed_media)
    allow_all = len(allowed_set) == 0

    if message.media:
        media_type = _normalize_media_type(message.media)
        if not allow_all and media_type not in allowed_set:
            return False, f"media_type:{media_type}"
    else:
        if not allow_all and "text" not in allowed_set:
            return False, "media_type:text"

    # Movie / Series filter — after media type, before block/whitelist.
    # Missing content_type (legacy jobs) → "all".
    from core.content_type import apply_content_type_filter, content_filter_log_line, normalize_content_type
    import logging
    ct = normalize_content_type(settings.get("content_type", "all"))
    if ct != "all":
        ok, reason = apply_content_type_filter(message, ct)
        if not ok:
            logging.getLogger(__name__).debug(content_filter_log_line(reason.split(":")[-1], ct))
            return False, reason

    text_content = message.caption or message.text or ""
    text_lower = str(text_content).lower() if text_content else ""

    # Default OFF — matches DEFAULT_TARGET_SETTINGS (was True, which blocked unexpectedly)
    if settings.get("block_words_enabled", False):
        block_words = settings.get("block_words", []) or []
        if block_words and text_lower:
            for word in block_words:
                if word and str(word).lower() in text_lower:
                    return False, f"blocked_word:{word}"

    if settings.get("whitelist_mode", False):
        whitelist = settings.get("whitelist", []) or []
        if not whitelist:
            return False, "whitelist_empty"
        if not text_lower:
            return False, "whitelist_no_text"
        if not any(w.lower() in text_lower for w in whitelist if w):
            return False, "whitelist_miss"

    # Minimum media size (Jobs op_filters → settings via merge_settings_for_forward)
    from core.media_size import passes_size_filter
    ok_sz, reason_sz = passes_size_filter(message, settings)
    if not ok_sz:
        return False, reason_sz

    return True, "ok"


def get_unique_file_id(message: Message) -> Optional[str]:
    """Extract file_unique_id from any media (Pyrogram/Kurigram safe)."""
    if not message or getattr(message, "empty", False):
        return None

    def _from_obj(obj) -> Optional[str]:
        if obj is None:
            return None
        # list/tuple of PhotoSize
        if isinstance(obj, (list, tuple)):
            for item in reversed(list(obj)):
                u = _from_obj(item)
                if u:
                    return u
            return None
        u = getattr(obj, "file_unique_id", None)
        if u:
            return str(u)
        # Photo container with .sizes
        sizes = getattr(obj, "sizes", None)
        if sizes:
            return _from_obj(sizes)
        # document nested
        doc = getattr(obj, "document", None)
        if doc is not None and doc is not obj:
            return _from_obj(doc)
        return None

    for attr in (
        "document",
        "video",
        "photo",
        "audio",
        "animation",
        "voice",
        "video_note",
        "sticker",
    ):
        u = _from_obj(getattr(message, attr, None))
        if u:
            return u

    media_enum = getattr(message, "media", None)
    if media_enum is not None:
        key = getattr(media_enum, "value", None) or str(media_enum)
        if isinstance(key, str):
            key = key.split(".")[-1].lower()
        u = _from_obj(getattr(message, key, None))
        if u:
            return u
    return None
