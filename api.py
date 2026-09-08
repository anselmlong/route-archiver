"""Mini-app backend: serves the route collection (JSON + photos) to the web UI.

Reads the same SQLite the bot writes to. Static frontend in ./static. Run via:
    venv/bin/uvicorn api:app --host 127.0.0.1 --port 8160
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from storage import V_ORDER, Storage

BASE_DIR = Path(__file__).parent
storage = Storage()

app = FastAPI(title="NUS USC Routes")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/")
def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.get("/api/routes")
def routes(grade: str | None = None, wall: str | None = None):
    """Return routes optionally filtered by minimum grade or exact wall."""
    rows = storage.list_routes(grade=grade, wall=wall)
    # don't leak server filesystem paths to the client
    for r in rows:
        r.pop("photo_path", None)
        r.pop("_id", None)
    return {"routes": rows}


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
