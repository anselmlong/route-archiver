"""Tests for the route lifecycle (/reset, retire toggle) bot.py additions.

python-telegram-bot's Update/CallbackQuery objects are heavy to construct
for real, but bot.py's handlers only ever touch a handful of attributes
(effective_user.id, effective_message.reply_text, callback_query.data/
answer/edit_message_text/edit_message_reply_markup, ctx.args) -- so these
tests use small duck-typed fakes instead of pulling in a mocking framework,
and drive the async handlers directly with asyncio.run.
"""
import asyncio

import pytest

ADMIN_ID = 111
NON_ADMIN_ID = 222


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})


class FakeUser:
    def __init__(self, id):
        self.id = id


class FakeChat:
    def __init__(self, id, type="private"):
        self.id = id
        self.type = type


class FakeBot:
    """Records send_message calls; chat_ids in `blocked` raise Forbidden,
    simulating a user who has blocked the bot."""
    def __init__(self, blocked=()):
        self.sent = []
        self.blocked = set(blocked)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.blocked:
            from telegram.error import Forbidden
            raise Forbidden("bot was blocked by the user")
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


class FakeCallbackQuery:
    def __init__(self, data, user):
        self.data = data
        self.from_user = user
        self.answers = []
        self.text_edits = []
        self.markup_edits = []

    async def answer(self, *a, **k):
        self.answers.append(a[0] if a else k.get("text"))

    async def edit_message_text(self, text, **kwargs):
        self.text_edits.append({"text": text, **kwargs})

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)


class FakeUpdate:
    def __init__(self, user=None, message=None, callback_query=None, chat=None):
        self.effective_user = user
        self.effective_message = message
        self.callback_query = callback_query
        self.effective_chat = chat


