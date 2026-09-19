"""End-to-end frontend tests: real FastAPI app + real static/index.html,
driven with headless Chromium via Playwright. No mocking of fetch() — this
exercises the actual client/server contract."""
import base64
import json
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from storage import V_ORDER
from tests.conftest import TEST_BOT_TOKEN, sign_init_data

CHROMIUM_PATH = "/opt/pw-browsers/chromium"

# smallest valid PNG (1x1 transparent pixel)
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _boot_server(app):
    """Start a uvicorn server for `app` on a free port in a daemon thread,
    block until /healthz responds, and return (base_url, server, thread).
    Caller is responsible for server.should_exit=True + thread.join()."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            import urllib.request
            urllib.request.urlopen(f"{base_url}/healthz", timeout=0.2)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("live server never came up")
    return base_url, server, thread


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    import api as api_mod
    import storage as storage_mod

    api_mod.storage = storage_mod.Storage(db_path=tmp_path / "frontend_test.db")
    api_mod.BOT_TOKEN = TEST_BOT_TOKEN

    photo = tmp_path / "route.png"
    photo.write_bytes(_PNG_1PX)

    s = api_mod.storage
    r1 = s.add_route(name="Crack Line", grade="V4", grade_low=V_ORDER.index("V4"), wall="Left",
                      description="crimpy start", photo_path=str(photo))
    # hostile setter_name but NO setter_id: the strip renders the name (so
    # list-level escaping is exercised) while leaderboard_setters -- which
    # groups on setter_id -- still sees an empty setter board
    r2 = s.add_route(name='Weird "Name" & <tag>', grade="V6", grade_low=V_ORDER.index("V6"), wall="Right",
                      description="<script>window.__xss=1</script>",
                      setter_name='Eve <img src=x onerror="window.__xss=1">',
                      photo_path=str(photo))
    s.set_rating(r1["id"], tg_user_id=1, tg_user_name="A", value=5)
    s.toggle_tick(r1["id"], tg_user_id=1, tg_user_name="A", suggested_grade="V4")

    r3 = s.add_route(name="Stripped Slab", grade="V2", grade_low=V_ORDER.index("V2"), wall="Left",
                      photo_path=str(photo))
    s.toggle_tick(r3["id"], tg_user_id=1, tg_user_name="A")
    s.retire_route(r3["id"])

    base_url, server, thread = _boot_server(api_mod.app)
    yield {"base_url": base_url, "route1": r1, "route2": r2, "route3_retired": r3, "storage": s}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def consensus_live_server(tmp_path, monkeypatch):
    """A separate, minimal live server (not the shared `live_server`
    fixture) so grade-consensus seed data doesn't perturb the route/tick
    counts other tests assert on."""
    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    import api as api_mod
    import storage as storage_mod

    api_mod.storage = storage_mod.Storage(db_path=tmp_path / "consensus_test.db")
    api_mod.BOT_TOKEN = TEST_BOT_TOKEN

    photo = tmp_path / "route.png"
    photo.write_bytes(_PNG_1PX)

    s = api_mod.storage
    route = s.add_route(name="Debated Line", grade="V4", grade_low=5, wall="Left",
                         photo_path=str(photo))
    s.toggle_tick(route["id"], tg_user_id=1, tg_user_name="A", suggested_grade="V4")
    s.toggle_tick(route["id"], tg_user_id=2, tg_user_name="B", suggested_grade="V6")

    base_url, server, thread = _boot_server(api_mod.app)
    yield {"base_url": base_url, "route": route}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def setter_live_server(tmp_path, monkeypatch):
    """A separate live server with routes that actually have a
    setter_name, so the "set by X" link (and the setter-profile drill-
    down it opens) can be exercised -- the shared `live_server` fixture
    deliberately has none, so its leaderboard's setter board stays empty."""
    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    import api as api_mod
    import storage as storage_mod

    api_mod.storage = storage_mod.Storage(db_path=tmp_path / "setter_test.db")
    api_mod.BOT_TOKEN = TEST_BOT_TOKEN

    photo = tmp_path / "route.png"
    photo.write_bytes(_PNG_1PX)

    s = api_mod.storage
    r1 = s.add_route(name="Crimpy Wall", grade="V4", grade_low=5, wall="Left",
                      setter_name="Sam Smith", setter_id=10, photo_path=str(photo))
    r2 = s.add_route(name="Slopey Arete", grade="V6", grade_low=7, wall="Right",
                      setter_name="Sam Smith", setter_id=10, photo_path=str(photo))
    s.retire_route(r2["id"])
    s.set_rating(r1["id"], tg_user_id=1, tg_user_name="A", value=4)

    base_url, server, thread = _boot_server(api_mod.app)
    yield {"base_url": base_url, "r1": r1, "r2_retired": r2}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def page(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(live_server["base_url"] + "/")
        pg.wait_for_selector(".card")
        yield pg
        browser.close()


@pytest.fixture
def authed_page(live_server):
    """Same live server, with state.initData set directly to a validly
    signed initData for tg_user_id=1 (who the fixture already gave a
    rating + two ticks, one on a retired route) -- exercises the Mine/
    rating/tick flows that require a verified initData.

    Setting state.initData post-load (rather than faking
    window.Telegram.WebApp before the page's script runs) sidesteps a race
    with the real telegram-web-app.js CDN script, which -- if it happens
    to be reachable from this sandbox -- would load after any injected
    window.Telegram and overwrite it. Top-level `const state` in the
    page's inline script is still reachable by identifier from any script
    evaluated in the same page realm, even though it's not a `window`
    property.
    """
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1, first_name="Tester")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(live_server["base_url"] + "/")
        pg.wait_for_selector(".card")
        pg.evaluate(f"state.initData = {json.dumps(raw)}")
        yield pg
        browser.close()


def test_cards_render_with_grade_and_wall(page, live_server):
    cards = page.locator(".card")
    assert cards.count() == 2
    assert page.locator("#count").inner_text() == "2 routes"
    names = page.locator(".nm").all_inner_texts()
    assert "Crack Line" in names


