"""Central Movie / Series classifier for Jobs, Quick Forward, and CNL.

One source of truth — do not reimplement detection in the three pipelines.

Primary detection: ``parsett==1.8.5`` via ``PTT.parse_title`` (seasons /
episodes → series; title+year/quality → movie). Regex is only a fallback when
PTT is unavailable or returns empty season/episode lists.

Returns:
    "movie" | "series" | "unknown"

Unknown is never treated as a movie or a series. Settings:
    all            → allow unknown
    movies         → skip unknown and series
    series         → skip unknown and movies
    movies_series  → allow movie AND series/show; skip unknown and everything else
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

CONTENT_ALL = "all"
CONTENT_MOVIES = "movies"
CONTENT_SERIES = "series"
CONTENT_MOVIES_SERIES = "movies_series"
VALID_CONTENT_TYPES = (
    CONTENT_ALL,
    CONTENT_MOVIES,
    CONTENT_SERIES,
    CONTENT_MOVIES_SERIES,
)

CONTENT_TYPE_LABELS = {
    CONTENT_ALL: "📦 All Content",
    CONTENT_MOVIES: "🎬 Movies Only",
    CONTENT_SERIES: "📺 Series Only",
    CONTENT_MOVIES_SERIES: "🎬📺 Movies + Series",
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
    re.compile(r"(?i)\bEp(?:isode)?\s*(\d{1,3})\s*[-–]\s*(\d{1,3})\b"),
]

_YEAR_RE = re.compile(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)")
_QUALITY_RE = re.compile(
    r"(?i)\b(2160p|1440p|1080p|720p|480p|360p|4k|8k|bluray|blu-ray|"
    r"web[- ]?dl|webrip|hdrip|remux|hdtv|dvdrip|x264|x265|hevc|avc)\b"
)
_VIDEO_EXT_RE = re.compile(r"(?i)\.(mkv|mp4|avi|mov|m4v|ts|m2ts|wmv)$")

# Release junk stripped from titles when building logical group keys.
_STRIP_TOKENS = {
    # quality / resolution
    "480p", "720p", "1080p", "1440p", "2160p", "360p", "4k", "8k",
    # codecs
    "hevc", "x264", "x265", "h264", "h265", "avc", "10bit", "8bit",
    "h", "265", "264",  # "H 265" / "H 264" after split
    # sources / platforms
    "webdl", "web-dl", "webrip", "bluray", "blu-ray", "hdrip", "hdtv",
    "bdrip", "dvdrip", "remux", "web", "dl",
    "amzn", "nf", "dsnp", "atvp", "hmax", "zee5", "hotstar", "prime",
    "netflix", "ds4k", "hdtv",
    # audio
    "aac", "ddp", "dd+", "atmos", "ddp5", "dd5", "dts", "truehd",
    "6ch", "2ch", "5ch", "7ch",
    # languages
    "hindi", "english", "tamil", "telugu", "malayalam", "kannada",
    "dual", "audio", "multi",
    # subs / container
    "esub", "esubs", "sub", "subs", "subtitle", "subtitles",
    "mkv", "mp4", "avi", "org", "orgn",
    # pack / episode markers that must not split series groups
    "combined", "complete", "series", "season", "episode", "ep",
    # common release-group / uploader tags (scene + Indian packs)
    "anup", "ospreay", "archie", "psa", "sampa", "aarav", "efx",
    "godfather", "rarbg", "yts", "yify", "evo", "sparks", "cmrg",
    "tigris", "pahe", "tgx",
    "ex", "uncut", "extended", "proper", "repack", "internal", "hdtc", "hdts", "cam", "telesync",
}

_TITLE_JUNK_RE = re.compile(
    r"(?i)\b("
    r"2160p|1440p|1080p|720p|480p|360p|4k|8k|"
    r"hevc|x264|x265|h\.?\s?264|h\.?\s?265|avc|10bit|8bit|"
    r"web[- ]?dl|webrip|bluray|blu-ray|hdrip|hdtv|bdrip|dvdrip|remux|"
    r"amzn|dsnp|atvp|hmax|zee5|hotstar|netflix|ds4k|\bnf\b|"
    r"aac|ddp?|atmos|truehd|dts|"
    r"6ch|2ch|5ch|7ch|"
    r"hindi|english|tamil|telugu|malayalam|kannada|"
    r"dual[\s-]?audio|multi[\s-]?audio|"
    r"esubs?|subs?|subtitles?|"
    r"combined|complete|"
    r"mkv|mp4|avi"
    r")\b"
)

# Cut title at first strong release marker (quality / source).
_CUT_AT_RELEASE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:2160p|1440p|1080p|720p|480p|360p|4k|8k)\b|"
    r"\b(?:web[- ]?dl|webrip|bluray|blu-ray|hdrip|hdtv|bdrip|dvdrip|remux)\b|"
    r"\b(?:x264|x265|h\.?\s?264|h\.?\s?265|hevc|avc)\b"
    r")"
)

# Episode range / season tokens inside title text
_EP_RANGE_RE = re.compile(
    r"(?i)\b(?:s\d{1,2}\s*)?(?:e(?:p)?\s*\d{1,3}\s*[-–]\s*e?(?:p)?\s*\d{1,3})\b"
)
_SEASON_TOKEN_RE = re.compile(r"(?i)\bS\d{1,2}\b|\bseason\s*\d{1,2}\b")

# Audio channel layouts must not become part of the logical title
# (e.g. "AAC 5.1" → "5 1" would otherwise split the same movie into 2 groups).
_AUDIO_CHANNEL_RE = re.compile(
    r"(?i)\b(?:dd[p+]?|dts|truehd|atmos|aac)?[\s._-]*"
    r"(?:5[\s._-]*1|7[\s._-]*1|2[\s._-]*0|6[\s._-]*1)\b"
)


def _strip_audio_channels(text: str) -> str:
    s = _AUDIO_CHANNEL_RE.sub(" ", text or "")
    # Bare "5 1" / "7 1" left after codec strip or after dots→spaces
    s = re.sub(r"(?i)\b([257])\s+([01])\b", " ", s)
    return s


def _normalize_for_match(text: str) -> str:
    """Dots/underscores → spaces so Season.2 / Name.2024.BluRay match word patterns."""
    s = re.sub(r"[._]+", " ", text)
    s = _strip_audio_channels(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalize_content_type(raw: Any) -> str:
    v = str(raw or CONTENT_ALL).strip().lower()
    v = re.sub(r"[\s+\-/]+", "_", v)
    v = re.sub(r"_+", "_", v).strip("_")
    if v in ("movie", "movies"):
        return CONTENT_MOVIES
    if v in ("series", "show", "shows", "tv", "tvshow", "tv_show"):
        return CONTENT_SERIES
    if v in (
        CONTENT_MOVIES_SERIES,
        "movie_series",
        "movieseries",
        "movies_and_series",
        "movie_and_series",
        "both",
        "films_series",
        "movies_shows",
        "movie_show",
    ):
        return CONTENT_MOVIES_SERIES
    return CONTENT_ALL


def content_type_label(raw: Any) -> str:
    return CONTENT_TYPE_LABELS[normalize_content_type(raw)]


def content_type_button_rows(current: Any, *, long: bool = False) -> list[list[tuple[str, str]]]:
    """Rows of (mode, marked_label) for Jobs / QF / CNL keyboards."""
    cur = normalize_content_type(current)

    def mark(mode: str, label: str) -> str:
        return ("● " if cur == mode else "") + label

    if long:
        return [
            [(CONTENT_ALL, mark(CONTENT_ALL, "📦 All Content"))],
            [(CONTENT_MOVIES, mark(CONTENT_MOVIES, "🎬 Movies Only"))],
            [(CONTENT_SERIES, mark(CONTENT_SERIES, "📺 Series Only"))],
            [(CONTENT_MOVIES_SERIES, mark(CONTENT_MOVIES_SERIES, "🎬📺 Movies + Series"))],
        ]
    return [
        [
            (CONTENT_ALL, mark(CONTENT_ALL, "📦 All")),
            (CONTENT_MOVIES, mark(CONTENT_MOVIES, "🎬 Movies")),
            (CONTENT_SERIES, mark(CONTENT_SERIES, "📺 Series")),
        ],
        [
            (CONTENT_MOVIES_SERIES, mark(CONTENT_MOVIES_SERIES, "🎬📺 Movies + Series")),
        ],
    ]


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


def _title_core_before_release(text: str) -> str:
    """Keep only the name part before quality/source markers."""
    s = text or ""
    # Prefer text before first (YEAR) or before quality when year uses dots: Title 2026.1080p
    m_year_dot = re.search(r"(?i)\b((?:19|20)\d{2})\s*[.\-]\s*(?:\d{3,4}p|web|blu)", s)
    if m_year_dot:
        s = s[: m_year_dot.start()] + " " + m_year_dot.group(1)
    m_cut = _CUT_AT_RELEASE_RE.search(s)
    if m_cut:
        s = s[: m_cut.start()]
    return s


def _fallback_title(text: str) -> str:
    """Strip release junk from raw text when PTT has no title."""
    s = _title_core_before_release(text)
    s = _normalize_for_match(s)
    s = _TITLE_JUNK_RE.sub(" ", s)
    s = _strip_audio_channels(s)
    s = _EP_RANGE_RE.sub(" ", s)
    s = _SEASON_TOKEN_RE.sub(" ", s)
    s = re.sub(r"(?i)\bS\d{1,2}(?:\s*E(?:P)?\d{1,3}(?:\s*[-–]\s*E?(?:P)?\d{1,3})?)?\b", " ", s)
    s = re.sub(r"(?i)\b(?:season|episode|ep)\s*\d+\b", " ", s)
    s = re.sub(r"\((?:19|20)\d{2}\)", " ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)  # bare year in title → year field owns it
    s = re.sub(r"[\[\](){}~#🔊]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_|+")
    return s


def parse_metadata(text: Optional[str]) -> dict:
    """PTT-first parse. Always returns type/title/year/seasons/episodes."""
    empty = {
        "type": "unknown",
        "title": "",
        "year": None,
        "seasons": [],
        "episodes": [],
    }
    if not text or not str(text).strip():
        return dict(empty)
    raw = str(text).strip()
    ptt = _ptt_parse(raw)
    if not (ptt.get("title") or ptt.get("seasons") or ptt.get("episodes")):
        ptt2 = _ptt_parse(_normalize_for_match(raw))
        if ptt2:
            ptt = ptt2

    seasons = list(ptt.get("seasons") or []) if isinstance(ptt.get("seasons"), list) else []
    episodes = list(ptt.get("episodes") or []) if isinstance(ptt.get("episodes"), list) else []
    title = (ptt.get("title") or "").strip() if isinstance(ptt.get("title"), str) else ""
    year = ptt.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None
    if year is None:
        m = _YEAR_RE.search(_normalize_for_match(raw))
        if m:
            year = int(m.group(1))

    if _looks_like_series(raw, ptt):
        kind = "series"
    elif _looks_like_movie(raw, ptt):
        kind = "movie"
    else:
        kind = "unknown"

    if not title:
        title = _fallback_title(raw)
    else:
        # PTT titles sometimes keep group tags — run the same cleaner
        cleaned = _fallback_title(title)
        if cleaned and len(cleaned) >= 2:
            title = cleaned

    return {
        "type": kind,
        "title": title,
        "year": year,
        "seasons": seasons,
        "episodes": episodes,
    }


def detect_from_text(text: Optional[str]) -> str:
    """Classify a caption / filename string. Never maps unknown → movie."""
    return parse_metadata(text).get("type") or "unknown"


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


def parse_message_metadata(message: Any) -> dict:
    """Best metadata across caption + filenames. Series wins over movie."""
    best = {
        "type": "unknown",
        "title": "",
        "year": None,
        "seasons": [],
        "episodes": [],
    }
    for src in collect_title_sources(message):
        meta = parse_metadata(src)
        if meta["type"] == "series":
            return meta
        if meta["type"] == "movie" and best["type"] != "movie":
            best = meta
        elif not best.get("title") and meta.get("title"):
            best = meta
    return best


def detect_content_type(message: Any) -> str:
    """Classify one Telegram message. Prefer series if any source is series."""
    return parse_message_metadata(message).get("type") or "unknown"


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


def normalize_group_title(title: str) -> str:
    """Lowercase title, drop release junk, keep distinguishing tokens (Dhamaal 4)."""
    s = _title_core_before_release(title or "")
    s = _normalize_for_match(s)
    s = _TITLE_JUNK_RE.sub(" ", s)
    s = _strip_audio_channels(s)
    s = _EP_RANGE_RE.sub(" ", s)
    s = _SEASON_TOKEN_RE.sub(" ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    parts = []
    for tok in s.lower().split():
        if tok in _STRIP_TOKENS:
            continue
        # skip pure noise tokens like "+" leftovers
        if tok in {"+", "-", "_"}:
            continue
        parts.append(tok)
    return "_".join(parts).strip("_")


def group_key_from_metadata(meta: dict) -> Optional[str]:
    """Deterministic logical group id. None for unknown / untitled."""
    kind = (meta or {}).get("type") or "unknown"
    if kind not in ("movie", "series"):
        return None
    title = normalize_group_title(meta.get("title") or "")
    if not title:
        return None
    year = meta.get("year")
    year_s = str(int(year)) if year else ""
    if kind == "movie":
        return f"movie:{title}:{year_s}" if year_s else f"movie:{title}"
    if year_s:
        return f"series:{title}:{year_s}"
    return f"series:{title}"


def get_content_group_key(message: Any) -> Optional[str]:
    """Logical movie/series group for completion stickers (quality ignored)."""
    return group_key_from_metadata(parse_message_metadata(message))


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
    if setting == CONTENT_MOVIES_SERIES:
        return kind in ("movie", "series")
    return False


def content_filter_reason(kind: str, setting: Any) -> str:
    setting = normalize_content_type(setting)
    return f"content_type:{setting}:{kind or 'unknown'}"


def content_filter_log_line(kind: str, setting: Any) -> str:
    setting = normalize_content_type(setting)
    label = {
        CONTENT_MOVIES: "Movies Only",
        CONTENT_SERIES: "Series Only",
        CONTENT_MOVIES_SERIES: "Movies + Series",
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
