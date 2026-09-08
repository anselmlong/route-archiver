"""Mini-app backend: serves the route collection (JSON + photos) to the web UI.

Reads the same SQLite the bot writes to. Static frontend in ./static. Run via:
    venv/bin/uvicorn api:app --host 127.0.0.1 --port 8160
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from storage import V_ORDER, Storage

BASE_DIR = Path(__file__).parent
storage = Storage()

# Bot token is only used to cryptographically verify Telegram Mini App init data
# (so votes are scoped to real Telegram users, one per person per route).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = FastAPI(title="NUS USC Routes")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------------------------------- #
# Telegram Mini App initData validation
# --------------------------------------------------------------------------- #
def _init_secret():
    # secret_key = HMAC-SHA256(key=bot_token, msg="WebAppData")
    return hmac.new(BOT_TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()


def validate_init_data(raw: str, max_age: int = 86400):
    """Return (tg_user_id, name) for a valid Telegram WebApp initData string.

    Verifies the HMAC-SHA256 signature and freshness. Raises ValueError when
    missing/invalid/stale. If BOT_TOKEN isn't configured, validation fails
    closed (no votes).
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


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.get("/api/routes")
def routes(grade: str | None = None, wall: str | None = None,
           tg: str | None = None,  # type: ignore[assignment]  # optional initData
           request: "Request" = None):  # type: ignore[assignment]  # fastapi injects
    """Return routes optionally filtered by minimum grade or exact wall.

    Votes are attached; 'my_vote' is personalised when a valid Telegram
    initData is supplied (as `tg` query param or X-Telegram-Init-Data header).
    """
    rows = storage.list_routes(grade=grade, wall=wall)
    viewer = None
    raw = tg or request.headers.get("X-Telegram-Init-Data", "")
    if raw:
        try:
            viewer, _ = validate_init_data(raw)
        except ValueError:
            viewer = None
    storage.attach_votes(rows, tg_user_id=viewer)
    for r in rows:
        r.pop("photo_path", None)
        r.pop("_id", None)
    return {"routes": rows}


@app.post("/api/vote/{route_id}")
async def vote(route_id: int, request: Request):
    """Upvote (value=1) or downvote (value=-1) a route as the requester.

    Requires Telegram initData (sent in the JSON body as `init_data`) so each
    Telegram account gets one vote per route. Voting the same way again retracts.
    """
    body = await request.json()
    value = body.get("value", 1)
    if value not in (1, -1):
        return JSONResponse(status_code=400, content={"error": "value must be 1 or -1"})

    # accept initData from body (preferred, avoids header-length/escaping issues)
    raw = body.get("init_data") or request.headers.get("X-Telegram-Init-Data", "")
    try:
        uid, name = validate_init_data(raw)
    except ValueError as e:
        return JSONResponse(status_code=401, content={"error": str(e)})

    if not storage.get_route(route_id):
        return JSONResponse(status_code=404, content={"error": "route not found"})

    result = storage.set_vote(route_id, uid, name, value)
    return result


@app.get("/api/meta")
def meta():
    return {
        "grades": V_ORDER,
        "walls": storage.walls(),
        "count": storage.stats()["total"],
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