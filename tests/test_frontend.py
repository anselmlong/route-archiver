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


def test_status_toggle_defaults_to_on_the_wall_hiding_retired(page):
    on_wall_btn = page.locator('[data-status="active"]')
    all_time_btn = page.locator('[data-status="all"]')
    assert "on" in (on_wall_btn.get_attribute("class") or "")
    assert "on" not in (all_time_btn.get_attribute("class") or "")
    names = page.locator(".nm").all_inner_texts()
    assert "Stripped Slab" not in names


def test_status_toggle_all_time_shows_retired_with_badge(page):
    page.locator('[data-status="all"]').click()
    page.wait_for_function("document.querySelectorAll('.card').length === 3")
    assert page.locator('[data-status="all"]').get_attribute("class").find("on") >= 0

    retired_card = page.locator(".card", has_text="Stripped Slab")
    assert retired_card.locator(".retired").count() == 1

    active_card = page.locator(".card", has_text="Crack Line")
    assert active_card.locator(".retired").count() == 0

    # switching back to "on the wall" drops it again
    page.locator('[data-status="active"]').click()
    page.wait_for_function("document.querySelectorAll('.card').length === 2")


def test_retired_badge_shown_in_detail_sheet(page):
    page.locator('[data-status="all"]').click()
    page.wait_for_function("document.querySelectorAll('.card').length === 3")
    page.locator(".card", has_text="Stripped Slab").click()
    page.wait_for_selector(".scrim.open")
    assert page.locator("#sretired").is_visible()
    # text-transform:uppercase in CSS -- innerText reflects the rendered case
    assert page.locator("#sretired").inner_text() == "RETIRED"

    # the sheet visually covers the scrim, so close it via a direct DOM
    # click rather than fighting Playwright's hit-testing
    page.eval_on_selector("#scrim", "el => el.click()")
    page.wait_for_selector(".scrim:not(.open)", state="attached")
    page.locator(".card", has_text="Crack Line").click()
    page.wait_for_selector(".scrim.open")
    assert not page.locator("#sretired").is_visible()


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
