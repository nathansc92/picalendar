"""Family hub kiosk API.

Calendar creates, edits, and deletes go to Google Calendar first when a
family member is linked, then to SQLite. If Google is unreachable those
routes return 503 and leave the local row unchanged. Chores and maintenance
stay local.
"""

from __future__ import annotations

import html
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import google_calendar
from database import (
    classify_due,
    compute_next_due,
    expand_events,
    get_connection,
    init_db,
    move_ordered_row,
    next_sort_order,
    normalize_rrule,
    parse_local_datetime,
    shift_date,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_PATH = STATIC_DIR / "index.html"
STARTED_AT = datetime.now(timezone.utc).isoformat()
logger = logging.getLogger("family_hub")

IntervalUnit = Literal["days", "weeks", "months", "years"]
DueStatus = Literal["overdue", "due_soon", "ok"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        google_calendar.sync_all_calendars,
        "interval",
        minutes=5,
        id="google_calendar_sync",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=datetime.now().astimezone() + timedelta(seconds=5),
    )
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Family Hub", lifespan=lifespan)


class FamilyMemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Name is required")
        return cleaned


class FamilyMemberOut(FamilyMemberIn):
    id: int
    created_at: str
    google_calendar_id: str | None = None
    linked: bool = False


class MemberCalendarIn(BaseModel):
    google_calendar_id: str = Field(min_length=1, max_length=320)

    @field_validator("google_calendar_id")
    @classmethod
    def strip_calendar(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Calendar id is required")
        return cleaned


class SyncStatusOut(BaseModel):
    online: bool
    last_synced_at: str | None = None
    detail: str = ""
    linked_count: int = 0
    credentials_present: bool = False
    syncing: bool = False


class ChoreIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)
    family_member_id: int | None = None
    due_date: date | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Title is required")
        return cleaned

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: str) -> str:
        return value.strip()


class ChoreOut(BaseModel):
    id: int
    title: str
    notes: str
    family_member_id: int | None
    member_name: str | None
    member_color: str | None
    due_date: str | None
    completed: bool
    completed_at: str | None
    created_at: str


class ListIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Name is required")
        return cleaned


class ListOut(BaseModel):
    id: int
    name: str
    sort_order: int
    unchecked_count: int
    item_count: int
    created_at: str


class ListItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Title is required")
        return cleaned


class ListItemOut(BaseModel):
    id: int
    list_id: int
    title: str
    checked: bool
    sort_order: int
    created_at: str


class MoveIn(BaseModel):
    direction: Literal["up", "down"]


class AssetIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    location: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Name is required")
        return cleaned

    @field_validator("location", "notes")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class AssetOut(AssetIn):
    id: int
    created_at: str


class MaintenanceTaskIn(BaseModel):
    asset_id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)
    interval_value: int = Field(ge=1, le=500)
    interval_unit: IntervalUnit
    last_done_date: date | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Title is required")
        return cleaned

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: str) -> str:
        return value.strip()


class MaintenanceTaskOut(BaseModel):
    id: int
    asset_id: int
    asset_name: str
    asset_location: str
    title: str
    notes: str
    interval_value: int
    interval_unit: IntervalUnit
    last_done_date: str | None
    next_due_date: str
    status: DueStatus
    days_until_due: int
    created_at: str


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    member_id: int
    calendar_id: str | None = Field(default=None, max_length=200)
    start_time: str = Field(min_length=8, max_length=40)
    end_time: str = Field(min_length=8, max_length=40)
    all_day: bool = False
    rrule: str | None = Field(default=None, max_length=500)
    google_event_id: str | None = Field(default=None, max_length=1024)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Title is required")
        return cleaned

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("calendar_id", "rrule", "google_event_id")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class EventOut(BaseModel):
    id: int
    google_event_id: str | None
    calendar_id: str
    member_id: int | None
    member_name: str | None
    color_hex: str | None
    title: str
    description: str
    start_time: str
    end_time: str
    all_day: bool
    rrule: str | None
    updated_at: str | None


class EventOccurrenceOut(EventOut):
    occurrence_id: str
    series_start: str
    series_end: str


