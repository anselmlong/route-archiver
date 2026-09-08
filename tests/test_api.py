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


def test_meta_reports_active_and_retired_counts(client, api_module):
    a = _seed_route(api_module)
    _seed_route(api_module, name="B")
    api_module.storage.retire_route(a["id"])
    body = client.get("/api/meta").json()
    assert body["active_count"] == 1
    assert body["retired_count"] == 1
    assert body["count"] == 2


def test_routes_defaults_to_active_only(client, api_module):
    a = _seed_route(api_module, name="On the wall")
    b = _seed_route(api_module, name="Stripped")
    api_module.storage.retire_route(b["id"])
    names = [r["name"] for r in client.get("/api/routes").json()["routes"]]
    assert names == ["On the wall"]


def test_routes_status_all_includes_retired(client, api_module):
    a = _seed_route(api_module, name="On the wall")
    b = _seed_route(api_module, name="Stripped")
    api_module.storage.retire_route(b["id"])
    names = {r["name"] for r in client.get("/api/routes", params={"status": "all"}).json()["routes"]}
    assert names == {"On the wall", "Stripped"}


def test_routes_status_retired_only(client, api_module):
    a = _seed_route(api_module, name="On the wall")
    b = _seed_route(api_module, name="Stripped")
    api_module.storage.retire_route(b["id"])
    names = [r["name"] for r in client.get("/api/routes", params={"status": "retired"}).json()["routes"]]
    assert names == ["Stripped"]


def test_routes_rejects_invalid_status_with_422(client, api_module):
    _seed_route(api_module)
    r = client.get("/api/routes", params={"status": "bogus"})
    assert r.status_code == 422


def test_retired_route_can_still_be_rated_and_ticked(client, api_module):
    """A route coming down in a reset shouldn't erase anyone's ability to
    log a send they made while it was still up."""
    route = _seed_route(api_module)
    api_module.storage.retire_route(route["id"])
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    r = client.post(f"/api/rate/{route['id']}", json={"value": 5, "init_data": raw})
    assert r.status_code == 200
    t = client.post(f"/api/tick/{route['id']}", json={"init_data": raw})
    assert t.status_code == 200
    assert t.json()["ticked"] is True


def test_routes_hides_internal_fields(client, api_module):
    _seed_route(api_module)
    r = client.get("/api/routes")
    row = r.json()["routes"][0]
    assert "photo_path" not in row
    assert "_id" not in row


def test_routes_includes_grade_consensus(client, api_module):
    route = _seed_route(api_module, grade="V4", grade_low=5)
    api_module.storage.toggle_tick(route["id"], tg_user_id=1, tg_user_name="Bob", suggested_grade="V5")
    api_module.storage.toggle_tick(route["id"], tg_user_id=2, tg_user_name="Cat", suggested_grade="not a grade")
    row = client.get("/api/routes").json()["routes"][0]
    assert row["consensus_grade"] == "V5"
    assert row["consensus_count"] == 1


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
# GET /api/me (personal logbook)
# --------------------------------------------------------------------------- #
def test_me_requires_auth(client, api_module):
    r = client.get("/api/me")
    assert r.status_code == 401


def test_me_rejects_invalid_init_data(client, api_module):
    r = client.get("/api/me", params={"tg": "garbage"})
    assert r.status_code == 401


def test_me_returns_only_my_ticks_shaped_like_routes(client, api_module):
    mine = _seed_route(api_module, name="Mine", grade="V4", grade_low=5)
    other = _seed_route(api_module, name="Not mine", grade="V6", grade_low=7)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    api_module.storage.toggle_tick(mine["id"], tg_user_id=1, tg_user_name="A")
    api_module.storage.toggle_tick(other["id"], tg_user_id=2, tg_user_name="B")

    r = client.get("/api/me", params={"tg": raw})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert [x["name"] for x in body["routes"]] == ["Mine"]
    row = body["routes"][0]
    assert "my_tick" in row and row["my_tick"] == 1
    assert "photo_path" not in row
    assert "consensus_grade" in row and "consensus_count" in row


