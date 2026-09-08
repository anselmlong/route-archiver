"""FastAPI endpoint tests: auth (signed Telegram initData), CRUD-ish flows,
and the error paths the api.py hardening pass added."""
from tests.conftest import TEST_BOT_TOKEN, sign_init_data


def _seed_route(api_module, **overrides):
    kwargs = dict(name="Crack Line", grade="V4", grade_low=5, wall="Left")
    kwargs.update(overrides)
    return api_module.storage.add_route(**kwargs)


# --------------------------------------------------------------------------- #
# initData validation
# --------------------------------------------------------------------------- #
def test_validate_init_data_accepts_well_signed_payload(api_module):
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=42, first_name="Ann")
    uid, name = api_module.validate_init_data(raw)
    assert uid == 42
    assert name == "Ann"


def test_validate_init_data_rejects_tampered_hash(api_module):
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=42)
    tampered = raw[:-1] + ("0" if raw[-1] != "0" else "1")
    try:
        api_module.validate_init_data(tampered)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "signature" in str(e) or "bad" in str(e).lower()


def test_validate_init_data_rejects_stale_auth_date(api_module):
    import time
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=42, auth_date=int(time.time()) - 999999)
    try:
        api_module.validate_init_data(raw)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "stale" in str(e)


def test_validate_init_data_fails_closed_without_bot_token(api_module):
    api_module.BOT_TOKEN = ""
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=42)
    try:
        api_module.validate_init_data(raw)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "not configured" in str(e)


def test_validate_init_data_rejects_empty_string(api_module):
    try:
        api_module.validate_init_data("")
        assert False, "expected ValueError"
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# GET /api/routes, /api/meta, /api/photo
# --------------------------------------------------------------------------- #
def test_meta_reports_grades_walls_count(client, api_module):
    _seed_route(api_module, wall="Left")
    _seed_route(api_module, name="Slab Line", wall="Right", grade="V2", grade_low=2)
    r = client.get("/api/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert set(body["walls"]) == {"Left", "Right"}
    assert "V8+" in body["grades"]


def test_routes_hides_internal_fields(client, api_module):
    _seed_route(api_module)
    r = client.get("/api/routes")
    row = r.json()["routes"][0]
    assert "photo_path" not in row
    assert "_id" not in row


def test_routes_filters_by_grade_and_wall(client, api_module):
    _seed_route(api_module, name="Easy", grade="V1", grade_low=1, wall="Left")
    _seed_route(api_module, name="Hard", grade="V6", grade_low=7, wall="Right")
    r = client.get("/api/routes", params={"grade": "V5"})
    names = [x["name"] for x in r.json()["routes"]]
    assert names == ["Hard"]
    r2 = client.get("/api/routes", params={"wall": "Left"})
    names2 = [x["name"] for x in r2.json()["routes"]]
    assert names2 == ["Easy"]


def test_routes_personalizes_when_valid_tg_query_param_given(client, api_module):
    route = _seed_route(api_module)
    api_module.storage.set_rating(route["id"], tg_user_id=7, tg_user_name="A", value=5)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=7)
    r = client.get("/api/routes", params={"tg": raw})
    row = r.json()["routes"][0]
    assert row["my_rating"] == 5


def test_routes_ignores_invalid_tg_param_instead_of_erroring(client, api_module):
    _seed_route(api_module)
    r = client.get("/api/routes", params={"tg": "not-a-valid-initdata"})
    assert r.status_code == 200
    assert r.json()["routes"][0]["my_rating"] is None


def test_photo_404_when_route_missing(client):
    r = client.get("/api/photo/999")
    assert r.status_code == 404


def test_photo_404_when_no_photo_on_route(client, api_module):
    route = _seed_route(api_module)
    r = client.get(f"/api/photo/{route['id']}")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# POST /api/rate
# --------------------------------------------------------------------------- #
def test_rate_requires_auth(client, api_module):
    route = _seed_route(api_module)
    r = client.post(f"/api/rate/{route['id']}", json={"value": 4})
    assert r.status_code == 401


