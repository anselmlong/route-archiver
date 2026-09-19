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
- **Mini-app** (`routes.anselmlong.com`) — list of what's on the wall now,
  sorted by hardest, easiest, newest, top rated or most sent, and filtered
  by a grade range and wall section; tap a route for the full photo +
  setter name, and leave via the ✕, the Telegram back button or the
  backdrop.
- **Admin CRUD** — admins can edit/delete routes from the inline buttons on the
  archived confirmation, or `/routes` to list them.
- **Route lifecycle** — spray wall routes get physically stripped and re-set,
  which isn't a mistake, so it's tracked separately from admin deletes:
  - `/reset left | middle | right | all` bulk-retires every currently active
    route on a wall (or the whole gym) in one shot, with a Yes/Cancel
    confirmation showing the count first.
  - the 🪨 Retire / ♻️ Restore button on each route's message toggles a
    single route without a full section reset.
  - retired routes keep their ratings/ticks and stay visible in a
    climber's own history (Mine, Hot) — a route coming down doesn't erase
    that you climbed it. Browse itself only ever shows what's currently
    on the wall.
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
- **New-route alerts** — `/notify [grade] [wall]` in a **private DM with
  the bot** (Telegram never lets a bot message someone who hasn't spoken
  to it first, so this can't be set up from inside the group) subscribes
  you to a DM whenever a matching route is archived; `/notify off` stops
  them, plain `/notify` shows your current subscription. A user who
  blocks the bot is silently unsubscribed on the next send.

## Grades
The scale runs `VB` through `V8+` and carries every half-step in between
(`V0+`, `V1+`, … `V8+`), so a caption reading `V4+` is archived and shown as
`V4+` rather than rounded to `V4`. Anything harder than `V8+` clamps to
`V8+`. A route captioned with a range (`V3-4`, `V3/V4`) keeps the range as
its display grade but sorts and filters at its **lowest** grade, so it can
never hide above the range someone filtered for. `V?` (or a bare `V`) marks
an ungraded route: it sorts last in either direction and drops out as soon
as a grade range is narrowed, since nothing can say whether it belongs
inside one.

`grade_low` is an index into that scale, so changing the scale changes what
every stored index means. `Storage._migrate_grade_scale()` re-indexes routes
and tick suggestions from the display grade they were stored with, remaps
subscription thresholds, and stamps `PRAGMA user_version` so it runs once.
It does all of that inside one `BEGIN IMMEDIATE` and re-reads the stamp only
after that write lock is held: the bot and the API each build a `Storage` at
import, so restarting both services races two migrations on one DB. Routes
and ticks recompute from their display grade and survive a second pass, but
the subscription remap feeds a bare index back through the legacy scale, so
without the lock a deploy would silently move every `/notify` threshold.
Half-steps flattened by the older parser are gone from the display too, so a
route archived as `V4+` back then stays `V4`; only routes captioned since
carry the plus.

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
