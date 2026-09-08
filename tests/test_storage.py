"""Caption parser + Storage CRUD/ratings/ticks tests."""
import pytest

from storage import GradeError, WILD_LOW, parse_caption


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


# --------------------------------------------------------------------------- #
# Storage: personal logbook + leaderboards
# --------------------------------------------------------------------------- #
def test_user_ticked_routes_ordered_newest_tick_first(storage):
    a = storage.add_route(name="A", grade="V1", grade_low=1)
    b = storage.add_route(name="B", grade="V2", grade_low=2)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    storage.toggle_tick(b["id"], tg_user_id=1, tg_user_name="Bob")
    names = [r["name"] for r in storage.user_ticked_routes(1)]
    assert names == ["B", "A"]


def test_user_ticked_routes_only_returns_that_users_ticks(storage):
    a = storage.add_route(name="A", grade="V1", grade_low=1)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    assert storage.user_ticked_routes(2) == []


def test_user_ticked_routes_includes_retired_excludes_deleted(storage):
    a = storage.add_route(name="Retired", grade="V1", grade_low=1)
    b = storage.add_route(name="Deleted", grade="V1", grade_low=1)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    storage.toggle_tick(b["id"], tg_user_id=1, tg_user_name="Bob")
    storage.retire_route(a["id"])
    storage.delete_route(b["id"])
    names = [r["name"] for r in storage.user_ticked_routes(1)]
    assert names == ["Retired"]


def test_user_ticked_routes_untick_removes_it(storage):
    a = storage.add_route(name="A", grade="V1", grade_low=1)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")  # toggles off
    assert storage.user_ticked_routes(1) == []


def test_leaderboard_climbers_ranked_by_tick_count_with_hardest_grade(storage):
    a = storage.add_route(name="A", grade="V2", grade_low=2)
    b = storage.add_route(name="B", grade="V6", grade_low=7)
    c = storage.add_route(name="C", grade="V1", grade_low=1)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    storage.toggle_tick(b["id"], tg_user_id=1, tg_user_name="Bob")
    storage.toggle_tick(c["id"], tg_user_id=2, tg_user_name="Cat")
    board = storage.leaderboard_climbers()
    assert board[0] == {"tg_user_id": 1, "tg_user_name": "Bob", "ticks": 2, "hardest_grade": "V6"}
    assert board[1] == {"tg_user_id": 2, "tg_user_name": "Cat", "ticks": 1, "hardest_grade": "V1"}


def test_leaderboard_climbers_ignores_wildcard_grade_for_hardest(storage):
    wild = storage.add_route(name="Wild", grade="V?", grade_low=WILD_LOW)
    graded = storage.add_route(name="Graded", grade="V3", grade_low=3)
    storage.toggle_tick(wild["id"], tg_user_id=1, tg_user_name="Bob")
    storage.toggle_tick(graded["id"], tg_user_id=1, tg_user_name="Bob")
    board = storage.leaderboard_climbers()
    assert board[0]["hardest_grade"] == "V3"


def test_leaderboard_climbers_excludes_deleted_route_ticks(storage):
    a = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    storage.delete_route(a["id"])
    assert storage.leaderboard_climbers() == []


def test_leaderboard_climbers_respects_limit(storage):
    for i in range(5):
        r = storage.add_route(name=f"R{i}", grade="V1", grade_low=1)
        storage.toggle_tick(r["id"], tg_user_id=i, tg_user_name=f"U{i}")
    assert len(storage.leaderboard_climbers(limit=2)) == 2


def test_leaderboard_setters_ranked_by_routes_set(storage):
    storage.add_route(name="A", grade="V1", grade_low=1, setter_name="Sam", setter_id=10)
    storage.add_route(name="B", grade="V1", grade_low=1, setter_name="Sam", setter_id=10)
    storage.add_route(name="C", grade="V1", grade_low=1, setter_name="Ana", setter_id=20)
    board = storage.leaderboard_setters()
    assert board[0] == {"setter_id": 10, "setter_name": "Sam", "routes_set": 2}
    assert board[1] == {"setter_id": 20, "setter_name": "Ana", "routes_set": 1}


def test_leaderboard_setters_excludes_deleted_routes(storage):
    r = storage.add_route(name="A", grade="V1", grade_low=1, setter_name="Sam", setter_id=10)
    storage.delete_route(r["id"])
    assert storage.leaderboard_setters() == []


