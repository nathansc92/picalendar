"""Multi-account Google Calendar OAuth and two-way sync.

Each family member keeps a refresh token in ``tokens/{member_id}.json``.
Reads use incremental ``syncToken`` polling. Creates, updates, and deletes
reach Google before SQLite changes. A down network raises ``GoogleOffline``
so calendar edits can pause while chores and maintenance stay local.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from database import get_connection

logger = logging.getLogger("family_hub.google")

# The kiosk's first login uses http://localhost through an SSH tunnel.
# oauthlib refuses that redirect unless local HTTP is explicitly allowed.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# Google sometimes returns extra scopes on the token response.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
OFFLINE_DETAIL = "Offline – Calendar edits paused"
FULL_SYNC_DAYS = 30

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKENS_DIR = BASE_DIR / "tokens"

OFFLINE_HTTP = {408, 500, 502, 503, 504}

_status_lock = threading.Lock()
_status = {
    "online": True,
    "last_synced_at": None,
    "detail": "Waiting for the first sync.",
    "syncing": False,
}
_sync_lock = threading.Lock()
_locks_guard = threading.Lock()
_member_locks: dict[int, threading.RLock] = {}
_pending_lock = threading.Lock()
_pending: dict[str, dict] = {}
_tz_cache: dict[tuple[int, str], tuple[float, str]] = {}


class GoogleCalendarError(Exception):
    """Expected failure while talking to Google Calendar."""


class GoogleOffline(GoogleCalendarError):
    """Google could not be reached."""


class GoogleNotLinked(GoogleCalendarError):
    """The event's family member has no usable Google account."""


class GoogleAuthError(GoogleCalendarError):
    """The saved login is missing or Google rejected it."""


class GoogleConfigError(GoogleCalendarError):
    """The OAuth client file is missing or unreadable."""