def test_me_includes_retired_routes_and_grade_pyramid(client, api_module):
    a = _seed_route(api_module, name="A", grade="V4", grade_low=5)
    b = _seed_route(api_module, name="B", grade="V6", grade_low=7)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    api_module.storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="A")
    api_module.storage.toggle_tick(b["id"], tg_user_id=1, tg_user_name="A")
    api_module.storage.retire_route(a["id"])

    body = client.get("/api/me", params={"tg": raw}).json()
    assert body["count"] == 2
    assert body["hardest_grade"] == "V6"
    assert body["grade_pyramid"] == {"V4": 1, "V6": 1}
    names = {x["name"] for x in body["routes"]}
    assert names == {"A", "B"}


def test_me_empty_when_no_ticks(client, api_module):
    _seed_route(api_module)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    body = client.get("/api/me", params={"tg": raw}).json()
    assert body == {"name": "Tester", "routes": [], "count": 0, "grade_pyramid": {}, "hardest_grade": None}


# --------------------------------------------------------------------------- #
# GET /api/leaderboard
# --------------------------------------------------------------------------- #
def test_leaderboard_is_public_no_auth_needed(client, api_module):
    r = client.get("/api/leaderboard")
    assert r.status_code == 200
    assert r.json() == {"climbers": [], "setters": []}


def test_leaderboard_ranks_climbers_by_ticks_and_setters_by_routes(client, api_module):
    a = _seed_route(api_module, name="A", grade="V4", grade_low=5, setter_name="Sam", setter_id=10)
    b = _seed_route(api_module, name="B", grade="V6", grade_low=7, setter_name="Sam", setter_id=10)
    c = _seed_route(api_module, name="C", grade="V2", grade_low=2, setter_name="Ana", setter_id=20)
    s = api_module.storage
    s.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    s.toggle_tick(b["id"], tg_user_id=1, tg_user_name="Bob")
    s.toggle_tick(c["id"], tg_user_id=2, tg_user_name="Cat")

    body = client.get("/api/leaderboard").json()
    assert body["climbers"][0] == {"tg_user_id": 1, "tg_user_name": "Bob", "ticks": 2, "hardest_grade": "V6"}
    assert body["setters"][0] == {"setter_id": 10, "setter_name": "Sam", "routes_set": 2}


def test_leaderboard_limit_is_clamped(client, api_module):
    for i in range(5):
        _seed_route(api_module, name=f"R{i}", setter_name=f"S{i}", setter_id=i)
    r = client.get("/api/leaderboard", params={"limit": 0})
    assert r.status_code == 200
    r2 = client.get("/api/leaderboard", params={"limit": 9999})
    assert r2.status_code == 200
    assert len(r2.json()["setters"]) == 5


# --------------------------------------------------------------------------- #
# GET /api/hot
# --------------------------------------------------------------------------- #
def test_hot_is_public_no_auth_needed(client, api_module):
    r = client.get("/api/hot")
    assert r.status_code == 200
    assert r.json() == {"routes": [], "days": 7}


def test_hot_ranks_by_recent_ticks_and_hides_internal_fields(client, api_module):
    a = _seed_route(api_module, name="A")
    b = _seed_route(api_module, name="B")
    s = api_module.storage
    s.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    s.toggle_tick(a["id"], tg_user_id=2, tg_user_name="Cat")
    s.toggle_tick(b["id"], tg_user_id=1, tg_user_name="Bob")

    body = client.get("/api/hot").json()
    assert [r["name"] for r in body["routes"]] == ["A", "B"]
    assert body["routes"][0]["recent_ticks"] == 2
    assert "photo_path" not in body["routes"][0]


def test_hot_personalizes_when_tg_param_given(client, api_module):
    route = _seed_route(api_module)
    raw = sign_init_data(TEST_BOT_TOKEN, user_id=1)
    api_module.storage.set_rating(route["id"], tg_user_id=1, tg_user_name="A", value=5)
    api_module.storage.toggle_tick(route["id"], tg_user_id=1, tg_user_name="A")

    row = client.get("/api/hot", params={"tg": raw}).json()["routes"][0]
    assert row["my_rating"] == 5
    assert row["my_tick"] == 1


def test_hot_days_and_limit_are_clamped(client, api_module):
    route = _seed_route(api_module)
    api_module.storage.toggle_tick(route["id"], tg_user_id=1, tg_user_name="Bob")
    r = client.get("/api/hot", params={"days": 0, "limit": 0})
    assert r.status_code == 200
    r2 = client.get("/api/hot", params={"days": 9999})
    assert r2.status_code == 200
    assert r2.json()["days"] == 90


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
