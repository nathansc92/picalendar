"""Family hub kiosk API. Phase 1: members, chores, assets, and maintenance."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from database import (
    classify_due,
    compute_next_due,
    get_connection,
    init_db,
    shift_date,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_PATH = STATIC_DIR / "index.html"
STARTED_AT = datetime.now(timezone.utc).isoformat()

IntervalUnit = Literal["days", "weeks", "months", "years"]
DueStatus = Literal["overdue", "due_soon", "ok"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


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


CHORE_SQL = """
SELECT c.id, c.title, c.notes, c.family_member_id, c.due_date, c.completed,
       c.completed_at, c.created_at, m.name AS member_name, m.color AS member_color
FROM chores c
LEFT JOIN family_members m ON m.id = c.family_member_id
"""

TASK_SQL = """
SELECT t.id, t.asset_id, t.title, t.notes, t.interval_value, t.interval_unit,
       t.last_done_date, t.next_due_date, t.created_at,
       a.name AS asset_name, a.location AS asset_location
FROM maintenance_tasks t
JOIN assets a ON a.id = t.asset_id
"""


def member_out(row: sqlite3.Row) -> FamilyMemberOut:
    return FamilyMemberOut(
        id=row["id"],
        name=row["name"],
        color=row["color"],
        created_at=row["created_at"],
    )


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


def require_asset_row(conn: sqlite3.Connection, asset_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return row


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


def iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


@app.get("/api/version")
def version(response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"started_at": STARTED_AT}


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


@app.put("/api/family-members/{member_id}", response_model=FamilyMemberOut)
def update_member(member_id: int, payload: FamilyMemberIn) -> FamilyMemberOut:
    with get_connection() as conn:
        require_member_row(conn, member_id)
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_PATH, headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000)
