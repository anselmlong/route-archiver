"""Mini-app backend: serves the route collection (JSON + photos) to the web UI.

Reads the same SQLite the bot writes to. Static frontend in ./static. Run via:
    venv/bin/uvicorn api:app --host 127.0.0.1 --port 8160
"""
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from storage import V_ORDER, Storage

BASE_DIR = Path(__file__).parent
storage = Storage()
log = logging.getLogger("api")

# Bot token is only used to cryptographically verify Telegram Mini App init data
# (so ratings/ticks are scoped to real Telegram users, one per person per route).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = FastAPI(title="USC Routes")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------------------------------- #
# Telegram Mini App initData validation
# --------------------------------------------------------------------------- #
def _init_secret():
    # Telegram: secret_key = HMAC-SHA256(key="WebAppData", msg=bot_token)
    return hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()


def validate_init_data(raw: str, max_age: int = 86400):
    """Return (tg_user_id, name) for a valid Telegram WebApp initData string.

    Verifies the HMAC-SHA256 signature and freshness. Raises ValueError when
    missing/invalid/stale. If BOT_TOKEN isn't configured, validation fails
    closed.
    """
    if not BOT_TOKEN:
        raise ValueError("bot token not configured")
    if not raw:
        raise ValueError("missing init data")

    params = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
    received = params.get("hash")
    if not received:
        raise ValueError("no signature hash")

    data_check = "\n".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k != "hash"
    )
    calc = hmac.new(_init_secret(), data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise ValueError("bad signature")

    try:
        auth_date = int(params.get("auth_date", 0))
    except (TypeError, ValueError):
        auth_date = 0
    if time.time() - auth_date > max_age:
        raise ValueError("stale init data")

    try:
        user = json.loads(params.get("user", "{}"))
    except (TypeError, json.JSONDecodeError):
        raise ValueError("missing user")
    uid = user.get("id")
    if not uid:
        raise ValueError("missing user id")
    name = user.get("first_name") or user.get("username") or f"user_{uid}"
    return int(uid), name


def _identity(body, request):
    """Resolve + verify the acting Telegram user from a request."""
    raw = body.init_data or request.headers.get("X-Telegram-Init-Data", "")
    return validate_init_data(raw)  # raises ValueError


# --------------------------------------------------------------------------- #
# request bodies (validated at the boundary; handlers below can trust shapes)
# --------------------------------------------------------------------------- #
class RateBody(BaseModel):
    value: Literal[1, 2, 3, 4, 5]
    init_data: str | None = None


class TickBody(BaseModel):
    suggested_grade: str | None = None
    init_data: str | None = None


class TickGradeBody(BaseModel):
    suggested_grade: str | None = None
    init_data: str | None = None


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.get("/api/routes")
def routes(request: Request, grade: str | None = None, wall: str | None = None,
           tg: str | None = None,  # tg: optional initData query param
           status: Literal["active", "retired", "all"] = "active"):
    """Return routes optionally filtered by minimum grade or exact wall.

    `status` picks "on the wall now" (active, default), routes stripped in
    a past reset (retired), or both (all) -- retired routes stay tickable
    since a climber's send history shouldn't vanish when a route comes down.
    Ratings/ticks are attached; my_rating / my_tick are personalised when a
    valid Telegram initData is supplied (as `tg` query param or header).
    """
    rows = storage.list_routes(grade=grade, wall=wall, status=status)
    viewer = None
    raw = tg or request.headers.get("X-Telegram-Init-Data", "")
    if raw:
        try:
            viewer, _ = validate_init_data(raw)
        except ValueError:
            viewer = None
    storage.attach_ratings_and_ticks(rows, tg_user_id=viewer)
    for r in rows:
        r.pop("photo_path", None)
        r.pop("_id", None)
    return {"routes": rows}


@app.post("/api/rate/{route_id}")
async def rate(route_id: int, body: RateBody, request: Request):
    """Set (1-5) or clear a user's star rating on a route."""
    try:
        uid, name = _identity(body, request)
    except ValueError as e:
        log.warning("rate %s rejected: %s", route_id, e)
        return JSONResponse(status_code=401, content={"error": str(e)})
    if not storage.get_route(route_id):
        return JSONResponse(status_code=404, content={"error": "route not found"})
    return storage.set_rating(route_id, uid, name, body.value)


@app.post("/api/tick/{route_id}")
async def tick(route_id: int, body: TickBody, request: Request):
    """Toggle a user's ascent tick on/off (optionally with a suggested grade)."""
    try:
        uid, name = _identity(body, request)
    except ValueError as e:
        return JSONResponse(status_code=401, content={"error": str(e)})
    if not storage.get_route(route_id):
        return JSONResponse(status_code=404, content={"error": "route not found"})
    return storage.toggle_tick(route_id, uid, name, body.suggested_grade)


@app.put("/api/tick/{route_id}/grade")
async def tick_grade(route_id: int, body: TickGradeBody, request: Request):
    """Update the suggested grade on an already-existing tick."""
    try:
        uid, _ = _identity(body, request)
    except ValueError as e:
        return JSONResponse(status_code=401, content={"error": str(e)})
    if not storage.get_route(route_id):
        return JSONResponse(status_code=404, content={"error": "route not found"})
    storage.set_tick_grade(route_id, uid, body.suggested_grade)
    return {"ticked": True, "suggested_grade": body.suggested_grade}


@app.get("/api/meta")
def meta():
    stats = storage.stats()
    return {
        "grades": V_ORDER,
        "walls": storage.walls(),
        "count": stats["total"],
        "active_count": stats["active"],
        "retired_count": stats["retired"],
    }


@app.get("/api/me")
def me(request: Request, tg: str | None = None):
    """The calling Telegram user's own send history (personal logbook).

    Requires a valid initData (via `tg` query param or header) -- unlike
    /api/routes this has no anonymous fallback, since there's no "someone
    else's" history to show. Routes are shaped identically to /api/routes
    rows (same rating/tick aggregate fields) so the frontend can reuse the
    same card + detail-sheet rendering.
    """
    raw = tg or request.headers.get("X-Telegram-Init-Data", "")
    try:
        uid, name = validate_init_data(raw)
    except ValueError as e:
        return JSONResponse(status_code=401, content={"error": str(e)})

    rows = storage.user_ticked_routes(uid)
    storage.attach_ratings_and_ticks(rows, tg_user_id=uid)
    for r in rows:
        r.pop("photo_path", None)

    pyramid: dict[str, int] = {}
    hardest_grade, hardest_low = None, -1
    for r in rows:
        pyramid[r["grade"]] = pyramid.get(r["grade"], 0) + 1
        if r["grade_low"] > hardest_low:
            hardest_low, hardest_grade = r["grade_low"], r["grade"]

    return {
        "name": name,
        "routes": rows,
        "count": len(rows),
        "grade_pyramid": pyramid,
        "hardest_grade": hardest_grade,
    }


@app.get("/api/leaderboard")
def leaderboard(limit: int = 10):
    """Public leaderboards -- top climbers (send count + hardest grade)
    and top setters (routes archived). No auth needed, this is aggregate
    data, not anyone's personal record."""
    limit = max(1, min(limit, 50))
    return {
        "climbers": storage.leaderboard_climbers(limit),
        "setters": storage.leaderboard_setters(limit),
    }


@app.get("/api/photo/{route_id}")
def photo(route_id: int):
    r = storage.get_route(route_id)
    if not r or not r.get("photo_path"):
        raise HTTPException(404, "no photo")
    p = Path(r["photo_path"])
    if not p.exists():
        raise HTTPException(404, "photo missing")
    return FileResponse(str(p))


@app.get("/healthz")
def health():
    return {"ok": True}