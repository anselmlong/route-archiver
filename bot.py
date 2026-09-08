"""USC Routes — telegram bot.

Detects route-pic photos (with a caption containing a V-grade) posted in the
group, parses name/grade/wall, stores the route + photo, and replies with a
confirmation linking to the mini-app. Provides admin CRUD (edit/delete/override).

Runs as a systemd service (see route-archiver.service). No cron, no Hermes.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
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

from storage import GradeError, Storage, WILD_LOW, _norm_wall, parse_caption

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


_MD_SPECIAL = re.compile(r"([_*`\[])")


def _md_escape(s) -> str:
    """Escape legacy-Markdown special chars in free-form user text.

    Route names, walls, descriptions and setter names all come from
    Telegram captions / display names we don't control; an unbalanced
    `_`/`*`/`` ` ``/`[` in any of them makes parse_mode="Markdown" reject
    the whole message, so escape before interpolating into any Markdown
    message.
    """
    if not s:
        return ""
    return _MD_SPECIAL.sub(r"\\\1", str(s))


def _route_line(r: dict) -> str:
    wall = f" · {_md_escape(r['wall'])}" if r.get("wall") else ""
    setter = f" by {_md_escape(r['setter_name'])}" if r.get("setter_name") else ""
    return f"🧗 *{_md_escape(r['name'])}* — {r['grade']}{wall}{setter}"


def _app_link(r: dict) -> InlineKeyboardMarkup:
    """URL button to the full-screen Mini App (t.me short link).

    Uses a plain URL button pointing at the t.me Mini App link — `web_app`
    inline buttons are only allowed in private chats, not groups, so in group
    chat a web_app button would crash with BUTTON_TYPE_INVALID. The t.me link
    opens the same Mini App full-screen and works everywhere.
    """
    kb = [[InlineKeyboardButton("🗂 Open collection", url=MINI_APP_LINK)]]
    return InlineKeyboardMarkup(kb)


# canonical wall arg parsing, shared by /wall and /reset
_WALL_ARG_CANON = {"left": "Left", "middle": "Middle", "right": "Right",
                    "l": "Left", "m": "Middle", "r": "Right"}


def _canon_wall_arg(raw: str):
    return _WALL_ARG_CANON.get(raw.strip().lower())


def _route_admin_buttons(route: dict) -> InlineKeyboardMarkup:
    """Edit/Delete/Retire quick actions attached to a route's message.

    These stay live on the original message indefinitely (Telegram
    callback_data is just re-looked-up against the DB, no in-memory
    session), so an admin can scroll back and retire/delete a route weeks
    after it was posted.
    """
    retire_label = "♻️ Restore" if route.get("retired_at") else "🪨 Retire"
    rows = [
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{route['id']}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"del:{route['id']}"),
        ],
        [InlineKeyboardButton(retire_label, callback_data=f"retire:{route['id']}")],
        [InlineKeyboardButton("🗂 Open collection", url=MINI_APP_LINK)],
    ]
    return InlineKeyboardMarkup(rows)


async def _topic_wall(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, thread_id: int):
    """Return the wall for a forum topic.

    Priority: an explicit wall set via /wall in that topic → a name captured
    from forum_topic_created/edited service messages → None. (The Bot API has no
    getForumTopics method, so we can't query topic names on demand.)
    """
    if not thread_id or thread_id == chat_id:
        return None  # General topic (or no topic)

    mapped = _topic_wall_map(chat_id, thread_id)
    if mapped:
        return mapped

    title = _load_topic_name(chat_id, thread_id)
    return _norm_wall(title) if title else None


TOPIC_NAMES_FILE = BASE_DIR / "data" / "topic_names.json"
TOPIC_WALLS_FILE = BASE_DIR / "data" / "topic_walls.json"


def _load_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_json(path, data):
    try:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data))
    except Exception as e:
        log.warning("couldn't save %s: %s", path.name, e)


def _load_topic_name(chat_id: int, thread_id: int):
    return _load_json(TOPIC_NAMES_FILE).get(str(chat_id), {}).get(str(thread_id))


def _topic_wall_map(chat_id: int, thread_id: int):
    """Explicit wall set for a topic via /wall (most reliable source)."""
    return _load_json(TOPIC_WALLS_FILE).get(str(chat_id), {}).get(str(thread_id))


def _remember_topic(chat_id: int, thread_id: int, name: str):
    if not name or not thread_id:
        return
    data = _load_json(TOPIC_NAMES_FILE)
    data.setdefault(str(chat_id), {})[str(thread_id)] = name
    _save_json(TOPIC_NAMES_FILE, data)


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
    # only archive from the configured group, if one is set
    if CHAT_ID and msg.chat.id != CHAT_ID:
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
        description=parsed["description"],
        photo_path=str(dest) if dest else None,
        photo_fid=fid,
        setter_name=setter_name,
        setter_id=setter_id,
    )
    # admin quick actions on the confirmation
    kb = _route_admin_buttons(route)

    desc = f"\n📝 {_md_escape(route['description'])}" if route.get("description") else ""
    await msg.reply_text(
        f"✅ Archived *{_md_escape(route['name'])}* — {route['grade']}"
        + (f" · {_md_escape(route['wall'])}" if route.get("wall") else "")
        + desc
        + (f"\n🧗 Set by {_md_escape(route['setter_name'])}" if route.get("setter_name") else "")
        + "\nTap to open the full collection.",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def on_topic_event(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Capture forum topic names from create/edit service messages so we can
    resolve a photo's wall from the topic it was posted in."""
    msg = update.effective_message
    if not msg or not msg.chat:
        return
    created = getattr(msg, "forum_topic_created", None)
    edited = getattr(msg, "forum_topic_edited", None)
    name = None
    if created:
        name = getattr(created, "name", None)
    elif edited:
        name = getattr(edited, "name", None)
    tid = getattr(msg, "message_thread_id", None) or msg.chat.id
    _remember_topic(msg.chat.id, tid, name)
    log.info("remembered topic %s: %r", tid, name)


# --------------------------------------------------------------------------- #
# browse / list commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🧗 *USC Routes*\n\n"
        "Setters: post a photo of a route with its name + grade.\n"
        "`Crack Line V4`\n"
        "Post it in the topic for its wall (left / overhang / slab) and I'll\n"
        "tag the wall automatically.\n\n"
        "/mine — your own send history\n"
        "/leaderboard — top climbers & setters\n"
        "/hot — what's hot this week\n\n"
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
    stats = storage.stats()
    lines.append(f"\n_{stats['active']} active · {stats['retired']} retired all-time_")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


def _hardest(routes: list[dict]):
    """(grade, grade_low) of the hardest route in a list, ignoring
    wildcard-graded ones. None if there's nothing gradeable."""
    hardest_grade, hardest_low = None, WILD_LOW
    for r in routes:
        if r["grade_low"] > hardest_low:
            hardest_low, hardest_grade = r["grade_low"], r["grade"]
    return hardest_grade


async def cmd_mine(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Your own send history: /mine."""
    user = update.effective_user
    if not user:
        return
    routes = storage.user_ticked_routes(user.id)
    if not routes:
        await update.effective_message.reply_text(
            "No ticks yet — open a route in the collection and mark it sent.",
            reply_markup=_app_link({}),
        )
        return
    hardest = _hardest(routes)
    lines = [f"*{len(routes)} sends* · hardest {hardest}"]
    for r in routes[:20]:
        suffix = " _(retired)_" if r.get("retired_at") else ""
        lines.append(_route_line(r) + suffix)
    if len(routes) > 20:
        lines.append(f"\n…and {len(routes)-20} more. Open the collection for your full list.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


_MEDALS = ["🥇", "🥈", "🥉"]


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Top climbers (sends + hardest grade) and top setters: /leaderboard."""
    climbers = storage.leaderboard_climbers(10)
    setters = storage.leaderboard_setters(10)

    lines = ["🏆 *Top climbers*"]
    if not climbers:
        lines.append("No ticks yet.")
    for i, c in enumerate(climbers):
        rank = _MEDALS[i] if i < 3 else f"{i + 1}."
        name = _md_escape(c["tg_user_name"] or f"user_{c['tg_user_id']}")
        lines.append(f"{rank} {name} — {c['ticks']} sends · hardest {c['hardest_grade']}")

    lines.append("\n🔨 *Top setters*")
    if not setters:
        lines.append("No routes set yet.")
    for i, s in enumerate(setters):
        rank = _MEDALS[i] if i < 3 else f"{i + 1}."
        name = _md_escape(s["setter_name"] or f"setter_{s['setter_id']}")
        lines.append(f"{rank} {name} — {s['routes_set']} routes set")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_hot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """What's hot this week -- routes with the most ticks in the last 7
    days, rank #1 being the de facto "route of the week": /hot."""
    routes = storage.hot_routes(days=7, limit=10)
    if not routes:
        await update.effective_message.reply_text(
            "No sends yet this week — be the first!", reply_markup=_app_link({})
        )
        return
    lines = ["🔥 *Hot this week*"]
    for i, r in enumerate(routes):
        rank = _MEDALS[i] if i < 3 else f"{i + 1}."
        n = r["recent_ticks"]
        lines.append(f"{rank} {_route_line(r)} — {n} send{'' if n == 1 else 's'} this week")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=_app_link({})
    )


# --------------------------------------------------------------------------- #
# admin CRUD
# --------------------------------------------------------------------------- #
async def _require_admin(update: Update, uid) -> bool:
    if not _is_admin(uid):
        await update.effective_message.reply_text("⛔ admins only.")
        return False
    return True


async def cmd_wall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bind the current forum topic to a wall: /wall left | middle | right.

    Admin runs this INSIDE the desired topic once. Persists chat→thread→wall,
    which is the most reliable wall source (doesn't depend on topic names).
    """
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await update.effective_message.reply_text("⛔ admins only.")
        return
    chat = update.effective_chat
    if not chat or chat.type not in ("supergroup", "group", "channel"):
        await update.effective_message.reply_text("Run this inside the topic you want to map.")
        return
    thread_id = getattr(update.effective_message, "message_thread_id", None) or chat.id
    args = (ctx.args or [])
    if not args:
        await update.effective_message.reply_text("Usage: /wall left | middle | right")
        return
    raw = " ".join(args).strip().lower()
    canon = _canon_wall_arg(raw)
    if not canon:
        await update.effective_message.reply_text("Wall must be left, middle or right.")
        return
    data = _load_json(TOPIC_WALLS_FILE)
    data.setdefault(str(chat.id), {})[str(thread_id)] = canon
    _save_json(TOPIC_WALLS_FILE, data)
    await update.effective_message.reply_text(f"✅ Topic -> *{canon}* wall. Photos here will tag {canon}.", parse_mode="Markdown")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bulk-retire every active route on a wall (or the whole gym):
    /reset left | middle | right | all.

    Mirrors a physical wall reset -- when a section gets stripped, every
    route on it comes down at once. Confirms first since it can affect
    many routes in one shot; retiring (unlike delete) is reversible one
    route at a time via the 🪨/♻️ button on each route's message.
    """
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await update.effective_message.reply_text("⛔ admins only.")
        return
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /reset left | middle | right | all")
        return
    raw = " ".join(args).strip().lower()
    if raw == "all":
        wall, label = None, "all walls"
    else:
        wall = _canon_wall_arg(raw)
        if not wall:
            await update.effective_message.reply_text("Wall must be left, middle, right, or all.")
            return
        label = wall

    count = storage.count_active_routes(wall)
    if count == 0:
        await update.effective_message.reply_text(f"No active routes on {label} to retire.")
        return

    token = "ALL" if wall is None else wall
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, retire them", callback_data=f"reset_yes:{token}"),
        InlineKeyboardButton("Cancel", callback_data=f"reset_no:{token}"),
    ]])
    await update.effective_message.reply_text(
        f"Retire {count} active route{'' if count == 1 else 's'} on *{_md_escape(label)}*?",
        parse_mode="Markdown", reply_markup=kb,
    )


async def cmd_setchat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await update.effective_message.reply_text("⛔ admins only.")
        return
    chat = update.effective_chat
    # record chat + group name in a small config file for /chat
    cfg = BASE_DIR / "data" / "chat.json"
    cfg.parent.mkdir(exist_ok=True)
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
        f"Delete *{_md_escape(route['name'])}* ({route['grade']})?",
        parse_mode="Markdown", reply_markup=kb
    )


async def _do_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE, route_id):
    storage.delete_route(route_id)
    await update.callback_query.edit_message_text("🗑 Deleted.")


async def _toggle_retire(update: Update, ctx: ContextTypes.DEFAULT_TYPE, route_id):
    route = storage.get_route(route_id)
    if not route:
        await update.callback_query.answer("Not found.")
        return
    if route.get("retired_at"):
        route = storage.unretire_route(route_id)
        await update.callback_query.answer("Restored to active.")
    else:
        route = storage.retire_route(route_id)
        await update.callback_query.answer("Retired.")
    try:
        await update.callback_query.edit_message_reply_markup(
            reply_markup=_route_admin_buttons(route)
        )
    except Exception as e:
        log.warning("couldn't refresh retire button: %s", e)


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
        description=parsed["description"],
    )
    ctx.user_data.pop("editing_route", None)
    await update.effective_message.reply_text(
        f"✅ Updated to *{_md_escape(r['name'])}* — {r['grade']}"
        + (f" · {_md_escape(r['wall'])}" if r.get("wall") else ""),
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
    elif data.startswith("retire:"):
        await _toggle_retire(update, ctx, int(data.split(":")[1]))
    elif data.startswith("reset_yes:"):
        token = data.split(":", 1)[1]
        wall = None if token == "ALL" else token
        n = storage.retire_wall(wall)
        label = "all walls" if wall is None else wall
        await q.edit_message_text(f"✅ Retired {n} route{'' if n == 1 else 's'} on {label}.")
    elif data.startswith("reset_no:"):
        await q.edit_message_text("Cancelled.")


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
    app.add_handler(CommandHandler("mine", cmd_mine))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("hot", cmd_hot))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("wall", cmd_wall))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("setchat", cmd_setchat))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    # topic create/edit service messages -> capture the topic name
    svc = filters.StatusUpdate.FORUM_TOPIC_CREATED | filters.StatusUpdate.FORUM_TOPIC_EDITED
    app.add_handler(MessageHandler(svc, on_topic_event))
    # photo first so captioned photos archive; text (admin edit) after commands
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    run()