def test_user_text_is_escaped_not_executed(page):
    """r2's name, setter_name and description carry HTML metacharacters
    and live payloads; the frontend must render them as inert text (strip
    via esc()-then-innerHTML, sheet via textContent) and never execute
    them. Description isn't shown in the strip any more, so the list-level
    check rides on setter_name."""
    assert page.evaluate("window.__xss") is None

    hostile_card = page.locator(".card", has_text="Weird")
    meta = hostile_card.locator(".meta")
    assert 'set by Eve <img src=x onerror="window.__xss=1">' in meta.inner_text()
    assert hostile_card.locator(".meta img").count() == 0

    hostile_card.click()
    page.wait_for_selector(".scrim.open")
    assert "<script>" in page.locator("#sdesc").inner_text()
    assert page.locator("#sdesc script").count() == 0
    assert page.locator("#sname").inner_text() == 'Weird "Name" & <tag>'


def test_wall_filter_narrows_grid(page):
    page.select_option("#wall", "Left")
    assert page.locator(".card").count() == 1
    assert page.locator(".nm").first.inner_text() == "Crack Line"
    page.select_option("#wall", "")
    assert page.locator(".card").count() == 2


def test_search_filters_by_name(page):
    page.fill("#search", "crack")
    assert page.locator(".card").count() == 1
    assert page.locator(".nm").first.inner_text() == "Crack Line"
    page.fill("#search", "")
    assert page.locator(".card").count() == 2


def test_search_is_case_insensitive_with_no_match_showing_empty_state(page):
    page.fill("#search", "CRACK")
    assert page.locator(".card").count() == 1
    page.fill("#search", "zzz-no-match")
    page.wait_for_selector("#empty:not([hidden])")