def test_leaderboard_setters_includes_retired_routes(storage):
    r = storage.add_route(name="A", grade="V1", grade_low=1, setter_name="Sam", setter_id=10)
    storage.retire_route(r["id"])
    assert storage.leaderboard_setters()[0]["routes_set"] == 1


# --------------------------------------------------------------------------- #
# Storage: crowd-sourced grade consensus
# --------------------------------------------------------------------------- #
def test_grade_low_from_free_text_parses_and_clamps(storage_module):
    f = storage_module._grade_low_from_free_text
    assert f("V5") == storage_module._V_IDX["V5"]
    assert f("feels harder, maybe v6") == storage_module._V_IDX["V6"]
    assert f("V12+") == storage_module.MAX_IDX  # clamps like caption parsing does
    assert f("just a note, no grade") is None
    assert f("") is None
    assert f(None) is None
    assert f("V?") is None  # wildcard doesn't contribute a number


def test_consensus_grade_absent_when_no_suggestions(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.toggle_tick(r["id"], tg_user_id=1, tg_user_name="Bob")  # no suggested_grade
    rows = storage.list_routes()
    storage.attach_ratings_and_ticks(rows)
    assert rows[0]["consensus_grade"] is None
    assert rows[0]["consensus_count"] == 0


def test_consensus_grade_ignores_unparseable_suggestions(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.toggle_tick(r["id"], tg_user_id=1, tg_user_name="Bob", suggested_grade="feels sandbagged")
    storage.toggle_tick(r["id"], tg_user_id=2, tg_user_name="Cat", suggested_grade="V5")
    rows = storage.list_routes()
    storage.attach_ratings_and_ticks(rows)
    assert rows[0]["consensus_grade"] == "V5"
    assert rows[0]["consensus_count"] == 1


def test_consensus_grade_is_median_odd_count(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    for uid, g in [(1, "V3"), (2, "V5"), (3, "V7")]:
        storage.toggle_tick(r["id"], tg_user_id=uid, tg_user_name=f"U{uid}", suggested_grade=g)
    rows = storage.list_routes()
    storage.attach_ratings_and_ticks(rows)
    assert rows[0]["consensus_grade"] == "V5"
    assert rows[0]["consensus_count"] == 3


def test_consensus_grade_updates_when_tick_grade_changed(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.toggle_tick(r["id"], tg_user_id=1, tg_user_name="Bob", suggested_grade="V3")
    storage.set_tick_grade(r["id"], tg_user_id=1, suggested_grade="V7")
    rows = storage.list_routes()
    storage.attach_ratings_and_ticks(rows)
    assert rows[0]["consensus_grade"] == "V7"


def test_consensus_grade_untick_removes_its_contribution(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.toggle_tick(r["id"], tg_user_id=1, tg_user_name="Bob", suggested_grade="V3")
    storage.toggle_tick(r["id"], tg_user_id=1, tg_user_name="Bob")  # toggles off
    rows = storage.list_routes()
    storage.attach_ratings_and_ticks(rows)
    assert rows[0]["consensus_grade"] is None
    assert rows[0]["consensus_count"] == 0


# --------------------------------------------------------------------------- #
# Storage: hot routes / route of the week
# --------------------------------------------------------------------------- #
def _backdate_tick(storage, route_id, tg_user_id, days_ago):
    with storage._connect() as conn:
        conn.execute(
            "UPDATE ticks SET created_at=datetime('now', ?) WHERE route_id=? AND tg_user_id=?",
            (f"-{days_ago} days", route_id, tg_user_id),
        )


def test_hot_routes_ranks_by_recent_tick_count(storage):
    a = storage.add_route(name="A", grade="V4", grade_low=5)
    b = storage.add_route(name="B", grade="V6", grade_low=7)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    storage.toggle_tick(a["id"], tg_user_id=2, tg_user_name="Cat")
    storage.toggle_tick(b["id"], tg_user_id=1, tg_user_name="Bob")
    hot = storage.hot_routes()
    assert [r["name"] for r in hot] == ["A", "B"]
    assert hot[0]["recent_ticks"] == 2
    assert hot[1]["recent_ticks"] == 1


def test_hot_routes_excludes_ticks_outside_window(storage):
    a = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    _backdate_tick(storage, a["id"], 1, days_ago=30)
    assert storage.hot_routes(days=7) == []
    assert len(storage.hot_routes(days=60)) == 1


def test_hot_routes_excludes_deleted_routes(storage):
    a = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    storage.delete_route(a["id"])
    assert storage.hot_routes() == []


def test_hot_routes_includes_retired_routes(storage):
    a = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.toggle_tick(a["id"], tg_user_id=1, tg_user_name="Bob")
    storage.retire_route(a["id"])
    hot = storage.hot_routes()
    assert len(hot) == 1
    assert hot[0]["retired_at"] is not None


def test_hot_routes_respects_limit(storage):
    for i in range(5):
        r = storage.add_route(name=f"R{i}", grade="V1", grade_low=1)
        storage.toggle_tick(r["id"], tg_user_id=i, tg_user_name=f"U{i}")
    assert len(storage.hot_routes(limit=2)) == 2


def test_hot_routes_empty_when_no_ticks(storage):
    storage.add_route(name="A", grade="V4", grade_low=5)
    assert storage.hot_routes() == []


# --------------------------------------------------------------------------- #
# Storage: setter profile
# --------------------------------------------------------------------------- #
def test_setter_profile_none_when_never_set_anything(storage):
    assert storage.setter_profile(999) is None


def test_setter_profile_aggregates_routes_and_active_retired_split(storage):
    a = storage.add_route(name="A", grade="V4", grade_low=5, setter_name="Sam", setter_id=10)
    b = storage.add_route(name="B", grade="V6", grade_low=7, setter_name="Sam", setter_id=10)
    storage.retire_route(b["id"])
    other = storage.add_route(name="C", grade="V1", grade_low=1, setter_name="Ana", setter_id=20)

    profile = storage.setter_profile(10)
    assert profile["setter_name"] == "Sam"
    assert profile["routes_set"] == 2
    assert profile["active"] == 1
    assert profile["retired"] == 1
    assert {r["name"] for r in profile["routes"]} == {"A", "B"}


def test_setter_profile_excludes_deleted_routes(storage):
    a = storage.add_route(name="A", grade="V4", grade_low=5, setter_name="Sam", setter_id=10)
    storage.delete_route(a["id"])
    assert storage.setter_profile(10) is None


def test_find_setter_id_case_insensitive_substring_match(storage):
    storage.add_route(name="A", grade="V4", grade_low=5, setter_name="Sam Smith", setter_id=10)
    assert storage.find_setter_id("sam") == 10
    assert storage.find_setter_id("SMITH") == 10
    assert storage.find_setter_id("nonexistent") is None


def test_find_setter_id_ignores_deleted_routes(storage):
    a = storage.add_route(name="A", grade="V4", grade_low=5, setter_name="Sam", setter_id=10)
    storage.delete_route(a["id"])
    assert storage.find_setter_id("sam") is None


# --------------------------------------------------------------------------- #
# Storage: list_routes search
# --------------------------------------------------------------------------- #
def test_list_routes_search_matches_name_case_insensitively(storage):
    storage.add_route(name="Crack Line", grade="V4", grade_low=5)
    storage.add_route(name="Slab Master", grade="V2", grade_low=2)
    names = [r["name"] for r in storage.list_routes(search="crack")]
    assert names == ["Crack Line"]


def test_list_routes_search_matches_setter_name(storage):
    storage.add_route(name="Crack Line", grade="V4", grade_low=5, setter_name="Sam Smith")
    storage.add_route(name="Slab Master", grade="V2", grade_low=2, setter_name="Ana")
    names = [r["name"] for r in storage.list_routes(search="sam")]
    assert names == ["Crack Line"]


def test_list_routes_search_no_match_returns_empty(storage):
    storage.add_route(name="Crack Line", grade="V4", grade_low=5)
    assert storage.list_routes(search="nonexistent") == []


def test_list_routes_search_combines_with_status_and_wall(storage):
    a = storage.add_route(name="Crack Line", grade="V4", grade_low=5, wall="Left")
    b = storage.add_route(name="Crack Attack", grade="V2", grade_low=2, wall="Right")
    storage.retire_route(b["id"])
    active_matches = storage.list_routes(search="crack", wall="Left")
    assert [r["name"] for r in active_matches] == ["Crack Line"]
    all_matches = storage.list_routes(search="crack", status="all")
    assert {r["name"] for r in all_matches} == {"Crack Line", "Crack Attack"}


# --------------------------------------------------------------------------- #
# Storage: comment threads
# --------------------------------------------------------------------------- #
def test_add_comment_and_list_oldest_first(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="first")
    storage.add_comment(r["id"], tg_user_id=2, tg_user_name="Cat", text="second")
    texts = [c["text"] for c in storage.list_comments(r["id"])]
    assert texts == ["first", "second"]


def test_add_comment_strips_whitespace(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    c = storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="  crimpy  ")
    assert c["text"] == "crimpy"


def test_add_comment_rejects_empty_or_whitespace_only(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    with pytest.raises(ValueError):
        storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="")
    with pytest.raises(ValueError):
        storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="   ")


def test_add_comment_rejects_over_500_chars(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    with pytest.raises(ValueError):
        storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="x" * 501)


def test_delete_own_comment_removes_it(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    c = storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="beta")
    assert storage.delete_own_comment(c["id"], tg_user_id=1) is True
    assert storage.list_comments(r["id"]) == []


def test_delete_comment_by_non_author_fails(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    c = storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="beta")
    assert storage.delete_own_comment(c["id"], tg_user_id=999) is False
    assert len(storage.list_comments(r["id"])) == 1


def test_delete_comment_twice_second_call_fails(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    c = storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="beta")
    assert storage.delete_own_comment(c["id"], tg_user_id=1) is True
    assert storage.delete_own_comment(c["id"], tg_user_id=1) is False


def test_comment_count_attached_via_attach_ratings_and_ticks(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="one")
    storage.add_comment(r["id"], tg_user_id=2, tg_user_name="Cat", text="two")
    rows = storage.list_routes()
    storage.attach_ratings_and_ticks(rows)
    assert rows[0]["comment_count"] == 2


def test_comment_count_excludes_deleted(storage):
    r = storage.add_route(name="A", grade="V4", grade_low=5)
    c = storage.add_comment(r["id"], tg_user_id=1, tg_user_name="Bob", text="one")
    storage.delete_own_comment(c["id"], tg_user_id=1)
    rows = storage.list_routes()
    storage.attach_ratings_and_ticks(rows)
    assert rows[0]["comment_count"] == 0


# --------------------------------------------------------------------------- #
# Storage: new-route alert subscriptions
# --------------------------------------------------------------------------- #
def test_subscribe_then_get_subscription(storage):
    storage.subscribe(1, 111, min_grade_low=5, wall="Left")
    sub = storage.get_subscription(1)
    assert sub["tg_chat_id"] == 111
    assert sub["min_grade_low"] == 5
    assert sub["wall"] == "Left"


def test_subscribe_defaults_to_any_grade_any_wall(storage):
    storage.subscribe(1, 111)
    sub = storage.get_subscription(1)
    assert sub["min_grade_low"] is None
    assert sub["wall"] is None


def test_get_subscription_none_when_not_subscribed(storage):
    assert storage.get_subscription(999) is None


def test_subscribe_again_overwrites_not_duplicates(storage):
    storage.subscribe(1, 111, min_grade_low=5, wall="Left")
    storage.subscribe(1, 222, min_grade_low=7, wall="Right")
    sub = storage.get_subscription(1)
    assert sub["tg_chat_id"] == 222
    assert sub["min_grade_low"] == 7
    assert sub["wall"] == "Right"


def test_unsubscribe_removes_it(storage):
    storage.subscribe(1, 111)
    storage.unsubscribe(1)
    assert storage.get_subscription(1) is None


def test_unsubscribe_when_never_subscribed_is_a_noop(storage):
    storage.unsubscribe(999)  # must not raise


def test_matching_subscribers_grade_and_wall_filters(storage):
    storage.subscribe(1, 111, min_grade_low=5, wall="Left")   # V4+ on Left
    storage.subscribe(2, 222)                                  # anything
    matches = {s["tg_user_id"] for s in storage.matching_subscribers(5, "Left")}
    assert matches == {1, 2}
    too_easy = {s["tg_user_id"] for s in storage.matching_subscribers(2, "Left")}
    assert too_easy == {2}
    wrong_wall = {s["tg_user_id"] for s in storage.matching_subscribers(7, "Right")}
    assert wrong_wall == {2}


def test_matching_subscribers_any_grade_includes_wildcard_routes(storage):
    from storage import WILD_LOW
    storage.subscribe(1, 111)  # any grade, any wall
    matches = {s["tg_user_id"] for s in storage.matching_subscribers(WILD_LOW, None)}
    assert matches == {1}


def test_matching_subscribers_specific_wall_excludes_wallless_route(storage):
    storage.subscribe(1, 111, wall="Left")
    assert storage.matching_subscribers(5, None) == []
