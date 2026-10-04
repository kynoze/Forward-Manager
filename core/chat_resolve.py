"""Resolve chats WITHOUT requiring the management bot to join/admin.

Resolution order:
1. Active user-account clients (get_chat)
2. Same accounts: scan recent dialogs for matching id (peer cache warm)
3. Active forward-bot clients (get_chat) — bot already in channel knows peer
4. Management bot only for public @username
5. Numeric private IDs may still be accepted as unresolved — caller can
   defer full resolve to the selected executor on permission check.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

from pyrogram import Client

logger = logging.getLogger(__name__)

_TME = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:c/)?([a-zA-Z0-9_]+|\d+)",
    re.I,
)


def parse_chat_ref(raw: str) -> Tuple[Optional[int], Optional[str], str]:
    """Return (chat_id or None, username or None, display_ref)."""
    raw = (raw or "").strip()
    if not raw:
        return None, None, ""
    if raw.startswith("@"):
        return None, raw, raw
    m = _TME.search(raw)
    if m:
        part = m.group(1)
        if part.isdigit():
            cid = int(f"-100{part}")
            return cid, None, str(cid)
        uname = part if part.startswith("@") else f"@{part}"
        return None, uname, uname
    # pure numeric / -100...
    try:
        if raw.lstrip("-").isdigit():
            cid = int(raw)
            if cid > 0:
                # user pasted internal channel id without -100
                cid = int(f"-100{cid}")
            return cid, None, str(cid)
    except Exception:
        pass
    return None, raw if raw.startswith("@") else None, raw


async def _try_get_chat(client: Client, ref) -> Tuple[Optional[Any], Optional[str]]:
    try:
        if isinstance(ref, str) and ref.lstrip("-").isdigit():
            ref = int(ref)
        chat = await client.get_chat(ref)
        return chat, None
    except Exception as e:
        return None, type(e).__name__


async def _find_in_dialogs(client: Client, chat_id: int) -> Optional[Any]:
    """Warm peer cache by scanning dialogs; return chat if id matches."""
    try:
        async for d in client.get_dialogs(limit=200):
            ch = getattr(d, "chat", None)
            if ch is not None and int(getattr(ch, "id", 0) or 0) == int(chat_id):
                return ch
    except Exception:
        logger.exception("dialog scan failed")
    return None


async def _iter_user_clients(user_id: int, account_ids: Optional[list] = None):
    from database import get_user_accounts, get_account, AccountStatus
    from core.job_worker import get_user_client
    from handlers.ui import active_accounts_only

    accounts: List[dict] = []
    if account_ids:
        for aid in account_ids:
            a = await get_account(user_id, str(aid))
            if a:
                accounts.append(a)
    else:
        accounts = await get_user_accounts(user_id) or []
    accounts = active_accounts_only(accounts)
    for acc in accounts:
        if (acc.get("status") or "").lower() == AccountStatus.SLEEPING.value:
            # still try — useful for resolve
            pass
        uc = await get_user_client(acc)
        if uc:
            yield acc, uc


async def _iter_bot_clients(user_id: int):
    from database import get_user_bots
    from core.job_worker import get_bot_client

    bots = await get_user_bots(user_id) or []
    for b in bots:
        st = (b.get("status") or "active").lower()
        if st in ("disabled", "inactive", "error"):
            continue
        try:
            bc = await get_bot_client(b)
        except Exception:
            bc = None
        if bc:
            yield b, bc


async def resolve_chat_for_user(
    mgmt_client: Client,
    user_id: int,
    raw: str,
    *,
    account_ids: Optional[list] = None,
) -> Tuple[Optional[Any], str]:
    """Resolve channel/group. Returns (chat, error). error empty on success.

    If only a numeric id is known and no client can resolve yet, returns
    (None, special) — callers for target-add may accept unresolved numeric ids.
    """
    chat_id, username, display = parse_chat_ref(raw)
    last_err = "unknown"
    tried = 0

    refs: list = []
    if username:
        refs.append(username)
    if chat_id is not None:
        refs.append(chat_id)
        # also try without forcing -100 if user sent full id
        refs.append(chat_id)

    # 1) User accounts — get_chat
    async for acc, uc in _iter_user_clients(user_id, account_ids):
        tried += 1
        for ref in refs:
            chat, err = await _try_get_chat(uc, ref)
            if chat:
                return chat, ""
            last_err = err or last_err
        if chat_id is not None:
            found = await _find_in_dialogs(uc, int(chat_id))
            if found:
                return found, ""

    # 2) Forward bots already in the chat often know the peer
    async for bot, bc in _iter_bot_clients(user_id):
        tried += 1
        for ref in refs:
            chat, err = await _try_get_chat(bc, ref)
            if chat:
                return chat, ""
            last_err = err or last_err

    # 3) Management bot — public username only
    if username:
        chat, err = await _try_get_chat(mgmt_client, username)
        if chat:
            return chat, ""
        last_err = err or last_err

    if tried == 0:
        return None, (
            "No **active** user account or bot available to resolve this chat.\n"
            "Add/enable a My Account or Forward Bot first."
        )

    # Numeric private id: allow caller to continue with unresolved id
    if chat_id is not None and not username:
        return None, f"UNRESOLVED:{chat_id}:{last_err}"

    return None, (
        f"Could not resolve that chat (`{last_err}`).\n\n"
        "• Use **@username** or invite link when possible\n"
        "• For private channels: the linked account must be a **member**\n"
        "• Or **forward a message** from that chat to this bot\n"
        "• Management Bot does **not** need to be a member or admin"
    )


async def resolve_source_chat_id(
    mgmt_client: Client,
    user_id: int,
    source_chat_id,
    *,
    account_ids: Optional[list] = None,
) -> Tuple[Optional[Any], str]:
    return await resolve_chat_for_user(
        mgmt_client, user_id, str(source_chat_id), account_ids=account_ids
    )


_PLACEHOLDER_TITLES = frozenset(
    {"source", "unknown", "—", "-", "detected source", "chat"}
)


def is_placeholder_title(title: Optional[str]) -> bool:
    s = (title or "").strip()
    if not s:
        return True
    if s.lstrip("-").isdigit():
        return True
    low = s.lower()
    if low in _PLACEHOLDER_TITLES:
        return True
    if low.startswith("chat ") and s[5:].lstrip("-").isdigit():
        return True
    return False


def clean_chat_title(title: Optional[str], fallback: str = "") -> str:
    s = (title or "").strip()
    if is_placeholder_title(s):
        return (fallback or "").strip()
    return s


async def resolve_title_via_job_executor(
    user_id: int,
    chat_id,
    *,
    method: str = "",
    bot_id: Optional[str] = None,
    account_ids: Optional[list] = None,
    fallback: str = "",
) -> str:
    """Resolve a real chat title using the SAME bot/account that will forward.

    Management bot is never used — it is often not a member of the source,
    which previously left Jobs named "Source" unless it happened to be admin.
    """
    kept = clean_chat_title(fallback)
    if chat_id is None or chat_id == "":
        return kept or (fallback or "Source")

    method = (method or "").lower()
    clients = []

    from database import get_account, get_bot
    from core.job_worker import get_user_client, get_bot_client

    ids = [str(a) for a in (account_ids or []) if a]
    if method in ("user", "account") or (ids and method != "bot"):
        for aid in ids:
            try:
                acc = await get_account(user_id, aid)
                if not acc:
                    continue
                uc = await get_user_client(acc)
                if uc:
                    clients.append(uc)
            except Exception:
                logger.debug("title via account %s failed", aid, exc_info=True)

    bid = str(bot_id) if bot_id else ""
    if bid and bid != "__mgmt__" and method != "user":
        try:
            bot = await get_bot(user_id, bid)
            if bot and not bot.get("is_mgmt"):
                bc = await get_bot_client(bot)
                if bc:
                    clients.append(bc)
        except Exception:
            logger.debug("title via bot %s failed", bid, exc_info=True)

    for c in clients:
        try:
            ref = chat_id
            if isinstance(ref, str) and ref.lstrip("-").isdigit():
                ref = int(ref)
            ch = await c.get_chat(ref)
            t = clean_chat_title(
                getattr(ch, "title", None)
                or getattr(ch, "first_name", None)
                or getattr(ch, "username", None)
            )
            if t:
                return t
        except Exception:
            continue

    return kept or (fallback or "").strip() or str(chat_id)