def test_search_filters_by_setter_name(setter_live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(setter_live_server["base_url"] + "/")
        pg.wait_for_selector(".card")
        pg.fill("#search", "sam")
        # both of Sam's routes match by setter, but only the active one
        # shows under the default "On the wall" status
        assert pg.locator(".card").count() == 1
        assert pg.locator(".nm").first.inner_text() == "Crimpy Wall"
        browser.close()


def _drag(page, which, index):
    page.eval_on_selector(
        which,
        "el => { el.value = %d; el.dispatchEvent(new Event('input')); "
        "el.dispatchEvent(new Event('change')); }" % index,
    )


def test_grade_range_slider_filters(page):
    # push the min thumb to V5, above Crack Line (V4) but below Weird (V6)
    _drag(page, "#min", V_ORDER.index("V5"))
    assert page.locator(".card").count() == 1
    assert page.locator(".nm").first.inner_text() != "Crack Line"


def test_grade_range_label_matches_what_is_actually_filtered(page):
    """Regression: the label read "all grades" whenever the min thumb sat at
    its floor, even with an upper bound clamped down hiding routes."""
    assert page.locator("#rangeLab").inner_text() == "all grades"
    assert page.locator(".card").count() == 2

    _drag(page, "#max", V_ORDER.index("V5"))
    # V6 is now filtered out, so the label must stop claiming otherwise
    assert page.locator(".card").count() == 1
    assert page.locator("#rangeLab").inner_text() != "all grades"
    assert "V5" in page.locator("#rangeLab").inner_text()


def test_grade_range_at_its_floor_is_not_an_empty_filter(page):
    """Regression: the scale used to carry a synthetic "all" slot at index 0,
    so dragging max to the bottom filtered for a grade nothing could hold and
    emptied the list while the label still said "all grades"."""
    _drag(page, "#max", 0)
    _drag(page, "#min", 0)
    # the floor of the scale is VB, a real grade, not a hole
    assert page.locator("#rangeLab").inner_text() == "VB\u2013VB"
    assert page.locator("#empty").is_visible()
    _drag(page, "#max", len(V_ORDER) - 1)
    assert page.locator(".card").count() == 2


def test_load_failure_shows_retry_and_recovers(live_server):
    """If /api/routes fails, load() must not leave a silently blank page --
    it should show a retry affordance, and retrying should recover once the
    network call starts succeeding again."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()

        first_call = {"failed": False}

        def maybe_fail(route):
            if not first_call["failed"]:
                first_call["failed"] = True
                route.fulfill(status=500, body="boom")
            else:
                route.continue_()

        pg.route("**/api/routes*", maybe_fail)
        pg.goto(live_server["base_url"] + "/")
        pg.wait_for_selector("#retry")
        assert "Couldn't load routes" in pg.locator("#empty").inner_text()

        pg.locator("#retry").click()
        pg.wait_for_selector(".card")
        assert pg.locator(".card").count() == 2
        browser.close()


def test_sort_control_is_labelled_and_reorders_the_list(page):
    """The old segmented pair painted its selected button the same colour as
    the strip behind it, so the active sort was invisible. A labelled select
    shows its own state."""
    assert page.locator("#sort").input_value() == "hard"
    # hardest first: V6 above V4
    assert page.locator(".nm").first.inner_text().startswith("Weird")

    page.select_option("#sort", "easy")
    assert page.locator(".nm").first.inner_text() == "Crack Line"

    page.select_option("#sort", "hard")
    assert page.locator(".nm").first.inner_text().startswith("Weird")


def test_opening_card_shows_detail_sheet(page):
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    assert page.locator("#sname").inner_text() == "Crack Line"
    assert page.locator("#sgrade").inner_text() == "V4"
    assert "1 ascent" in page.locator("#sascent").inner_text()


def test_sheet_is_hidden_and_non_interactive_until_a_card_is_opened(page):
    """Regression test: the bottom sheet is `position:fixed` at the bottom
    of the viewport: without an explicit hidden state it stays in the
    layout and intercepts clicks on whatever's underneath even when
    logically 'closed' (only the scrim backdrop used to toggle)."""
    assert not page.locator("#sheet").is_visible()
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    assert page.locator("#sheet").is_visible()
    page.eval_on_selector("#scrim", "el => el.click()")
    assert not page.locator("#sheet").is_visible()


def test_rating_without_telegram_context_prompts_alert_not_a_request(page):
    """Outside of Telegram, state.initData is empty; tapping a star should
    surface the explanatory alert and must NOT hit the network."""
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")

    dialog_messages = []
    page.on("dialog", lambda d: (dialog_messages.append(d.message), d.accept()))

    got_request = {"hit": False}
    page.route("**/api/rate/**", lambda route: (got_request.__setitem__("hit", True), route.continue_()))

    page.locator("#starselect span").nth(2).click()
    page.wait_for_timeout(150)

    assert dialog_messages, "expected an alert() prompting to open via Telegram"
    assert not got_request["hit"], "rating must not be sent without a verified Telegram session"


def test_tick_without_telegram_context_prompts_alert(page):
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")

    dialog_messages = []
    page.on("dialog", lambda d: (dialog_messages.append(d.message), d.accept()))

    page.locator("#stck").click()
    page.wait_for_timeout(150)
    assert dialog_messages, "expected an alert() prompting to open via Telegram"


def test_browse_always_hides_retired_routes(page):
    """Browse has no on-the-wall/all-time toggle -- it only ever shows
    active routes; retired ones only surface in views that explicitly
    include history (Mine, Hot)."""
    names = page.locator(".nm").all_inner_texts()
    assert "Stripped Slab" not in names
    assert page.locator(".card").count() == 2


def test_retired_badge_shown_in_detail_sheet(authed_page):
    # Stripped Slab is retired but still shows up in Mine (authed_page's
    # user ticked it before it came down) -- that's how we reach a
    # retired route's sheet now that Browse never shows one
    authed_page.locator('[data-view="mine"]').click()
    # #mineStats .mrow only exists once /api/me has resolved and renderMine
    # ran -- a card count would be satisfied by Browse's grid already
    authed_page.wait_for_selector("#mineStats .mrow")
    authed_page.locator(".card", has_text="Stripped Slab").click()
    authed_page.wait_for_selector(".scrim.open")
    assert authed_page.locator("#sretired").is_visible()
    # text-transform:uppercase in CSS -- innerText reflects the rendered case
    assert authed_page.locator("#sretired").inner_text() == "RETIRED"

    # the sheet visually covers the scrim, so close it via a direct DOM
    # click rather than fighting Playwright's hit-testing
    authed_page.eval_on_selector("#scrim", "el => el.click()")
    authed_page.wait_for_selector(".scrim:not(.open)", state="attached")
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    assert not authed_page.locator("#sretired").is_visible()


# --------------------------------------------------------------------------- #
# Mine (personal logbook) view
# --------------------------------------------------------------------------- #
def test_mine_view_without_telegram_prompts_open_message(page):
    page.locator('[data-view="mine"]').click()
    page.wait_for_selector("#empty:not([hidden])")
    assert "Open via Telegram" in page.locator("#empty").inner_text()
    assert page.locator(".card").count() == 0


def test_mine_view_shows_my_ticks_with_stats(authed_page):
    authed_page.locator('[data-view="mine"]').click()
    authed_page.wait_for_selector("#mineStats .mrow")
    names = set(authed_page.locator(".nm").all_inner_texts())
    assert names == {"Crack Line", "Stripped Slab"}
    stats = authed_page.locator("#mineStats").inner_text()
    assert "2 sends" in stats
    assert "hardest V4" in stats


def test_mine_view_shows_retired_badge_on_retired_tick(authed_page):
    authed_page.locator('[data-view="mine"]').click()
    authed_page.wait_for_selector("#mineStats .mrow")
    retired_card = authed_page.locator(".card", has_text="Stripped Slab")
    assert retired_card.locator(".retired").count() == 1
    active_card = authed_page.locator(".card", has_text="Crack Line")
    assert active_card.locator(".retired").count() == 0


def test_switching_between_views_toggles_correct_panels(authed_page):
    authed_page.locator('[data-view="mine"]').click()
    authed_page.wait_for_selector("#mineStats:not([hidden])")
    assert authed_page.locator("#browseControls").is_hidden()

    authed_page.locator('[data-view="board"]').click()
    authed_page.wait_for_selector("#board:not([hidden])")
    assert authed_page.locator("#mineStats").is_hidden()
    assert authed_page.locator("#grid").is_hidden()

    authed_page.locator('[data-view="browse"]').click()
    authed_page.wait_for_function("document.querySelectorAll('.card').length === 2")
    assert authed_page.locator("#browseControls").is_visible()
    assert authed_page.locator("#board").is_hidden()


# --------------------------------------------------------------------------- #
# Leaderboard view
# --------------------------------------------------------------------------- #
def test_leaderboard_view_shows_climber_ranking(authed_page):
    authed_page.locator('[data-view="board"]').click()
    authed_page.wait_for_selector(".brow")
    text = authed_page.locator("#board").inner_text()
    # headings render as uppercase micro-labels, so compare case-insensitively
    assert "top climbers" in text.lower()
    # the leaderboard reports each tick's tg_user_name as stored at tick
    # time ("A" in the fixture), not the display name from the current
    # initData ("Tester") -- a climber's history keeps the name they had
    # when they sent it
    assert "🥇 A" in text
    assert "2 sends" in text
    # no route in the fixture has a setter_name, so the setter board is empty
    assert "No routes set yet" in text


# --------------------------------------------------------------------------- #
# Grade consensus
# --------------------------------------------------------------------------- #
def test_consensus_grade_shown_in_detail_sheet(consensus_live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(consensus_live_server["base_url"] + "/")
        pg.wait_for_selector(".card")
        pg.locator(".card", has_text="Debated Line").click()
        pg.wait_for_selector(".scrim.open")
        text = pg.locator("#sconsensus").inner_text()
        assert "V5" in text
        assert "2 suggestions" in text
        browser.close()


def test_consensus_grade_shown_when_a_single_suggestion_exists(page):
    """Crack Line has exactly one tick with a parseable suggested_grade
    ("V4", matching the setter's own grade) -- consensus should still
    surface it, count and all."""
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    text = page.locator("#sconsensus").inner_text()
    assert "V4" in text
    assert "1 suggestion" in text


def test_consensus_grade_hidden_when_no_ticks(page):
    """The 'Weird Name' route has no ticks at all in the fixture, so
    there's nothing to build a consensus from."""
    page.locator(".card", has_text="Weird").click()
    page.wait_for_selector(".scrim.open")
    assert not page.locator("#sconsensus").is_visible()


# --------------------------------------------------------------------------- #
# Hot (route of the week) view
# --------------------------------------------------------------------------- #
def test_hot_view_shows_ticked_routes_with_stats_header(page):
    page.locator('[data-view="hot"]').click()
    # same reasoning as Mine: wait for Hot's own header, not a card count
    page.wait_for_selector("#hotStats .mrow")
    names = set(page.locator(".nm").all_inner_texts())
    # Crack Line and Stripped Slab both have a tick in the fixture;
    # "Weird Name" has none, so it's excluded from Hot entirely
    assert names == {"Crack Line", "Stripped Slab"}
    assert "route of the week" in page.locator("#hotStats").inner_text()


def test_hot_view_card_shows_recent_tick_badge(page):
    page.locator('[data-view="hot"]').click()
    page.wait_for_selector("#hotStats .mrow")
    badge = page.locator(".card", has_text="Crack Line").locator(".hotcount")
    assert badge.count() == 1
    assert "🔥" in badge.inner_text()


def test_hot_view_empty_state_when_nothing_ticked_this_week(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    import api as api_mod
    import storage as storage_mod

    api_mod.storage = storage_mod.Storage(db_path=tmp_path / "empty_hot.db")
    api_mod.BOT_TOKEN = TEST_BOT_TOKEN
    api_mod.storage.add_route(name="Untouched", grade="V4", grade_low=5)

    base_url, server, thread = _boot_server(api_mod.app)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
            pg = browser.new_page()
            pg.goto(base_url + "/")
            pg.wait_for_selector(".card")
            pg.locator('[data-view="hot"]').click()
            pg.wait_for_selector("#empty:not([hidden])")
            assert "No sends yet this week" in pg.locator("#empty").inner_text()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Setter profile drill-down
# --------------------------------------------------------------------------- #
def test_clicking_setter_name_opens_their_profile(setter_live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(setter_live_server["base_url"] + "/")
        pg.wait_for_selector(".card")

        pg.locator(".card", has_text="Crimpy Wall").click()
        pg.wait_for_selector(".scrim.open")
        pg.locator(".setterlink").click()
        # #setterStats is unhidden synchronously with a "loading…"
        # placeholder; .mrow only appears once the profile has loaded
        pg.wait_for_selector("#setterStats .mrow")

        stats = pg.locator("#setterStats").inner_text()
        assert "Sam Smith" in stats
        assert "2 routes set" in stats
        assert "1 active, 1 retired" in stats
        assert "4.0" in stats  # avg rating received

        # both of Sam's routes render in the reused grid, active + retired
        pg.wait_for_function("document.querySelectorAll('.card').length === 2")
        names = set(pg.locator(".nm").all_inner_texts())
        assert names == {"Crimpy Wall", "Slopey Arete"}
        browser.close()


def test_setter_profile_back_button_returns_to_browse(setter_live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(setter_live_server["base_url"] + "/")
        pg.wait_for_selector(".card")

        pg.locator(".card", has_text="Crimpy Wall").click()
        pg.wait_for_selector(".scrim.open")
        pg.locator(".setterlink").click()
        pg.wait_for_selector("#setterStats:not([hidden])")

        pg.locator("#setterBack").click()
        pg.wait_for_selector("#browseControls:not([hidden])")
        assert pg.locator("#setterStats").is_hidden()
        # default active-only browse view: Sam's retired route drops out
        pg.wait_for_function("document.querySelectorAll('.card').length === 1")
        browser.close()


# --------------------------------------------------------------------------- #
# Comment threads
# --------------------------------------------------------------------------- #
def test_comment_thread_empty_state(page):
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    page.wait_for_selector("#clist .cempty")
    assert "No comments yet" in page.locator("#clist").inner_text()


def test_posting_comment_without_telegram_prompts_alert_not_a_request(page):
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    page.wait_for_selector("#clist .cempty")

    dialog_messages = []
    page.on("dialog", lambda d: (dialog_messages.append(d.message), d.accept()))
    got_request = {"hit": False}
    page.route("**/api/comments/**", lambda route: (got_request.__setitem__("hit", True), route.continue_()))

    page.fill("#cinput", "some beta")
    page.locator("#cpost").click()
    page.wait_for_timeout(150)

    assert dialog_messages, "expected an alert() prompting to open via Telegram"
    assert not got_request["hit"], "comment must not be posted without a verified Telegram session"
    assert "No comments yet" in page.locator("#clist").inner_text()


def test_posting_and_deleting_own_comment(authed_page):
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.wait_for_selector("#clist .cempty")

    authed_page.fill("#cinput", "watch the crimpy start")
    authed_page.locator("#cpost").click()
    authed_page.wait_for_selector(".crow")
    text = authed_page.locator(".crow").inner_text()
    assert "watch the crimpy start" in text
    assert "Tester" in text  # tg_user_name from the signed initData
    assert authed_page.locator("#cinput").input_value() == ""

    # the card behind the sheet reflects the new comment count
    authed_page.eval_on_selector("#scrim", "el => el.click()")
    authed_page.wait_for_selector(".scrim:not(.open)", state="attached")
    badge = authed_page.locator(".card", has_text="Crack Line").locator(".commentcount")
    assert "💬 1" in badge.inner_text()

    # delete it -- back to the empty state, count badge gone
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.locator(".cdel").click()
    authed_page.wait_for_selector("#clist .cempty")
    assert "No comments yet" in authed_page.locator("#clist").inner_text()
    authed_page.eval_on_selector("#scrim", "el => el.click()")
    authed_page.wait_for_selector(".scrim:not(.open)", state="attached")
    assert authed_page.locator(".card", has_text="Crack Line").locator(".commentcount").count() == 0


def test_comment_from_another_user_has_no_delete_button(live_server, authed_page):
    # seed a comment from a different Telegram user directly via storage
    live_server["storage"].add_comment(
        live_server["route1"]["id"], tg_user_id=999, tg_user_name="Other Climber", text="beta from someone else"
    )
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.wait_for_selector(".crow")
    row = authed_page.locator(".crow").first
    assert "Other Climber" in row.inner_text()
    assert row.locator(".cdel").count() == 0


def test_comment_rejected_when_over_500_chars_shows_alert(authed_page):
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.wait_for_selector("#clist .cempty")

    dialog_messages = []
    authed_page.on("dialog", lambda d: (dialog_messages.append(d.message), d.accept()))
    # the <input maxlength=500> already blocks this in a real browser UI,
    # but exercise the server-side 422 path directly to make sure it's
    # actually enforced, not just a client-side nicety
    authed_page.eval_on_selector(
        "#cinput", "(el, v) => { el.value = v; el.removeAttribute('maxlength'); }", "x" * 501
    )
    authed_page.locator("#cpost").click()
    authed_page.wait_for_timeout(150)
    assert dialog_messages
    assert "No comments yet" in authed_page.locator("#clist").inner_text()


# --------------------------------------------------------------------------- #
# leaving the detail sheet
# --------------------------------------------------------------------------- #
def test_close_button_exits_the_detail_sheet(page):
    """The sheet can stand 94vh tall, leaving the scrim a sliver at the top,
    so tapping the backdrop was a near-impossible exit on a phone."""
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    page.locator("#sheetclose").click()
    page.wait_for_selector(".scrim:not(.open)", state="attached")
    assert not page.locator("#sheet").is_visible()


def test_escape_closes_the_detail_sheet(page):
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    page.keyboard.press("Escape")
    page.wait_for_selector(".scrim:not(.open)", state="attached")
    assert not page.locator("#sheet").is_visible()


def test_telegram_back_button_tracks_the_sheet(page):
    """Inside Telegram the native back button is the exit people reach for,
    so it has to appear with the sheet and go away with it."""
    page.evaluate(
        "window.Telegram={WebApp:{BackButton:{"
        "show(){window.__bb=true},hide(){window.__bb=false}}}}"
    )
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    assert page.evaluate("window.__bb") is True
    page.locator("#sheetclose").click()
    page.wait_for_selector(".scrim:not(.open)", state="attached")
    assert page.evaluate("window.__bb") is False


# --------------------------------------------------------------------------- #
# write confirmation
# --------------------------------------------------------------------------- #
def test_rating_shows_an_inline_confirmation(authed_page):
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.locator("#starselect span").nth(2).click()
    authed_page.wait_for_selector("#ratesaved:not([hidden])")
    assert "saved" in authed_page.locator("#ratesaved").inner_text()


def test_grade_suggestion_confirms_and_refreshes_the_consensus(authed_page):
    """Regression: submitting a suggested grade wrote to the server and
    changed nothing on screen -- no confirmation, and the community-grade
    line above it stayed stale until a full reload."""
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    # tg_user_id=1 already ticked this route suggesting V4
    assert "V4" in authed_page.locator("#sconsensus").inner_text()

    authed_page.eval_on_selector(
        "#stckgrade",
        "el => { el.value = 'V6'; el.dispatchEvent(new Event('change')); }",
    )
    authed_page.wait_for_selector("#gradesaved:not([hidden])")
    assert "saved" in authed_page.locator("#gradesaved").inner_text()
    # the consensus line reflects the new suggestion without a reload
    assert "V6" in authed_page.locator("#sconsensus").inner_text()


def test_failed_rating_surfaces_an_error_instead_of_going_quiet(authed_page):
    authed_page.route("**/api/rate/**",
                      lambda route: route.fulfill(status=500, body='{"error":"boom"}'))
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.locator("#starselect span").nth(2).click()
    authed_page.wait_for_selector("#ratesaved:not([hidden])")
    assert "bad" in authed_page.locator("#ratesaved").get_attribute("class")


def test_mine_header_names_the_climber(authed_page):
    """The endpoint always returned the caller's name; the view never used
    it, so the page never said whose logbook it was."""
    authed_page.locator('[data-view="mine"]').click()
    authed_page.wait_for_selector("#mineStats .mrow")
    assert "Tester" in authed_page.locator("#mineStats").inner_text()


# --------------------------------------------------------------------------- #
# ungraded (V?) routes under the grade filter
# --------------------------------------------------------------------------- #
@pytest.fixture
def wildcard_live_server(tmp_path, monkeypatch):
    """A live server holding one ungraded route alongside a graded one."""
    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    import api as api_mod
    import storage as storage_mod

    api_mod.storage = storage_mod.Storage(db_path=tmp_path / "wildcard_test.db")
    api_mod.BOT_TOKEN = TEST_BOT_TOKEN
    photo = tmp_path / "route.png"
    photo.write_bytes(_PNG_1PX)

    s = api_mod.storage
    s.add_route(name="Graded Line", grade="V4", grade_low=V_ORDER.index("V4"),
                wall="Left", photo_path=str(photo))
    s.add_route(name="Mystery Line", grade="V?", grade_low=storage_mod.WILD_LOW,
                wall="Left", photo_path=str(photo))

    base_url, server, thread = _boot_server(api_mod.app)
    yield {"base_url": base_url}
    server.should_exit = True
    thread.join(timeout=5)


def test_ungraded_routes_show_until_the_range_is_narrowed(wildcard_live_server):
    """Regression: an ungraded route used to vanish the moment the min thumb
    moved, yet survive an upper bound -- inconsistent in both directions."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(wildcard_live_server["base_url"] + "/")
        pg.wait_for_selector(".card")
        assert pg.locator(".card").count() == 2

        # an upper bound is a narrowed range, so the ungraded route drops out
        _drag(pg, "#max", V_ORDER.index("V5"))
        names = pg.locator(".nm").all_inner_texts()
        assert names == ["Graded Line"]

        # clearing the range brings it back
        _drag(pg, "#max", len(V_ORDER) - 1)
        assert pg.locator(".card").count() == 2
        browser.close()


def test_ungraded_routes_sort_last_in_both_directions(wildcard_live_server):
    """An ungraded route has no place on the scale, so it belongs at the end
    of a grade sort either way, never leading the easiest-first list."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(wildcard_live_server["base_url"] + "/")
        pg.wait_for_selector(".card")
        assert pg.locator(".nm").last.inner_text() == "Mystery Line"

        pg.select_option("#sort", "easy")
        assert pg.locator(".nm").last.inner_text() == "Mystery Line"
        browser.close()


def test_search_is_scoped_to_browse(authed_page):
    """Regression: the search box sat outside the browse controls, so it
    stayed on screen in Mine/Leaderboard/Hot while still filtering the browse
    collection -- one keystroke there swapped browse results into the view
    without the tab changing."""
    assert authed_page.locator("#search").is_visible()

    authed_page.locator('[data-view="mine"]').click()
    authed_page.wait_for_selector("#mineStats .mrow")
    assert authed_page.locator("#search").is_hidden()

    authed_page.locator('[data-view="board"]').click()
    authed_page.wait_for_selector(".brow")
    assert authed_page.locator("#search").is_hidden()

    authed_page.locator('[data-view="browse"]').click()
    authed_page.wait_for_function("document.querySelectorAll('.card').length === 2")
    assert authed_page.locator("#search").is_visible()


def test_close_button_stays_reachable_after_scrolling_the_sheet(page):
    """The exit used to sit inside the scrolling content, so on a long route
    it scrolled away with the photo, leaving only the scrim sliver."""
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    page.eval_on_selector("#sheet", "el => { el.scrollTop = el.scrollHeight }")
    page.wait_for_timeout(120)

    box = page.locator("#sheetclose").bounding_box()
    sheet = page.locator("#sheet").bounding_box()
    assert box is not None
    # still pinned near the top edge of the sheet, not scrolled off it
    assert box["y"] - sheet["y"] < 60
    page.locator("#sheetclose").click()
    page.wait_for_selector(".scrim:not(.open)", state="attached")
    assert not page.locator("#sheet").is_visible()


# --------------------------------------------------------------------------- #
# defects found by the correctness review
# --------------------------------------------------------------------------- #
@pytest.fixture
def hostile_wall_live_server(tmp_path, monkeypatch):
    """A wall name carrying an HTML payload. An unrecognised wall is stored
    verbatim by _norm_wall, and the bot derives walls from forum topic titles,
    so a crafted topic name reaches every viewer's wall dropdown."""
    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    import api as api_mod
    import storage as storage_mod

    api_mod.storage = storage_mod.Storage(db_path=tmp_path / "wall_xss.db")
    api_mod.BOT_TOKEN = TEST_BOT_TOKEN
    photo = tmp_path / "route.png"
    photo.write_bytes(_PNG_1PX)
    api_mod.storage.add_route(
        name="Innocent", grade="V4", grade_low=V_ORDER.index("V4"),
        wall='left"><img src=x onerror="window.__wallxss=1">', photo_path=str(photo))

    base_url, server, thread = _boot_server(api_mod.app)
    yield {"base_url": base_url}
    server.should_exit = True
    thread.join(timeout=5)


def test_wall_names_are_escaped_in_the_filter_dropdown(hostile_wall_live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(hostile_wall_live_server["base_url"] + "/")
        pg.wait_for_selector(".card")
        assert pg.evaluate("window.__wallxss") is None
        assert pg.locator("#wall img").count() == 0
        # the payload survives as inert text in the option label...
        assert "onerror" in pg.locator("#wall").inner_text()
        # ...and, the part that actually breaks the feature, the option's
        # value is the whole wall name rather than truncating at the quote,
        # so selecting it still filters to that wall
        values = pg.eval_on_selector_all("#wall option", "els => els.map(e => e.value)")
        assert '\'left"><img src=x onerror="window.__wallxss=1">\'' .strip("'") in values
        browser.close()


def test_a_missing_setter_profile_does_not_break_later_writes(authed_page):
    """Regression: openSetter stored the raw response, so a 404 body (which
    has no routes array) poisoned findRoutes. Every later rating and tick then
    threw after the server had already stored it, so the sheet reported a
    connection failure -- and the obvious retry silently undid the write."""
    authed_page.evaluate("openSetter(99999)")
    authed_page.wait_for_selector("#setterBack")
    assert "not found" in authed_page.locator("#setterStats").inner_text().lower()
    authed_page.locator("#setterBack").click()
    authed_page.wait_for_function("document.querySelectorAll('.card').length === 2")

    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.locator("#starselect span").nth(2).click()
    authed_page.wait_for_selector("#ratesaved:not([hidden])")
    saved = authed_page.locator("#ratesaved")
    assert "bad" not in (saved.get_attribute("class") or "")
    assert "saved" in saved.inner_text()


def test_grade_range_can_be_reopened_from_the_top_of_the_scale(page):
    """Regression: both range inputs overlap, and at the top of the scale the
    one painted on top was the max thumb, which is clamped by min. Dragging it
    back did nothing, leaving an empty list with no way out but a reload."""
    top = len(V_ORDER) - 1
    _drag(page, "#min", top)
    assert page.locator(".card").count() == 0

    box = page.locator(".range-wrap").bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] - 2, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.3, y, steps=12)
    page.mouse.up()

    assert page.evaluate("state.min") < top
    assert page.locator(".card").count() > 0


def test_a_late_rating_response_does_not_confirm_on_another_route(authed_page):
    """Regression: the saved indicator was not gated on the sheet still
    showing the route that was rated, so a slow response painted a green
    'rating saved' next to a different route's empty stars."""
    # hold the rating response open from inside the page, so the ordering is
    # deterministic -- a sleeping route handler would block the click itself
    authed_page.evaluate("""
      window.__release = null;
      const real = window.fetch;
      window.fetch = (u, o) => String(u).includes('/api/rate/')
        ? new Promise(done => { window.__release = () => done(new Response(
            JSON.stringify({avg: 3, count: 1, my_rating: 3}),
            {status: 200, headers: {'Content-Type': 'application/json'}})); })
        : real(u, o);
    """)
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.locator("#starselect span").nth(2).click()
    authed_page.wait_for_function("window.__release !== null")

    authed_page.keyboard.press("Escape")
    authed_page.wait_for_selector(".scrim:not(.open)", state="attached")
    authed_page.locator(".card", has_text="Weird").click()
    authed_page.wait_for_selector(".scrim.open")

    authed_page.evaluate("window.__release()")
    authed_page.wait_for_timeout(250)

    assert authed_page.locator("#ratesaved").is_hidden()
    assert authed_page.evaluate("state.cur.my_rating") in (None, 0)


def _reload_personalised(pg):
    """The authed fixture sets initData after the first load, so re-fetch to
    get my_tick / my_suggested_grade attached to the routes."""
    pg.evaluate("load()")
    pg.wait_for_function("state.routes.length && state.routes.some(r => r.my_tick)")


def test_escape_commits_a_pending_grade_edit(authed_page):
    """Regression: closeSheet nulled state.cur inside the keydown handler, so
    the input's change event (which only fires on blur) found nothing to save.
    The X button saved the edit and Escape silently dropped it."""
    _reload_personalised(authed_page)
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.locator("#stckgrade").fill("V7")
    authed_page.keyboard.press("Escape")   # first leaves the field
    authed_page.keyboard.press("Escape")   # second closes the sheet
    authed_page.wait_for_selector(".scrim:not(.open)", state="attached")

    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    assert authed_page.locator("#stckgrade").input_value() == "V7"
    assert "V7" in authed_page.locator("#sconsensus").inner_text()


def test_escape_in_the_comment_box_keeps_the_draft(authed_page):
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.locator("#cinput").click()
    authed_page.locator("#cinput").fill("heel hook the arete")
    authed_page.keyboard.press("Escape")

    assert authed_page.locator("#sheet").is_visible()
    assert authed_page.locator("#cinput").input_value() == "heel hook the arete"
    authed_page.keyboard.press("Escape")
    authed_page.wait_for_selector(".scrim:not(.open)", state="attached")


def test_re_ticking_clears_the_previous_suggested_grade(authed_page):
    """Regression: a fresh tick carries no suggestion server-side, but the
    sheet kept showing the old one, so the climber had no reason to re-enter
    it and it never rejoined the consensus."""
    _reload_personalised(authed_page)
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    # make the suggestion differ from the setter's own grade, so the fallback
    # and the stale value are telling apart
    authed_page.locator("#stckgrade").fill("V7")
    authed_page.locator("#stckgrade").blur()
    authed_page.wait_for_function("state.cur.my_suggested_grade === 'V7'")

    authed_page.locator("#stck").click()          # untick
    authed_page.wait_for_function("state.cur.my_tick === 0")
    authed_page.locator("#stck").click()          # re-tick
    authed_page.wait_for_function("state.cur.my_tick === 1")

    assert authed_page.evaluate("state.cur.my_suggested_grade") in (None, "")
    # falls back to the setter's grade, not the suggestion the server dropped
    assert authed_page.locator("#stckgrade").input_value() == "V4"


# --------------------------------------------------------------------------- #
# full-screen route photo
# --------------------------------------------------------------------------- #
def test_sheet_photo_is_never_cropped(page):
    """A route photo is the point of the route. The band used to cover-crop
    to a fixed height, so a portrait shot lost its top and bottom -- exactly
    the parts that show where the route starts and finishes."""
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    fit = page.eval_on_selector("#simg", "el => getComputedStyle(el).objectFit")
    assert fit == "contain"


def test_tapping_the_photo_opens_it_full_screen(page):
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    assert page.locator("#lightbox").is_hidden()

    page.locator("#shot").click()
    page.wait_for_selector(".lightbox.open")
    assert page.locator("#lbimg").get_attribute("src")
    assert page.eval_on_selector("#lbimg", "el => getComputedStyle(el).objectFit") == "contain"

    page.locator("#lbclose").click()
    page.wait_for_selector(".lightbox:not(.open)", state="attached")
    # closing the photo returns to the route, it does not dump you on the list
    assert page.locator("#sheet").is_visible()
    assert page.locator("#sname").inner_text() == "Crack Line"


def test_escape_unwinds_the_photo_before_the_sheet(page):
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    page.locator("#shot").click()
    page.wait_for_selector(".lightbox.open")

    page.keyboard.press("Escape")
    page.wait_for_selector(".lightbox:not(.open)", state="attached")
    assert page.locator("#sheet").is_visible()

    page.keyboard.press("Escape")
    page.wait_for_selector(".scrim:not(.open)", state="attached")
    assert not page.locator("#sheet").is_visible()


def test_telegram_back_unwinds_the_photo_before_the_sheet(page):
    """Inside Telegram the native back button is the exit people reach for,
    and it has to peel one layer at a time."""
    page.evaluate(
        "window.Telegram={WebApp:{BackButton:{"
        "show(){window.__bb=true},hide(){window.__bb=false},"
        "onClick(fn){window.__fire=fn}}}}"
    )
    page.evaluate("window.Telegram.WebApp.BackButton.onClick(onBack)")
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    page.locator("#shot").click()
    page.wait_for_selector(".lightbox.open")

    page.evaluate("window.__fire()")
    page.wait_for_selector(".lightbox:not(.open)", state="attached")
    assert page.locator("#sheet").is_visible()
    assert page.evaluate("window.__bb") is True   # still armed for the sheet

    page.evaluate("window.__fire()")
    page.wait_for_selector(".scrim:not(.open)", state="attached")
    assert page.evaluate("window.__bb") is False


def test_closing_the_sheet_takes_the_photo_with_it(page):
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    page.locator("#shot").click()
    page.wait_for_selector(".lightbox.open")

    page.eval_on_selector("#scrim", "el => el.click()")
    page.wait_for_selector(".scrim:not(.open)", state="attached")
    # a full-screen photo left floating over the route list would trap the user
    assert page.locator("#lightbox").is_hidden()


@pytest.fixture
def photoless_live_server(tmp_path, monkeypatch):
    """A route archived without a photo, so /api/photo 404s for it."""
    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    import api as api_mod
    import storage as storage_mod

    api_mod.storage = storage_mod.Storage(db_path=tmp_path / "nophoto.db")
    api_mod.BOT_TOKEN = TEST_BOT_TOKEN
    api_mod.storage.add_route(name="No Shot", grade="V4",
                              grade_low=V_ORDER.index("V4"), wall="Left")
    base_url, server, thread = _boot_server(api_mod.app)
    yield {"base_url": base_url}
    server.should_exit = True
    thread.join(timeout=5)


def test_a_route_without_a_photo_offers_no_enlarge(photoless_live_server):
    """Regression: the band still said 'tap to enlarge' and opened a
    full-screen black rectangle for a route that has no photo at all."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(photoless_live_server["base_url"] + "/")
        pg.wait_for_selector(".card")
        pg.locator(".card").first.click()
        pg.wait_for_selector(".scrim.open")
        pg.wait_for_function("document.querySelector('#shot').classList.contains('nophoto')")

        assert pg.locator(".zoomhint").is_hidden()
        assert pg.locator("#shot").get_attribute("role") is None
        pg.locator("#shot").click()
        pg.wait_for_timeout(150)
        assert pg.locator("#lightbox").is_hidden()
        browser.close()


def test_the_open_photo_blocks_the_keyboard_from_what_is_underneath(page):
    """Regression: nothing behind the overlay was inert, so Tab walked onto
    the tick button hidden under the photo. One blind Tab+Enter un-ticked the
    route while the screen showed only the image."""
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    page.locator("#shot").click()
    page.wait_for_selector(".lightbox.open")

    assert page.evaluate("document.activeElement && document.activeElement.id") == "lbclose"
    for _ in range(6):
        page.keyboard.press("Tab")
        inside_sheet = page.evaluate(
            "document.activeElement ? document.querySelector('#sheet').contains(document.activeElement) : false")
        assert not inside_sheet, "focus reached a control hidden under the photo"

    page.keyboard.press("Escape")
    page.wait_for_selector(".lightbox:not(.open)", state="attached")
    # the controls come back once the photo is gone
    assert page.evaluate("document.querySelector('#stck').closest('[inert]') === null")


def test_a_failed_reload_clears_the_stale_list(live_server):
    """Regression: load()'s catch showed a retry link but left the old cards
    on screen and state.routes undefined, so tapping one threw after the
    server had already stored the write."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.goto(live_server["base_url"] + "/")
        pg.wait_for_selector(".card")

        pg.route("**/api/routes*", lambda r: r.fulfill(status=500, body="boom"))
        pg.evaluate("load()")
        pg.wait_for_selector("#retry")

        assert pg.locator(".card").count() == 0
        # a rejected payload is never adopted, so the collection stays an array
        assert pg.evaluate("Array.isArray(state.routes)") is True
        # and even if it were not, the lookup returns empty instead of throwing
        assert pg.evaluate("state.routes = undefined; findRoutes(1).length") == 0
        browser.close()


def test_a_failed_write_does_not_report_into_another_route(authed_page):
    """Regression: the wrong-route gate was added to the success branch only,
    so a rejected rating painted a red error under a different route's stars."""
    authed_page.evaluate("""
      window.__release = null;
      const real = window.fetch;
      window.fetch = (u, o) => String(u).includes('/api/rate/')
        ? new Promise(done => { window.__release = () => done(new Response(
            JSON.stringify({error: 'server exploded'}),
            {status: 500, headers: {'Content-Type': 'application/json'}})); })
        : real(u, o);
    """)
    authed_page.locator(".card", has_text="Crack Line").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.locator("#starselect span").nth(2).click()
    authed_page.wait_for_function("window.__release !== null")

    authed_page.keyboard.press("Escape")
    authed_page.wait_for_selector(".scrim:not(.open)", state="attached")
    authed_page.locator(".card", has_text="Weird").click()
    authed_page.wait_for_selector(".scrim.open")
    authed_page.evaluate("window.__release()")
    authed_page.wait_for_timeout(250)

    assert authed_page.locator("#ratesaved").is_hidden()


def test_a_collapsed_grade_range_can_still_be_moved(page):
    """Regression: the z-index swap only rescued the very top of the scale.
    Collapse the range anywhere else and the thumb painted on top was pinned
    by the other one, so the list stayed empty with no way to reopen it."""
    mid = V_ORDER.index("V5")
    _drag(page, "#max", mid)
    _drag(page, "#min", mid)
    assert page.evaluate("[state.min, state.max]") == [mid, mid]

    box = page.locator(".range-wrap").bounding_box()
    y = box["y"] + box["height"] / 2
    x = box["x"] + box["width"] * (mid / (len(V_ORDER) - 1))
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.05, y, steps=12)
    page.mouse.up()

    # the pair moves instead of jamming, so the filter is never a dead end
    assert page.evaluate("state.min") < mid


def test_telegram_back_button_is_actually_wired_to_the_unwind(live_server):
    """The sibling test re-registers the handler by hand, so it exercises the
    unwind logic but not the registration itself. This one installs a stub
    before the page's own script runs, so the real wiring has to happen."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        pg = browser.new_page()
        pg.add_init_script("""
          window.Telegram = {WebApp: {
            initData: "", initDataUnsafe: {},
            ready(){}, expand(){},
            BackButton: {
              show(){window.__bb = true}, hide(){window.__bb = false},
              onClick(fn){window.__registered = fn},
            },
          }};
        """)
        pg.goto(live_server["base_url"] + "/")
        pg.wait_for_selector(".card")
        assert pg.evaluate("typeof window.__registered") == "function"

        pg.locator(".card", has_text="Crack Line").click()
        pg.wait_for_selector(".scrim.open")
        pg.locator("#shot").click()
        pg.wait_for_selector(".lightbox.open")

        pg.evaluate("window.__registered()")
        pg.wait_for_selector(".lightbox:not(.open)", state="attached")
        assert pg.locator("#sheet").is_visible()

        pg.evaluate("window.__registered()")
        pg.wait_for_selector(".scrim:not(.open)", state="attached")
        assert pg.evaluate("window.__bb") is False
        browser.close()