CHORE_SQL = """
SELECT c.id, c.title, c.notes, c.family_member_id, c.due_date, c.completed,
       c.completed_at, c.created_at, m.name AS member_name, m.color AS member_color
FROM chores c
LEFT JOIN family_members m ON m.id = c.family_member_id
"""

LIST_SQL = """
SELECT l.id, l.name, l.sort_order, l.created_at,
       COALESCE(SUM(CASE WHEN i.checked = 0 THEN 1 ELSE 0 END), 0) AS unchecked_count,
       COUNT(i.id) AS item_count
FROM lists l
LEFT JOIN list_items i ON i.list_id = l.id
"""

TASK_SQL = """
SELECT t.id, t.asset_id, t.title, t.notes, t.interval_value, t.interval_unit,
       t.last_done_date, t.next_due_date, t.created_at,
       a.name AS asset_name, a.location AS asset_location
FROM maintenance_tasks t
JOIN assets a ON a.id = t.asset_id
"""

EVENT_SQL = """
SELECT e.id, e.google_event_id, e.calendar_id, e.member_id, e.title, e.description,
       e.start_time, e.end_time, e.all_day, e.rrule, e.updated_at,
       m.name AS member_name, m.color AS color_hex
FROM events e
LEFT JOIN family_members m ON m.id = e.member_id
"""


def member_out(row: sqlite3.Row) -> FamilyMemberOut:
    keys = set(row.keys())
    calendar_id = row["google_calendar_id"] if "google_calendar_id" in keys else None
    return FamilyMemberOut(
        id=row["id"],
        name=row["name"],
        color=row["color"],
        created_at=row["created_at"],
        google_calendar_id=calendar_id,
        linked=google_calendar.member_is_linked(row),
    )