class GoogleRequestError(GoogleCalendarError):
    """Google answered, but rejected the calendar request."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SyncTokenExpired(GoogleCalendarError):
    """Google returned 410 for an incremental sync token."""


@dataclass
class WriteResult:
    google_event_id: str | None
    calendar_id: str
    member_id: int | None


def sync_state_key(member_id: int, calendar_id: str) -> str:
    """Key ``sync_state`` so two ``primary`` calendars do not share a token.

    ``primary`` is an alias for whichever account is calling. A shared
    calendar id (an email or group calendar) stays as itself.
    """
    cleaned = (calendar_id or "").strip() or "primary"
    if cleaned.lower() == "primary":
        return f"member-{int(member_id)}:primary"
    return cleaned


def member_is_linked(row) -> bool:
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    calendar_id = (row["google_calendar_id"] or "").strip() if "google_calendar_id" in keys else ""
    token_file = (row["token_file"] or "").strip() if "token_file" in keys else ""
    path = _safe_token_path(token_file) if token_file else None
    return bool(calendar_id and path and path.is_file())


def current_status() -> dict:
    with _status_lock:
        snapshot = {
            "online": bool(_status["online"]),
            "last_synced_at": _status["last_synced_at"],
            "detail": _status["detail"] or "",
            "syncing": bool(_status["syncing"]),
        }
    if not snapshot["last_synced_at"]:
        snapshot["last_synced_at"] = _latest_sync_time()
    snapshot["linked_count"] = sum(1 for member in _member_records() if member["linked"])
    snapshot["credentials_present"] = CREDENTIALS_PATH.is_file()
    if snapshot["linked_count"] == 0:
        snapshot["online"] = True
    return snapshot


def authorization_url(member_id: int, redirect_uri: str) -> str:
    """Build the Google consent URL for one family member."""
    if not CREDENTIALS_PATH.is_file():
        raise GoogleConfigError(
            "credentials.json is not next to main.py. Download the OAuth client file from Google Cloud Console first."
        )
    try:
        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_PATH),
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
    except (OSError, ValueError, UnicodeError) as exc:
        raise GoogleConfigError("credentials.json is not a valid Google OAuth client file.") from exc
    state = secrets.token_urlsafe(32)
    url, returned_state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    with _pending_lock:
        _prune_pending()
        _pending[returned_state] = {
            "member_id": int(member_id),
            "redirect_uri": redirect_uri,
            "code_verifier": flow.code_verifier,
            "created": time.time(),
        }
    return url


def finish_oauth(state: str | None, code: str | None) -> int:
    """Exchange the callback code and store ``tokens/{member_id}.json``."""
    if not state or not code:
        raise GoogleAuthError("Google did not return a login code. Try linking again.")
    with _pending_lock:
        _prune_pending()
        pending = _pending.pop(state, None)
    if not pending:
        raise GoogleAuthError("That Google login expired. Start it again from Settings.")
    if not CREDENTIALS_PATH.is_file():
        raise GoogleConfigError("credentials.json is not next to main.py.")
    try:
        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_PATH),
            scopes=SCOPES,
            redirect_uri=pending["redirect_uri"],
            state=state,
            code_verifier=pending["code_verifier"],
            autogenerate_code_verifier=False,
        )
        flow.fetch_token(code=code)
    except GoogleCalendarError:
        raise
    except Exception as exc:
        if _is_offline(exc):
            note_offline()
            raise GoogleOffline(OFFLINE_DETAIL) from exc
        raise GoogleAuthError("Google did not accept that login. Try linking again.") from exc

    creds = flow.credentials
    member_id = int(pending["member_id"])
    path = TOKENS_DIR / f"{member_id}.json"
    _keep_existing_refresh_token(path, creds)
    if not creds.refresh_token:
        raise GoogleAuthError(
            "Google did not grant offline access. Link the account again and allow calendar access."
        )
    _write_token(path, creds)
    relative = f"tokens/{member_id}.json"
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, google_calendar_id FROM family_members WHERE id = ?",
            (member_id,),
        ).fetchone()
        if row is None:
            raise GoogleAuthError("That family member no longer exists.")
        calendar_id = (row["google_calendar_id"] or "").strip() or "primary"
        conn.execute(
            """
            UPDATE family_members
            SET token_file = ?, google_calendar_id = ?
            WHERE id = ?
            """,
            (relative, calendar_id, member_id),
        )
    with _status_lock:
        _status["online"] = True
        _status["detail"] = "Google account linked. Sync to pull events."
    return member_id


def unlink_member(member_id: int) -> None:
    """Forget a member's token and the sync cursor for their calendar."""
    with _lock_for(member_id):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT google_calendar_id, token_file FROM family_members WHERE id = ?",
                (member_id,),
            ).fetchone()
            if row is None:
                raise GoogleNotLinked("Family member not found.")
            calendar_id = (row["google_calendar_id"] or "").strip()
            token_file = row["token_file"] or ""
            conn.execute(
                "UPDATE family_members SET token_file = NULL WHERE id = ?",
                (member_id,),
            )
            keys = {sync_state_key(member_id, calendar_id or "primary")}
            if calendar_id:
                keys.add(calendar_id)
            conn.executemany(
                "DELETE FROM sync_state WHERE calendar_id = ?",
                [(key,) for key in keys],
            )
        for candidate in {_safe_token_path(token_file), TOKENS_DIR / f"{member_id}.json"}:
            if candidate is not None and candidate.is_file():
                candidate.unlink()
    if not any(member["linked"] for member in _member_records()):
        _update_status(online=True, detail="No Google accounts linked yet.", syncing=False)


