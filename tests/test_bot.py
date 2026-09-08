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
    def __init__(self, user=None, message=None, callback_query=None):
        self.effective_user = user
        self.effective_message = message
        self.callback_query = callback_query


class FakeCtx:
    def __init__(self, args=None):
        self.args = args or []
        self.user_data = {}


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
    active_kb = bot_module._route_admin_buttons(active_route)
    retired_kb = bot_module._route_admin_buttons(retired_route)
    assert active_kb.inline_keyboard[1][0].text == "🪨 Retire"
    assert active_kb.inline_keyboard[1][0].callback_data == "retire:1"
    assert retired_kb.inline_keyboard[1][0].text == "♻️ Restore"


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
# /routes footer
# --------------------------------------------------------------------------- #
def test_cmd_routes_footer_reports_active_and_retired_totals(bot_module):
    a = _seed(bot_module, wall="Left")
    _seed(bot_module, name="Route 2", wall="Right")
    bot_module.storage.retire_route(a["id"])
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(ADMIN_ID), message=msg)
    run(bot_module.cmd_routes(update, FakeCtx(args=[])))
    text = msg.replies[0]["text"]
    assert "1 active" in text
    assert "1 retired all-time" in text


def test_cmd_routes_lists_active_only_by_default(bot_module):
    a = _seed(bot_module, name="Retired One", wall="Left")
    _seed(bot_module, name="Still Up", wall="Right")
    bot_module.storage.retire_route(a["id"])
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(ADMIN_ID), message=msg)
    run(bot_module.cmd_routes(update, FakeCtx(args=[])))
    text = msg.replies[0]["text"]
    assert "Still Up" in text
    assert "Retired One" not in text


# --------------------------------------------------------------------------- #
# /mine (personal logbook) -- open to any user, not just admins
# --------------------------------------------------------------------------- #
def test_cmd_mine_no_ticks_shows_hint(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_mine(update, FakeCtx()))
    assert "No ticks yet" in msg.replies[0]["text"]


def test_cmd_mine_lists_own_ticks_with_hardest_grade(bot_module):
    a = _seed(bot_module, name="Easy", grade="V2", grade_low=2)
    b = _seed(bot_module, name="Hard", grade="V6", grade_low=7)
    other = _seed(bot_module, name="Not mine", grade="V8", grade_low=9)
    bot_module.storage.toggle_tick(a["id"], tg_user_id=NON_ADMIN_ID, tg_user_name="Climber")
    bot_module.storage.toggle_tick(b["id"], tg_user_id=NON_ADMIN_ID, tg_user_name="Climber")
    bot_module.storage.toggle_tick(other["id"], tg_user_id=999, tg_user_name="Someone Else")

    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_mine(update, FakeCtx()))
    text = msg.replies[0]["text"]
    assert "2 sends" in text
    assert "hardest V6" in text
    assert "Easy" in text and "Hard" in text
    assert "Not mine" not in text


def test_cmd_mine_marks_retired_routes(bot_module):
    r = _seed(bot_module, name="Stripped")
    bot_module.storage.toggle_tick(r["id"], tg_user_id=NON_ADMIN_ID, tg_user_name="Climber")
    bot_module.storage.retire_route(r["id"])
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_mine(update, FakeCtx()))
    assert "(retired)" in msg.replies[0]["text"]


# --------------------------------------------------------------------------- #
# /leaderboard -- also open to any user
# --------------------------------------------------------------------------- #
def test_cmd_leaderboard_empty_state(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_leaderboard(update, FakeCtx()))
    text = msg.replies[0]["text"]
    assert "No ticks yet" in text
    assert "No routes set yet" in text


def test_cmd_leaderboard_ranks_climbers_and_setters(bot_module):
    a = _seed(bot_module, name="A", grade="V2", grade_low=2, setter_name="Sam", setter_id=10)
    b = _seed(bot_module, name="B", grade="V6", grade_low=7, setter_name="Sam", setter_id=10)
    c = _seed(bot_module, name="C", grade="V1", grade_low=1, setter_name="Ana", setter_id=20)
    s = bot_module.storage
    s.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    s.toggle_tick(b["id"], tg_user_id=1, tg_user_name="Bob")
    s.toggle_tick(c["id"], tg_user_id=2, tg_user_name="Cat")

    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_leaderboard(update, FakeCtx()))
    text = msg.replies[0]["text"]
    assert "🥇 Bob — 2 sends · hardest V6" in text
    assert "🥈 Cat — 1 sends · hardest V1" in text
    assert "🥇 Sam — 2 routes set" in text
    assert "🥈 Ana — 1 routes set" in text


# --------------------------------------------------------------------------- #
# /hot -- also open to any user
# --------------------------------------------------------------------------- #
def test_cmd_hot_empty_state(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_hot(update, FakeCtx()))
    assert "No sends yet this week" in msg.replies[0]["text"]


def test_cmd_hot_ranks_by_recent_ticks(bot_module):
    a = _seed(bot_module, name="Popular", grade="V4", grade_low=5)
    b = _seed(bot_module, name="Quiet", grade="V6", grade_low=7)
    s = bot_module.storage
    s.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    s.toggle_tick(a["id"], tg_user_id=2, tg_user_name="Cat")
    s.toggle_tick(b["id"], tg_user_id=1, tg_user_name="Bob")

    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_hot(update, FakeCtx()))
    text = msg.replies[0]["text"]
    assert text.index("Popular") < text.index("Quiet")
    assert "2 sends this week" in text
    assert "1 send this week" in text


# --------------------------------------------------------------------------- #
# /setter -- also open to any user
# --------------------------------------------------------------------------- #
def test_cmd_setter_usage_without_args(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_setter(update, FakeCtx(args=[])))
    assert "Usage" in msg.replies[0]["text"]


def test_cmd_setter_not_found(bot_module):
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_setter(update, FakeCtx(args=["Nobody"])))
    assert "No setter matching" in msg.replies[0]["text"]


def test_cmd_setter_shows_profile_by_partial_name(bot_module):
    _seed(bot_module, name="Crack Line", grade="V4", grade_low=5,
          setter_name="Sam Smith", setter_id=10)
    _seed(bot_module, name="Not Sam's", grade="V2", grade_low=2,
          setter_name="Ana", setter_id=20)
    msg = FakeMessage()
    update = FakeUpdate(user=FakeUser(NON_ADMIN_ID), message=msg)
    run(bot_module.cmd_setter(update, FakeCtx(args=["sam"])))
    text = msg.replies[0]["text"]
    assert "Sam Smith" in text
    assert "1 routes set" in text  # matches cmd_leaderboard's (imperfect) grammar
    assert "Crack Line" in text
    assert "Not Sam's" not in text
