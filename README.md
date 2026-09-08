# NUS USC Routes 🧗

Climbing-route archive for the NUS USC gym, run through Telegram.

Setters post a route photo with a caption like:

```
Crack Line / V4+ / left vertical
```

The bot auto-detects the photo, parses the **name + V-grade + wall section**,
stores it, and links to a browsable mini-app. Zero behavior change for setters —
they just keep posting route pics.

## Features
- **Auto-archive** — captioned route photos in the group are parsed and saved
  (name, grade, wall, setter, photo).
- **Mini-app** (`routes.anselmlong.com`) — sortable/filterable grid by grade
  (VB–V8+) and wall section (left vertical / middle overhang / right slab);
  tap a route for the full photo + setter name.
- **Admin CRUD** — admins can edit/delete routes from the inline buttons on the
  archived confirmation, or `/routes` to list them.

## Wall sections (canonical)
| Alias input            | Canonical              |
|------------------------|------------------------|
| left, vertical, left wall | `left vertical`      |
| middle, overhang, cave  | `middle overhang`      |
| right, slab, right wall | `right slab`           |

## Architecture
```
route-archiver/
├── bot.py       telegram bot (python-telegram-bot) — photo detection + admin CRUD
├── api.py       FastAPI backend — serves routes JSON + photos to the mini-app
├── storage.py   SQLite source of truth + caption parser (shared by bot & api)
└── static/      mini-app frontend (single-file HTML/JS)
```

- **bot** runs as `routes-bot.service` (systemd), **api** as `routes-api.service`,
  behind nginx + Let's Encrypt on `routes.anselmlong.com`.
- SQLite at `data/routes.db` is the single source of truth; photos in `data/photos/`.

## Setup
```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
# .env: BOT_TOKEN, ADMIN_IDS, APP_BASE, CHAT_ID
sudo cp routes-bot.service routes-api.service /etc/systemd/system/
sudo systemctl enable --now routes-bot routes-api
```

## Testing
```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m playwright install chromium   # first run only
./venv/bin/pytest tests/
```
`tests/test_storage.py` / `tests/test_api.py` cover the caption parser, grade
clamping, ratings/ticks, and every API endpoint (auth via signed Telegram
initData, 401/404/422 error paths) against an isolated SQLite DB.
`tests/test_frontend.py` drives the real static frontend against a live
FastAPI instance with headless Chromium (Playwright) — filtering, sorting,
the detail sheet, XSS-escaping of user content, and rating/tick gating
outside of Telegram.