def sync_all_calendars() -> dict:
    """Pull every linked calendar. Safe to call from the scheduler and a button."""
    if not _sync_lock.acquire(blocking=False):
        status = current_status()
        status["syncing"] = True
        return status
    try:
        try:
            _set_syncing(True)
            members = [
                member
                for member in _member_records()
                if member["google_calendar_id"] and member["token_file"]
            ]
            if not members:
                _update_status(online=True, detail="No Google accounts linked yet.", syncing=False)
                return current_status()

            synced = 0
            network_failures = 0
            problems: list[str] = []
            seen: set[str] = set()
            for member in members:
                key = sync_state_key(member["id"], member["google_calendar_id"])
                if key in seen:
                    continue
                seen.add(key)
                try:
                    _sync_member(member)
                    synced += 1
                except GoogleOffline:
                    network_failures += 1
                    logger.warning("Google Calendar was unreachable for %s", member["name"])
                except GoogleCalendarError as exc:
                    problems.append(f"{member['name']}: {exc}")
                    logger.warning("Sync failed for %s: %s", member["name"], exc)
                except Exception:
                    logger.exception("Unexpected sync failure for %s", member["name"])
                    problems.append(f"{member['name']}: sync failed")

            last = _latest_sync_time()
            if network_failures:
                _update_status(
                    online=False,
                    detail=OFFLINE_DETAIL,
                    last_synced_at=last,
                    syncing=False,
                )
            else:
                if problems and synced == 0:
                    detail = problems[0]
                elif problems:
                    detail = f"Synced {synced} calendar{'s' if synced != 1 else ''}. {problems[0]}"
                else:
                    detail = f"Synced {synced} calendar{'s' if synced != 1 else ''}."
                _update_status(online=True, detail=detail, last_synced_at=last, syncing=False)
            return current_status()
        except Exception:
            logger.exception("Google Calendar sync failed")
            _update_status(online=False, detail=OFFLINE_DETAIL, syncing=False)
            return current_status()
    finally:
        _sync_lock.release()


def push_create(event_data: dict) -> WriteResult:
    """Insert on Google when an account is linked. Local-only when none are."""
    _block_if_offline()
    linked = [member for member in _member_records() if member["linked"]]
    if not linked:
        return WriteResult(
            google_event_id=event_data.get("google_event_id"),
            calendar_id=(event_data.get("calendar_id") or "").strip() or "primary",
            member_id=event_data.get("member_id"),
        )
    account, calendar_id = _account_for_create(event_data, linked)
    with _lock_for(account["id"]):
        service = _service_for(account)
        zone = _calendar_timezone(service, account["id"], calendar_id)
        body = _event_body(event_data, zone, clear_missing_rule=False)
        created = _execute(
            service.events().insert(calendarId=calendar_id, body=body, sendUpdates="none")
        )
    google_id = created.get("id") if isinstance(created, dict) else None
    if not google_id:
        raise GoogleRequestError("Google Calendar did not return an event id.")
    note_reachable()
    member_id = event_data.get("member_id")
    if member_id is None:
        member_id = account["id"]
    return WriteResult(google_id, calendar_id, member_id)


def push_update(existing: dict, event_data: dict) -> WriteResult:
    """Patch the Google event, or insert it when the local row was never synced."""
    _block_if_offline()
    linked = [member for member in _member_records() if member["linked"]]
    if not linked:
        google_id = event_data.get("google_event_id") or existing.get("google_event_id")
        calendar_id = (event_data.get("calendar_id") or existing.get("calendar_id") or "primary")
        return WriteResult(google_id, str(calendar_id).strip() or "primary", event_data.get("member_id"))
    if not existing.get("google_event_id"):
        return push_create(event_data)

    account, calendar_id = _account_for_existing(existing, linked)
    google_id = existing["google_event_id"]
    with _lock_for(account["id"]):
        service = _service_for(account)
        zone = _calendar_timezone(service, account["id"], calendar_id)
        body = _event_body(event_data, zone, clear_missing_rule=True)
        updated = _execute(
            service.events().patch(
                calendarId=calendar_id,
                eventId=google_id,
                body=body,
                sendUpdates="none",
            )
        )
    note_reachable()
    returned = updated.get("id") if isinstance(updated, dict) else None
    return WriteResult(returned or google_id, calendar_id, event_data.get("member_id"))


