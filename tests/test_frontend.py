"""End-to-end frontend tests: real FastAPI app + real static/index.html,
driven with headless Chromium via Playwright. No mocking of fetch() — this
exercises the actual client/server contract."""
import base64
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

    port = _free_port()
    config = uvicorn.Config(api_mod.app, host="127.0.0.1", port=port, log_level="error")
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

    yield {"base_url": base_url, "route1": r1, "route2": r2, "storage": s}

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
