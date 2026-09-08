"""Storage & domain logic for the route archiver.
SQLite is the single source of truth. Used by both the bot (writes) and the
mini-app API (reads), so keep this module free of any telegram/async I/O.
"""
import re
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "routes.db"

# V-scale ordering for sorting. Higher index = harder. No half-steps below the
# top (V4+ -> V4), since nobody at NUS realistically climbs past V8 — so the
# cap is V8+ (the single remaining half-step). Wildcard routes (V? / V) are NOT
# in this scale — they get grade_low -1 below everything.
V_ORDER = [
    "VB", "V0", "V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V8+",
]
_V_IDX = {g: i for i, g in enumerate(V_ORDER)}
MAX_IDX = _V_IDX["V8+"]  # anything harder than this gets clamped to V8+
WILD_LOW = -1  # sort index for V? / V wildcard routes

# fuller accepted scale for parsing; grades harder than V8+ clamp down to V8+
_FULL = [
    "VB", "V0", "V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V8+",
    "V9", "V10", "V11", "V12", "V13", "V14", "V15", "V16", "V17",
]
_FULL_IDX = {g: i for i, g in enumerate(_FULL)}

# canonical wall sections (Left / Middle / Right) + common aliases
WALL_ALIASES = {
    "left": "Left", "left wall": "Left", "left vertical": "Left",
    "vertical": "Left", "vertical (left)": "Left", "left (vertical)": "Left",
    "middle": "Middle", "middle overhang": "Middle", "overhang": "Middle",
    "overhang wall": "Middle", "cave": "Middle",
    "overhang (middle)": "Middle", "middle (overhang)": "Middle",
    "right": "Right", "right wall": "Right", "right slab": "Right",
    "slab": "Right", "slab (right)": "Right", "right (slab)": "Right",
}

# token-ish grade regex: a real grade (VB/V0..V17, optional +, optional range)
# OR a wildcard route with no grade (just 'V?' or a standalone 'V').
# Ranges may lack the V prefix (V3-4) and use / or en-dash. Grades above V8+
# are accepted here then clamped by _clamp_grade().
_GRADE_RE = re.compile(
    r"\bV(?:(?:B|1[0-7]|[0-9])\+?(?:\s*[-/–]\s*V?(?:B|1[0-7]|[0-9])\+?)?|\?|(?![0-9A-Za-z]))",
    re.IGNORECASE,
)

# fallback name when a caption is just a grade (e.g. "V4") with no route name
DEFAULT_NAME = "Untitled"


class GradeError(ValueError):
    """Raised when a grade can't be parsed from a caption."""


def _norm_grade(s: str) -> str:
    """Normalize 'v4+' -> 'V4+', 'vB'->'VB'. Removes spaces, keeps +/range."""
    return re.sub(r"\s+", "", s).upper()


def _norm_one(p: str) -> str:
    """Normalize a single grade part: prefix V and strip half-steps.
    V8+ is the intended max (nobody at NUS realistically climbs past it), so it
    is the ONE half-step that survives: 'V4+'->'V4', 'V7+'->'V7', 'V8+'->'V8+'.
    """
    p = p.strip()
    if p and not p.startswith("V"):
        p = "V" + p
    if p == "V8+":
        return p
    return p.rstrip("+")


def _normalize_grade_token(tok: str) -> str:
    """Turn a raw grade token into a canonical form with V on every side.

    Half-steps are stripped below the top (V4+ -> V4); V8+ is kept. Range sides
    get the same treatment: 'V3-4', 'V3/V4', 'v4+' -> 'V3-V4'/'V3-V4'/'V4'.
    """
    parts = re.split(r"[-/–]", tok)
    out = [_norm_one(p) for p in parts]
    return "-".join(out)


def _grade_low(display: str) -> int:
    """Lower-bound sort index for a (possibly ranged) grade display."""
    parts = re.split(r"[-/]", display)
    return _V_IDX[parts[0]]


def _clamp_grade(display):
    """Clamp any grade harder than V8+ down to V8+ (display + sort index)."""
    parts = re.split(r"[-/]", display)
    indices = [_FULL_IDX[p] for p in parts]
    if max(indices) > MAX_IDX:
        return "V8+", MAX_IDX
    return display, _grade_low(display)