def push_delete(existing: dict) -> None:
    """Delete on Google when this row has a Google id and an account is linked."""
    _block_if_offline()
    linked = [member for member in _member_records() if member["linked"]]
    google_id = existing.get("google_event_id")
    if not linked or not google_id:
        return
    account, calendar_id = _account_for_existing(existing, linked)
    with _lock_for(account["id"]):
        service = _service_for(account)
        _execute(
            service.events().delete(
                calendarId=calendar_id,
                eventId=google_id,
                sendUpdates="none",
            ),
            allow_missing=True,
        )
    note_reachable()


def _sync_member(member: dict) -> None:
    path = member["token_path"]
    if path is None or not path.is_file():
        raise GoogleAuthError(f"{member['name']} needs to link Google again.")
    calendar_id = member["google_calendar_id"]
    key = sync_state_key(member["id"], calendar_id)
    with _lock_for(member["id"]):
        service = _service_for(member)
        previous = _read_sync_token(key)
        items, new_token = _list_events(service, calendar_id, previous)
        _apply_items(member["id"], calendar_id, items, key, new_token, previous)


def _list_events(service, calendar_id: str, sync_token: str | None, *, retried: bool = False):
    """List one calendar. A 410 sync token triggers one full sync from 30 days ago."""
    params: dict = {
        "calendarId": calendar_id,
        "showDeleted": True,
        "maxResults": 250,
    }
    if sync_token:
        params["syncToken"] = sync_token
    else:
        params["timeMin"] = _full_sync_min()
    items: list = []
    while True:
        try:
            result = _execute(service.events().list(**params))
        except SyncTokenExpired:
            if sync_token and not retried:
                logger.info("Sync token expired for %s; running a full sync", calendar_id)
                return _list_events(service, calendar_id, None, retried=True)
            raise GoogleRequestError("Google Calendar could not resume the previous sync.")
        if not isinstance(result, dict):
            raise GoogleRequestError("Google Calendar returned an unexpected event list.")
        items.extend(result.get("items") or [])
        page = result.get("nextPageToken")
        if not page:
            return items, result.get("nextSyncToken")
        params["pageToken"] = page