def raise_for_google(exc: Exception) -> None:
    if isinstance(exc, google_calendar.GoogleOffline):
        raise HTTPException(status_code=503, detail=google_calendar.OFFLINE_DETAIL) from exc
    if isinstance(exc, google_calendar.GoogleNotLinked):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, google_calendar.GoogleAuthError):
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if isinstance(exc, google_calendar.GoogleConfigError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, google_calendar.GoogleRequestError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise exc


def google_redirect_uri(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}/oauth2callback"


def oauth_message_page(message: str, status: int) -> HTMLResponse:
    safe = html.escape(message)
    body = f"""<!DOCTYPE html>
<html lang="en"><body style="font-family: sans-serif; padding: 2rem; font-size: 1.4rem;">
<p>{safe}</p>
<p><a href="/?settings=1">Back to Family Hub</a></p>
</body></html>"""
    return HTMLResponse(body, status_code=status)


def auth_error_redirect(message: str) -> RedirectResponse:
    cleaned = " ".join(message.split())[:180]
    return RedirectResponse(f"/?settings=1&auth_error={quote(cleaned)}", status_code=302)


def event_write_payload(payload: EventIn, start_s: str, end_s: str, rrule: str | None) -> dict:
    return {
        "member_id": payload.member_id,
        "calendar_id": payload.calendar_id,
        "title": payload.title,
        "description": payload.description,
        "start_time": start_s,
        "end_time": end_s,
        "all_day": payload.all_day,
        "rrule": rrule,
        "google_event_id": payload.google_event_id,
    }


def chore_out(row: sqlite3.Row) -> ChoreOut:
    return ChoreOut(
        id=row["id"],
        title=row["title"],
        notes=row["notes"],
        family_member_id=row["family_member_id"],
        member_name=row["member_name"],
        member_color=row["member_color"],
        due_date=row["due_date"],
        completed=bool(row["completed"]),
        completed_at=row["completed_at"],
        created_at=row["created_at"],
    )


def asset_out(row: sqlite3.Row) -> AssetOut:
    return AssetOut(
        id=row["id"],
        name=row["name"],
        location=row["location"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


def task_out(row: sqlite3.Row, today: date | None = None) -> MaintenanceTaskOut:
    next_due = date.fromisoformat(row["next_due_date"])
    status, days_until = classify_due(next_due, today)
    return MaintenanceTaskOut(
        id=row["id"],
        asset_id=row["asset_id"],
        asset_name=row["asset_name"],
        asset_location=row["asset_location"],
        title=row["title"],
        notes=row["notes"],
        interval_value=row["interval_value"],
        interval_unit=row["interval_unit"],
        last_done_date=row["last_done_date"],
        next_due_date=row["next_due_date"],
        status=status,
        days_until_due=days_until,
        created_at=row["created_at"],
    )


def require_member(conn: sqlite3.Connection, member_id: int | None) -> None:
    if member_id is None:
        return
    row = conn.execute(
        "SELECT id FROM family_members WHERE id = ?",
        (member_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="Family member not found")


def require_asset(conn: sqlite3.Connection, asset_id: int) -> None:
    row = conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="Asset not found")


def require_chore(conn: sqlite3.Connection, chore_id: int) -> sqlite3.Row:
    row = conn.execute(CHORE_SQL + " WHERE c.id = ?", (chore_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Chore not found")
    return row


def list_out(row: sqlite3.Row) -> ListOut:
    return ListOut(
        id=row["id"],
        name=row["name"],
        sort_order=row["sort_order"],
        unchecked_count=int(row["unchecked_count"]),
        item_count=int(row["item_count"]),
        created_at=row["created_at"],
    )


def list_item_out(row: sqlite3.Row) -> ListItemOut:
    return ListItemOut(
        id=row["id"],
        list_id=row["list_id"],
        title=row["title"],
        checked=bool(row["checked"]),
        sort_order=row["sort_order"],
        created_at=row["created_at"],
    )


def require_list_row(conn: sqlite3.Connection, list_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="List not found")
    return row


def require_list(conn: sqlite3.Connection, list_id: int) -> sqlite3.Row:
    row = conn.execute(
        LIST_SQL + " WHERE l.id = ? GROUP BY l.id",
        (list_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="List not found")
    return row


def require_list_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM list_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="List item not found")
    return row


def require_asset_row(conn: sqlite3.Connection, asset_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return row


def require_unique_member_name(
    conn: sqlite3.Connection, name: str, exclude_id: int | None = None
) -> None:
    row = conn.execute(
        """
        SELECT name FROM family_members
        WHERE name = ? COLLATE NOCASE AND id != ?
        """,
        (name, -1 if exclude_id is None else exclude_id),
    ).fetchone()
    if row is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A person named {row['name']} already exists.",
        )


def require_member_row(conn: sqlite3.Connection, member_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM family_members WHERE id = ?",
        (member_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Family member not found")
    return row


def require_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute(TASK_SQL + " WHERE t.id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Maintenance task not found")
    return row


def require_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = conn.execute(EVENT_SQL + " WHERE e.id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return row


def event_out(row: sqlite3.Row) -> EventOut:
    return EventOut(
        id=row["id"],
        google_event_id=row["google_event_id"],
        calendar_id=row["calendar_id"] or "primary",
        member_id=row["member_id"],
        member_name=row["member_name"],
        color_hex=row["color_hex"],
        title=row["title"],
        description=row["description"] or "",
        start_time=row["start_time"],
        end_time=row["end_time"],
        all_day=bool(row["all_day"]),
        rrule=row["rrule"],
        updated_at=row["updated_at"],
    )


def parse_event_input(value: str, all_day: bool) -> datetime:
    text = value.strip()
    if all_day:
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError as exc:
            raise ValueError("Date must be ISO 8601") from exc
    parsed = parse_local_datetime(text)
    return parsed.replace(second=0, microsecond=0)


def canonical_event_times(payload: EventIn) -> tuple[str, str, datetime]:
    try:
        start_dt = parse_event_input(payload.start_time, payload.all_day)
        end_dt = parse_event_input(payload.end_time, payload.all_day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Start and end must be ISO 8601 values") from exc
    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="End time must be after the start time")
    if payload.all_day:
        return start_dt.date().isoformat(), end_dt.date().isoformat(), start_dt
    return (
        start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        start_dt,
    )


def validated_rrule(payload: EventIn, start_dt: datetime) -> str | None:
    try:
        return normalize_rrule(payload.rrule, start_dt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


@app.get("/api/version")
def version(response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"started_at": STARTED_AT}


@app.get("/api/sync/status", response_model=SyncStatusOut)
def sync_status(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return google_calendar.current_status()


@app.post("/api/sync", response_model=SyncStatusOut)
def sync_now() -> dict:
    return google_calendar.sync_all_calendars()


@app.get("/api/auth/google/{member_id}")
def start_google_auth(member_id: int, request: Request):
    with get_connection() as conn:
        require_member_row(conn, member_id)
    try:
        url = google_calendar.authorization_url(member_id, google_redirect_uri(request))
    except google_calendar.GoogleConfigError as exc:
        return oauth_message_page(str(exc), 400)
    except google_calendar.GoogleCalendarError as exc:
        raise_for_google(exc)
    return RedirectResponse(url, status_code=302)


@app.get("/oauth2callback")
def oauth2callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        message = (
            "Google sign-in was cancelled."
            if error == "access_denied"
            else "Google sign-in did not finish."
        )
        return auth_error_redirect(message)
    try:
        member_id = google_calendar.finish_oauth(state, code)
    except google_calendar.GoogleCalendarError as exc:
        return auth_error_redirect(str(exc))
    except Exception:
        logger.exception("OAuth callback failed")
        return auth_error_redirect("Google sign-in could not be finished.")
    return RedirectResponse(f"/?settings=1&linked={member_id}", status_code=302)


@app.delete("/api/auth/google/{member_id}", status_code=204)
def unlink_google(member_id: int) -> Response:
    with get_connection() as conn:
        require_member_row(conn, member_id)
    try:
        google_calendar.unlink_member(member_id)
    except google_calendar.GoogleCalendarError as exc:
        raise_for_google(exc)
    return Response(status_code=204)


@app.get("/api/family-members", response_model=list[FamilyMemberOut])
def list_members() -> list[FamilyMemberOut]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM family_members ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
    return [member_out(row) for row in rows]


@app.post("/api/family-members", response_model=FamilyMemberOut, status_code=201)
def create_member(payload: FamilyMemberIn) -> FamilyMemberOut:
    with get_connection() as conn:
        require_unique_member_name(conn, payload.name)
        cursor = conn.execute(
            "INSERT INTO family_members (name, color) VALUES (?, ?)",
            (payload.name, payload.color),
        )
        row = require_member_row(conn, int(cursor.lastrowid))
    return member_out(row)


@app.get("/api/family-members/{member_id}", response_model=FamilyMemberOut)
def get_member(member_id: int) -> FamilyMemberOut:
    with get_connection() as conn:
        row = require_member_row(conn, member_id)
    return member_out(row)


@app.put("/api/family-members/{member_id}/calendar", response_model=FamilyMemberOut)
def update_member_calendar(member_id: int, payload: MemberCalendarIn) -> FamilyMemberOut:
    with get_connection() as conn:
        require_member_row(conn, member_id)
        conn.execute(
            "UPDATE family_members SET google_calendar_id = ? WHERE id = ?",
            (payload.google_calendar_id, member_id),
        )
        row = require_member_row(conn, member_id)
    return member_out(row)


@app.put("/api/family-members/{member_id}", response_model=FamilyMemberOut)
def update_member(member_id: int, payload: FamilyMemberIn) -> FamilyMemberOut:
    with get_connection() as conn:
        require_member_row(conn, member_id)
        require_unique_member_name(conn, payload.name, exclude_id=member_id)
        conn.execute(
            "UPDATE family_members SET name = ?, color = ? WHERE id = ?",
            (payload.name, payload.color, member_id),
        )
        row = require_member_row(conn, member_id)
    return member_out(row)


@app.delete("/api/family-members/{member_id}", status_code=204)
def delete_member(member_id: int) -> Response:
    with get_connection() as conn:
        require_member_row(conn, member_id)
    google_calendar.unlink_member(member_id)
    with get_connection() as conn:
        conn.execute("DELETE FROM events WHERE member_id = ?", (member_id,))
        conn.execute("DELETE FROM family_members WHERE id = ?", (member_id,))
    return Response(status_code=204)


@app.get("/api/chores", response_model=list[ChoreOut])
def list_chores(
    family_member_id: int | None = None,
    unassigned: bool = False,
    completed: bool | None = Query(default=None),
) -> list[ChoreOut]:
    clauses = ["1 = 1"]
    params: list[object] = []
    if unassigned:
        clauses.append("c.family_member_id IS NULL")
    elif family_member_id is not None:
        clauses.append("c.family_member_id = ?")
        params.append(family_member_id)
    if completed is not None:
        clauses.append("c.completed = ?")
        params.append(1 if completed else 0)
    sql = (
        CHORE_SQL
        + " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY c.completed ASC, c.due_date IS NULL, c.due_date ASC, c.id ASC"
    )
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [chore_out(row) for row in rows]


@app.post("/api/chores", response_model=ChoreOut, status_code=201)
def create_chore(payload: ChoreIn) -> ChoreOut:
    with get_connection() as conn:
        require_member(conn, payload.family_member_id)
        cursor = conn.execute(
            """
            INSERT INTO chores (title, notes, family_member_id, due_date, completed)
            VALUES (?, ?, ?, ?, 0)
            """,
            (payload.title, payload.notes, payload.family_member_id, iso_date(payload.due_date)),
        )
        row = require_chore(conn, int(cursor.lastrowid))
    return chore_out(row)


@app.get("/api/chores/{chore_id}", response_model=ChoreOut)
def get_chore(chore_id: int) -> ChoreOut:
    with get_connection() as conn:
        row = require_chore(conn, chore_id)
    return chore_out(row)


@app.put("/api/chores/{chore_id}", response_model=ChoreOut)
def update_chore(chore_id: int, payload: ChoreIn) -> ChoreOut:
    with get_connection() as conn:
        require_chore(conn, chore_id)
        require_member(conn, payload.family_member_id)
        conn.execute(
            """
            UPDATE chores
            SET title = ?, notes = ?, family_member_id = ?, due_date = ?
            WHERE id = ?
            """,
            (
                payload.title,
                payload.notes,
                payload.family_member_id,
                iso_date(payload.due_date),
                chore_id,
            ),
        )
        row = require_chore(conn, chore_id)
    return chore_out(row)


@app.delete("/api/chores/{chore_id}", status_code=204)
def delete_chore(chore_id: int) -> Response:
    with get_connection() as conn:
        require_chore(conn, chore_id)
        conn.execute("DELETE FROM chores WHERE id = ?", (chore_id,))
    return Response(status_code=204)


@app.post("/api/chores/{chore_id}/toggle", response_model=ChoreOut)
def toggle_chore(chore_id: int) -> ChoreOut:
    with get_connection() as conn:
        row = require_chore(conn, chore_id)
        completed = 0 if row["completed"] else 1
        completed_at = (
            datetime.now().isoformat(timespec="seconds") if completed else None
        )
        conn.execute(
            "UPDATE chores SET completed = ?, completed_at = ? WHERE id = ?",
            (completed, completed_at, chore_id),
        )
        updated = require_chore(conn, chore_id)
    return chore_out(updated)


@app.get("/api/assets", response_model=list[AssetOut])
def list_assets() -> list[AssetOut]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM assets ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
    return [asset_out(row) for row in rows]


@app.post("/api/assets", response_model=AssetOut, status_code=201)
def create_asset(payload: AssetIn) -> AssetOut:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO assets (name, location, notes) VALUES (?, ?, ?)",
            (payload.name, payload.location, payload.notes),
        )
        row = require_asset_row(conn, int(cursor.lastrowid))
    return asset_out(row)


@app.get("/api/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: int) -> AssetOut:
    with get_connection() as conn:
        row = require_asset_row(conn, asset_id)
    return asset_out(row)


@app.put("/api/assets/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: int, payload: AssetIn) -> AssetOut:
    with get_connection() as conn:
        require_asset_row(conn, asset_id)
        conn.execute(
            "UPDATE assets SET name = ?, location = ?, notes = ? WHERE id = ?",
            (payload.name, payload.location, payload.notes, asset_id),
        )
        row = require_asset_row(conn, asset_id)
    return asset_out(row)


@app.delete("/api/assets/{asset_id}", status_code=204)
def delete_asset(asset_id: int) -> Response:
    with get_connection() as conn:
        require_asset_row(conn, asset_id)
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    return Response(status_code=204)


@app.get("/api/maintenance-tasks", response_model=list[MaintenanceTaskOut])
def list_tasks(asset_id: int | None = None) -> list[MaintenanceTaskOut]:
    sql = TASK_SQL
    params: list[object] = []
    if asset_id is not None:
        sql += " WHERE t.asset_id = ?"
        params.append(asset_id)
    sql += " ORDER BY t.next_due_date ASC, t.id ASC"
    today = date.today()
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [task_out(row, today) for row in rows]


@app.post("/api/maintenance-tasks", response_model=MaintenanceTaskOut, status_code=201)
def create_task(payload: MaintenanceTaskIn) -> MaintenanceTaskOut:
    next_due = compute_next_due(
        payload.last_done_date,
        payload.interval_value,
        payload.interval_unit,
    )
    with get_connection() as conn:
        require_asset(conn, payload.asset_id)
        cursor = conn.execute(
            """
            INSERT INTO maintenance_tasks (
                asset_id, title, notes, interval_value, interval_unit,
                last_done_date, next_due_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.asset_id,
                payload.title,
                payload.notes,
                payload.interval_value,
                payload.interval_unit,
                iso_date(payload.last_done_date),
                next_due.isoformat(),
            ),
        )
        row = require_task(conn, int(cursor.lastrowid))
    return task_out(row)


@app.get("/api/maintenance-tasks/{task_id}", response_model=MaintenanceTaskOut)
def get_task(task_id: int) -> MaintenanceTaskOut:
    with get_connection() as conn:
        row = require_task(conn, task_id)
    return task_out(row)


@app.put("/api/maintenance-tasks/{task_id}", response_model=MaintenanceTaskOut)
def update_task(task_id: int, payload: MaintenanceTaskIn) -> MaintenanceTaskOut:
    next_due = compute_next_due(
        payload.last_done_date,
        payload.interval_value,
        payload.interval_unit,
    )
    with get_connection() as conn:
        require_task(conn, task_id)
        require_asset(conn, payload.asset_id)
        conn.execute(
            """
            UPDATE maintenance_tasks
            SET asset_id = ?, title = ?, notes = ?, interval_value = ?,
                interval_unit = ?, last_done_date = ?, next_due_date = ?
            WHERE id = ?
            """,
            (
                payload.asset_id,
                payload.title,
                payload.notes,
                payload.interval_value,
                payload.interval_unit,
                iso_date(payload.last_done_date),
                next_due.isoformat(),
                task_id,
            ),
        )
        row = require_task(conn, task_id)
    return task_out(row)


@app.delete("/api/maintenance-tasks/{task_id}", status_code=204)
def delete_task(task_id: int) -> Response:
    with get_connection() as conn:
        require_task(conn, task_id)
        conn.execute("DELETE FROM maintenance_tasks WHERE id = ?", (task_id,))
    return Response(status_code=204)


@app.post("/api/maintenance-tasks/{task_id}/done", response_model=MaintenanceTaskOut)
def mark_task_done(task_id: int) -> MaintenanceTaskOut:
    """Set last done to today and push next due out by the task interval."""
    today = date.today()
    with get_connection() as conn:
        row = require_task(conn, task_id)
        try:
            next_due = shift_date(today, row["interval_value"], row["interval_unit"])
        except (OverflowError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Could not calculate the next due date") from exc
        conn.execute(
            """
            UPDATE maintenance_tasks
            SET last_done_date = ?, next_due_date = ?
            WHERE id = ?
            """,
            (today.isoformat(), next_due.isoformat(), task_id),
        )
        updated = require_task(conn, task_id)
    return task_out(updated, today)


@app.get("/api/lists", response_model=list[ListOut])
def list_lists() -> list[ListOut]:
    with get_connection() as conn:
        rows = conn.execute(
            LIST_SQL + " GROUP BY l.id ORDER BY l.sort_order ASC, l.id ASC"
        ).fetchall()
    return [list_out(row) for row in rows]


@app.post("/api/lists", response_model=ListOut, status_code=201)
def create_list(payload: ListIn) -> ListOut:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO lists (name, sort_order) VALUES (?, ?)",
            (payload.name, next_sort_order(conn, "lists")),
        )
        row = require_list(conn, int(cursor.lastrowid))
    return list_out(row)


@app.put("/api/lists/{list_id}", response_model=ListOut)
def update_list(list_id: int, payload: ListIn) -> ListOut:
    with get_connection() as conn:
        require_list_row(conn, list_id)
        conn.execute("UPDATE lists SET name = ? WHERE id = ?", (payload.name, list_id))
        row = require_list(conn, list_id)
    return list_out(row)


@app.post("/api/lists/{list_id}/move", response_model=ListOut)
def move_list(list_id: int, payload: MoveIn) -> ListOut:
    with get_connection() as conn:
        require_list_row(conn, list_id)
        move_ordered_row(conn, "lists", list_id, payload.direction)
        row = require_list(conn, list_id)
    return list_out(row)


@app.delete("/api/lists/{list_id}", status_code=204)
def delete_list(list_id: int) -> Response:
    with get_connection() as conn:
        require_list_row(conn, list_id)
        conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    return Response(status_code=204)


@app.get("/api/lists/{list_id}/items", response_model=list[ListItemOut])
def list_items(list_id: int) -> list[ListItemOut]:
    with get_connection() as conn:
        require_list_row(conn, list_id)
        rows = conn.execute(
            """
            SELECT * FROM list_items
            WHERE list_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (list_id,),
        ).fetchall()
    return [list_item_out(row) for row in rows]


@app.post("/api/lists/{list_id}/items", response_model=ListItemOut, status_code=201)
def create_list_item(list_id: int, payload: ListItemIn) -> ListItemOut:
    with get_connection() as conn:
        require_list_row(conn, list_id)
        cursor = conn.execute(
            """
            INSERT INTO list_items (list_id, title, checked, sort_order)
            VALUES (?, ?, 0, ?)
            """,
            (list_id, payload.title, next_sort_order(conn, "list_items", list_id)),
        )
        row = require_list_item(conn, int(cursor.lastrowid))
    return list_item_out(row)


@app.post("/api/lists/{list_id}/clear-checked", response_model=list[ListItemOut])
def clear_checked_items(list_id: int) -> list[ListItemOut]:
    """Delete checked items and leave the rest in their current order."""
    with get_connection() as conn:
        require_list_row(conn, list_id)
        conn.execute(
            "DELETE FROM list_items WHERE list_id = ? AND checked = 1",
            (list_id,),
        )
        rows = conn.execute(
            """
            SELECT * FROM list_items
            WHERE list_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (list_id,),
        ).fetchall()
    return [list_item_out(row) for row in rows]


@app.put("/api/list-items/{item_id}", response_model=ListItemOut)
def update_list_item(item_id: int, payload: ListItemIn) -> ListItemOut:
    with get_connection() as conn:
        require_list_item(conn, item_id)
        conn.execute(
            "UPDATE list_items SET title = ? WHERE id = ?",
            (payload.title, item_id),
        )
        row = require_list_item(conn, item_id)
    return list_item_out(row)


@app.post("/api/list-items/{item_id}/toggle", response_model=ListItemOut)
def toggle_list_item(item_id: int) -> ListItemOut:
    with get_connection() as conn:
        row = require_list_item(conn, item_id)
        checked = 0 if row["checked"] else 1
        conn.execute(
            "UPDATE list_items SET checked = ? WHERE id = ?",
            (checked, item_id),
        )
        updated = require_list_item(conn, item_id)
    return list_item_out(updated)


@app.post("/api/list-items/{item_id}/move", response_model=ListItemOut)
def move_list_item(item_id: int, payload: MoveIn) -> ListItemOut:
    with get_connection() as conn:
        row = require_list_item(conn, item_id)
        move_ordered_row(
            conn,
            "list_items",
            item_id,
            payload.direction,
            int(row["list_id"]),
        )
        updated = require_list_item(conn, item_id)
    return list_item_out(updated)


@app.delete("/api/list-items/{item_id}", status_code=204)
def delete_list_item(item_id: int) -> Response:
    with get_connection() as conn:
        require_list_item(conn, item_id)
        conn.execute("DELETE FROM list_items WHERE id = ?", (item_id,))
    return Response(status_code=204)


@app.get("/api/events", response_model=list[EventOccurrenceOut])
def list_events(
    start: str = Query(..., description="Range start, ISO 8601"),
    end: str = Query(..., description="Range end, ISO 8601, exclusive"),
) -> list[dict]:
    try:
        range_start = parse_local_datetime(start)
        range_end = parse_local_datetime(end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="start and end must be ISO 8601 datetimes") from exc
    if range_end <= range_start:
        raise HTTPException(status_code=400, detail="end must be after start")
    with get_connection() as conn:
        rows = conn.execute(EVENT_SQL + " ORDER BY e.start_time, e.id").fetchall()
        return expand_events(rows, range_start, range_end)


@app.post("/api/events", response_model=EventOut, status_code=201)
def create_event(payload: EventIn) -> EventOut:
    start_s, end_s, start_dt = canonical_event_times(payload)
    rrule = validated_rrule(payload, start_dt)
    with get_connection() as conn:
        require_member(conn, payload.member_id)
    try:
        written = google_calendar.push_create(event_write_payload(payload, start_s, end_s, rrule))
    except google_calendar.GoogleCalendarError as exc:
        raise_for_google(exc)
    member_id = payload.member_id if payload.member_id is not None else written.member_id
    updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with get_connection() as conn:
        require_member(conn, member_id)
        try:
            cursor = conn.execute(
                """
                INSERT INTO events (
                    google_event_id, calendar_id, member_id, title, description,
                    start_time, end_time, all_day, rrule, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    written.google_event_id,
                    written.calendar_id or "primary",
                    member_id,
                    payload.title,
                    payload.description,
                    start_s,
                    end_s,
                    1 if payload.all_day else 0,
                    rrule,
                    updated_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="An event with that Google id already exists",
            ) from exc
        row = require_event(conn, int(cursor.lastrowid))
    return event_out(row)


@app.put("/api/events/{event_id}", response_model=EventOut)
def update_event(event_id: int, payload: EventIn) -> EventOut:
    start_s, end_s, start_dt = canonical_event_times(payload)
    rrule = validated_rrule(payload, start_dt)
    with get_connection() as conn:
        existing = require_event(conn, event_id)
        require_member(conn, payload.member_id)
        existing_values = {key: existing[key] for key in existing.keys()}
    try:
        written = google_calendar.push_update(
            existing_values,
            event_write_payload(payload, start_s, end_s, rrule),
        )
    except google_calendar.GoogleCalendarError as exc:
        raise_for_google(exc)
    member_id = payload.member_id if payload.member_id is not None else written.member_id
    updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    calendar_id = written.calendar_id or existing_values["calendar_id"] or "primary"
    with get_connection() as conn:
        require_event(conn, event_id)
        require_member(conn, member_id)
        try:
            conn.execute(
                """
                UPDATE events
                SET google_event_id = ?, calendar_id = ?, member_id = ?, title = ?,
                    description = ?, start_time = ?, end_time = ?, all_day = ?,
                    rrule = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    written.google_event_id,
                    calendar_id,
                    member_id,
                    payload.title,
                    payload.description,
                    start_s,
                    end_s,
                    1 if payload.all_day else 0,
                    rrule,
                    updated_at,
                    event_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="An event with that Google id already exists",
            ) from exc
        row = require_event(conn, event_id)
    return event_out(row)


@app.delete("/api/events/{event_id}", status_code=204)
def delete_event(event_id: int) -> Response:
    with get_connection() as conn:
        existing = require_event(conn, event_id)
        existing_values = {key: existing[key] for key in existing.keys()}
    try:
        google_calendar.push_delete(existing_values)
    except google_calendar.GoogleCalendarError as exc:
        raise_for_google(exc)
    with get_connection() as conn:
        require_event(conn, event_id)
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return Response(status_code=204)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_PATH, headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000)
