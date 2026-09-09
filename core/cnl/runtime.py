"""CNL runtime — bind enabled rules only on process start (restart / redeploy).

Jobs keep a continuous poll loop because work is cursor-based in Mongo.
CNL needs live bot/account clients; those are started once when the
management process boots. Manual Enable still starts a client immediately.
There is NO periodic rebind every N seconds — only boot + explicit UI actions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from core.cnl.db import get_cnl, close_all_cnl

logger = logging.getLogger(__name__)

# Let management bot + event loop settle before first bind (same window as Jobs resume)
CNL_BOOT_DELAY = 3

_boot_task: Optional[asyncio.Task] = None


def _bot_needed_running(uid: int, rule: dict) -> bool:
    from core.cnl.bots import get_user_bot_manager
    bid = rule.get("my_bot_id") or rule.get("exec_bot_id")
    if not bid:
        return get_user_bot_manager().is_running(uid)
    return get_user_bot_manager().is_running(uid, str(bid))


def _acc_needed_running(uid: int, account_id: str) -> bool:
    from core.cnl.clients import get_user_client_manager
    return get_user_client_manager().is_running(uid, str(account_id))


async def ensure_user_cnl_clients(uid: int, *, reason: str = "boot") -> dict:
    """Start clients for enabled rules + Global Copy (same path as UI Enable)."""
    from core.lifecycle import on_cnl_rule_saved, acquire_my_account

    stats = {"rules_ok": 0, "rules_fail": 0, "gcopy": None, "skipped_already": 0}
    cnl = await get_cnl(uid)
    if not cnl:
        logger.warning("CNL ensure user=%s: DB not available (%s)", uid, reason)
        return stats

    try:
        rules = await cnl.forward_rules.find({"owner_id": int(uid)}).to_list(1000)
    except Exception:
        logger.exception("CNL ensure: load rules user=%s", uid)
        return stats

    for r in rules:
        if r.get("enabled") is False:
            continue
        via = (r.get("forward_via") or "user_bot").lower()
        try:
            if via == "user_bot":
                if _bot_needed_running(uid, r):
                    stats["skipped_already"] += 1
                    continue
            elif via in ("user_account", "user"):
                aid = r.get("my_account_id") or r.get("exec_account_id")
                if aid and _acc_needed_running(uid, str(aid)):
                    stats["skipped_already"] += 1
                    continue
            else:
                continue

            ok, msg = await on_cnl_rule_saved(uid, r)
            if ok:
                stats["rules_ok"] += 1
                logger.info(
                    "CNL boot-bind rule %s→%s user=%s via=%s → %s",
                    r.get("source_chat_id"),
                    r.get("target_chat_id"),
                    uid,
                    via,
                    msg,
                )
            else:
                stats["rules_fail"] += 1
                logger.warning(
                    "CNL boot-bind rule %s→%s user=%s FAILED: %s | my_bot_id=%s my_account_id=%s",
                    r.get("source_chat_id"),
                    r.get("target_chat_id"),
                    uid,
                    msg,
                    r.get("my_bot_id") or r.get("exec_bot_id"),
                    r.get("my_account_id") or r.get("exec_account_id"),
                )
        except Exception:
            stats["rules_fail"] += 1
            logger.exception(
                "CNL boot-bind exception user=%s rule %s→%s",
                uid, r.get("source_chat_id"), r.get("target_chat_id"),
            )

    try:
        gc = await cnl.get_global_copy(uid)
        if gc and gc.get("enabled") and gc.get("my_account_id"):
            aid = str(gc["my_account_id"])
            if _acc_needed_running(uid, aid):
                stats["gcopy"] = "already"
            else:
                ok, msg = await acquire_my_account(uid, aid, "cnl:gcopy")
                stats["gcopy"] = "ok" if ok else f"fail:{msg}"
                logger.info(
                    "CNL boot-bind Global Copy user=%s account=%s → %s",
                    uid, aid, stats["gcopy"],
                )
    except Exception:
        logger.exception("CNL boot-bind Global Copy user=%s", uid)

    return stats


async def ensure_all_cnl_clients(*, reason: str = "boot") -> None:
    """One-shot: bind all CNL owners (called only on process start)."""
    from core.cnl.gate import list_cnl_owner_ids
    from core.cnl.bots import get_user_bot_manager
    from core.cnl.clients import get_user_client_manager

    owners = await list_cnl_owner_ids()
    if not owners:
        logger.warning(
            "CNL boot-bind: owner list empty — check cnl_gate / Global DB / ADMINS",
        )
        return

    logger.info("CNL boot-bind: probing %s candidate owner(s)", len(owners))
    any_ok = False
    for uid in owners:
        try:
            cnl = await get_cnl(uid)
            if not cnl:
                logger.debug("CNL boot-bind user=%s: no CNL DB (skip)", uid)
                continue
            any_ok = True
            await ensure_user_cnl_clients(uid, reason=reason)
            bots = get_user_bot_manager().running_count(uid)
            amgr = get_user_client_manager()
            accs = sum(
                1
                for k, c in getattr(amgr, "_clients", {}).items()
                if (k == str(uid) or k.startswith(f"{uid}:"))
                and getattr(c, "is_connected", False)
            )
            logger.info(
                "CNL boot-bind user=%s · bots_online=%s · accounts_online=%s",
                uid, bots, accs,
            )
        except Exception:
            logger.exception("CNL boot-bind user=%s failed", uid)
    if not any_ok:
        logger.warning(
            "CNL boot-bind: no candidate could open CNL DB — rules will not rebind",
        )


async def _boot_bind_once() -> None:
    try:
        await asyncio.sleep(CNL_BOOT_DELAY)
    except asyncio.CancelledError:
        return
    try:
        await ensure_all_cnl_clients(reason="boot")
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("CNL boot-bind failed")
    logger.info("CNL boot-bind finished (no periodic rebind — only on restart/redeploy)")


async def start_cnl_runtime():
    """Schedule a single boot bind after short delay. No continuous loop."""
    global _boot_task
    if _boot_task and not _boot_task.done():
        logger.info("CNL boot-bind already scheduled")
        return
    _boot_task = asyncio.create_task(_boot_bind_once(), name="cnl_boot_bind")
    logger.info(
        "CNL boot-bind scheduled (delay=%ss · only on restart/redeploy, not periodic)",
        CNL_BOOT_DELAY,
    )


async def stop_cnl_runtime():
    global _boot_task
    t = _boot_task
    _boot_task = None
    if t and not t.done():
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    try:
        from core.cnl.bots import get_user_bot_manager
        from core.cnl.clients import get_user_client_manager
        await get_user_bot_manager().stop_all()
        await get_user_client_manager().stop_all()
    except Exception:
        logger.exception("cnl runtime stop clients")
    try:
        await close_all_cnl()
    except Exception:
        logger.exception("cnl close")
