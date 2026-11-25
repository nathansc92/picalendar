"""SQLite setup for the family hub kiosk.

Phase 1 tables cover family members, chores, assets, and maintenance.
Phase 2 adds the local calendar cache and expands repeating rows on read.
Phase 3 stores each person's Google calendar id, token path, and sync tokens.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterator

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

DB_PATH = Path(__file__).resolve().parent / "family_hub.db"

INTERVAL_UNITS = ("days", "weeks", "months", "years")
MAX_OCCURRENCES = 500


def shift_date(start: date, value: int, unit: str) -> date:
    """Move a date forward by a maintenance interval."""
    if unit not in INTERVAL_UNITS:
        raise ValueError(f"Unsupported interval unit: {unit}")
    if value < 1:
        raise ValueError("Interval must be at least 1")
    deltas = {
        "days": relativedelta(days=value),
        "weeks": relativedelta(weeks=value),
        "months": relativedelta(months=value),
        "years": relativedelta(years=value),
    }
    return start + deltas[unit]


def classify_due(next_due: date, today: date | None = None) -> tuple[str, int]:
    """Return (status, days_until_due).

    overdue: before today. due_soon: today through 14 days. ok: later than that.
    """
    today = today or date.today()
    days_until = (next_due - today).days
    if days_until < 0:
        return "overdue", days_until
    if days_until <= 14:
        return "due_soon", days_until
    return "ok", days_until


def compute_next_due(
    last_done: date | None,
    interval_value: int,
    interval_unit: str,
    today: date | None = None,
) -> date:
    """Next due is one interval after the last completion, or today if never done."""
    today = today or date.today()
    if last_done is None:
        return today
    return shift_date(last_done, interval_value, interval_unit)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with the kiosk concurrency pragmas applied."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS family_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                color TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                family_member_id INTEGER REFERENCES family_members(id) ON DELETE SET NULL,
                due_date TEXT,
                completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS maintenance_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                interval_value INTEGER NOT NULL CHECK (interval_value >= 1),
                interval_unit TEXT NOT NULL CHECK (interval_unit IN ('days', 'weeks', 'months', 'years')),
                last_done_date TEXT,
                next_due_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
            )
            """
        )
        _ensure_member_google_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                calendar_id TEXT PRIMARY KEY,
                sync_token TEXT,
                last_synced_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chores_member ON chores(family_member_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_due ON maintenance_tasks(next_due_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_asset ON maintenance_tasks(asset_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_event_id TEXT UNIQUE,
                calendar_id TEXT DEFAULT 'primary',
                member_id INTEGER REFERENCES family_members(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                description TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                all_day INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
                rrule TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_member ON events(member_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time)"
        )
        seed_if_empty(conn)
        seed_events_if_empty(conn)


def _ensure_member_google_columns(conn: sqlite3.Connection) -> None:
    """Add Google account columns without rebuilding an existing family list."""
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(family_members)")
    }
    if "google_calendar_id" not in existing:
        conn.execute("ALTER TABLE family_members ADD COLUMN google_calendar_id TEXT")
    if "token_file" not in existing:
        conn.execute("ALTER TABLE family_members ADD COLUMN token_file TEXT")


def seed_if_empty(conn: sqlite3.Connection) -> None:
    """Insert sample people, chores, and assets only on a brand-new database."""
    counts = [
        conn.execute("SELECT COUNT(*) FROM family_members").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM chores").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM maintenance_tasks").fetchone()[0],
    ]
    if any(counts):
        return

    today = date.today()
    members = (
        ("Katie", "#2563EB"),
        ("Nathan", "#DB2777"),
        ("Lily", "#059669"),
        ("Owen", "#F59E0B"),
        ("Aiden", "#8B5CF6"),
    )
    member_ids: list[int] = []
    for name, color in members:
        cursor = conn.execute(
            "INSERT INTO family_members (name, color) VALUES (?, ?)",
            (name, color),
        )
        member_ids.append(int(cursor.lastrowid))
    katie_id, nathan_id, lily_id, owen_id, aiden_id = member_ids

    sample_chores = (
        ("Empty the dishwasher", "After dinner", katie_id, today.isoformat(), 0, None),
        (
            "Wash the dishes",
            "Rinse and load the dishwasher",
            nathan_id,
            (today + relativedelta(days=1)).isoformat(),
            0,
            None,
        ),
        (
            "Vacuum your room",
            "",
            lily_id,
            (today - relativedelta(days=1)).isoformat(),
            1,
            today.isoformat() + "T12:00:00",
        ),
        (
            "Wipe the kitchen counters",
            "",
            None,
            (today + relativedelta(days=2)).isoformat(),
            0,
            None,
        ),
        (
            "Take out the trash",
            "Bins at the curb",
            owen_id,
            (today + relativedelta(days=3)).isoformat(),
            0,
            None,
        ),
        (
            "Get the mail",
            "Mailbox at the end of the driveway",
            aiden_id,
            (today + relativedelta(days=4)).isoformat(),
            0,
            None,
        ),
    )
    conn.executemany(
        """
        INSERT INTO chores (title, notes, family_member_id, due_date, completed, completed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        sample_chores,
    )

    sample_assets = (
        ("Furnace", "Utility room", ""),
        ("Water heater", "Basement", "Tank water heater"),
        ("Smoke alarms", "Hallways and bedrooms", "Interconnected alarms"),
    )
    asset_ids: list[int] = []
    for name, location, notes in sample_assets:
        cursor = conn.execute(
            "INSERT INTO assets (name, location, notes) VALUES (?, ?, ?)",
            (name, location, notes),
        )
        asset_ids.append(int(cursor.lastrowid))
    furnace_id, heater_id, alarm_id = asset_ids

    furnace_last = today - relativedelta(days=80)
    heater_last = today - relativedelta(years=1, days=21)
    alarm_last = today - relativedelta(days=12)
    sample_tasks = (
        (
            furnace_id,
            "Replace air filter",
            "Use a 16x25x1 filter",
            90,
            "days",
            furnace_last.isoformat(),
            shift_date(furnace_last, 90, "days").isoformat(),
        ),
        (
            heater_id,
            "Flush the tank",
            "Attach a hose and drain until the water runs clear",
            1,
            "years",
            heater_last.isoformat(),
            shift_date(heater_last, 1, "years").isoformat(),
        ),
        (
            alarm_id,
            "Test every alarm",
            "Hold the test button until it sounds",
            6,
            "months",
            alarm_last.isoformat(),
            shift_date(alarm_last, 6, "months").isoformat(),
        ),
    )
    conn.executemany(
        """
        INSERT INTO maintenance_tasks (
            asset_id, title, notes, interval_value, interval_unit,
            last_done_date, next_due_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        sample_tasks,
    )


def seed_events_if_empty(conn: sqlite3.Connection) -> None:
    """Insert a few local events, including one weekly series, when none exist."""
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if count:
        return

    def member_id(name: str) -> int | None:
        row = conn.execute(
            "SELECT id FROM family_members WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        return int(row["id"]) if row else None

    today = date.today()
    tuesday = today - timedelta(days=(today.weekday() - 1) % 7)
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)
    friday = week_start + timedelta(days=5)
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    soccer_start = datetime.combine(tuesday, time(17, 0))
    soccer_end = soccer_start + timedelta(minutes=75)
    dinner_start = datetime.combine(today, time(18, 0))
    dinner_end = datetime.combine(today, time(19, 30))
    dentist_start = datetime.combine(tomorrow, time(9, 30))
    dentist_end = datetime.combine(tomorrow, time(10, 15))

    conn.executemany(
        """
        INSERT INTO events (
            google_event_id, calendar_id, member_id, title, description,
            start_time, end_time, all_day, rrule, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                None,
                "primary",
                member_id("Katie"),
                "Family dinner",
                "At the kitchen table",
                dinner_start.strftime("%Y-%m-%dT%H:%M:%S"),
                dinner_end.strftime("%Y-%m-%dT%H:%M:%S"),
                0,
                None,
                now,
            ),
            (
                None,
                "primary",
                member_id("Owen"),
                "Dentist",
                "Cleaning",
                dentist_start.strftime("%Y-%m-%dT%H:%M:%S"),
                dentist_end.strftime("%Y-%m-%dT%H:%M:%S"),
                0,
                None,
                now,
            ),
            (
                None,
                "primary",
                member_id("Aiden"),
                "Soccer practice",
                "Bring shin guards",
                soccer_start.strftime("%Y-%m-%dT%H:%M:%S"),
                soccer_end.strftime("%Y-%m-%dT%H:%M:%S"),
                0,
                "FREQ=WEEKLY;BYDAY=TU",
                now,
            ),
            (
                None,
                "primary",
                member_id("Lily"),
                "No school",
                "",
                friday.isoformat(),
                (friday + timedelta(days=1)).isoformat(),
                1,
                None,
                now,
            ),
        ),
    )


def parse_local_datetime(value: str) -> datetime:
    """Parse an ISO datetime and convert zoned values to naive local time."""
    text = value.strip()
    if not text:
        raise ValueError("Datetime is required")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("Datetime must be ISO 8601") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def normalize_rrule(value: str | None, dtstart: datetime) -> str | None:
    """Store a bare RFC 5545 rule, rejecting values dateutil cannot expand."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.upper().startswith("RRULE:"):
        text = text.split(":", 1)[1].strip()
    if not text:
        return None
    try:
        rrulestr(f"RRULE:{text}", dtstart=dtstart)
    except (TypeError, ValueError) as exc:
        raise ValueError("Recurrence rule is not valid") from exc
    return text


def _parse_stored(value: str, all_day: bool) -> datetime:
    if all_day or "T" not in value:
        return datetime.fromisoformat(value[:10])
    return parse_local_datetime(value)


def _emit(value: datetime, all_day: bool) -> str:
    if all_day:
        return value.date().isoformat()
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat(timespec="seconds")


def _overlaps(start: datetime, end: datetime, range_start: datetime, range_end: datetime) -> bool:
    return start < range_end and end > range_start


def _occurrence(row: sqlite3.Row, start: datetime, end: datetime) -> dict:
    all_day = bool(row["all_day"])
    start_iso = _emit(start, all_day)
    series_start = _parse_stored(row["start_time"], all_day)
    series_end = _parse_stored(row["end_time"], all_day)
    return {
        "id": row["id"],
        "occurrence_id": f"{row['id']}:{start_iso}",
        "google_event_id": row["google_event_id"],
        "calendar_id": row["calendar_id"] or "primary",
        "member_id": row["member_id"],
        "member_name": row["member_name"],
        "color_hex": row["color_hex"],
        "title": row["title"],
        "description": row["description"] or "",
        "start_time": start_iso,
        "end_time": _emit(end, all_day),
        "all_day": all_day,
        "rrule": row["rrule"],
        "series_start": _emit(series_start, all_day),
        "series_end": _emit(series_end, all_day),
        "updated_at": row["updated_at"],
    }


def _compile_rule(rrule: str, dtstart: datetime):
    text = rrule.strip()
    if text.upper().startswith("RRULE:"):
        text = text.split(":", 1)[1].strip()
    return rrulestr(f"RRULE:{text}", dtstart=dtstart)


def expand_events(
    rows: list[sqlite3.Row],
    range_start: datetime,
    range_end: datetime,
) -> list[dict]:
    """Turn stored rows into the occurrences that overlap [start, end)."""
    found: list[tuple[datetime, dict]] = []
    for row in rows:
        all_day = bool(row["all_day"])
        try:
            master_start = _parse_stored(row["start_time"], all_day)
            master_end = _parse_stored(row["end_time"], all_day)
        except ValueError:
            continue
        duration = master_end - master_start
        if duration <= timedelta(0):
            continue
        rule_text = (row["rrule"] or "").strip()
        if not rule_text:
            if _overlaps(master_start, master_end, range_start, range_end):
                found.append((master_start, _occurrence(row, master_start, master_end)))
            continue
        try:
            rule = _compile_rule(rule_text, master_start)
        except (TypeError, ValueError):
            if _overlaps(master_start, master_end, range_start, range_end):
                found.append((master_start, _occurrence(row, master_start, master_end)))
            continue
        window_start = range_start - duration
        for occ_start in rule.between(window_start, range_end, inc=True):
            occ_end = occ_start + duration
            if not _overlaps(occ_start, occ_end, range_start, range_end):
                continue
            found.append((occ_start, _occurrence(row, occ_start, occ_end)))
            if len(found) >= MAX_OCCURRENCES:
                break
        if len(found) >= MAX_OCCURRENCES:
            break
    found.sort(key=lambda item: (item[0], item[1]["id"], item[1]["occurrence_id"]))
    return [item[1] for item in found[:MAX_OCCURRENCES]]


if __name__ == "__main__":
    init_db()
    print(f"Database ready at {DB_PATH}")