class FakeCtx:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.user_data = {}
        self.bot = bot


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bot_module(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.setenv("ADMIN_IDS", str(ADMIN_ID))
    monkeypatch.delenv("CHAT_ID", raising=False)
    import bot as bot_mod
    import storage as storage_mod

    bot_mod.storage = storage_mod.Storage(db_path=tmp_path / "bot_test.db")
    return bot_mod


def _seed(bot_module, **overrides):
    kwargs = dict(name="Route", grade="V4", grade_low=5, wall="Left")
    kwargs.update(overrides)
    return bot_module.storage.add_route(**kwargs)


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_canon_wall_arg(bot_module):
    assert bot_module._canon_wall_arg("left") == "Left"
    assert bot_module._canon_wall_arg("L") == "Left"
    assert bot_module._canon_wall_arg("MIDDLE") == "Middle"
    assert bot_module._canon_wall_arg("r") == "Right"
    assert bot_module._canon_wall_arg("nonsense") is None


def test_route_admin_buttons_label_reflects_status(bot_module):
    active_route = {"id": 1, "retired_at": None}
    retired_route = {"id": 1, "retired_at": "2024-01-01 00:00:00"}
    active_kb = bot_module._route_admin_buttons(active_route, admin=True)
    retired_kb = bot_module._route_admin_buttons(retired_route, admin=True)
    assert active_kb.inline_keyboard[1][0].text == "🪨 Retire"
    assert active_kb.inline_keyboard[1][0].callback_data == "retire:1"
    assert retired_kb.inline_keyboard[1][0].text == "♻️ Restore"


def test_route_admin_buttons_hidden_for_non_admin(bot_module):
    route = {"id": 1, "retired_at": None}
    kb = bot_module._route_admin_buttons(route, admin=False)
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "✏️ Edit" not in texts
    assert "🗑 Delete" not in texts
    assert "🪨 Retire" not in texts
    assert "🗂 Open collection" in texts  # public link always present
    assert len(kb.inline_keyboard) == 1  # only the open-collection row


# --------------------------------------------------------------------------- #
# /reset command
# --------------------------------------------------------------------------- #
def test_cmd_reset_requires_admin(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_reset(update, FakeCtx(args=["left"])))
    assert "admins only" in msg.replies[0]["text"]


def test_cmd_reset_usage_without_args(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(ADMIN_ID), message=msg)
    run(bot_module.cmd_reset(update, FakeCtx(args=[])))
    assert "Usage" in msg.replies[0]["text"]


def test_cmd_reset_rejects_invalid_wall(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(ADMIN_ID), message=msg)
    run(bot_module.cmd_reset(update, FakeCtx(args=["diagonal"])))
    assert "must be left, middle, right, or all" in msg.replies[0]["text"]


def test_cmd_reset_no_active_routes_short_circuits(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(ADMIN_ID), message=msg)
    run(bot_module.cmd_reset(update, FakeCtx(args=["left"])))
    assert "No active routes on Left" in msg.replies[0]["text"]
    assert "reply_markup" not in msg.replies[0]


def test_cmd_reset_shows_confirmation_with_count_and_callback_tokens(bot_module):
    _seed(bot_module, wall="Left")
    _seed(bot_module, name="Route 2", wall="Left")
    _seed(bot_module, name="Route 3", wall="Right")
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(ADMIN_ID), message=msg)
    run(bot_module.cmd_reset(update, FakeCtx(args=["left"])))
    reply = msg.replies[0]
    assert "Retire 2 active routes on" in reply["text"]
    kb = reply["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data == "reset_yes:Left"
    assert kb[0][1].callback_data == "reset_no:Left"


def test_cmd_reset_all_walls_uses_all_token(bot_module):
    _seed(bot_module, wall="Left")
    _seed(bot_module, wall=None)
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(ADMIN_ID), message=msg)
    run(bot_module.cmd_reset(update, FakeCtx(args=["all"])))
    reply = msg.replies[0]
    assert "Retire 2 active routes on" in reply["text"]
    kb = reply["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data == "reset_yes:ALL"


# --------------------------------------------------------------------------- #
# callback dispatch: reset_yes / reset_no / retire toggle
# --------------------------------------------------------------------------- #
def test_on_callback_reset_yes_retires_and_reports_count(bot_module):
    r1 = _seed(bot_module, wall="Left")
    r2 = _seed(bot_module, name="Route 2", wall="Left")
    _seed(bot_module, name="Route 3", wall="Right")
    cq = FakeCallbackQuery("reset_yes:Left", FakeUser(ADMIN_ID))
    update = FakeUpdate(callback_query=cq)
    run(bot_module.on_callback(update, FakeCtx()))
    assert cq.text_edits[0]["text"] == "✅ Retired 2 routes on Left."
    assert bot_module.storage.get_route(r1["id"])["retired_at"] is not None
    assert bot_module.storage.get_route(r2["id"])["retired_at"] is not None


def test_on_callback_reset_yes_all_uses_none_wall(bot_module):
    _seed(bot_module, wall="Left")
    _seed(bot_module, wall=None)
    cq = FakeCallbackQuery("reset_yes:ALL", FakeUser(ADMIN_ID))
    update = FakeUpdate(callback_query=cq)
    run(bot_module.on_callback(update, FakeCtx()))
    assert cq.text_edits[0]["text"] == "✅ Retired 2 routes on all walls."
    assert bot_module.storage.count_active_routes() == 0


def test_on_callback_reset_no_cancels_without_retiring(bot_module):
    r = _seed(bot_module, wall="Left")
    cq = FakeCallbackQuery("reset_no:Left", FakeUser(ADMIN_ID))
    update = FakeUpdate(callback_query=cq)
    run(bot_module.on_callback(update, FakeCtx()))
    assert cq.text_edits[0]["text"] == "Cancelled."
    assert bot_module.storage.get_route(r["id"])["retired_at"] is None


def test_on_callback_retire_toggles_and_refreshes_buttons(bot_module):
    r = _seed(bot_module)
    cq = FakeCallbackQuery(f"retire:{r['id']}", FakeUser(ADMIN_ID))
    update = FakeUpdate(callback_query=cq)

    run(bot_module.on_callback(update, FakeCtx()))
    assert bot_module.storage.get_route(r["id"])["retired_at"] is not None
    assert cq.markup_edits[-1].inline_keyboard[1][0].text == "♻️ Restore"

    run(bot_module.on_callback(update, FakeCtx()))
    assert bot_module.storage.get_route(r["id"])["retired_at"] is None
    assert cq.markup_edits[-1].inline_keyboard[1][0].text == "🪨 Retire"


def test_on_callback_requires_admin_for_reset_and_retire(bot_module):
    r = _seed(bot_module, wall="Left")
    cq = FakeCallbackQuery(f"retire:{r['id']}", FakeUser(NON_ADMIN_ID))
    update = FakeUpdate(callback_query=cq)
    run(bot_module.on_callback(update, FakeCtx()))
    assert "admins only" in cq.text_edits[0]["text"]
    assert bot_module.storage.get_route(r["id"])["retired_at"] is None


# --------------------------------------------------------------------------- #
# /notify -- private-chat-only, open to any user
# --------------------------------------------------------------------------- #
def test_cmd_notify_rejects_group_chat(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg,
                         chat=FakeChat(id=-100, type="supergroup"))
    run(bot_module.cmd_notify(update, FakeCtx(args=["V4"])))
    assert "DM me" in msg.replies[0]["text"]
    assert bot_module.storage.get_subscription(NON_ADMIN_ID) is None


def test_cmd_notify_no_args_shows_not_subscribed(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg,
                         chat=FakeChat(id=NON_ADMIN_ID, type="private"))
    run(bot_module.cmd_notify(update, FakeCtx(args=[])))
    assert "not subscribed" in msg.replies[0]["text"]


def test_cmd_notify_subscribes_with_grade_and_wall(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg,
                         chat=FakeChat(id=NON_ADMIN_ID, type="private"))
    run(bot_module.cmd_notify(update, FakeCtx(args=["V4", "left"])))
    assert "Subscribed" in msg.replies[0]["text"]
    sub = bot_module.storage.get_subscription(NON_ADMIN_ID)
    assert sub["tg_chat_id"] == NON_ADMIN_ID
    assert sub["min_grade_low"] == bot_module._V_IDX["V4"]
    assert sub["wall"] == "Left"


def test_cmd_notify_no_args_shows_current_subscription(bot_module):
    chat = FakeChat(id=NON_ADMIN_ID, type="private")
    user = FakeUser(NON_ADMIN_ID)
    run(bot_module.cmd_notify(FakeUpdate(user=user, message=FakeMessage(), chat=chat),
                               FakeCtx(args=["V4", "left"])))
    msg = FakeMessage()
    run(bot_module.cmd_notify(FakeUpdate(user=user, message=msg, chat=chat), FakeCtx(args=[])))
    text = msg.replies[0]["text"]
    # a threshold is a floor, described in words -- "V4+" would now name the
    # half-step grade instead of "V4 and harder"
    assert "V4 and up" in text
    assert "Left" in text


def test_cmd_notify_half_step_threshold_is_not_double_plussed(bot_module):
    chat = FakeChat(id=NON_ADMIN_ID, type="private")
    user = FakeUser(NON_ADMIN_ID)
    run(bot_module.cmd_notify(FakeUpdate(user=user, message=FakeMessage(), chat=chat),
                               FakeCtx(args=["V4+"])))
    msg = FakeMessage()
    run(bot_module.cmd_notify(FakeUpdate(user=user, message=msg, chat=chat), FakeCtx(args=[])))
    text = msg.replies[0]["text"]
    assert "V4+ and up" in text
    assert "V4++" not in text


def test_cmd_notify_off_unsubscribes(bot_module):
    chat = FakeChat(id=NON_ADMIN_ID, type="private")
    user = FakeUser(NON_ADMIN_ID)
    run(bot_module.cmd_notify(FakeUpdate(user=user, message=FakeMessage(), chat=chat),
                               FakeCtx(args=["V4"])))
    msg = FakeMessage()
    run(bot_module.cmd_notify(FakeUpdate(user=user, message=msg, chat=chat), FakeCtx(args=["off"])))
    assert "Unsubscribed" in msg.replies[0]["text"]
    assert bot_module.storage.get_subscription(NON_ADMIN_ID) is None


def test_cmd_notify_with_no_grade_or_wall_subscribes_to_everything(bot_module):
    chat = FakeChat(id=NON_ADMIN_ID, type="private")
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg, chat=chat)
    run(bot_module.cmd_notify(update, FakeCtx(args=["gibberish"])))
    sub = bot_module.storage.get_subscription(NON_ADMIN_ID)
    assert sub["min_grade_low"] is None
    assert sub["wall"] is None


