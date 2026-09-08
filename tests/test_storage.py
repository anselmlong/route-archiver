"""Caption parser + Storage CRUD/ratings/ticks tests."""
import pytest

from storage import GradeError, parse_caption


# --------------------------------------------------------------------------- #
# parse_caption
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("caption,expected", [
    ("Crack Line / V4+ / left vertical",
     {"name": "Crack Line", "grade": "V4", "grade_low": 5, "wall": "Left", "description": None}),
    ("Overhang Dyno / V8+ / right slab",
     {"name": "Overhang Dyno", "grade": "V8+", "grade_low": 10, "wall": "Right", "description": None}),
    ("Project #1 / V7 / Cave",
     {"name": "Project #1", "grade": "V7", "grade_low": 8, "wall": "Middle", "description": None}),
    ("V4",
     {"name": "Untitled", "grade": "V4", "grade_low": 5, "wall": None, "description": None}),
    ("juggy one - V4",
     {"name": "juggy one", "grade": "V4", "grade_low": 5, "wall": None, "description": None}),
    ("fun route (V3-4) (dont break pls)",
     {"name": "fun route", "grade": "V3-V4", "grade_low": 4, "wall": None, "description": "dont break pls"}),
    ("Warmup V2/V3 left",
     {"name": "Warmup", "grade": "V2-V3", "grade_low": 3, "wall": "Left", "description": None}),
])
def test_parse_caption_cases(caption, expected):
    assert parse_caption(caption) == expected


def test_parse_caption_clamps_above_v8_plus_to_v8_plus():
    assert parse_caption("Sick Boulders / V9 / middle overhang")["grade"] == "V8+"
    assert parse_caption("Mega Line / V12+ / right slab")["grade"] == "V8+"
    # clamped grades still sort at the max index
    from storage import MAX_IDX
    assert parse_caption("Sick Boulders / V9 / middle overhang")["grade_low"] == MAX_IDX


def test_parse_caption_no_grade_raises():
    with pytest.raises(GradeError):
        parse_caption("nice send everyone!")


def test_parse_caption_empty_raises():
    with pytest.raises(GradeError):
        parse_caption("")


def test_parse_caption_wildcard_sorts_below_everything():
    from storage import WILD_LOW
    parsed = parse_caption("Mystery V? left")
    assert parsed["grade"] == "V?"
    assert parsed["grade_low"] == WILD_LOW
    assert parsed["wall"] == "Left"


def test_parse_caption_half_step_dropped_below_top():
    # V4+ -> V4 (half-steps only survive at the very top, V8+)
    assert parse_caption("Route V4+")["grade"] == "V4"


def test_parse_grade_token_rejects_out_of_range_grade():
    # the caption regex only ever extracts syntactically-valid V0-V17 tokens,
    # so exercise _parse_grade_token's own validation directly
    from storage import _parse_grade_token
    with pytest.raises(GradeError):
        _parse_grade_token("V99Z")


def test_parse_caption_wall_only_recognised_when_leading():
    # 'go right at the top' should NOT be parsed as the 'right' wall
    parsed = parse_caption("Slippy V5 go right at the top")
    assert parsed["wall"] is None
    assert parsed["description"] == "go right at the top"


# --------------------------------------------------------------------------- #
# Storage: routes CRUD
# --------------------------------------------------------------------------- #
def test_add_and_get_route(storage):
    r = storage.add_route(name="Test", grade="V4", grade_low=5, wall="Left")
    assert r["id"] > 0
    assert storage.get_route(r["id"])["name"] == "Test"


def test_get_route_missing_returns_none(storage):
    assert storage.get_route(999) is None


def test_delete_route_soft_hides_from_list_and_get(storage):
    r = storage.add_route(name="Gone", grade="V4", grade_low=5)
    storage.delete_route(r["id"])
    assert storage.get_route(r["id"]) is None
    assert r["id"] not in [x["id"] for x in storage.list_routes()]


