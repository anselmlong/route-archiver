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
- **Route lifecycle** — spray wall routes get physically stripped and re-set,
  which isn't a mistake, so it's tracked separately from admin deletes:
  - `/reset left | middle | right | all` bulk-retires every currently active
    route on a wall (or the whole gym) in one shot, with a Yes/Cancel
    confirmation showing the count first.
  - the 🪨 Retire / ♻️ Restore button on each route's message toggles a
    single route without a full section reset.
  - retired routes keep their ratings/ticks and stay in the mini-app's
    "All-time" view (toggle next to "On the wall") — a climber's send
    history doesn't vanish just because the route came down.
- **Personal logbook** — `/mine` (bot) or the mini-app's "Mine" tab shows
  your own send history: total sends, hardest grade, a grade pyramid, and
  the actual routes (including retired ones you climbed before they came
  down).
- **Leaderboards** — `/leaderboard` (bot) or the mini-app's "Leaderboard"
  tab: top climbers by send count (with each one's hardest grade) and top
  setters by routes archived.
- **Crowd-sourced grade consensus** — when ticking a route, climbers can
  suggest a grade; the mini-app's detail sheet shows the community's median
  suggestion (and how many people weighed in) alongside the setter's own
  grade. Free-text suggestions that don't parse as a V-grade are simply
  excluded, not rejected.
- **Route of the week** — `/hot` (bot) or the mini-app's "🔥 Hot" tab: the
  routes with the most sends in the last 7 days, rank #1 being the de
  facto route of the week.
- **Setter profiles** — `/setter <name>` (bot), or tap a route's "set by
  X" credit in the mini-app: routes set (active/retired), a weighted
  average of ratings received, and their most-sent route.
- **Search** — `/search <name or setter>` (bot), or the search box in the
  mini-app's Browse tab: case-insensitive match against route name or
  setter name.
- **Comment threads** — climbers can leave beta/tips on a route from the
  mini-app's detail sheet (mini-app only; posting a longer comment doesn't
  fit a bot command well). Flat, chronological, 500 chars max. Anyone can
  read; only the author can delete their own comment -- moderating
  someone else's is an admin/bot-side action, not exposed here.

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
