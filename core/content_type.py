"""Central Movie / Series classifier for Jobs, Quick Forward, and CNL.

One source of truth — do not reimplement detection in the three pipelines.

Primary detection: ``parsett==1.8.5`` via ``PTT.parse_title`` (seasons /
episodes → series; title+year/quality → movie). Regex is only a fallback when
PTT is unavailable or returns empty season/episode lists.

Returns:
    "movie" | "series" | "unknown"

Unknown is never treated as a movie or a series. Settings:
    all     → allow unknown
    movies  → skip unknown and series
    series  → skip unknown and movies
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

CONTENT_ALL = "all"
CONTENT_MOVIES = "movies"
CONTENT_SERIES = "series"
VALID_CONTENT_TYPES = (CONTENT_ALL, CONTENT_MOVIES, CONTENT_SERIES)

CONTENT_TYPE_LABELS = {
    CONTENT_ALL: "📦 All Content",
    CONTENT_MOVIES: "🎬 Movies Only",
    CONTENT_SERIES: "📺 Series Only",
}

# Season / episode patterns (reference clone.py + common scene names).
# Applied on a space-normalized form so "Season.2" / "S02.E05" still match.
_SERIES_RES = [
    re.compile(r"(?i)\bS(\d{1,2})\s*E(?:P)?(\d{1,3})(?:\s*[-–]\s*E?(?:P)?(\d{1,3}))?\b"),
    re.compile(r"(?i)\bSeason\s*(\d{1,2})(?:\s*(?:Episode|Ep\.?|E)\s*(\d{1,3}))?\b"),
    re.compile(r"(?i)\b(?:Complete\s+)?Season\s*(\d{1,2})\b"),
    re.compile(r"(?i)\b(?:E|EP|Episode)\s*(\d{1,3})(?:\s*(?:-|to|–)\s*(\d{1,3}))?\b"),
    re.compile(r"(?i)\bS(\d{1,2})\b"),
    re.compile(r"(?i)\b(\d{1,2})x(\d{1,3})\b"),
]

_YEAR_RE = re.compile(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)")
_QUALITY_RE = re.compile(
    r"(?i)\b(2160p|1440p|1080p|720p|480p|360p|4k|8k|bluray|blu-ray|bluray|"
    r"web[- ]?dl|webrip|hdrip|remux|hdtv|dvdrip|x264|x265|hevc|avc)\b"
)
_VIDEO_EXT_RE = re.compile(r"(?i)\.(mkv|mp4|avi|mov|m4v|ts|m2ts|wmv)$")


def _normalize_for_match(text: str) -> str:
    """Dots/underscores → spaces so Season.2 / Name.2024.BluRay match word patterns."""
    s = re.sub(r"[._]+", " ", text)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalize_content_type(raw: Any) -> str:
    v = str(raw or CONTENT_ALL).strip().lower()
    if v in ("movie", "movies"):
        return CONTENT_MOVIES
    if v in ("series", "show", "shows", "tv", "tvshow", "tv-show"):
        return CONTENT_SERIES
    return CONTENT_ALL


def content_type_label(raw: Any) -> str:
    return CONTENT_TYPE_LABELS[normalize_content_type(raw)]


def _ptt_parse(text: str) -> dict:
    """Parse with parsett (PTT). Requires parsett==1.8.5 for reliable S/E fields."""
    try:
        from PTT import parse_title
        data = parse_title(text, translate_languages=True) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("PTT parse_title failed (parsett missing or error)", exc_info=True)
        return {}


def _looks_like_series(text: str, ptt: Optional[dict] = None) -> bool:
    if ptt:
        seasons = ptt.get("seasons") or []
        episodes = ptt.get("episodes") or []
        if seasons or episodes:
            return True
    norm = _normalize_for_match(text)
    lowered = norm.lower()
    if "complete" in lowered and re.search(r"(?i)\b(season|series|s\d{1,2})\b", norm):
        return True
    for rx in _SERIES_RES:
        if rx.search(norm) or rx.search(text):
            return True
    return False


def _looks_like_movie(text: str, ptt: Optional[dict] = None) -> bool:
    """Movie only when there is a reliable title+year or title+quality signal."""
    ptt = ptt or {}
    norm = _normalize_for_match(text)
    title = (ptt.get("title") or "").strip() if isinstance(ptt.get("title"), str) else ""
    year = ptt.get("year")
    has_year = bool(year) or bool(_YEAR_RE.search(norm)) or bool(_YEAR_RE.search(text))
    has_quality = (
        bool(_QUALITY_RE.search(norm))
        or bool(_QUALITY_RE.search(text))
        or bool(ptt.get("quality") or ptt.get("source") or ptt.get("codec"))
    )
    has_ext = bool(_VIDEO_EXT_RE.search(text.strip()))
    # Enough letter tokens to look like a real title (not just "2024 BluRay")
    word_tokens = [w for w in re.findall(r"[A-Za-z]{2,}", norm) if w.lower() not in {
        "bluray", "webrip", "webdl", "hdrip", "remux", "hdtv", "dvdrip",
        "x264", "x265", "hevc", "avc", "mkv", "mp4", "dual", "audio",
        "hindi", "english", "tamil", "telugu",
    }]
    has_title_words = len(word_tokens) >= 1 or len(title) >= 2

    if title and len(title) >= 2 and has_year:
        return True
    if title and len(title) >= 2 and has_quality:
        return True
    if has_year and has_quality and has_title_words:
        return True
    if has_year and has_quality and (has_ext or "." in text or "_" in text):
        return True
    return False


def detect_from_text(text: Optional[str]) -> str:
    """Classify a caption / filename string. Never maps unknown → movie.

    Strategy (same idea as reference clone.py + parsett 1.8.5):
    1. ``parse_title`` → seasons/episodes ⇒ series
    2. regex fallback for SxxExx / Season / Episode if PTT missed them
    3. ``parse_title`` title + year/quality ⇒ movie (not merely “no season”)
    """
    if not text or not str(text).strip():
        return "unknown"
    raw = str(text).strip()
    # PTT on raw first (parsett handles dotted scene names well at 1.8.5)
    ptt = _ptt_parse(raw)
    if not (ptt.get("title") or ptt.get("seasons") or ptt.get("episodes")):
        ptt2 = _ptt_parse(_normalize_for_match(raw))
        if ptt2:
            ptt = ptt2
    # Reference logic: is_series = bool(seasons or episodes)
    if _looks_like_series(raw, ptt):
        return "series"
    if _looks_like_movie(raw, ptt):
        return "movie"
    return "unknown"


def collect_title_sources(message: Any) -> list[str]:
    """Caption first, then video/document/audio/animation file names."""
    out: list[str] = []
    cap = getattr(message, "caption", None) or getattr(message, "text", None)
    if cap and str(cap).strip():
        out.append(str(cap).strip())
    for attr in ("video", "document", "audio", "animation"):
        obj = getattr(message, attr, None)
        if obj is None:
            continue
        fn = getattr(obj, "file_name", None)
        if fn and str(fn).strip():
            s = str(fn).strip()
            if s not in out:
                out.append(s)
    return out


def detect_content_type(message: Any) -> str:
    """Classify one Telegram message. Prefer series if any source is series."""
    sources = collect_title_sources(message)
    if not sources:
        return "unknown"
    kinds = [detect_from_text(s) for s in sources]
    if "series" in kinds:
        return "series"
    if "movie" in kinds:
        return "movie"
    return "unknown"


def detect_content_type_from_messages(messages: Iterable[Any]) -> str:
    """Album-safe: one type for the whole group. Series wins over movie."""
    kinds = []
    for m in messages or []:
        k = detect_content_type(m)
        if k != "unknown":
            kinds.append(k)
    if "series" in kinds:
        return "series"
    if "movie" in kinds:
        return "movie"
    return "unknown"


def content_type_allows(kind: str, setting: Any) -> bool:
    setting = normalize_content_type(setting)
    if setting == CONTENT_ALL:
        return True
    kind = (kind or "unknown").lower()
    if kind == "unknown":
        return False
    if setting == CONTENT_MOVIES:
        return kind == "movie"
    if setting == CONTENT_SERIES:
        return kind == "series"
    return True


def content_filter_reason(kind: str, setting: Any) -> str:
    setting = normalize_content_type(setting)
    return f"content_type:{setting}:{kind or 'unknown'}"


def content_filter_log_line(kind: str, setting: Any) -> str:
    setting = normalize_content_type(setting)
    label = {
        CONTENT_MOVIES: "Movies Only",
        CONTENT_SERIES: "Series Only",
    }.get(setting, setting)
    return f"[CONTENT_FILTER] Skipped {kind or 'unknown'}: {label}"


def apply_content_type_filter(message: Any, setting: Any) -> tuple[bool, str]:
    """Return (allow, reason). reason is 'ok' or content_type:<setting>:<kind>."""
    setting = normalize_content_type(setting)
    if setting == CONTENT_ALL:
        return True, "ok"
    kind = detect_content_type(message)
    if content_type_allows(kind, setting):
        return True, "ok"
    return False, content_filter_reason(kind, setting)


def apply_content_type_filter_album(messages: Iterable[Any], setting: Any) -> tuple[bool, str]:
    setting = normalize_content_type(setting)
    if setting == CONTENT_ALL:
        return True, "ok"
    msgs = list(messages or [])
    kind = detect_content_type_from_messages(msgs)
    if content_type_allows(kind, setting):
        return True, "ok"
    return False, content_filter_reason(kind, setting)