def test_rate_rejects_out_of_range_value_with_422(client, api_module):
    route = _seed_route(api_module)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    r = client.post(f"/api/rate/{route['id']}", json={"value": 9, "init_data": raw})
    assert r.status_code == 422


def test_rate_rejects_non_integer_value_with_422(client, api_module):
    route = _seed_route(api_module)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    r = client.post(f"/api/rate/{route['id']}", json={"value": "high", "init_data": raw})
    assert r.status_code == 422


def test_rate_404_for_missing_route(client, api_module):
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    r = client.post("/api/rate/999", json={"value": 4, "init_data": raw})
    assert r.status_code == 404


def test_rate_success_then_toggle_off(client, api_module):
    route = _seed_route(api_module)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    r = client.post(f"/api/rate/{route['id']}", json={"value": 4, "init_data": raw})
    assert r.status_code == 200
    assert r.json() == {"avg": 4.0, "count": 1, "my_rating": 4}
    r2 = client.post(f"/api/rate/{route['id']}", json={"value": 4, "init_data": raw})
    assert r2.json()["my_rating"] is None


def test_rate_via_header_instead_of_body(client, api_module):
    route = _seed_route(api_module)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    r = client.post(f"/api/rate/{route['id']}", json={"value": 3},
                     headers={"X-Telegram-Init-Data": raw})
    assert r.status_code == 200
    assert r.json()["my_rating"] == 3


def test_rate_missing_body_is_422_not_500(client, api_module):
    route = _seed_route(api_module)
    r = client.post(f"/api/rate/{route['id']}", content=b"not json",
                     headers={"Content-Type": "application/json"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# POST /api/tick, PUT /api/tick/{id}/grade
# --------------------------------------------------------------------------- #
def test_tick_requires_auth(client, api_module):
    route = _seed_route(api_module)
    r = client.post(f"/api/tick/{route['id']}", json={})
    assert r.status_code == 401


def test_tick_toggle_on_then_off(client, api_module):
    route = _seed_route(api_module)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=5)
    r = client.post(f"/api/tick/{route['id']}", json={"suggested_grade": "V5", "init_data": raw})
    assert r.status_code == 200
    assert r.json() == {"ticked": True, "count": 1, "suggested_grade": "V5"}
    r2 = client.post(f"/api/tick/{route['id']}", json={"init_data": raw})
    assert r2.json() == {"ticked": False, "count": 0, "suggested_grade": None}


def test_tick_404_for_missing_route(client, api_module):
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=5)
    r = client.post("/api/tick/999", json={"init_data": raw})
    assert r.status_code == 404


def test_tick_rejects_non_string_suggested_grade_with_422(client, api_module):
    """Regression test: suggested_grade used to be passed through unvalidated
    into storage._clean_free, which calls .strip() and raised an uncaught
    AttributeError (-> 500) for a non-string JSON value like an int."""
    route = _seed_route(api_module)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=5)
    r = client.post(f"/api/tick/{route['id']}",
                     json={"suggested_grade": 5, "init_data": raw})
    assert r.status_code == 422


def test_tick_grade_updates_existing_tick(client, api_module):
    route = _seed_route(api_module)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=5)
    client.post(f"/api/tick/{route['id']}", json={"init_data": raw})
    r = client.put(f"/api/tick/{route['id']}/grade",
                    json={"suggested_grade": "V6", "init_data": raw})
    assert r.status_code == 200
    assert r.json() == {"ticked": True, "suggested_grade": "V6"}


def test_tick_grade_requires_auth(client, api_module):
    route = _seed_route(api_module)
    r = client.put(f"/api/tick/{route['id']}/grade", json={"suggested_grade": "V6"})
    assert r.status_code == 401


def test_tick_grade_404_for_missing_route(client, api_module):
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=5)
    r = client.put("/api/tick/999/grade", json={"suggested_grade": "V6", "init_data": raw})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "USC Routes" in r.text
