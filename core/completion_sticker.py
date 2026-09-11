"""Per-Job completion stickers for logical movie/series groups.

Batch model (not lifetime-per-title):
  - A consecutive run of the same title is one batch.
  - When the title changes (or the open batch is flushed), send 1 sticker
    for the batch that just ended (if it had >=1 successful forward).
  - If the same title appears again later (after other titles), that is a
    NEW batch and gets its own sticker when it completes.

This matches channel packs and interleaved re-posts without permanently
locking a title after the first sticker.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _stickers(job: dict) -> list:
    raw = job.get("completion_stickers") or []
    out = []
    for s in raw:
        sid = str(s).strip() if s is not None else ""
        if sid:
            out.append(sid)
    return out


def feature_on(job: Optional[dict]) -> bool:
    if not job:
        return False
    return bool(job.get("completion_sticker_enabled")) and bool(_stickers(job))


def pick_sticker(file_ids: list) -> Optional[str]:
    ids = [str(x).strip() for x in (file_ids or []) if x]
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    return secrets.choice(ids)


def _active_entry(raw) -> Optional[dict]:
    """Normalize completion_active[target] to {key, forwarded, sticker_sent}."""
    if not raw:
        return None
    if isinstance(raw, str):
        # Legacy: active was plain group_key string
        return {"key": raw, "forwarded": 1, "sticker_sent": False}
    if isinstance(raw, dict) and raw.get("key"):
        return {
            "key": str(raw["key"]),
            "forwarded": int(raw.get("forwarded") or 0),
            "sticker_sent": bool(raw.get("sticker_sent")),
        }
    return None


async def _save_active(user_id: int, job_id: str, active: dict):
    from database import update_job
    await update_job(user_id, job_id, {"completion_active": active})


async def send_completion_sticker(client, target_chat_id: int, file_id: str) -> bool:
    """Send one sticker with FloodWait respect. False = not confirmed sent."""
    if not client or not file_id:
        return False
    try:
        await client.send_sticker(int(target_chat_id), file_id)
        return True
    except Exception as e:
        name = type(e).__name__
        if name in ("FloodWait", "SlowmodeWait"):
            wait = int(getattr(e, "value", 0) or 0)
            logger.warning(
                "completion sticker FloodWait %ss target=%s", wait, target_chat_id
            )
            try:
                import asyncio
                await asyncio.sleep(min(max(wait, 1), 120))
                await client.send_sticker(int(target_chat_id), file_id)
                return True
            except Exception:
                logger.exception("completion sticker retry failed")
                return False
        logger.exception("completion sticker send failed target=%s", target_chat_id)
        return False


async def _send_for_batch(
    client,
    user_id: int,
    job: dict,
    target_chat_id: int,
    entry: dict,
) -> bool:
    """Send sticker for one finished batch. Returns True if sent."""
    if not entry or entry.get("sticker_sent"):
        return False
    if int(entry.get("forwarded") or 0) < 1:
        return False
    if not bool(job.get("completion_sticker_enabled")):
        return False
    ids = _stickers(job)
    if not ids:
        return False
    fid = pick_sticker(ids)
    if not fid:
        return False
    ok = await send_completion_sticker(client, target_chat_id, fid)
    if ok:
        logger.debug(
            "Job %s completion sticker sent target=%s group=%s forwarded=%s",
            job.get("job_id"),
            target_chat_id,
            entry.get("key"),
            entry.get("forwarded"),
        )
    else:
        logger.warning(
            "Job %s completion sticker FAILED target=%s group=%s",
            job.get("job_id"),
            target_chat_id,
            entry.get("key"),
        )
    return ok


async def flush_active_group(client, user_id: int, job_id: str, target_chat_id: int) -> None:
    """Close the open batch for this target (end of historical target / monitor stop)."""
    from database import get_job

    job = await get_job(user_id, job_id) or {}
    if not bool(job.get("completion_sticker_enabled")):
        return
    active = dict(job.get("completion_active") or {})
    tkey = str(int(target_chat_id))
    entry = _active_entry(active.get(tkey))
    if not entry:
        return
    await _send_for_batch(client, user_id, job, int(target_chat_id), entry)
    active.pop(tkey, None)
    await _save_active(user_id, job_id, active)


async def prepare_before_forward(
    client,
    user_id: int,
    job_id: str,
    target_chat_id: int,
    message: Any,
) -> None:
    """If title batch changes, send sticker for the previous batch BEFORE this file."""
    if not job_id:
        return
    from database import get_job
    from core.content_type import get_content_group_key

    job = await get_job(user_id, job_id) or {}
    if not bool(job.get("completion_sticker_enabled")):
        return
    if not _stickers(job):
        return

    group_key = get_content_group_key(message)
    active = dict(job.get("completion_active") or {})
    tkey = str(int(target_chat_id))
    entry = _active_entry(active.get(tkey))
    prev_key = entry.get("key") if entry else None

    # Same batch continues — nothing to close
    if entry and prev_key == group_key:
        return

    # Title changed or non-media interrupted a batch → close previous batch
    if entry and prev_key and prev_key != group_key:
        logger.debug(
            "Job %s title change target=%s %s → %s (send completion sticker)",
            job_id,
            target_chat_id,
            prev_key,
            group_key,
        )
        await _send_for_batch(client, user_id, job, int(target_chat_id), entry)
        active.pop(tkey, None)
        await _save_active(user_id, job_id, active)


async def on_successful_forward(
    client,
    user_id: int,
    job_id: str,
    target_chat_id: int,
    message: Any,
) -> None:
    """After a successful forward: track this message into the current open batch."""
    if not job_id:
        return
    from database import get_job
    from core.content_type import get_content_group_key

    job = await get_job(user_id, job_id) or {}
    if not bool(job.get("completion_sticker_enabled")):
        return

    group_key = get_content_group_key(message)
    active = dict(job.get("completion_active") or {})
    tkey = str(int(target_chat_id))
    entry = _active_entry(active.get(tkey))
    prev_key = entry.get("key") if entry else None

    # Safety: prepare_before_forward should already have closed prev; if not, close now
    if entry and prev_key and prev_key != group_key:
        await _send_for_batch(client, user_id, job, int(target_chat_id), entry)
        entry = None
        active.pop(tkey, None)

    if not group_key:
        if entry:
            active.pop(tkey, None)
            await _save_active(user_id, job_id, active)
        return

    if entry and entry.get("key") == group_key:
        entry["forwarded"] = int(entry.get("forwarded") or 0) + 1
        entry["sticker_sent"] = False
        active[tkey] = entry
    else:
        # New batch (first file of this title run)
        active[tkey] = {
            "key": group_key,
            "forwarded": 1,
            "sticker_sent": False,
        }

    await _save_active(user_id, job_id, active)
