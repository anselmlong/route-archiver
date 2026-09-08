"""Storage & domain logic for the route archiver.
SQLite is the single source of truth. Used by both the bot (writes) and the
mini-app API (reads), so keep this module free of any telegram/async I/O.
"""
import re
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "routes.db"

# V-scale ordering for sorting. Higher index = harder. Capped at V8+ per the gym.
V_ORDER = [
    "VB", "V0", "V0+", "V1", "V1+", "V2", "V2+", "V3", "V3+",
    "V4", "V4+", "V5", "V5+", "V6", "V6+", "V7", "V7+", "V8", "V8+",
]
_V_IDX = {g: i for i, g in enumerate(V_ORDER)}
MAX_IDX = _V_IDX["V8+"]  # anything harder than this gets clamped to V8+

# fuller accepted scale for parsing; grades above V8+ are clamped, not rejected
_FULL = V_ORDER + [
    "V9", "V9+", "V10", "V10+", "V11", "V11+", "V12", "V12+",
    "V13", "V13+", "V14", "V14+", "V15", "V15+", "V16", "V16+", "V17",
]
_FULL_IDX = {g: i for i, g in enumerate(_FULL)}

# canonical wall sections (the three the gym uses) + common aliases
WALL_ALIASES = {
    "left": "left vertical", "left wall": "left vertical",
    "left vertical": "left vertical", "vertical": "left vertical",
    "middle": "middle overhang", "middle overhang": "middle overhang",
    "overhang": "middle overhang", "overhang wall": "middle overhang",
    "cave": "middle overhang",
    "right": "right slab", "right wall": "right slab",
    "right slab": "right slab", "slab": "right slab",
}

# token-ish grade regex: VB, V0..V17, optional +, optional / Vx range.
# Grades above V8+ are accepted here then clamped by _clamp_grade().
_GRADE_RE = re.compile(
    r"\bV(?:B|(?:1[0-7])|[0-9])\s*\+?\s*(?:[/-]\s*V(?:B|(?:1[0-7])|[0-9])\s*\+?)?",
    re.IGNORECASE,
)


class GradeError(ValueError):
    """Raised when a grade can't be parsed from a caption."""


def _norm_grade(s: str) -> str:
    """Normalize 'v4+' -> 'V4+', 'vB'->'VB'. Keeps + and range separators."""
    return re.sub(r"\s+", "", s).upper()


def _grade_low(display: str) -> int:
    """Lower-bound sort index for a (possibly ranged) grade display."""
    parts = re.split(r"[/-]", display)
    return _V_IDX[parts[0]]


def _clamp_grade(display):
    """Clamp any grade harder than V8+ down to V8+ (display + sort index)."""
    parts = re.split(r"[/-]", display)
    indices = [_FULL_IDX[p] for p in parts]
    if max(indices) > MAX_IDX:
        return "V8+", MAX_IDX
    return display, _grade_low(display)


def _norm_wall(w):
    if not w:
        return None
    return WALL_ALIASES.get(w.strip().lower(), w.strip().lower())


def parse_caption(caption: str) -> dict:
    """Parse 'Route Name / V4+ / left vertical' style captions.

    Returns dict with keys: name, grade (display), grade_low (int sort index),
    wall (canonical). Raises GradeError if no valid V-grade found.
    """
    if not caption:
        raise GradeError("no caption")

    m = _GRADE_RE.search(caption)
    if not m:
        raise GradeError("no V-grade found")

    raw = m.group(0)
    grade_display = _norm_grade(raw)
    for part in re.split(r"[/-]", grade_display):
        if part not in _FULL_IDX:
            raise GradeError(f"unsupported grade {part}")
    # clamp any grade harder than V8+ down to V8+
    grade_display, grade_low = _clamp_grade(grade_display)

    # name = everything before the grade token; wall = everything after
    before = caption[: m.start()].strip(" /-,;:")
    after = caption[m.end():].strip(" /-,;:")

    if not before:
        raise GradeError("no route name")

    # extract wall: split on structured separators if present, else take the
    # whole trailing text and strip any leading connector word ("on"/"at")
    wall = None
    for sep in ("/", "-", "|", "@"):
        if sep in after:
            wall = after.split(sep)[0].strip() or None
            break
    if wall is None and after:
        wall = re.sub(r"^(?:on|at|the)\s+", "", after, flags=re.IGNORECASE).strip() or None

    return {
        "name": before,
        "grade": grade_display,
        "grade_low": grade_low,
        "wall": _norm_wall(wall),
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_routes_grade ON routes(grade_low)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_routes_wall ON routes(wall)")

    def add_route(self, *, name, grade, grade_low, wall=None,
                  photo_path=None, photo_fid=None,
                  setter_name=None, setter_id=None):
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO routes
                    (name, grade, grade_low, wall, photo_path, photo_fid,
                     setter_name, setter_id)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (name, grade, grade_low, wall, photo_path, photo_fid,
                 setter_name, setter_id),
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
        allowed = {"name", "grade", "grade_low", "wall",
                   "photo_path", "photo_fid", "setter_name", "setter_id"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get_route(route_id)
        if "grade" in updates and "grade_low" not in updates:
            updates["grade"], updates["grade_low"] = _clamp_grade(
                _norm_grade(str(updates["grade"]))
            )
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
                "Overhang Dyno / V8 / Cave",
                "Warmup V2/V3 right",
                "no grade here"]:
        try:
            print(cap, "->", parse_caption(cap))
        except GradeError as e:
            print(cap, "-> GradeError:", e)
