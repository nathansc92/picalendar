# Family Touch Dashboard (Raspberry Pi Kiosk App)

## 1. Project Overview
A local, touch-friendly family hub web application designed to run 24/7 in **Chromium Kiosk mode** (`--kiosk`) on a Raspberry Pi connected to a 15-inch touchscreen.

The app unifies four core household workflows into a single full-screen interface:
1. **Two-Way Multi-Account Google Calendar** (Single Day, 3-Day, and 1-Week views with per-person color chips)
2. **Family Chore List** (Daily/recurring task tracking)
3. **Home Assets & Preventative Maintenance (PM)** (Upcoming and overdue maintenance tracker)
4. **Checklists** (Shared lists such as groceries or todos, not assigned to a person)

---

## 2. Tech Stack & Constraints
* **Backend:** Python 3.11+ with **FastAPI** and **Uvicorn**
* **Database:** **SQLite** (`family_hub.db`) using standard `sqlite3` or `aiosqlite` with **WAL mode enabled** (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`) to prevent database lock errors during concurrent background syncs and phone/kiosk edits.
* **Frontend:** Vanilla HTML5, **Tailwind CSS**, **Alpine.js**, **FullCalendar** (Standard JS bundle), `rrule.js` (for recurrence expansion), and **simple-keyboard**.
* **Build System:** **None.** Serve static HTML/CSS/JS directly from FastAPI (`/static`). Avoid Node.js, npm build steps, React, or Vite to keep Raspberry Pi memory usage low and keep the codebase compact.
* **Offline/Local Assets:** Bundle JS/CSS libraries locally in `/static/vendor/` so UI rendering never breaks if internet drops.

---

## 3. Target Hardware & UX Guidelines
* **Display:** 15-inch touchscreen (1920x1080 landscape), wall/countertop mounted, no physical keyboard or mouse attached.
* **Touch Targets:** Minimum `48px x 48px` (`min-h-12`) for all buttons, tabs, checkboxes, filter chips, and calendar slots.
* **No Hover States:** Do not hide actions behind `:hover`. All edit, delete, and complete buttons must be visible and tappable.
* **On-Screen Typing (`simple-keyboard`):**
  * Chromium Kiosk mode on Raspberry Pi OS does not reliably trigger an OS virtual keyboard.
  * Dock a `simple-keyboard` instance at the bottom of the viewport (`z-50`).
  * Automatically open when any `<input type="text">` or `<textarea>` gains focus; pipe keystrokes into the active element and dispatch standard `input` events so Alpine.js reactivity stays in sync.
  * Provide a prominent **"Done"** key to dismiss the keyboard.
* **Date & Time Input:** Avoid manual typing for dates and times. Use touch-friendly date pickers and large `+` / `-` stepper buttons (e.g., 15-minute increments for calendar event times).
* **Kiosk Auto-Reload:** Expose `GET /api/version` returning a server startup timestamp. Have Alpine.js poll `/api/version` every 60 seconds and call `window.location.reload()` automatically whenever the backend restarts with updated code.

---

## 4. Architecture & Folder Structure
```text
family-dashboard/
├── PROJECT.md              # This specification
├── requirements.txt        # fastapi, uvicorn, google-api-python-client, google-auth-oauthlib, apscheduler, python-dateutil
├── main.py                 # FastAPI entry point, static file mounting, /api/version, background sync scheduler
├── database.py             # SQLite WAL initialization, schema setup, and CRUD helpers
├── google_calendar.py      # Multi-account Google Calendar OAuth2 & two-way write-through sync logic
├── tokens/                 # (Git-ignored) Folder storing per-account OAuth token JSON files
├── credentials.json        # (Git-ignored) Google Cloud OAuth client secrets
├── family_hub.db           # (Git-ignored) Local SQLite database
└── static/
    ├── index.html          # Main 4-tab Kiosk UI & modals
    ├── app.js              # Alpine.js state management, API client, version poller, and virtual keyboard controller
    └── styles.css          # Custom FullCalendar touch overrides & keyboard styling
```

---

## 5. Database Schema (SQLite)

### `family_members` (Color coding & multi-calendar ownership)
* `id` INTEGER PRIMARY KEY AUTOINCREMENT
* `name` TEXT NOT NULL (e.g., "Alex", "Jordan", "Kids")
* `color_hex` TEXT NOT NULL (e.g., "#3B82F6")
* `google_calendar_id` TEXT (Optional linked Google Calendar ID, e.g., `primary` or shared family email)
* `token_file` TEXT (Optional path to OAuth token in `tokens/` if connecting separate Google accounts)

### `events` (Local cache for Google Calendar)
* `id` INTEGER PRIMARY KEY AUTOINCREMENT
* `google_event_id` TEXT UNIQUE
* `calendar_id` TEXT DEFAULT 'primary'
* `member_id` INTEGER REFERENCES `family_members(id)` ON DELETE SET NULL
* `title` TEXT NOT NULL
* `description` TEXT
* `start_time` TEXT NOT NULL (ISO 8601 format)
* `end_time` TEXT NOT NULL (ISO 8601 format)
* `all_day` BOOLEAN DEFAULT 0
* `rrule` TEXT (Optional RFC 5545 recurrence rule; store repeating events as a single row and expand dynamically for the requested date range)
* `updated_at` TEXT

### `chores`
* `id` INTEGER PRIMARY KEY AUTOINCREMENT
* `title` TEXT NOT NULL
* `member_id` INTEGER REFERENCES `family_members(id)` ON DELETE SET NULL
* `frequency` TEXT (e.g., 'once', 'daily', 'weekly')
* `due_date` TEXT (YYYY-MM-DD)
* `completed` BOOLEAN DEFAULT 0
* `completed_at` TEXT

### `assets`
* `id` INTEGER PRIMARY KEY AUTOINCREMENT
* `name` TEXT NOT NULL (e.g., "Main HVAC", "Water Softener", "Family SUV")
* `category` TEXT (e.g., "Appliance", "HVAC/Plumbing", "Vehicle", "Exterior")
* `notes` TEXT

### `maintenance_tasks`
* `id` INTEGER PRIMARY KEY AUTOINCREMENT
* `asset_id` INTEGER REFERENCES `assets(id)` ON DELETE CASCADE
* `task_name` TEXT NOT NULL (e.g., "Replace 20x25x4 MERV 11 filter")
* `interval_days` INTEGER NOT NULL
* `last_done_date` TEXT (YYYY-MM-DD)
* `next_due_date` TEXT NOT NULL (YYYY-MM-DD)

### `lists` (Shared checklists, not assigned to a person)
* `id` INTEGER PRIMARY KEY AUTOINCREMENT
* `name` TEXT NOT NULL
* `sort_order` INTEGER NOT NULL
* `created_at` TEXT

### `list_items`
* `id` INTEGER PRIMARY KEY AUTOINCREMENT
* `list_id` INTEGER NOT NULL REFERENCES `lists(id)` ON DELETE CASCADE
* `title` TEXT NOT NULL
* `checked` BOOLEAN DEFAULT 0
* `sort_order` INTEGER NOT NULL (order within that list; checking an item does not move it)
* `created_at` TEXT

---

## 6. Feature Specifications

### Tab 1: Calendar (Two-Way Multi-Account Google Calendar Sync)
* **Views & Filtering:**
  * Top view switcher for **1-Day** (`timeGridDay`), **3-Day** (`timeGridThreeDay`), and **1-Week** (`timeGridWeek`), plus `Today`, `<`, and `>` navigation buttons.
  * **Per-Person Color Chips:** Tap-to-toggle family member filter chips at the top of the calendar; color-code events by `family_members.color_hex`.
* **Recurrence Handling:** Store repeating Google events as single rows with `rrule` strings and expand occurrences on the fly for the active date range. Push edits on repeating events to the master Google event series ID.
* **Two-Way Sync Strategy (Write-Through + Background Poll):**
  * **Read:** UI reads from SQLite `/api/events?start=...&end=...` for instant view switching. Background job polls configured Google Calendars every 5 minutes using incremental `syncToken`.
  * **Create / Edit / Delete:** Tapping a time slot or existing event opens a touch modal (with person selector, `simple-keyboard` title input, and 15-min time steppers). Saving sends `events().insert()`, `events().patch()`, or `events().delete()` to Google Calendar API first, then updates SQLite on success.
  * **Offline Resilience:** If Google API is unreachable, keep rendering the cached SQLite schedule cleanly and show a subtle **"Offline – Calendar edits paused"** badge while keeping Chores and Maintenance editable.

### Tab 2: Chore List
* View active chores grouped or filtered by `family_members` color chip or due date.
* Large one-tap checkbox to mark complete (with visual strike-through).
* Recurring chores automatically reset or generate the next due instance upon completion/daily rollover.
* "+ Add Chore" modal with `simple-keyboard` title input and quick-tap family member chips.

### Tab 3: Home Assets & Maintenance
* **Upcoming View (Default):** Lists all `maintenance_tasks` sorted by `next_due_date` ascending, highlighted with color-coded status badges:
  * **Red:** Overdue (`next_due_date < today`)
  * **Yellow:** Due within 14 days
  * **Green:** Upcoming (> 14 days out)
* **One-Tap Completion:** Tapping **"Mark Done"** sets `last_done_date = today` and recalculates `next_due_date = today + interval_days`.
* **Assets Management:** Sub-view or modal to add new `assets` and attach recurring `maintenance_tasks`.

### Tab 4: Lists
* Header button **Lists**, alongside Calendar, Chores, and Home Maintenance.
* Two panes: list names on the left, the selected list's items on the right. Lists are household-wide and have no family-member field.
* Each sidebar row shows how many items are still unchecked. **New List** sits at the bottom of that column. **Edit** above the lists reveals up, down, and delete icons beside each name; it switches to **Done** and hides those icons again. Deleting a list still asks for confirmation.
* Items have a large checkbox. Checked items stay in place with a strikethrough. **Clear checked** deletes every checked item on the open list and leaves the rest in order. **Edit** beside that button reveals up, down, and delete icons on each item, and turns the list name into a text field. It switches to **Done** and hides those icons again.
* Tap an item's text at any time to rename it. Deleting a whole list still asks for confirmation, from the list's trash icon.
* Names use the on-screen keyboard.

---

## 7. Raspberry Pi Deployment & OAuth Notes
* **Google Cloud Console Setup:** Set OAuth consent screen status to **"In Production" (Published)** rather than "Testing" so refresh tokens do not expire every 7 days. Use `https://googleapis.com` scope.
* **Initial OAuth Login over LAN:** Google rejects LAN IPs (`192.168.x.x`) and `.local` redirect URIs. Run initial login via SSH port forwarding (`ssh -L 8080:localhost:8080 pi@raspberrypi.local`) and authenticate through `http://localhost:8080`.
* **Kiosk Autostart & Overnight Screen Blank (Wayland / Bookworm+):**
  * Run FastAPI via a `systemd` service on boot.
  * Launch Chromium with `--kiosk --noerrdialogs --disable-infobars --incognito http://localhost:8080`.
  * Schedule overnight screen shutoff via `cron` using Wayland's `wlr-randr` (e.g., `WAYLAND_DISPLAY=wayland-0 wlr-randr --output HDMI-A-1 --off` at night and `--on` in the morning) rather than legacy X11 `xset`.

---

## 8. Phased Implementation Plan
* **Phase 1 (Core App & Local Tabs):** Set up FastAPI, SQLite WAL database, `family_members` seed data, `/api/version` auto-reload poller, Alpine.js/Tailwind shell, `simple-keyboard` integration, and full CRUD for Chores, Home Maintenance, and Lists.
* **Phase 2 (Calendar UI & Local CRUD):** Integrate FullCalendar with 1-Day, 3-Day, and 1-Week views, per-person color chips, `rrule` expansion, and touch create/edit/delete modals backed by SQLite.
* **Phase 3 (Google Calendar OAuth & Sync):** Connect `google_calendar.py` for multi-account `token.json` refresh, write-through mutations, offline status badge, and 5-minute background `syncToken` polling.
* **Phase 4 (Pi Kiosk Deployment):** Add `systemd` service unit file, Chromium `--kiosk` autostart script, and `wlr-randr` overnight display schedule.
