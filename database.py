"""SQLite setup for the family hub kiosk.

PROJECT.md did not include a table definition, so this schema covers the
Phase 1 screens: family members, chores, home assets, and maintenance tasks.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

from dateutil.relativedelta import relativedelta

DB_PATH = Path(__file__).resolve().parent / "family_hub.db"

INTERVAL_UNITS = ("days", "weeks", "months", "years")


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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chores_member ON chores(family_member_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_due ON maintenance_tasks(next_due_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_asset ON maintenance_tasks(asset_id)"
        )
        seed_if_empty(conn)


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


if __name__ == "__main__":
    init_db()
    print(f"Database ready at {DB_PATH}")
