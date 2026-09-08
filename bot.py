"""NUS USC Routes — telegram bot.

Detects route-pic photos (with a caption containing a V-grade) posted in the
group, parses name/grade/wall, stores the route + photo, and replies with a
confirmation linking to the mini-app. Provides admin CRUD (edit/delete/override).

Runs as a systemd service (see route-archiver.service). No cron, no Hermes.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from storage import GradeError, Storage, _norm_wall, parse_caption

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("routes-bot")

BASE_DIR = Path(__file__).parent
PHOTO_DIR = BASE_DIR / "data" / "photos"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Comma-separated admin Telegram ids (owner override access).
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "495290408").split(",") if x.strip()}
# Mini-app base URL (public HTTPS) used for the app itself.
APP_BASE = os.environ.get("APP_BASE", "https://routes.anselmlong.com")
# Full-screen Mini App entry. Use the t.me short link as a URL button: inline
# `web_app` buttons are only allowed in private chats (not groups), but this
# t.me link opens the Mini App full-screen and works everywhere.
MINI_APP_LINK = os.environ.get("MINI_APP_LINK", "https://t.me/nuscc_routes_bot/USC_ROUTES")
# The group the bot archives from. Empty until set (see /setchat).
CHAT_ID = int(os.environ["CHAT_ID"]) if os.environ.get("CHAT_ID") else None

storage = Storage()

STARTUP = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _is_stale(msg) -> bool:
    dt = msg.date
    if dt and dt.replace(tzinfo=timezone.utc) < STARTUP:
        return True
    return False


def _is_admin(uid) -> bool:
    return uid in ADMINS


def _route_line(r: dict) -> str:
    wall = f" · {r['wall']}" if r.get("wall") else ""
    setter = f" by {r['setter_name']}" if r.get("setter_name") else ""
    return f"🧗 *{r['name']}* — {r['grade']}{wall}{setter}"


def _app_link(r: dict) -> InlineKeyboardMarkup:
    """URL button to the full-screen Mini App (t.me short link).

    Uses a plain URL button pointing at the t.me Mini App link — `web_app`
    inline buttons are only allowed in private chats, not groups, so in group
    chat a web_app button would crash with BUTTON_TYPE_INVALID. The t.me link
    opens the same Mini App full-screen and works everywhere.
    """
    kb = [[InlineKeyboardButton("🗂 Open collection", url=MINI_APP_LINK)]]
    return InlineKeyboardMarkup(kb)


async def _topic_wall(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, thread_id: int):
    """Return the canonical wall for a forum topic, or None.

    Resolves the topic name via the raw getForumTopics API (the python lib
    doesn't wrap the lookup) and maps it through WALL_ALIASES. Cached in
    ctx.bot_data per (chat, thread) with a TTL so we don't hammer the API.
    """
    if not thread_id or thread_id == chat_id:
        return None  # General topic (or no topic) — not a named wall

    cache = ctx.bot_data.setdefault("topic_cache", {})
    key = (chat_id, thread_id)
    hit = cache.get(key)
    if hit and datetime.now(timezone.utc) - hit["t"] < timedelta(hours=6):
        return hit["wall"]

    title = None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getForumTopics",
                json={"chat_id": chat_id, "limit": 100},
            )
            r.raise_for_status()
            for t in r.json().get("result", {}).get("forum_topics", []):
                if t.get("message_thread_id") == thread_id:
                    title = t.get("name")
                    break
    except Exception as e:
        log.warning("getForumTopics failed: %s", e)

    wall = _norm_wall(title) if title else None
    cache[key] = {"wall": wall, "t": datetime.now(timezone.utc)}
    return wall


# --------------------------------------------------------------------------- #
# photo detection (the core flow — zero behavior change for setters)
# --------------------------------------------------------------------------- #
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    if _is_stale(msg):
        return
    # ignore the bot's own reposts (avoid reply loops)
    if update.effective_user and update.effective_user.id == ctx.bot.id:
        return

    caption = msg.caption or ""
    # a route archive photo must have a caption with a V-grade; otherwise skip
    if not caption:
        return

    try:
        parsed = parse_caption(caption)
    except GradeError as e:
        log.info("skip (unparsed): %s — %s", caption, e)
        return

    # resolve wall: forum topic wins over caption wall, else caption, else none
    wall = parsed["wall"]
    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id and thread_id != msg.chat.id:
        topic_wall = await _topic_wall(ctx, msg.chat.id, thread_id)
        if topic_wall:
            wall = topic_wall

    # download the largest photo
    photo = msg.photo[-1]
    fid = photo.file_id
    afile = await ctx.bot.get_file(fid)
    route_id = None
    suffix = Path(afile.file_path or "").suffix or ".jpg"
    dest = PHOTO_DIR / f"{fid}{suffix}"
    try:
        await afile.download_to_drive(dest)
    except Exception as e:
        log.warning("photo download failed: %s", e)
        dest = None

    setter_name = None
    setter_id = None
    if update.effective_user:
        setter_name = update.effective_user.full_name
        setter_id = update.effective_user.id

    route = storage.add_route(
        name=parsed["name"],
        grade=parsed["grade"],
        grade_low=parsed["grade_low"],
        wall=wall,
        photo_path=str(dest) if dest else None,
        photo_fid=fid,
        setter_name=setter_name,
        setter_id=setter_id,
    )
    route_id = route["id"]

    # admin quick actions on the confirmation
    buttons = [
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{route_id}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"del:{route_id}"),
        ]
    ]
    kb = InlineKeyboardMarkup(buttons + [[InlineKeyboardButton("🗂 Open collection", url=MINI_APP_LINK)]])

    await msg.reply_text(
        f"✅ Archived *{route['name']}* — {route['grade']}"
        + (f" · {route['wall']}" if route.get("wall") else "")
        + (f"\n🧗 Set by {route['setter_name']}" if route.get("setter_name") else "")
        + "\nTap to open the full collection.",
        parse_mode="Markdown",
        reply_markup=kb,
    )


# --------------------------------------------------------------------------- #
# browse / list commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🧗 *NUS USC Routes*\n\n"
        "Setters: post a photo of a route with its name + grade.\n"
        "`Crack Line V4`\n"
        "Post it in the topic for its wall (left / overhang / slab) and I'll\n"
        "tag the wall automatically.\n\n"
        "Tap for the full collection.",
        parse_mode="Markdown",
        reply_markup=_app_link({}),
    )


async def cmd_routes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    grade_filter = None
    wall_filter = None
    if args:
        for a in args:
            if a.upper().startswith("V") or a.upper() == "VB":
                grade_filter = a
            else:
                wall_filter = a
    routes = storage.list_routes(grade=grade_filter, wall=wall_filter)
    if not routes:
        await update.effective_message.reply_text("No routes found.")
        return
    lines = [f"*{len(routes)} routes*"
             + (f" · ≥{grade_filter}" if grade_filter else "")
             + (f" · {wall_filter}" if wall_filter else "")]
    for r in routes[:30]:
        lines.append(_route_line(r))
    if len(routes) > 30:
        lines.append(f"\n…and {len(routes)-30} more. Open the collection for all.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


# --------------------------------------------------------------------------- #
# admin CRUD
# --------------------------------------------------------------------------- #
async def _require_admin(update: Update, uid) -> bool:
    if not _is_admin(uid):
        await update.effective_message.reply_text("⛔ admins only.")
        return False
    return True


async def cmd_setchat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await update.effective_message.reply_text("⛔ admins only.")
        return
    chat = update.effective_chat
    # record chat + group name in a small config file for /chat
    cfg = BASE_DIR / "data" / "chat.json"
    cfg.parent.mkdir(exist_ok=True)
    import json
    data = {"chat_id": chat.id, "title": getattr(chat, "title", None)}
    cfg.write_text(json.dumps(data))
    await update.effective_message.reply_text(
        f"✅ Tracking group: {getattr(chat,'title', chat.id)} (id {chat.id})."
    )


async def _delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, route_id):
    route = storage.get_route(route_id)
    if not route:
        await update.callback_query.answer("Not found.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, delete", callback_data=f"del_yes:{route_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"del_no:{route_id}"),
    ]])
    await update.callback_query.edit_message_text(
        f"Delete *{route['name']}* ({route['grade']})?", parse_mode="Markdown", reply_markup=kb
    )


async def _do_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE, route_id):
    storage.delete_route(route_id)
    await update.callback_query.edit_message_text("🗑 Deleted.")


async def _edit_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE, route_id):
    route = storage.get_route(route_id)
    if not route:
        await update.callback_query.answer("Not found.")
        return
    ctx.user_data["editing_route"] = route_id
    await update.callback_query.edit_message_text(
        "Send the new caption (e.g. `New Name / V5 / right slab`). "
        "Or /cancel.",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("editing_route", None)
    await update.effective_message.reply_text("Cancelled.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not _is_admin(user.id):
        return
    route_id = ctx.user_data.get("editing_route")
    if route_id is None:
        return
    text = (update.effective_message.text or "").strip()
    try:
        parsed = parse_caption(text)
    except GradeError as e:
        await update.effective_message.reply_text(f"Couldn't parse: {e}. Try again /cancel.")
        return
    r = storage.update_route(
        route_id,
        name=parsed["name"],
        grade=parsed["grade"],
        grade_low=parsed["grade_low"],
        wall=parsed["wall"],
    )
    ctx.user_data.pop("editing_route", None)
    await update.effective_message.reply_text(
        f"✅ Updated to *{r['name']}* — {r['grade']}"
        + (f" · {r['wall']}" if r.get("wall") else ""),
        parse_mode="Markdown",
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    if not user or not _is_admin(user.id):
        await q.edit_message_text("⛔ admins only.")
        return
    data = q.data
    if data.startswith("del:"):
        await _delete_confirm(update, ctx, int(data.split(":")[1]))
    elif data.startswith("del_yes:"):
        await _do_delete(update, ctx, int(data.split(":")[1]))
    elif data.startswith("del_no:"):
        await q.edit_message_text("Cancelled.")
    elif data.startswith("edit:"):
        await _edit_flow(update, ctx, int(data.split(":")[1]))


async def cmd_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Open the Mini App directly."""
    await update.effective_message.reply_text(
        "Open the route collection 👇",
        reply_markup=_app_link({}),
    )


async def _set_menu_button(app) -> None:
    """Pin the Mini App as the bot's menu button (the ⋯ / ⚙️ menu)."""
    try:
        await app.bot.set_chat_menu_button(menu_button={"type": "web_app", "text": "Routes", "web_app": {"url": APP_BASE}})
    except Exception as e:
        log.warning("set_chat_menu_button failed: %s", e)


def run():
    app = Application.builder().token(BOT_TOKEN).post_init(_set_menu_button).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("routes", cmd_routes))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("setchat", cmd_setchat))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    # photo first so captioned photos archive; text (admin edit) after commands
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    run()
