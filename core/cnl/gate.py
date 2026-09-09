"""CNL gate — stores per-user MongoDB URI pointer in main DB only."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from pymongo import ASCENDING
from core.cnl.constants import DEFAULT_DB_NAME
from core.security import decrypt_session, encrypt_session

logger = logging.getLogger(__name__)
COLL = "cnl_gate"

def mask_uri(uri: Optional[str]) -> str:
    if not uri:
        return "Not set"
    try:
        if "@" in uri:
            return f"••••@{uri.split('@', 1)[1].split('/', 1)[0]}"
        if "://" in uri:
            return f"••••@{uri.split('://', 1)[1].split('/', 1)[0]}"
        return "••••"
    except Exception:
        return "••••"

def db_name_from_uri(uri: str, default: str = DEFAULT_DB_NAME) -> str:
    try:
        path = (urlparse(uri).path or "").lstrip("/")
        name = path.split("?")[0].strip()
        if name:
            return name.split("/")[0]
    except Exception:
        pass
    return default

def _coll():
    from database import db
    return db.db[COLL]

async def ensure_gate_indexes() -> None:
    try:
        await _coll().create_index([("user_id", ASCENDING)], unique=True, name="cnl_gate_user")
        await _coll().create_index([("enabled", ASCENDING)], name="cnl_gate_enabled")
    except Exception:
        logger.exception("cnl_gate index")

async def get_gate(user_id: int) -> Optional[Dict[str, Any]]:
    return await _coll().find_one({"user_id": int(user_id)}, {"uri_encrypted": 0})

async def get_gate_uri_plain(user_id: int) -> Optional[str]:
    doc = await _coll().find_one({"user_id": int(user_id)}, {"uri_encrypted": 1})
    if not doc or not doc.get("uri_encrypted"):
        return None
    return decrypt_session(doc["uri_encrypted"])

async def is_cnl_configured(user_id: int) -> bool:
    try:
        from core.db_resolver import resolve_feature_db
        r = await resolve_feature_db(int(user_id), "cnl")
        return bool(r.get("configured") and r.get("uri"))
    except Exception:
        pass
    doc = await _coll().find_one(
        {"user_id": int(user_id), "enabled": True, "uri_encrypted": {"$exists": True, "$ne": ""}},
        {"_id": 1},
    )
    return doc is not None

async def set_gate_uri(user_id: int, uri: str, db_name: Optional[str] = None) -> bool:
    await ensure_gate_indexes()
    stored = encrypt_session((uri or "").strip())
    name = db_name or db_name_from_uri(uri)
    now = datetime.now(timezone.utc)
    await _coll().update_one(
        {"user_id": int(user_id)},
        {"$set": {
            "user_id": int(user_id), "uri_encrypted": stored, "db_name": name,
            "enabled": True, "updated_at": now,
        }, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return True

async def remove_gate(user_id: int) -> bool:
    return (await _coll().delete_one({"user_id": int(user_id)})).deleted_count > 0

async def list_enabled_gates() -> List[Dict[str, Any]]:
    """Gates that have a URI and are not explicitly disabled."""
    await ensure_gate_indexes()
    return await _coll().find(
        {
            "uri_encrypted": {"$exists": True, "$ne": ""},
            "$or": [{"enabled": True}, {"enabled": {"$exists": False}}],
        }
    ).to_list(length=None)


async def list_cnl_owner_ids() -> List[int]:
    """Every user who can open CNL with a working DB (same sources as get_cnl).

    Discovery must mirror resolve_feature_db / get_cnl:
      1) cnl_gate URI
      2) user_db_config.features.cnl
      3) user_db_config.global_uri (Global DB — common path)
      4) Config ADMINS / OWNER_IDS (privileged may use main / global)
      5) DB admins collection if present

    Previous bug: only (1)+(2) → Global-DB users got
    \"CNL: no configured owners yet\" after every restart.
    """
    ids: set = set()

    def _add(v) -> None:
        try:
            ids.add(int(v))
        except Exception:
            pass

    # 1) cnl_gate
    try:
        await ensure_gate_indexes()
        async for doc in _coll().find(
            {"uri_encrypted": {"$exists": True, "$ne": ""}},
            {"user_id": 1},
        ):
            _add(doc.get("user_id"))
    except Exception:
        logger.exception("list_cnl_owner_ids gates")

    try:
        from database import db
        if db.db is None:
            return sorted(ids)

        # 2+3) feature CNL URI OR Global DB URI
        try:
            cursor = db.db["user_db_config"].find(
                {
                    "$or": [
                        {"features.cnl.uri_encrypted": {"$exists": True, "$ne": ""}},
                        {"features.cnl.enabled": True},
                        {"global_uri_encrypted": {"$exists": True, "$ne": ""}},
                        {"global_enabled": True},
                    ]
                },
                {"user_id": 1},
            )
            async for doc in cursor:
                _add(doc.get("user_id"))
        except Exception:
            logger.debug("list_cnl_owner_ids user_db_config scan failed", exc_info=True)

        # 4) Config admins / owners — get_cnl often works via main/global for them
        try:
            from config import Config
            for x in list(Config.ADMINS or []) + list(Config.OWNER_IDS or []):
                _add(x)
        except Exception:
            pass

        # 5) Runtime admin list in main DB (if used)
        try:
            async for doc in db.db["admins"].find({}, {"user_id": 1}):
                _add(doc.get("user_id"))
        except Exception:
            pass
        try:
            async for doc in db.users.find(
                {"$or": [{"is_admin": True}, {"role": "admin"}, {"role": "owner"}]},
                {"user_id": 1},
            ):
                _add(doc.get("user_id"))
        except Exception:
            pass

    except Exception:
        logger.exception("list_cnl_owner_ids main scan")

    out = sorted(ids)
    logger.info("list_cnl_owner_ids → %s user(s): %s", len(out), out[:20])
    return out
