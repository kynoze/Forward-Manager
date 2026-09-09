# config.py

import os

from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str):
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
            out.append(int(part))
    return out


class Config:
    API_ID = int(os.environ.get("API_ID", "0") or 0)
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    MONGO_URI = os.environ.get("MONGO_URI", "")
    DB_NAME = os.environ.get("DB_NAME", "cloner_boy")
    ADMINS = _int_list(os.environ.get("ADMINS", ""))
    # Bot owner(s). If empty, first ADMIN is treated as owner.
    OWNER_IDS = _int_list(os.environ.get("OWNER_IDS", "") or os.environ.get("OWNER_ID", ""))
    # Optional fallback when DB is down (Telegram chat id)
    OWNER_LOG_CHAT_ID = int(os.environ.get("OWNER_LOG_CHAT_ID", "0") or 0) or None
    # Encrypt session strings / bot tokens at rest. Prefer SESSION_ENC_KEY.
    SESSION_ENC_KEY = (
        os.environ.get("SESSION_ENC_KEY", "")
        or os.environ.get("SESSION_SECRET", "")
        or os.environ.get("ENCRYPTION_KEY", "")
    )
    SESSION_SECRET = SESSION_ENC_KEY  # alias
    # Wroxen Trending (TMDB) — optional; Trending Now disabled until set.
    # Prefer TMDB_API_KEYS="key1,key2,..." (rotate on rate-limit). TMDB_API_KEY still works.
    TMDB_API_KEY = (os.environ.get("TMDB_API_KEY", "") or "").strip()
    TMDB_API_KEYS = (os.environ.get("TMDB_API_KEYS", "") or "").strip()
    TRENDING_UPDATE_INTERVAL = int(os.environ.get("TRENDING_UPDATE_INTERVAL", "7200") or 7200)
    BOT_TZ = (os.environ.get("BOT_TZ", "Asia/Kolkata") or "Asia/Kolkata").strip()

    @staticmethod
    def tmdb_api_keys() -> list[str]:
        """Ordered unique TMDB v3 API keys (multi-key rotation support)."""
        raw = []
        multi = (Config.TMDB_API_KEYS or "").strip()
        single = (Config.TMDB_API_KEY or "").strip()
        if multi:
            for part in multi.replace(";", ",").split(","):
                k = part.strip()
                if k:
                    raw.append(k)
        if single:
            raw.append(single)
        seen = set()
        out: list[str] = []
        for k in raw:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out


def validate_config(*, strict: bool = True) -> list[str]:
    """Return list of fatal config errors. strict=True for production boot."""
    errors: list[str] = []
    if not Config.API_ID or int(Config.API_ID) <= 0:
        errors.append("API_ID is missing or invalid")
    if not (Config.API_HASH or "").strip():
        errors.append("API_HASH is missing")
    if not (Config.BOT_TOKEN or "").strip():
        errors.append("BOT_TOKEN is missing")
    if not (Config.MONGO_URI or "").strip():
        errors.append("MONGO_URI is missing")
    if not (Config.ADMINS or Config.OWNER_IDS):
        errors.append("ADMINS or OWNER_IDS must include at least one Telegram user id")
    if strict and not (Config.SESSION_ENC_KEY or "").strip():
        errors.append(
            "SESSION_ENC_KEY is required in production "
            "(encrypts user sessions and bot tokens at rest)"
        )
    return errors


def owner_ids() -> list[int]:
    ids = list(Config.OWNER_IDS or [])
    if not ids and Config.ADMINS:
        ids = [Config.ADMINS[0]]
    return ids