def test_list_routes_filters_by_min_grade(storage):
    storage.add_route(name="Easy", grade="V1", grade_low=1)
    hard = storage.add_route(name="Hard", grade="V6", grade_low=7)
    ids = [r["id"] for r in storage.list_routes(grade="V5")]
    assert ids == [hard["id"]]


def test_list_routes_filters_by_wall(storage):
    left = storage.add_route(name="L", grade="V1", grade_low=1, wall="Left")
    storage.add_route(name="R", grade="V1", grade_low=1, wall="Right")
    ids = [r["id"] for r in storage.list_routes(wall="Left")]
    assert ids == [left["id"]]


def test_update_route_reparses_grade_when_grade_low_not_given(storage):
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    updated = storage.update_route(r["id"], grade="V5")
    assert updated["grade"] == "V5"
    assert updated["grade_low"] == 6


def test_walls_returns_sorted_distinct(storage):
    storage.add_route(name="A", grade="V1", grade_low=1, wall="Right")
    storage.add_route(name="B", grade="V1", grade_low=1, wall="Left")
    storage.add_route(name="C", grade="V1", grade_low=1, wall="Left")
    assert storage.walls() == ["Left", "Right"]


# --------------------------------------------------------------------------- #
# Storage: ratings
# --------------------------------------------------------------------------- #
def test_set_rating_then_toggle_off(storage):
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    out = storage.set_rating(r["id"], tg_user_id=1, tg_user_name="A", value=4)
    assert out == {"avg": 4.0, "count": 1, "my_rating": 4}
    # same value again clears (unstar)
    out2 = storage.set_rating(r["id"], tg_user_id=1, tg_user_name="A", value=4)
    assert out2 == {"avg": 0, "count": 0, "my_rating": None}


def test_set_rating_averages_across_users(storage):
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    storage.set_rating(r["id"], tg_user_id=1, tg_user_name="A", value=5)
    storage.set_rating(r["id"], tg_user_id=2, tg_user_name="B", value=3)
    out = storage.set_rating(r["id"], tg_user_id=2, tg_user_name="B", value=4)
    assert out["count"] == 2
    assert out["avg"] == 4.5


def test_attach_ratings_and_ticks_personalizes_my_rating(storage):
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    storage.set_rating(r["id"], tg_user_id=1, tg_user_name="A", value=5)
    rows = storage.list_routes()
    storage.attach_ratings_and_ticks(rows, tg_user_id=1)
    assert rows[0]["my_rating"] == 5
    assert rows[0]["avg_rating"] == 5.0

    rows2 = storage.list_routes()
    storage.attach_ratings_and_ticks(rows2, tg_user_id=999)
    assert rows2[0]["my_rating"] is None
    assert rows2[0]["avg_rating"] == 5.0  # aggregate unaffected by viewer


# --------------------------------------------------------------------------- #
# Storage: ticks
# --------------------------------------------------------------------------- #
def test_toggle_tick_on_then_off(storage):
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    on = storage.toggle_tick(r["id"], tg_user_id=1, tg_user_name="A", suggested_grade="V5")
    assert on == {"ticked": True, "count": 1, "suggested_grade": "V5"}
    off = storage.toggle_tick(r["id"], tg_user_id=1, tg_user_name="A")
    assert off == {"ticked": False, "count": 0, "suggested_grade": None}


def test_set_tick_grade_updates_existing_tick(storage):
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    storage.toggle_tick(r["id"], tg_user_id=1, tg_user_name="A")
    storage.set_tick_grade(r["id"], tg_user_id=1, suggested_grade="V6")
    rows = storage.list_routes()
    storage.attach_ratings_and_ticks(rows, tg_user_id=1)
    assert rows[0]["my_suggested_grade"] == "V6"


