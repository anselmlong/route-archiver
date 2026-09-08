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
    r1 = s.add_route(name="Crack Line", grade="V4", grade_low=5, wall="Left",
                      description="crimpy start", photo_path=str(photo))
    r2 = s.add_route(name='Weird "Name" & <tag>', grade="V6", grade_low=7, wall="Right",
                      description="<script>window.__xss=1</script>", photo_path=str(photo))
    s.set_rating(r1["id"], tg_user_id=1, tg_user_name="A", value=5)
    s.toggle_tick(r1["id"], tg_user_id=1, tg_user_name="A", suggested_grade="V4")

    r3 = s.add_route(name="Stripped Slab", grade="V2", grade_low=2, wall="Left",
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


def test_description_and_name_are_escaped_not_executed(page):
    """r2's name/description contain HTML metacharacters and a literal
    <script> tag; the frontend must render them as inert text (grid via
    esc()-then-innerHTML, sheet via textContent) and never execute them."""
    assert page.evaluate("window.__xss") is None

    hostile_card = page.locator(".card", has_text="Weird")
    assert "<script>" in hostile_card.locator(".desc").inner_text()
    assert hostile_card.locator(".desc script").count() == 0

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


def test_grade_range_slider_filters(page):
    # push the min-grade thumb up past V4 (index of V4 in [all,VB,V0..V8+])
    page.eval_on_selector(
        "#min",
        "el => { el.value = 8; el.dispatchEvent(new Event('input')); "
        "el.dispatchEvent(new Event('change')); }",
    )
    assert page.locator(".card").count() == 1
    assert page.locator(".nm").first.inner_text() != "Crack Line"


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


def test_sort_toggle_switches_active_button(page):
    grade_btn = page.locator('[data-sort="grade"]')
    new_btn = page.locator('[data-sort="new"]')
    assert "on" in grade_btn.get_attribute("class")
    new_btn.click()
    assert "on" in new_btn.get_attribute("class")
    assert "on" not in grade_btn.get_attribute("class")


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
    authed_page.wait_for_function("document.querySelectorAll('.card').length === 2")
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
    authed_page.wait_for_function("document.querySelectorAll('.card').length === 2")
    names = set(authed_page.locator(".nm").all_inner_texts())
    assert names == {"Crack Line", "Stripped Slab"}
    stats = authed_page.locator("#mineStats").inner_text()
    assert "2 sends" in stats
    assert "hardest V4" in stats


def test_mine_view_shows_retired_badge_on_retired_tick(authed_page):
    authed_page.locator('[data-view="mine"]').click()
    authed_page.wait_for_function("document.querySelectorAll('.card').length === 2")
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
    assert "Top climbers" in text
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
    page.wait_for_function("document.querySelectorAll('.card').length === 2")
    names = set(page.locator(".nm").all_inner_texts())
    # Crack Line and Stripped Slab both have a tick in the fixture;
    # "Weird Name" has none, so it's excluded from Hot entirely
    assert names == {"Crack Line", "Stripped Slab"}
    assert "route of the week" in page.locator("#hotStats").inner_text()


def test_hot_view_card_shows_recent_tick_badge(page):
    page.locator('[data-view="hot"]').click()
    page.wait_for_function("document.querySelectorAll('.card').length === 2")
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
        pg.wait_for_selector("#setterStats:not([hidden])")

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
