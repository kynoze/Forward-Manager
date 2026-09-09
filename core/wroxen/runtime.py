"""Run Wroxen bot clients: event-driven auto-index + group search.

Each unique bot_token used by active Wroxen configs gets one Client.
No userbot. Handlers attached when the client starts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import CallbackQuery, Message

from config import Config
from core.security import decrypt_session
from core.wroxen import db as wxdb
from core.wroxen.indexer import index_message_to_db
from core.wroxen.search import (
    RESULTS_PER_PAGE,
    build_results_text,
    format_result_line,
    get_cached,
    pagination_keyboard,
    recall_query,
    remember_query,
    set_cached,
)

logger = logging.getLogger(__name__)

def _cb(*parts: str) -> str:
    """Join callback parts; hard-cap 64 bytes (Telegram limit)."""
    s = ":".join(str(p) for p in parts if p is not None and str(p) != "")
    if len(s.encode("utf-8")) > 64:
        # drop trailing qhash-like segment if present
        bits = s.split(":")
        while len(":".join(bits).encode("utf-8")) > 64 and len(bits) > 4:
            bits.pop()
        s = ":".join(bits)
    return s[:64]




async def _safe_edit_message(message, text: str, *, reply_markup=None, **kwargs) -> bool:
    """edit_text with FloodWait / MessageNotModified handling. Returns True on success."""
    for attempt in range(3):
        try:
            await message.edit_text(text, reply_markup=reply_markup, **kwargs)
            return True
        except MessageNotModified:
            return True
        except FloodWait as e:
            wait = int(getattr(e, "value", None) or getattr(e, "x", None) or 5)
            wait = max(1, min(wait, 30))
            logger.warning("FloodWait %ss on edit_message — sleeping", wait)
            await asyncio.sleep(wait + 0.5)
            continue
        except Exception as e:
            logger.warning("safe_edit failed: %s: %s", type(e).__name__, e)
            return False
    return False


async def _safe_edit_markup(message, reply_markup) -> bool:
    try:
        await message.edit_reply_markup(reply_markup)
        return True
    except MessageNotModified:
        return True
    except FloodWait as e:
        wait = int(getattr(e, "value", None) or getattr(e, "x", None) or 5)
        wait = max(1, min(wait, 30))
        try:
            await asyncio.sleep(wait + 0.5)
            await message.edit_reply_markup(reply_markup)
            return True
        except Exception:
            return False
    except Exception:
        return False

# bot_id -> Client
_CLIENTS: Dict[str, Client] = {}
# bot_id -> owner_user_id (management user who owns configs)
_BOT_OWNER: Dict[str, int] = {}
# target_chat_id -> list of (owner_user_id, wroxen_id, bot_id)
_TARGET_MAP: Dict[int, List[tuple]] = {}
# source_chat_id -> list of (owner_user_id, wroxen_id, bot_id)
_SOURCE_MAP: Dict[int, List[tuple]] = {}
_STARTED: Set[str] = set()
_lock = asyncio.Lock()


def _rebuild_maps(configs: List[Dict[str, Any]]) -> None:
    _TARGET_MAP.clear()
    _SOURCE_MAP.clear()
    for c in configs:
        if not c.get("enabled", True):
            continue
        wid = c["wroxen_id"]
        owner = c["user_id"]
        bot_id = c["bot_id"]
        src = int(c["source_chat_id"])
        tgt = int(c["target_chat_id"])
        _SOURCE_MAP.setdefault(src, []).append((owner, wid, bot_id))
        _TARGET_MAP.setdefault(tgt, []).append((owner, wid, bot_id))


async def refresh_routing() -> None:
    """Reload active configs from main DB and ensure bot clients running."""
    from database import list_all_enabled_wroxen

    configs = await list_all_enabled_wroxen()
    _rebuild_maps(configs)

    needed_bots: Dict[str, Dict] = {}
    for c in configs:
        if not c.get("enabled", True):
            continue
        needed_bots[c["bot_id"]] = c

    # stop unused
    for bot_id in list(_CLIENTS.keys()):
        if bot_id not in needed_bots:
            await stop_bot(bot_id)

    for bot_id, cfg in needed_bots.items():
        if bot_id not in _CLIENTS:
            await start_bot_for_config(cfg)


async def start_bot_for_config(cfg: Dict[str, Any]) -> Optional[Client]:
    bot_id = cfg["bot_id"]
    owner = cfg["user_id"]
    if bot_id in _CLIENTS:
        _BOT_OWNER[bot_id] = owner
        return _CLIENTS[bot_id]

    from database import get_bot

    bot_doc = await get_bot(owner, bot_id)
    if not bot_doc:
        logger.error("Wroxen bot doc missing %s", bot_id)
        return None

    token = bot_doc.get("bot_token")
    try:
        token = decrypt_session(token)
    except Exception:
        logger.exception("decrypt wroxen bot token")
        return None
    if not token:
        return None

    client = Client(
        name=f"wroxen_{bot_id}",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=token,
        in_memory=True,
        parse_mode=ParseMode.HTML,
    )

    # Auto-index: media in source chats
    @client.on_message(
        (filters.channel | filters.group)
        & (filters.video | filters.document | filters.photo | filters.audio | filters.animation)
    )
    async def _auto_index(c: Client, message: Message):
        try:
            chat_id = message.chat.id
            entries = _SOURCE_MAP.get(chat_id) or []
            for owner_uid, wroxen_id, bid in entries:
                if bid != bot_id:
                    continue
                # ensure DB
                from core.db_resolver import resolve_feature_db
                resolved = await resolve_feature_db(owner_uid, "wroxen")
                uri = resolved.get("uri")

                if not uri:
                    continue
                ok, _ = await wxdb.ensure_connected(owner_uid, uri)
                if not ok:
                    continue
                result = await index_message_to_db(owner_uid, wroxen_id, chat_id, message)
                if result == "saved":
                    logger.info("Wroxen auto-index %s msg %s", wroxen_id, message.id)
        except Exception:
            logger.exception("wroxen auto-index")

    # Search: text in target groups
    @client.on_message(filters.group & filters.text & ~filters.command(["start"]))
    async def _search(c: Client, message: Message):
        if not message.from_user:
            return
        query = (message.text or "").strip()
        if not query or query.startswith(("/", ".", "!", ",")):
            return
        chat_id = message.chat.id
        entries = _TARGET_MAP.get(chat_id) or []
        # Prefer config whose bot_id matches this client
        matched = [(o, w, b) for o, w, b in entries if b == bot_id]
        if not matched:
            return
        owner_uid, wroxen_id, _ = matched[0]
        try:
            from core.db_resolver import resolve_feature_db
            resolved = await resolve_feature_db(owner_uid, "wroxen")
            uri = resolved.get("uri")

            if not uri:
                return
            ok, _ = await wxdb.ensure_connected(owner_uid, uri)
            if not ok:
                return

            cached = get_cached(wroxen_id, query)
            if cached:
                results, total = cached
            else:
                data = await wxdb.search_media(owner_uid, wroxen_id, query, limit=200)
                results = data["results"]
                total = data["total"]
                set_cached(wroxen_id, query, results, total)

            if not results:
                return

            # Trending feature (button + Top Searches analytics) — default OFF
            tr_on = False
            try:
                from database import is_wroxen_trending_enabled
                tr_on = await is_wroxen_trending_enabled(wroxen_id)
            except Exception:
                tr_on = False

            if tr_on:
                # Successful search → Top Searches counts *canonical* Wroxen title
                try:
                    from core.wroxen.trending.analytics import record_successful_search
                    canonical = (results[0].get("title") or "").strip() or query
                    await record_successful_search(chat_id, canonical)
                except Exception:
                    logger.debug("wroxen search analytics failed", exc_info=True)

            remember_query(query)
            pages = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
            # build page 1 text
            from html import escape

            text = (
                f"<b>🔎 Results for:</b> <code>{escape(query)}</code>\n"
                f"📄 Page 1/{pages} • Total: {total}\n\n"
            )
            for i, movie in enumerate(results[:RESULTS_PER_PAGE], start=1):
                text += format_result_line(i, movie)

            kb = pagination_keyboard(
                wroxen_id, query, 1, pages, message.from_user.id,
                include_trending=tr_on,
            )
            try:
                from pyrogram.types import LinkPreviewOptions
                _lp = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
            except Exception:
                _lp = {"link_preview_options": {"is_disabled": True}}
            await message.reply_text(
                text,
                reply_markup=kb,
                **_lp,
            )
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            logger.exception("wroxen search")

    @client.on_callback_query(filters.regex(r"^wxpage:"))
    async def _page(c: Client, cq: CallbackQuery):
        try:
            # wxpage:wroxen_id:page:owner_id:qhash
            parts = cq.data.split(":")
            wroxen_id = parts[1]
            page = int(parts[2])
            owner_id = int(parts[3])
            qhash = parts[4]
        except Exception:
            return await cq.answer("Invalid", show_alert=True)

        if cq.from_user and cq.from_user.id != owner_id:
            return await cq.answer("Not for you!", show_alert=True)

        query = recall_query(qhash)
        if not query:
            return await cq.answer("Cache expired — search again", show_alert=True)

        # find owner from target map for this wroxen
        owner_uid = None
        for entries in _TARGET_MAP.values():
            for o, w, b in entries:
                if w == wroxen_id and b == bot_id:
                    owner_uid = o
                    break
            if owner_uid:
                break
        if owner_uid is None:
            return await cq.answer("Config not found", show_alert=True)

        cached = get_cached(wroxen_id, query)
        if not cached:
            return await cq.answer("Cache expired — search again", show_alert=True)
        results, total = cached
        pages = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        page = max(1, min(page, pages))
        start = (page - 1) * RESULTS_PER_PAGE
        end = start + RESULTS_PER_PAGE
        from html import escape

        text = (
            f"<b>🔎 Results for:</b> <code>{escape(query)}</code>\n"
            f"📄 Page {page}/{pages} • Total: {total}\n\n"
        )
        for i, movie in enumerate(results[start:end], start=start + 1):
            text += format_result_line(i, movie)
        tr_on = False
        try:
            from database import is_wroxen_trending_enabled
            tr_on = await is_wroxen_trending_enabled(wroxen_id)
        except Exception:
            tr_on = False
        kb = pagination_keyboard(
            wroxen_id, query, page, pages, owner_id,
            include_trending=tr_on,
        )
        try:
            from pyrogram.types import LinkPreviewOptions
            _lp = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        except Exception:
            _lp = {"link_preview_options": {"is_disabled": True}}
        ok = await _safe_edit_message(cq.message, text, reply_markup=kb, **_lp)
        if ok:
            await cq.answer()
        else:
            await cq.answer("Try again in a moment", show_alert=True)


    # ── Wroxen Trending / Top Searches (callbacks on search bot) ──────────
    @client.on_callback_query(filters.regex(r"^wxtr:"))
    async def _trending_cb(c: Client, cq: CallbackQuery):
        try:
            await _handle_trending_callback(c, cq, bot_id)
        except Exception:
            logger.exception("wroxen trending callback")
            try:
                await cq.answer("Error", show_alert=True)
            except Exception:
                pass

    try:
        await client.start()
        _CLIENTS[bot_id] = client
        _BOT_OWNER[bot_id] = owner
        _STARTED.add(bot_id)
        me = await client.get_me()
        logger.info("Wroxen bot started: @%s (%s)", me.username, bot_id)
        return client
    except Exception:
        logger.exception("Failed to start Wroxen bot %s", bot_id)
        try:
            from core.log_chat import report_user_auto_stop
            await report_user_auto_stop(
                owner,
                feature="Wroxen Search",
                title=cfg.get("name") or bot_id,
                reason="Wroxen bot client failed to start. Search/auto-index is stopped.",
                error="client.start failed — see bot logs",
            )
        except Exception:
            pass
        return None


async def stop_bot(bot_id: str) -> None:
    client = _CLIENTS.pop(bot_id, None)
    _BOT_OWNER.pop(bot_id, None)
    _STARTED.discard(bot_id)
    if client:
        try:
            await client.stop()
        except Exception:
            pass


async def stop_all() -> None:
    for bot_id in list(_CLIENTS.keys()):
        await stop_bot(bot_id)


async def get_client(bot_id: str) -> Optional[Client]:
    return _CLIENTS.get(bot_id)



# ── Trending UI helpers (search-bot callbacks) ─────────────────────────────

_PERIOD_LABELS = {
    "1d": "Today",
    "3d": "Last 3 Days",
    "7d": "Last 7 Days",
    "15d": "Last 15 Days",
    "30d": "Last 30 Days",
}

_TRENDING_PER_PAGE = 10  # titles per page (Trending On Internet + Top Searches)
_BTN_PER_ROW = 5  # numbered result buttons per keyboard row


def _owner_for_wroxen(wroxen_id: str, bot_id: str):
    """Resolve management owner_uid for this Wroxen + search bot."""
    for entries in _TARGET_MAP.values():
        for o, w, b in entries:
            if w == wroxen_id and b == bot_id:
                return o
    return None


async def _handle_trending_callback(c: Client, cq: CallbackQuery, bot_id: str) -> None:
    from html import escape
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    data = cq.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return await cq.answer("Invalid", show_alert=True)
    action = parts[1]

    def _ids():
        wid = parts[2] if len(parts) > 2 else ""
        oid = 0
        if len(parts) > 3:
            try:
                oid = int(parts[3])
            except Exception:
                oid = 0
        return wid, oid

    # Feature gate — default OFF. Stops all wxtr:* when owner disabled Trending.
    _wid_gate, _ = _ids()
    if _wid_gate and action != "b":
        try:
            from database import is_wroxen_trending_enabled
            if not await is_wroxen_trending_enabled(_wid_gate):
                return await cq.answer(
                    "Trending is OFF for this Wroxen (enable in My Wroxen settings)",
                    show_alert=True,
                )
        except Exception:
            return await cq.answer("Trending unavailable", show_alert=True)

    if action in ("m", "n", "t", "s", "b", "c", "k") and len(parts) > 3:
        try:
            oid_chk = int(parts[3])
            if cq.from_user and cq.from_user.id != oid_chk:
                return await cq.answer("Not for you!", show_alert=True)
        except Exception:
            pass

    try:
        from pyrogram.types import LinkPreviewOptions
        _lp = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    except Exception:
        _lp = {"link_preview_options": {"is_disabled": True}}

    if action == "m":
        wid, oid = _ids()
        # Optional qhash from search-results "📈 Trending" so ◀️ Back can restore
        qhash = parts[4] if len(parts) > 4 else ""
        back_cb = f"wxtr:b:{wid}:{oid}:{qhash}" if qhash else f"wxtr:b:{wid}:{oid}"
        qh_sfx = f":{qhash}" if qhash else ""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🌐 Trending On Internet",
                callback_data=f"wxtr:n:{wid}:{oid}:G:a:1{qh_sfx}",
            )],
            [InlineKeyboardButton(
                "🔎 Top Searches",
                callback_data=f"wxtr:t:{wid}:{oid}:7d:1{qh_sfx}",
            )],
            [InlineKeyboardButton("◀️ Back", callback_data=back_cb)],
        ])
        await _safe_edit_message(
            cq.message,
            "<b>📈 Trending</b>\n\n"
            "🌐 <b>Trending On Internet</b> — TMDB global trending\n"
            "🔎 <b>Top Searches</b> — this group's successful searches",
            reply_markup=kb,
            **_lp,
        )
        return await cq.answer()

    if action == "b":
        # Restore previous search-results page when qhash is present
        wid, oid = _ids()
        qhash = parts[4] if len(parts) > 4 else ""
        query = recall_query(qhash) if qhash else None
        if query:
            owner_uid = _owner_for_wroxen(wid, bot_id)
            if owner_uid is None:
                return await cq.answer("Config not found", show_alert=True)
            cached = get_cached(wid, query)
            if not cached:
                return await cq.answer("Cache expired — search again", show_alert=True)
            results, total = cached
            pages = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
            page = 1
            start = 0
            end = RESULTS_PER_PAGE
            text = (
                f"<b>🔎 Results for:</b> <code>{escape(query)}</code>\n"
                f"📄 Page {page}/{pages} • Total: {total}\n\n"
            )
            for i, movie in enumerate(results[start:end], start=start + 1):
                text += format_result_line(i, movie)
            tr_on = False
            try:
                from database import is_wroxen_trending_enabled
                tr_on = await is_wroxen_trending_enabled(wid)
            except Exception:
                tr_on = False
            kb = pagination_keyboard(
                wid, query, page, pages, oid or owner_uid,
                include_trending=tr_on,
            )
            ok = await _safe_edit_message(cq.message, text, reply_markup=kb, **_lp)
            if not ok:
                return await cq.answer("Try again in a moment", show_alert=True)
            return await cq.answer()
        # No search context — leave a way back into Trending menu only
        await cq.answer("Search again to see results")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Trending", callback_data=f"wxtr:m:{wid}:{oid}")],
        ])
        await _safe_edit_markup(cq.message, kb)
        return

    # Legacy region / kind pickers (countries-wise removed) → redirect to global list
    if action in ("c", "k"):
        wid, oid = _ids()
        qhash = ""
        if action == "c" and len(parts) > 5 and parts[5]:
            qhash = parts[5]
        elif action == "k" and len(parts) > 5 and parts[5]:
            qhash = parts[5]
        qh_sfx = f":{qhash}" if qhash else ""
        # Re-dispatch as global TMDB list
        parts = ["wxtr", "n", wid, str(oid), "G", "a", "1"]
        if qhash:
            parts.append(qhash)
        action = "n"
        # fall through to action == "n"

    # Trending titles (global TMDB only — countries-wise removed)
    # parts: wxtr:n:wid:oid:region:kind:page[:qhash]
    if action == "n":
        wid, oid = _ids()
        # Always global; ignore any legacy region token
        region = "global"
        # kind short: m|t|a (also movie|tv|all legacy) — still support filter
        kind = "all"
        page = 1
        qhash = ""
        if len(parts) > 5:
            if str(parts[5]).isdigit():
                page = int(parts[5])
                qhash = parts[6] if len(parts) > 6 and parts[6] else ""
            else:
                kraw = (parts[5] or "a").lower()
                if kraw in ("m", "movie", "movies"):
                    kind = "movie"
                elif kraw in ("t", "tv", "show", "shows"):
                    kind = "tv"
                else:
                    kind = "all"
                page = int(parts[6]) if len(parts) > 6 and str(parts[6]).isdigit() else 1
                qhash = parts[7] if len(parts) > 7 and parts[7] else ""
        kind_tok = "m" if kind == "movie" else ("t" if kind == "tv" else "a")
        region_tok = "G"
        qh_sfx = f":{qhash}" if qhash else ""
        menu_cb = f"wxtr:m:{wid}:{oid}:{qhash}" if qhash else f"wxtr:m:{wid}:{oid}"
        nav_base = f"wxtr:n:{wid}:{oid}:{region_tok}:{kind_tok}"

        owner_uid = _owner_for_wroxen(wid, bot_id)
        if owner_uid is None:
            return await cq.answer("Config not found", show_alert=True)

        from core.wroxen.trending.service import get_trending_now_items
        items = await get_trending_now_items(
            owner_uid, wid, region, media_type=kind,
        )
        kind_label = (
            "🎬 Movies" if kind == "movie"
            else "📺 Shows" if kind == "tv"
            else "Trending On Internet"
        )
        if not items:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Menu", callback_data=menu_cb)],
            ])
            await _safe_edit_message(
                cq.message,
                f"<b>🌐 {escape(kind_label)}</b>\n\n"
                "⚠️ Trending is temporarily unavailable.\n"
                "Please try again later.",
                reply_markup=kb,
                **_lp,
            )
            return await cq.answer()

        per = _TRENDING_PER_PAGE
        pages = max(1, (len(items) + per - 1) // per)
        page = max(1, min(page, pages))
        start_i = (page - 1) * per
        chunk = items[start_i:start_i + per]
        lines = [f"<b>🌐 {escape(kind_label)}</b>", ""]
        # Numbered buttons only for available titles — 5 per row
        num_btns: list = []
        for i, it in enumerate(chunk, start=start_i + 1):
            title = it.get("title") or "?"
            mt = it.get("media_type") or "movie"
            year = it.get("year")
            avail = bool(it.get("available"))
            icon = "📺" if mt == "tv" else "🎬"
            year_s = str(year) if year else "—"
            flag = "🟢 Available" if avail else "🔴 Not Available"
            lines.append(f"{i}. {icon} <b>{escape(title)}</b>")
            lines.append(f"   📅 {escape(year_s)} · {flag}")
            lines.append("")
            if avail:
                qh = remember_query(title)
                num_btns.append(InlineKeyboardButton(
                    f"{i}",
                    callback_data=f"wxtr:s:{wid}:{oid}:{qh}",
                ))
        lines.append(f"📄 {page}/{pages} · max 30 · {per}/page")
        text = "\n".join(lines)

        rows = []
        for bi in range(0, len(num_btns), _BTN_PER_ROW):
            rows.append(num_btns[bi:bi + _BTN_PER_ROW])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"{nav_base}:{page - 1}{qh_sfx}",
            ))
        if page < pages:
            nav.append(InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"{nav_base}:{page + 1}{qh_sfx}",
            ))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("◀️ Menu", callback_data=menu_cb)])
        await _safe_edit_message(
            cq.message, text, reply_markup=InlineKeyboardMarkup(rows), **_lp
        )
        return await cq.answer()

    if action == "t":
        # parts: wxtr:t:wid:oid:period:page[:qhash]
        wid, oid = _ids()
        period = parts[4] if len(parts) > 4 else "7d"
        if period not in _PERIOD_LABELS:
            period = "7d"
        page = int(parts[5]) if len(parts) > 5 and str(parts[5]).isdigit() else 1
        qhash = parts[6] if len(parts) > 6 and parts[6] else ""
        qh_sfx = f":{qhash}" if qhash else ""
        menu_cb = f"wxtr:m:{wid}:{oid}:{qhash}" if qhash else f"wxtr:m:{wid}:{oid}"
        chat_id = cq.message.chat.id if cq.message and cq.message.chat else 0
        from core.wroxen.trending.analytics import top_searches
        rows_data = await top_searches(chat_id, period, limit=50)
        label = _PERIOD_LABELS.get(period, period)
        per = _TRENDING_PER_PAGE
        total_n = len(rows_data)
        pages = max(1, (total_n + per - 1) // per) if total_n else 1
        page = max(1, min(page, pages))
        start_i = (page - 1) * per
        chunk = rows_data[start_i:start_i + per]

        lines = [f"<b>🔎 Top Searches — {escape(label)}</b>\n"]
        if not rows_data:
            lines.append("<i>No successful searches in this period yet.</i>")
        num_btns: list = []
        for i, row in enumerate(chunk, start=start_i + 1):
            title = row.get("display_title") or row.get("normalized_title") or "?"
            cnt = row.get("count") or 0
            lines.append(f"{i}. <b>{escape(title)}</b>")
            lines.append(f"   🔎 {cnt} searches")
            lines.append("")
            qh = remember_query(title)
            num_btns.append(InlineKeyboardButton(
                f"{i}",
                callback_data=f"wxtr:s:{wid}:{oid}:{qh}",
            ))
        if rows_data:
            lines.append(f"📄 {page}/{pages} · max 50 · {per}/page")

        kb_rows = []
        for bi in range(0, len(num_btns), _BTN_PER_ROW):
            kb_rows.append(num_btns[bi:bi + _BTN_PER_ROW])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"wxtr:t:{wid}:{oid}:{period}:{page - 1}{qh_sfx}",
            ))
        if page < pages:
            nav.append(InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"wxtr:t:{wid}:{oid}:{period}:{page + 1}{qh_sfx}",
            ))
        if nav:
            kb_rows.append(nav)
        filt = []
        for key, lab in (("1d", "Today"), ("3d", "3d"), ("7d", "7d"), ("15d", "15d"), ("30d", "30d")):
            mark = "•" if key == period else ""
            filt.append(InlineKeyboardButton(
                f"{mark}{lab}",
                callback_data=f"wxtr:t:{wid}:{oid}:{key}:1{qh_sfx}",
            ))
        kb_rows.append(filt[:3])
        kb_rows.append(filt[3:])
        kb_rows.append([InlineKeyboardButton("◀️ Menu", callback_data=menu_cb)])
        await _safe_edit_message(
            cq.message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            **_lp,
        )
        return await cq.answer()

    if action == "s":
        wid, oid = _ids()
        qhash = parts[4] if len(parts) > 4 else ""
        query = recall_query(qhash)
        if not query:
            return await cq.answer("Expired — open Trending again", show_alert=True)
        owner_uid = _owner_for_wroxen(wid, bot_id)
        if owner_uid is None:
            return await cq.answer("Config not found", show_alert=True)

        from core.db_resolver import resolve_feature_db
        resolved = await resolve_feature_db(owner_uid, "wroxen")
        uri = resolved.get("uri")
        if not uri:
            return await cq.answer("DB not configured", show_alert=True)
        ok, _ = await wxdb.ensure_connected(owner_uid, uri)
        if not ok:
            return await cq.answer("DB offline", show_alert=True)

        cached = get_cached(wid, query)
        if cached:
            results, total = cached
        else:
            data = await wxdb.search_media(owner_uid, wid, query, limit=200)
            results, total = data["results"], data["total"]
            set_cached(wid, query, results, total)

        if not results:
            return await cq.answer("No results in this Wroxen", show_alert=True)

        tr_on = False
        try:
            from database import is_wroxen_trending_enabled
            tr_on = await is_wroxen_trending_enabled(wid)
        except Exception:
            tr_on = False
        if tr_on:
            try:
                from core.wroxen.trending.analytics import record_successful_search
                if cq.message and cq.message.chat:
                    canonical = (results[0].get("title") or "").strip() or query
                    await record_successful_search(cq.message.chat.id, canonical)
            except Exception:
                pass

        remember_query(query)
        pages = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        text = (
            f"<b>🔎 Results for:</b> <code>{escape(query)}</code>\n"
            f"📄 Page 1/{pages} • Total: {total}\n\n"
        )
        for i, movie in enumerate(results[:RESULTS_PER_PAGE], start=1):
            text += format_result_line(i, movie)
        kb = pagination_keyboard(
            wid, query, 1, pages, oid, include_trending=tr_on,
        )
        ok = await _safe_edit_message(cq.message, text, reply_markup=kb, **_lp)
        if not ok:
            return await cq.answer("Try again in a moment", show_alert=True)
        return await cq.answer()

    await cq.answer()
