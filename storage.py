"""Storage & domain logic for the route archiver.
SQLite is the single source of truth. Used by both the bot (writes) and the
mini-app API (reads), so keep this module free of any telegram/async I/O.
"""
import re
import sqlite3
import threading
from datetime import datetime, timezone
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


def _grade_low_from_free_text(raw):
    """Best-effort parse of a free-form suggested-grade string (from a
    tick) into a normalized grade_low sort index, or None if it doesn't
    look like a V-grade. Used for the crowd-sourced grade consensus --
    garbage input (typos, "feels harder", etc.) is simply excluded rather
    than rejected, since suggested_grade is never validated at write time.
    """
    if not raw:
        return None
    m = _GRADE_RE.search(raw)
    if not m:
        return None
    token = _norm_grade(m.group(0))
    if token in ("V?", "V"):
        return None  # a wildcard suggestion doesn't contribute a number
    try:
        _, grade_low = _parse_grade_token(token)
    except GradeError:
        return None
    return grade_low


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
            # migrate older DBs that lack the description/retired_at columns
            cols = {c[1] for c in conn.execute("PRAGMA table_info(routes)").fetchall()}
            if "description" not in cols:
                conn.execute("ALTER TABLE routes ADD COLUMN description TEXT")
            if "retired_at" not in cols:
                # NULL = still on the wall; set when a wall reset (or a
                # one-off swap) takes the route down. Distinct from
                # `deleted`, which is for mis-posts -- a retired route keeps
                # its ratings/ticks and stays in climbers' history.
                conn.execute("ALTER TABLE routes ADD COLUMN retired_at TEXT")
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
            tick_cols = {c[1] for c in conn.execute("PRAGMA table_info(ticks)").fetchall()}
            if "suggested_grade_low" not in tick_cols:
                # normalized sort index for suggested_grade, computed at
                # write time so the grade consensus can aggregate without
                # re-parsing free text on every read. NULL when the
                # suggestion doesn't parse as a V-grade.
                conn.execute("ALTER TABLE ticks ADD COLUMN suggested_grade_low INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_route ON ticks(route_id)")
            # beta/tip comment threads: flat, one route -> many comments
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS comments (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_id     INTEGER NOT NULL REFERENCES routes(id),
                    tg_user_id   INTEGER NOT NULL,
                    tg_user_name TEXT,
                    text         TEXT NOT NULL,
                    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    deleted      INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_route ON comments(route_id)")
            # new-route alerts: one subscription per Telegram user, set up
            # via a private DM with the bot (the only chat Telegram lets a
            # bot message into later without the user speaking first)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id    INTEGER NOT NULL UNIQUE,
                    tg_chat_id    INTEGER NOT NULL,
                    min_grade_low INTEGER,
                    wall          TEXT,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            # legacy up/down votes table is gone; drop any stale one from an old deploy
            conn.execute("DROP TABLE IF EXISTS votes")
            # usage analytics: one lightweight row per action (no heavy deps)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind       TEXT NOT NULL,
                    tg_user_id INTEGER,
                    route_id   INTEGER,
                    payload    TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(tg_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_day ON events(created_at)")

    # ---- usage analytics ------------------------------------------------
    def track(self, kind: str, tg_user_id=None, route_id=None, payload=None):
        """Record a lightweight usage event (view/rate/tick/command/new-route)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events (kind, tg_user_id, route_id, payload) VALUES (?,?,?,?)",
                (kind, tg_user_id, route_id, payload),
            )

    # bump counters inside high-frequency paths *without* waiting on the write
    def track_route_view(self, tg_user_id=None, route_id=None):
        self.track("view", tg_user_id, route_id)

    def analytics(self, days: int = 14):
        """Aggregate usage analytics for the last `days` days.

        Returns unique users, event counts, top routes, ratings/ticks traffic,
        and subscription count — everything admin needs without heavy deps.
        Also prunes events older than 90 days so the table stays small.
        """
        with self._lock, self._connect() as conn:
            # prune old events (low-volume club traffic; 90 days is plenty)
            conn.execute(
                "DELETE FROM events WHERE created_at < datetime('now','-90 days')"
            )
            u = conn.execute(
                "SELECT COUNT(DISTINCT tg_user_id) c FROM events "
                "WHERE created_at >= datetime('now', ?)",
                (f"-{days} days",),
            ).fetchone()["c"]

            total_users = conn.execute(
                "SELECT COUNT(DISTINCT tg_user_id) c FROM events"
            ).fetchone()["c"]

            by_kind = {
                r["kind"]: r["c"]
                for r in conn.execute(
                    "SELECT kind, COUNT(*) c FROM events "
                    "WHERE created_at >= datetime('now', ?) GROUP BY kind",
                    (f"-{days} days",),
                ).fetchall()
            }

            top_routes = [
                dict(r)
                for r in conn.execute(
                    "SELECT route_id, COUNT(*) c FROM events "
                    "WHERE kind='view' AND route_id IS NOT NULL "
                    "AND created_at >= datetime('now', ?) "
                    "GROUP BY route_id ORDER BY c DESC LIMIT 8",
                    (f"-{days} days",),
                ).fetchall()
            ]
            for t in top_routes:
                r = self.get_route(t["route_id"])
                t["name"] = r["name"] if r else f"route#{t['route_id']}"

            day_activity = [
                dict(r)
                for r in conn.execute(
                    "SELECT substr(created_at,1,10) day, COUNT(*) c FROM events "
                    "WHERE created_at >= datetime('now', ?) "
                    "GROUP BY day ORDER BY day",
                    (f"-{days} days",),
                ).fetchall()
            ]

        sub_count = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions"
        ).fetchone()["c"]

        return {
            "days": days,
            "active_users": u,
            "total_known_users": total_users,
            "events": by_kind,
            "top_routes": top_routes,
            "activity_by_day": day_activity,
            "subscriptions": sub_count,
        }

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

    # ---- wall resets / route lifecycle ----------------------------------
    def retire_route(self, route_id):
        """Mark a single route retired (removed from the wall), keeping its
        ratings/ticks. Reversible via unretire_route."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE routes SET retired_at=datetime('now'), updated_at=datetime('now') "
                "WHERE id=? AND deleted=0",
                (route_id,),
            )
        return self.get_route(route_id)

    def unretire_route(self, route_id):
        """Restore a retired route to active (e.g. a mistaken /reset)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE routes SET retired_at=NULL, updated_at=datetime('now') WHERE id=? AND deleted=0",
                (route_id,),
            )
        return self.get_route(route_id)

    def count_active_routes(self, wall=None):
        """How many currently-active routes a /reset would affect."""
        sql = "SELECT COUNT(*) c FROM routes WHERE deleted=0 AND retired_at IS NULL"
        params = []
        if wall:
            sql += " AND wall=?"
            params.append(wall)
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchone()["c"]

    def retire_wall(self, wall=None):
        """Bulk-retire every currently-active route, optionally scoped to
        one wall (wall=None retires the whole gym). Mirrors a physical wall
        reset, where every route on a stripped section comes down at once.
        Returns the number of routes retired.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        sql = ("UPDATE routes SET retired_at=?, updated_at=? "
               "WHERE deleted=0 AND retired_at IS NULL")
        params = [now, now]
        if wall:
            sql += " AND wall=?"
            params.append(wall)
        with self._lock, self._connect() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def list_routes(self, grade=None, wall=None, status="active", search=None):
        """Return routes, newest-graded first, optionally filtered.

        status: "active" (default -- currently on the wall), "retired"
        (removed in a reset but kept for history), or "all" (both, still
        excluding admin-deleted rows).
        search: case-insensitive substring match against route name OR
        setter name.
        """
        sql = "SELECT * FROM routes WHERE deleted=0"
        params = []
        if status == "active":
            sql += " AND retired_at IS NULL"
        elif status == "retired":
            sql += " AND retired_at IS NOT NULL"
        elif status != "all":
            raise ValueError(f"unknown status {status!r}")
        if grade:
            low = _V_IDX.get(_norm_grade(grade))
            if low is not None:
                sql += " AND grade_low >= ?"
                params.append(low)
        if wall:
            sql += " AND wall = ?"
            params.append(wall)
        if search:
            sql += " AND (name LIKE ? OR setter_name LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
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
                cleaned = _clean_free(suggested_grade) if suggested_grade else None
                conn.execute(
                    "INSERT INTO ticks (route_id, tg_user_id, tg_user_name, "
                    "suggested_grade, suggested_grade_low) VALUES (?,?,?,?,?)",
                    (route_id, tg_user_id, tg_user_name, cleaned,
                     _grade_low_from_free_text(cleaned)),
                )
                ticked = True
        count = self._tick_count(route_id)
        return {"ticked": ticked, "count": count,
                "suggested_grade": None if not ticked else (suggested_grade or "").strip()}

    def set_tick_grade(self, route_id, tg_user_id, suggested_grade):
        """Update the suggested grade on an existing tick."""
        cleaned = _clean_free(suggested_grade) if suggested_grade else None
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE ticks SET suggested_grade=?, suggested_grade_low=?, "
                "updated_at=datetime('now') WHERE route_id=? AND tg_user_id=?",
                (cleaned, _grade_low_from_free_text(cleaned), route_id, tg_user_id),
            )

    def _tick_count(self, route_id):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM ticks WHERE route_id=?", (route_id,)
            ).fetchone()
        return row["c"]

    # ---- beta/tip comment threads -----------------------------------------
    def add_comment(self, route_id, tg_user_id, tg_user_name, text):
        """Post a comment on a route. Raises ValueError for empty or
        over-length text (comments are meant to be a quick beta note, not
        an essay)."""
        text = (text or "").strip()
        if not text:
            raise ValueError("empty comment")
        if len(text) > 500:
            raise ValueError("comment too long (max 500 chars)")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO comments (route_id, tg_user_id, tg_user_name, text) "
                "VALUES (?,?,?,?)",
                (route_id, tg_user_id, tg_user_name, text),
            )
            row = conn.execute(
                "SELECT * FROM comments WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def list_comments(self, route_id):
        """A route's comments, oldest first (chronological conversation),
        excluding soft-deleted ones."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM comments WHERE route_id=? AND deleted=0 "
                "ORDER BY created_at ASC, id ASC",
                (route_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_own_comment(self, comment_id, tg_user_id):
        """Soft-delete a comment, but only if tg_user_id is its author.
        Returns True if a comment was actually deleted. Moderating
        someone else's comment is an admin/bot-side concern, not exposed
        here."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE comments SET deleted=1 WHERE id=? AND tg_user_id=? AND deleted=0",
                (comment_id, tg_user_id),
            )
            return cur.rowcount > 0

    def _comment_count(self, route_id):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM comments WHERE route_id=? AND deleted=0", (route_id,)
            ).fetchone()
        return row["c"]

    # ---- personal logbook + leaderboards ---------------------------------
    def user_ticked_routes(self, tg_user_id):
        """A climber's own send history, newest tick first. Includes
        retired routes -- a route coming down in a reset shouldn't erase
        the fact that you climbed it -- but never deleted (mis-posted)
        ones."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT r.* FROM ticks t JOIN routes r ON r.id = t.route_id "
                "WHERE t.tg_user_id = ? AND r.deleted = 0 "
                "ORDER BY t.created_at DESC, t.id DESC",
                (tg_user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def leaderboard_climbers(self, limit=10):
        """Top climbers by tick count, with each climber's hardest send.
        Aggregated in Python over a single fetch rather than fancier SQL --
        a gym's whole tick history is a few hundred rows at most, and this
        stays easy to follow. Wildcard-graded sends (grade_low -1) never
        count as "hardest"."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT t.tg_user_id, t.tg_user_name, t.created_at, "
                "r.grade, r.grade_low FROM ticks t "
                "JOIN routes r ON r.id = t.route_id WHERE r.deleted = 0"
            ).fetchall()
        by_user = {}
        for row in rows:
            uid = row["tg_user_id"]
            e = by_user.setdefault(uid, {
                "tg_user_id": uid, "tg_user_name": row["tg_user_name"],
                "ticks": 0, "hardest_grade": None, "_hardest_low": WILD_LOW,
                "_latest": row["created_at"],
            })
            e["ticks"] += 1
            if row["created_at"] >= e["_latest"]:
                e["tg_user_name"] = row["tg_user_name"]
                e["_latest"] = row["created_at"]
            if row["grade_low"] > e["_hardest_low"]:
                e["_hardest_low"] = row["grade_low"]
                e["hardest_grade"] = row["grade"]
        ranked = sorted(by_user.values(), key=lambda e: e["ticks"], reverse=True)[:limit]
        for e in ranked:
            del e["_hardest_low"], e["_latest"]
        return ranked

    def leaderboard_setters(self, limit=10):
        """Top setters by number of routes archived (active + retired,
        never deleted mis-posts)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT setter_id, MAX(setter_name) setter_name, COUNT(*) c FROM routes "
                "WHERE deleted = 0 AND setter_id IS NOT NULL "
                "GROUP BY setter_id ORDER BY c DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"setter_id": r["setter_id"], "setter_name": r["setter_name"],
                  "routes_set": r["c"]} for r in rows]

    def hot_routes(self, days=7, limit=10):
        """Routes with the most ticks recorded in the last `days` days --
        "what's hot right now" / route-of-the-week is just rank #1 of this
        list. Ranked by recent tick count, ties broken by the most recent
        tick. Retired routes can still show up here (a route can get hot
        right before a reset); deleted ones never do. Each row is a full
        route dict (like list_routes) plus `recent_ticks`."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT r.*, COUNT(*) recent_ticks, MAX(t.created_at) latest_tick "
                "FROM ticks t JOIN routes r ON r.id = t.route_id "
                "WHERE r.deleted = 0 AND t.created_at >= datetime('now', ?) "
                "GROUP BY r.id "
                "ORDER BY recent_ticks DESC, latest_tick DESC "
                "LIMIT ?",
                (f"-{int(days)} days", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def setter_profile(self, setter_id):
        """A setter's own routes (active + retired, not deleted), newest-
        graded first. None if this setter_id has never set anything.
        Rating/tick aggregates aren't attached here -- callers that want
        them (the API layer) run attach_ratings_and_ticks on the result,
        same as any other route list."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM routes WHERE setter_id=? AND deleted=0 "
                "ORDER BY grade_low DESC, created_at DESC",
                (setter_id,),
            ).fetchall()
        routes = [dict(r) for r in rows]
        if not routes:
            return None
        name = next((r["setter_name"] for r in routes if r["setter_name"]), None)
        active = sum(1 for r in routes if not r["retired_at"])
        return {
            "setter_id": setter_id,
            "setter_name": name,
            "routes_set": len(routes),
            "active": active,
            "retired": len(routes) - active,
            "routes": routes,
        }

    def find_setter_id(self, name):
        """Case-insensitive substring match against setter_name (active +
        retired routes only). Returns the first matching setter_id, or
        None. Used by the bot's /setter <name> lookup -- the mini-app
        links to a setter profile directly by id instead."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT setter_id FROM routes WHERE deleted=0 AND setter_id IS NOT NULL "
                "AND setter_name LIKE ? LIMIT 1",
                (f"%{name}%",),
            ).fetchone()
        return row["setter_id"] if row else None

    # ---- new-route alerts ---------------------------------------------
    def subscribe(self, tg_user_id, tg_chat_id, min_grade_low=None, wall=None):
        """Set (or replace) a user's new-route alert preferences. One
        subscription per user -- calling again overwrites the old one."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subscriptions (tg_user_id, tg_chat_id, min_grade_low, wall)
                VALUES (?,?,?,?)
                ON CONFLICT(tg_user_id) DO UPDATE SET
                    tg_chat_id=excluded.tg_chat_id,
                    min_grade_low=excluded.min_grade_low,
                    wall=excluded.wall,
                    updated_at=datetime('now')
                """,
                (tg_user_id, tg_chat_id, min_grade_low, wall),
            )

    def unsubscribe(self, tg_user_id):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM subscriptions WHERE tg_user_id=?", (tg_user_id,))

    def get_subscription(self, tg_user_id):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE tg_user_id=?", (tg_user_id,)
            ).fetchone()
        return dict(row) if row else None

    def matching_subscribers(self, grade_low, wall):
        """Subscriptions whose preferences match a newly-archived route.
        A subscription with min_grade_low=NULL matches any grade
        (wildcard routes, grade_low=-1, included); wall=NULL matches any
        wall."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE "
                "(min_grade_low IS NULL OR ? >= min_grade_low) "
                "AND (wall IS NULL OR wall = ?)",
                (grade_low, wall),
            ).fetchall()
        return [dict(r) for r in rows]

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
            comment_rows = conn.execute(
                f"SELECT route_id, COUNT(*) c FROM comments "
                f"WHERE route_id IN ({marks}) AND deleted=0 GROUP BY route_id",
                ids,
            ).fetchall()
            consensus_rows = conn.execute(
                f"SELECT route_id, suggested_grade_low FROM ticks "
                f"WHERE route_id IN ({marks}) AND suggested_grade_low IS NOT NULL "
                f"ORDER BY route_id, suggested_grade_low",
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
        agg_c = {r["route_id"]: r for r in comment_rows}
        consensus_values = {}
        for row in consensus_rows:
            consensus_values.setdefault(row["route_id"], []).append(row["suggested_grade_low"])
        for rid, r in by_id.items():
            ar = agg_r.get(rid)
            at = agg_t.get(rid)
            ac = agg_c.get(rid)
            r["avg_rating"] = round((ar["av"] or 0) * 2) / 2 if ar and ar["c"] else None
            r["rating_count"] = ar["c"] if ar else 0
            r["my_rating"] = mine_r.get(rid)
            r["tick_count"] = at["c"] if at else 0
            r["my_tick"] = 1 if rid in mine_t else 0
            r["my_suggested_grade"] = mine_tg.get(rid)
            r["comment_count"] = ac["c"] if ac else 0
            r["consensus_grade"], r["consensus_count"] = self._consensus_from_values(
                consensus_values.get(rid, [])
            )
        return routes

    @staticmethod
    def _consensus_from_values(values):
        """Crowd-sourced grade consensus from a sorted list of valid
        suggested_grade_low values: the median, mapped back to its V-grade
        display. (values, sorted) -> (display_or_None, count)."""
        if not values:
            return None, 0
        n = len(values)
        mid = n // 2
        median_low = values[mid] if n % 2 else round((values[mid - 1] + values[mid]) / 2)
        display = V_ORDER[median_low] if 0 <= median_low < len(V_ORDER) else None
        return display, n

    def stats(self):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) total, "
                "SUM(CASE WHEN retired_at IS NULL THEN 1 ELSE 0 END) active "
                "FROM routes WHERE deleted=0"
            ).fetchone()
        total = row["total"] or 0
        active = row["active"] or 0
        return {"total": total, "active": active, "retired": total - active}


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