def _apply_items(
    member_id: int,
    calendar_id: str,
    items: list,
    sync_key: str,
    sync_token: str | None,
    previous_token: str | None,
) -> None:
    token = sync_token or previous_token
    synced_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with get_connection() as conn:
        for item in items:
            change = _change_from_item(item)
            if change is None:
                continue
            kind, payload = change
            if kind == "delete":
                conn.execute("DELETE FROM events WHERE google_event_id = ?", (payload,))
                continue
            conn.execute(
                """
                INSERT INTO events (
                    google_event_id, calendar_id, member_id, title, description,
                    start_time, end_time, all_day, rrule, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(google_event_id) DO UPDATE SET
                    calendar_id = excluded.calendar_id,
                    member_id = COALESCE(events.member_id, excluded.member_id),
                    title = excluded.title,
                    description = excluded.description,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    all_day = excluded.all_day,
                    rrule = excluded.rrule,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["google_event_id"],
                    calendar_id,
                    member_id,
                    payload["title"],
                    payload["description"],
                    payload["start_time"],
                    payload["end_time"],
                    payload["all_day"],
                    payload["rrule"],
                    payload["updated_at"],
                ),
            )
        conn.execute(
            """
            INSERT INTO sync_state (calendar_id, sync_token, last_synced_at)
            VALUES (?, ?, ?)
            ON CONFLICT(calendar_id) DO UPDATE SET
                sync_token = excluded.sync_token,
                last_synced_at = excluded.last_synced_at
            """,
            (sync_key, token, synced_at),
        )


def _change_from_item(item: dict):
    """Turn one Google event into a delete, an upsert, or a skip."""
    if not isinstance(item, dict):
        return None
    event_id = item.get("id")
    if not event_id:
        return None
    if item.get("status") == "cancelled":
        return ("delete", event_id)
    # Instances of a series would duplicate the expanded master row.
    if item.get("recurringEventId"):
        return None
    start = item.get("start") or {}
    end = item.get("end") or {}
    try:
        if start.get("date"):
            all_day = 1
            start_time = str(start["date"])[:10]
            end_time = str(end.get("date") or "")[:10]
            if not end_time or end_time <= start_time:
                end_time = _shift_date(start_time, 1)
        elif start.get("dateTime"):
            all_day = 0
            start_time = _to_local_naive(str(start["dateTime"]))
            if end.get("dateTime"):
                end_time = _to_local_naive(str(end["dateTime"]))
            else:
                end_time = _add_hours(start_time, 1)
            if end_time <= start_time:
                return None
        else:
            return None
    except (TypeError, ValueError):
        logger.warning("Skipped Google event %s because its times were unreadable", event_id)
        return None
    title = (item.get("summary") or "").strip() or "(No title)"
    description = item.get("description") or ""
    if not isinstance(description, str):
        description = str(description)
    updated = item.get("updated") or datetime.now().astimezone().isoformat(timespec="seconds")
    return (
        "upsert",
        {
            "google_event_id": event_id,
            "title": title,
            "description": description,
            "start_time": start_time,
            "end_time": end_time,
            "all_day": all_day,
            "rrule": _rrule_from_event(item),
            "updated_at": updated,
        },
    )


def _rrule_from_event(item: dict) -> str | None:
    for entry in item.get("recurrence") or []:
        text = str(entry).strip()
        if text.upper().startswith("RRULE:"):
            rule = text.split(":", 1)[1].strip()
            return rule or None
    return None


def _event_body(event_data: dict, time_zone: str, *, clear_missing_rule: bool) -> dict:
    body: dict = {
        "summary": event_data["title"],
        "description": event_data.get("description") or "",
    }
    if event_data.get("all_day"):
        body["start"] = {"date": str(event_data["start_time"])[:10]}
        body["end"] = {"date": str(event_data["end_time"])[:10]}
    else:
        body["start"] = {
            "dateTime": _wall_time(str(event_data["start_time"])),
            "timeZone": time_zone,
        }
        body["end"] = {
            "dateTime": _wall_time(str(event_data["end_time"])),
            "timeZone": time_zone,
        }
    rule = (event_data.get("rrule") or "").strip()
    if rule.upper().startswith("RRULE:"):
        rule = rule.split(":", 1)[1].strip()
    if rule:
        body["recurrence"] = [f"RRULE:{rule}"]
    elif clear_missing_rule:
        body["recurrence"] = []
    return body


def _account_for_create(event_data: dict, linked: list[dict]) -> tuple[dict, str]:
    member_id = event_data.get("member_id")
    if member_id is not None:
        account = next((row for row in linked if row["id"] == member_id), None)
        if account is None:
            raise GoogleNotLinked(
                "Link this person's Google account in Settings before saving calendar events."
            )
    elif len(linked) == 1:
        account = linked[0]
    else:
        raise GoogleNotLinked(
            "Choose a family member so this event is saved to their Google Calendar."
        )
    requested = (event_data.get("calendar_id") or "").strip()
    calendar_id = requested or account["google_calendar_id"] or "primary"
    return account, calendar_id


def _account_for_existing(existing: dict, linked: list[dict]) -> tuple[dict, str]:
    calendar_id = (existing.get("calendar_id") or "").strip() or "primary"
    member_id = existing.get("member_id")
    if member_id is not None:
        account = next((row for row in linked if row["id"] == member_id), None)
        if account is not None:
            return account, calendar_id
    if calendar_id.lower() != "primary":
        owners = [row for row in linked if row["google_calendar_id"] == calendar_id]
        if owners:
            return owners[0], calendar_id
    raise GoogleNotLinked("Link the Google account for this event in Settings before changing it.")


def _service_for(account: dict):
    path = account["token_path"]
    if path is None or not path.is_file():
        raise GoogleAuthError(f"{account['name']} needs to link Google again.")
    creds = _load_credentials(path)
    return _build_service(creds)


def _load_credentials(path: Path) -> Credentials:
    if not path.is_file():
        raise GoogleAuthError("Google account is not linked. Use Settings to connect it.")
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except (OSError, ValueError) as exc:
        raise GoogleAuthError("Saved Google login could not be read. Link the account again.") from exc
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(AuthRequest())
        except RefreshError as exc:
            raise GoogleAuthError("Google rejected the saved login. Link the account again.") from exc
        except Exception as exc:
            if _is_offline(exc):
                note_offline()
                raise GoogleOffline(OFFLINE_DETAIL) from exc
            raise
        _write_token(path, creds)
        return creds
    raise GoogleAuthError("Google account needs to be linked again.")


def _build_service(creds: Credentials):
    try:
        authed = AuthorizedHttp(creds, http=httplib2.Http(timeout=20))
        return build("calendar", "v3", http=authed, cache_discovery=False)
    except GoogleCalendarError:
        raise
    except Exception as exc:
        if _is_offline(exc) or _looks_offline(exc):
            note_offline()
            raise GoogleOffline(OFFLINE_DETAIL) from exc
        raise


def _calendar_timezone(service, member_id: int, calendar_id: str) -> str:
    cache_key = (member_id, calendar_id)
    cached = _tz_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < 3600:
        return cached[1]
    result = _execute(service.calendars().get(calendarId=calendar_id))
    zone = "UTC"
    if isinstance(result, dict):
        zone = (result.get("timeZone") or "UTC").strip() or "UTC"
    _tz_cache[cache_key] = (now, zone)
    return zone


def _execute(request, *, allow_missing: bool = False):
    try:
        return request.execute()
    except HttpError as exc:
        status = _http_status(exc)
        if allow_missing and status in {404, 410}:
            return None
        if status == 410:
            raise SyncTokenExpired("Google sync token expired") from exc
        if status in OFFLINE_HTTP:
            note_offline()
            raise GoogleOffline(OFFLINE_DETAIL) from exc
        if status in {401, 403}:
            raise GoogleAuthError("Google rejected this account. Link it again in Settings.") from exc
        raise GoogleRequestError(_http_message(exc), status) from exc
    except Exception as exc:
        if isinstance(exc, GoogleCalendarError):
            raise
        if _is_offline(exc) or _looks_offline(exc):
            note_offline()
            raise GoogleOffline(OFFLINE_DETAIL) from exc
        raise


def _block_if_offline() -> None:
    if not any(member["linked"] for member in _member_records()):
        return
    with _status_lock:
        online = bool(_status["online"])
    if not online:
        raise GoogleOffline(OFFLINE_DETAIL)


def note_offline() -> None:
    with _status_lock:
        _status["online"] = False
        _status["detail"] = OFFLINE_DETAIL


def note_reachable() -> None:
    with _status_lock:
        _status["online"] = True
        if _status["detail"] == OFFLINE_DETAIL:
            _status["detail"] = "Google Calendar is reachable."


def _update_status(
    *,
    online: bool,
    detail: str,
    syncing: bool,
    last_synced_at: str | None = None,
) -> None:
    with _status_lock:
        _status["online"] = online
        _status["detail"] = detail
        _status["syncing"] = syncing
        if last_synced_at:
            _status["last_synced_at"] = last_synced_at


def _set_syncing(value: bool) -> None:
    with _status_lock:
        _status["syncing"] = value


def _member_records() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, google_calendar_id, token_file
            FROM family_members
            ORDER BY id
            """
        ).fetchall()
    records = []
    for row in rows:
        token_file = (row["token_file"] or "").strip()
        calendar_id = (row["google_calendar_id"] or "").strip()
        path = _safe_token_path(token_file) if token_file else None
        records.append(
            {
                "id": int(row["id"]),
                "name": row["name"],
                "google_calendar_id": calendar_id,
                "token_file": token_file,
                "token_path": path,
                "linked": bool(calendar_id and path and path.is_file()),
            }
        )
    return records