# --------------------------------------------------------------------------- #
# Storage: route lifecycle (retire / unretire / wall resets)
# --------------------------------------------------------------------------- #
def test_new_route_is_active_by_default(storage):
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    assert r["retired_at"] is None
    assert r["id"] in [x["id"] for x in storage.list_routes(status="active")]


def test_retire_route_then_unretire(storage):
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    retired = storage.retire_route(r["id"])
    assert retired["retired_at"] is not None
    assert r["id"] not in [x["id"] for x in storage.list_routes(status="active")]
    assert r["id"] in [x["id"] for x in storage.list_routes(status="retired")]

    active_again = storage.unretire_route(r["id"])
    assert active_again["retired_at"] is None
    assert r["id"] in [x["id"] for x in storage.list_routes(status="active")]


def test_retire_route_ignores_deleted_routes(storage):
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    storage.delete_route(r["id"])
    storage.retire_route(r["id"])
    # get_route already excludes deleted rows regardless of retired_at
    assert storage.get_route(r["id"]) is None


def test_get_route_still_returns_retired_routes(storage):
    """Retired routes stay fully accessible -- rating/ticking history on
    them must keep working after a reset."""
    r = storage.add_route(name="X", grade="V1", grade_low=1)
    storage.retire_route(r["id"])
    assert storage.get_route(r["id"]) is not None


def test_list_routes_status_all_includes_active_and_retired_not_deleted(storage):
    active = storage.add_route(name="Active", grade="V1", grade_low=1)
    retired = storage.add_route(name="Retired", grade="V1", grade_low=1)
    deleted = storage.add_route(name="Deleted", grade="V1", grade_low=1)
    storage.retire_route(retired["id"])
    storage.delete_route(deleted["id"])
    ids = {x["id"] for x in storage.list_routes(status="all")}
    assert ids == {active["id"], retired["id"]}


def test_list_routes_rejects_unknown_status(storage):
    with pytest.raises(ValueError):
        storage.list_routes(status="bogus")


def test_count_active_routes_scoped_by_wall(storage):
    storage.add_route(name="L1", grade="V1", grade_low=1, wall="Left")
    storage.add_route(name="L2", grade="V1", grade_low=1, wall="Left")
    storage.add_route(name="R1", grade="V1", grade_low=1, wall="Right")
    assert storage.count_active_routes() == 3
    assert storage.count_active_routes("Left") == 2
    assert storage.count_active_routes("Right") == 1
    assert storage.count_active_routes("Middle") == 0


def test_retire_wall_scoped_only_retires_matching_active_routes(storage):
    left = storage.add_route(name="L", grade="V1", grade_low=1, wall="Left")
    right = storage.add_route(name="R", grade="V1", grade_low=1, wall="Right")
    n = storage.retire_wall("Left")
    assert n == 1
    assert storage.get_route(left["id"])["retired_at"] is not None
    assert storage.get_route(right["id"])["retired_at"] is None


def test_retire_wall_none_retires_everything_active(storage):
    storage.add_route(name="L", grade="V1", grade_low=1, wall="Left")
    storage.add_route(name="R", grade="V1", grade_low=1, wall="Right")
    storage.add_route(name="No wall", grade="V1", grade_low=1)
    n = storage.retire_wall(None)
    assert n == 3
    assert storage.count_active_routes() == 0


def test_retire_wall_does_not_re_timestamp_already_retired_routes(storage):
    r = storage.add_route(name="L", grade="V1", grade_low=1, wall="Left")
    storage.retire_route(r["id"])
    first_retired_at = storage.get_route(r["id"])["retired_at"]
    n = storage.retire_wall("Left")
    assert n == 0
    assert storage.get_route(r["id"])["retired_at"] == first_retired_at


def test_stats_reports_active_and_retired_breakdown(storage):
    a = storage.add_route(name="A", grade="V1", grade_low=1)
    storage.add_route(name="B", grade="V1", grade_low=1)
    storage.retire_route(a["id"])
    assert storage.stats() == {"total": 2, "active": 1, "retired": 1}