# --------------------------------------------------------------------------- #
# on_photo -> _notify_subscribers integration
# --------------------------------------------------------------------------- #
def test_notify_subscribers_sends_to_matching_and_skips_others(bot_module):
    bot_module.storage.subscribe(1, 111, min_grade_low=bot_module._V_IDX["V4"], wall="Left")
    bot_module.storage.subscribe(2, 222, min_grade_low=bot_module._V_IDX["V6"], wall="Left")
    bot = FakeBot()
    ctx = FakeCtx(bot=bot)
    route = {"name": "Crack Line", "grade": "V4", "grade_low": bot_module._V_IDX["V4"]}
    run(bot_module._notify_subscribers(ctx, route, "Left"))
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 111
    assert "Crack Line" in bot.sent[0]["text"]


def test_notify_subscribers_no_matches_sends_nothing(bot_module):
    bot_module.storage.subscribe(1, 111, wall="Right")
    bot = FakeBot()
    ctx = FakeCtx(bot=bot)
    route = {"name": "Crack Line", "grade": "V4", "grade_low": 5}
    run(bot_module._notify_subscribers(ctx, route, "Left"))
    assert bot.sent == []


def test_notify_subscribers_auto_unsubscribes_on_forbidden(bot_module):
    bot_module.storage.subscribe(1, 111)
    bot = FakeBot(blocked=[111])
    ctx = FakeCtx(bot=bot)
    route = {"name": "Crack Line", "grade": "V4", "grade_low": 5}
    run(bot_module._notify_subscribers(ctx, route, None))
    assert bot.sent == []
    assert bot_module.storage.get_subscription(1) is None


def test_notify_subscribers_escapes_markdown_in_route_name(bot_module):
    bot_module.storage.subscribe(1, 111)
    bot = FakeBot()
    ctx = FakeCtx(bot=bot)
    route = {"name": "Route_Name*here", "grade": "V4", "grade_low": 5}
    run(bot_module._notify_subscribers(ctx, route, None))
    assert "Route\\_Name\\*here" in bot.sent[0]["text"]