def _parse_grade_token(tok: str):
    """Validate + normalize a grade token; returns (display, grade_low)."""
    display = _normalize_grade_token(_norm_grade(tok))
    for part in re.split(r"[-/]", display):
        if part not in _FULL_IDX:
            raise GradeError(f"unsupported grade {part}")
    return _clamp_grade(display)


def _norm_wall(w):
    if not w:
        return None
    return WALL_ALIASES.get(w.strip().lower(), w.strip().lower())


def _clean_name(s: str) -> str:
    """Strip trailing separators/brackets from the text before the grade."""
    s = s.strip().rstrip(" /-,;:()[]{}").strip()
    return s or DEFAULT_NAME


def _clean_free(s: str):
    """Clean leftover text into a description (or None if empty)."""
    if not s:
        return None
    s = s.strip(" \t\r\n()[]{}.,;:/\\|-@\"'")
    return s or None


def _split_wall(after: str):
    """Split the tail text into (canonical_wall_or_None, description_or_None).

    A wall is only recognised when it leads the tail (after stripping
    separators / brackets), so notes like 'go right at the top' or
    '(dont break pls)' become a description instead of a wall. The leftover
    text after the wall phrase is returned as the description.
    """
    if not after:
        return None, None
    s = after.strip(" \t\r\n()[]{}.,;:/\\|-@\"'")
    if not s:
        return None, None
    low = s.lower()
    if low in WALL_ALIASES:
        return WALL_ALIASES[low], None
    # longest phrases first so 'left wall' wins over 'left'
    for phrase, canon in sorted(WALL_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if low.startswith(phrase):
            return canon, _clean_free(s[len(phrase):])
    return None, _clean_free(s)


def parse_caption(caption: str) -> dict:
    """Parse a free-form route caption.

    Handles 'Route / V4+ / left vertical', 'juggy one - V4', bare 'V4',
    wildcards ('Mystery V?', 'Wildcard V'), and 'fun route (V3-4)'. Returns
    dict with keys: name, grade, grade_low, wall (canonical or None),
    description (str or None). Raises GradeError if no V-grade found.
    """
    if not caption:
        raise GradeError("no caption")

    m = _GRADE_RE.search(caption)
    if not m:
        raise GradeError("no V-grade found")

    token = _norm_grade(m.group(0))
    if token in ("V?", "V"):
        # wildcard route — no grade, sorts below everything
        grade_display, grade_low = token, WILD_LOW
    else:
        grade_display, grade_low = _parse_grade_token(token)

    # name = the leading label before the grade; wall + description from the tail
    name = _clean_name(caption[: m.start()])
    wall, description = _split_wall(caption[m.end():])

    return {
        "name": name,
        "grade": grade_display,
        "grade_low": grade_low,
        "wall": wall,
        "description": description,
    }


class Storage:
    def __init__(self, db_path=DB_PATH):
        self._lock = threading.RLock()
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self):
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS routes (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    name          TEXT NOT NULL,
                    grade         TEXT NOT NULL,
                    grade_low     INTEGER NOT NULL,
                    wall          TEXT,
                    description   TEXT,
                    photo_path    TEXT,
                    photo_fid     TEXT,
                    setter_name   TEXT,
                    setter_id     INTEGER,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
                    deleted       INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # migrate older DBs that lack the description column
            cols = {c[1] for c in conn.execute("PRAGMA table_info(routes)").fetchall()}
            if "description" not in cols:
                conn.execute("ALTER TABLE routes ADD COLUMN description TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_routes_grade ON routes(grade_low)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_routes_wall ON routes(wall)")
            # star ratings: one 1-5 rating per user per route
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ratings (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_id     INTEGER NOT NULL REFERENCES routes(id),
                    tg_user_id   INTEGER NOT NULL,
                    tg_user_name TEXT,
                    value        INTEGER NOT NULL CHECK (value BETWEEN 1 AND 5),
                    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (route_id, tg_user_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ratings_route ON ratings(route_id)"
            )
            # ascent ticks: one self-reported send per user per route
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ticks (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_id        INTEGER NOT NULL REFERENCES routes(id),
                    tg_user_id      INTEGER NOT NULL,
                    tg_user_name    TEXT,
                    suggested_grade TEXT,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (route_id, tg_user_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_route ON ticks(route_id)")
            # legacy up/down votes table is gone; drop any stale one from an old deploy
            conn.execute("DROP TABLE IF EXISTS votes")

    def add_route(self, *, name, grade, grade_low, wall=None, description=None,
                  photo_path=None, photo_fid=None,
                  setter_name=None, setter_id=None):
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO routes
                    (name, grade, grade_low, wall, description, photo_path,
                     photo_fid, setter_name, setter_id)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (name, grade, grade_low, wall, description, photo_path,
                 photo_fid, setter_name, setter_id),
            )
            row_id = cur.lastrowid
        return self.get_route(row_id)

    def get_route(self, route_id):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routes WHERE id=? AND deleted=0", (route_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_route(self, route_id, **fields):
        allowed = {"name", "grade", "grade_low", "wall", "description",
                   "photo_path", "photo_fid", "setter_name", "setter_id"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get_route(route_id)
        if "grade" in updates and "grade_low" not in updates:
            g = _norm_grade(str(updates["grade"]))
            if g in ("V?", "V"):
                updates["grade"], updates["grade_low"] = g, WILD_LOW
            else:
                updates["grade"], updates["grade_low"] = _parse_grade_token(g)
        sets = ", ".join(f"{k}=?" for k in updates)
        params = list(updates.values()) + [route_id]
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE routes SET {sets}, updated_at=datetime('now') WHERE id=?",
                params,
            )
        return self.get_route(route_id)

    def delete_route(self, route_id, hard=False):
        with self._lock, self._connect() as conn:
            if hard:
                conn.execute("DELETE FROM routes WHERE id=?", (route_id,))
            else:
                conn.execute(
                    "UPDATE routes SET deleted=1, updated_at=datetime('now') WHERE id=?",
                    (route_id,),
                )

    def list_routes(self, grade=None, wall=None):
        """Return active routes, newest first, optionally filtered."""
        sql = "SELECT * FROM routes WHERE deleted=0"
        params = []
        if grade:
            low = _V_IDX.get(_norm_grade(grade))
            if low is not None:
                sql += " AND grade_low >= ?"
                params.append(low)
        if wall:
            sql += " AND wall = ?"
            params.append(wall)
        sql += " ORDER BY grade_low DESC, created_at DESC, id DESC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def walls(self):
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT wall FROM routes WHERE deleted=0 AND wall IS NOT NULL",
            ).fetchall()
        return sorted(r["wall"] for r in rows)

        # ---- star ratings (1-5) -----------------------------------------
    def set_rating(self, route_id, tg_user_id, tg_user_name, value):
        """Upsert a user's 1-5 star rating on a route.

        Tapping your own rating again clears it (unstar). Returns
        {avg, count, my_rating} where my_rating is the stored value or None.
        """
        value = max(1, min(5, int(value)))
        my = None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM ratings WHERE route_id=? AND tg_user_id=?",
                (route_id, tg_user_id),
            ).fetchone()
            if row and row["value"] == value:
                conn.execute(
                    "DELETE FROM ratings WHERE route_id=? AND tg_user_id=?",
                    (route_id, tg_user_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO ratings (route_id, tg_user_id, tg_user_name, value)
                    VALUES (?,?,?,?)
                    ON CONFLICT(route_id, tg_user_id)
                    DO UPDATE SET value=excluded.value,
                                  tg_user_name=excluded.tg_user_name,
                                  updated_at=datetime('now')
                    """,
                    (route_id, tg_user_id, tg_user_name, value),
                )
                my = value
        out = self._rating_summary(route_id)
        out["my_rating"] = my
        return out

    def _rating_summary(self, route_id):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c, AVG(value) av FROM ratings WHERE route_id=?",
                (route_id,),
            ).fetchone()
        c = row["c"] or 0
        return {"avg": round((row["av"] or 0) * 2) / 2, "count": c}

    # ---- ascent ticks (self-reported sends) -----------------------------
    def toggle_tick(self, route_id, tg_user_id, tg_user_name, suggested_grade=None):
        """Toggle a user's ascent tick on/off. Returns
        {ticked, count, suggested_grade}."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM ticks WHERE route_id=? AND tg_user_id=?",
                (route_id, tg_user_id),
            ).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM ticks WHERE route_id=? AND tg_user_id=?",
                    (route_id, tg_user_id),
                )
                ticked = False
            else:
                conn.execute(
                    "INSERT INTO ticks (route_id, tg_user_id, tg_user_name, suggested_grade) VALUES (?,?,?,?)",
                    (route_id, tg_user_id, tg_user_name,
                     _clean_free(suggested_grade) if suggested_grade else None),
                )
                ticked = True
        count = self._tick_count(route_id)
        return {"ticked": ticked, "count": count,
                "suggested_grade": None if not ticked else (suggested_grade or "").strip()}

    def set_tick_grade(self, route_id, tg_user_id, suggested_grade):
        """Update the suggested grade on an existing tick."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE ticks SET suggested_grade=?, updated_at=datetime('now') "
                "WHERE route_id=? AND tg_user_id=?",
                (_clean_free(suggested_grade) if suggested_grade else None,
                 route_id, tg_user_id),
            )

    def _tick_count(self, route_id):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM ticks WHERE route_id=?", (route_id,)
            ).fetchone()
        return row["c"]

    def attach_ratings_and_ticks(self, routes, tg_user_id=None):
        """Mutate route dicts in place: avg/count/my_rating, tick_count/my_tick."""
        if not routes:
            return routes
        ids = [r["id"] for r in routes]
        marks = ",".join("?" * len(ids))
        with self._lock, self._connect() as conn:
            rating_rows = conn.execute(
                f"SELECT route_id, AVG(value) av, COUNT(*) c FROM ratings "
                f"WHERE route_id IN ({marks}) GROUP BY route_id",
                ids,
            ).fetchall()
            tick_rows = conn.execute(
                f"SELECT route_id, COUNT(*) c FROM ticks "
                f"WHERE route_id IN ({marks}) GROUP BY route_id",
                ids,
            ).fetchall()
            mine_r = {}
            mine_t = {}
            mine_tg = {}
            if tg_user_id:
                for row in conn.execute(
                    f"SELECT route_id, value FROM ratings "
                    f"WHERE tg_user_id=? AND route_id IN ({marks})",
                    [tg_user_id] + ids,
                ).fetchall():
                    mine_r[row["route_id"]] = row["value"]
                for row in conn.execute(
                    f"SELECT route_id, suggested_grade FROM ticks "
                    f"WHERE tg_user_id=? AND route_id IN ({marks})",
                    [tg_user_id] + ids,
                ).fetchall():
                    mine_t[row["route_id"]] = 1
                    mine_tg[row["route_id"]] = row["suggested_grade"]
        by_id = {r["id"]: r for r in routes}
        agg_r = {r["route_id"]: r for r in rating_rows}
        agg_t = {r["route_id"]: r for r in tick_rows}
        for rid, r in by_id.items():
            ar = agg_r.get(rid)
            at = agg_t.get(rid)
            r["avg_rating"] = round((ar["av"] or 0) * 2) / 2 if ar and ar["c"] else None
            r["rating_count"] = ar["c"] if ar else 0
            r["my_rating"] = mine_r.get(rid)
            r["tick_count"] = at["c"] if at else 0
            r["my_tick"] = 1 if rid in mine_t else 0
            r["my_suggested_grade"] = mine_tg.get(rid)
        return routes

    def stats(self):
        with self._lock, self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) c FROM routes WHERE deleted=0"
            ).fetchone()["c"]
        return {"total": total}


if __name__ == "__main__":
    # quick smoke test
    s = Storage()
    for cap in ["Crack Line / V4+ / Left Wall",
                "Balancy Slab V6",
                "Overhang Dyno / V8 / Cave / dynamic, big lockoff",
                "Warmup V2/V3 right",
                "no grade here",
                "Juggy V4 (dont break pls) big holds",
                "Sick crimp line V5 left - matchy topout"]:
        try:
            print(cap, "->", parse_caption(cap))
        except GradeError as e:
            print(cap, "-> GradeError:", e)