def _read_sync_token(key: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT sync_token FROM sync_state WHERE calendar_id = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    token = (row["sync_token"] or "").strip()
    return token or None


def _latest_sync_time() -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(last_synced_at) AS ts FROM sync_state").fetchone()
    if row is None:
        return None
    return row["ts"]


def _safe_token_path(token_file: str) -> Path | None:
    raw = token_file.replace("\\", "/").strip()
    if not raw or raw.startswith("/") or ".." in raw.split("/"):
        return None
    path = (BASE_DIR / raw).resolve()
    try:
        path.relative_to(TOKENS_DIR.resolve())
    except ValueError:
        return None
    return path


def _write_token(path: Path, creds: Credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(creds.to_json(), encoding="utf-8")
    temporary.replace(path)


def _keep_existing_refresh_token(path: Path, creds: Credentials) -> None:
    if creds.refresh_token or not path.is_file():
        return
    try:
        previous = Credentials.from_authorized_user_file(str(path), SCOPES)
    except (OSError, ValueError):
        return
    if previous.refresh_token:
        creds.refresh_token = previous.refresh_token


def _lock_for(member_id: int) -> threading.RLock:
    with _locks_guard:
        lock = _member_locks.get(member_id)
        if lock is None:
            lock = threading.RLock()
            _member_locks[member_id] = lock
        return lock


def _prune_pending() -> None:
    cutoff = time.time() - 900
    stale = [key for key, value in _pending.items() if value.get("created", 0) < cutoff]
    for key in stale:
        _pending.pop(key, None)


def _full_sync_min() -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=FULL_SYNC_DAYS)
    return moment.isoformat().replace("+00:00", "Z")


def _to_local_naive(value: str) -> str:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def _wall_time(value: str) -> str:
    text = value.strip()
    if "T" not in text:
        return text[:10]
    return _to_local_naive(text)


def _shift_date(iso_date: str, days: int) -> str:
    return (date.fromisoformat(iso_date[:10]) + timedelta(days=days)).isoformat()


def _add_hours(local_stamp: str, hours: int) -> str:
    parsed = datetime.fromisoformat(local_stamp) + timedelta(hours=hours)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _http_status(exc: HttpError) -> int:
    status = getattr(exc, "status_code", None)
    if status is None and getattr(exc, "resp", None) is not None:
        status = getattr(exc.resp, "status", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return 0


def _http_message(exc: HttpError) -> str:
    status = _http_status(exc)
    reason = getattr(exc, "reason", None) or "request failed"
    text = str(reason).strip().splitlines()[0][:180]
    return f"Google Calendar returned {status}: {text}"


def _is_offline(exc: BaseException) -> bool:
    if isinstance(exc, GoogleCalendarError):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, TransportError, httplib2.HttpLib2Error)):
        return True
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    if module.startswith("requests") and name in {
        "ConnectionError",
        "Timeout",
        "ConnectTimeout",
        "ReadTimeout",
    }:
        return True
    if isinstance(exc, OSError) and not isinstance(
        exc, (PermissionError, FileNotFoundError, IsADirectoryError)
    ):
        return True
    return False


def _looks_offline(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "unable to find the server",
        "name or service not known",
        "getaddrinfo",
        "network is unreachable",
        "temporary failure in name resolution",
        "connection aborted",
        "connection reset",
    )
    return any(marker in text for marker in markers)
