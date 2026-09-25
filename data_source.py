"""
Reads the "Course Calendar (2027-2028)" Google Sheet and turns it into the
same JSON shape the dashboard's front end expects: a flat list of session
rows (pulled from every tab except MIS/Mastersheet) plus a courses_overview
list (pulled from the MIS tab).

This mirrors, column-for-column, the parsing logic originally written in
JavaScript (SheetJS) for the Claude-artifact version of this dashboard,
including the same data-quality workarounds:

- a date-only cell comes back from Google Sheets as a serial day-count
  (days since 1899-12-30, the same epoch Excel uses) when we ask for
  UNFORMATTED_VALUE -- converted to an ISO date with plain arithmetic
- the FRCR "Mini Mocks" row types a date *range* across cells that are
  normally single values (start date / time / session-no columns), which
  shifts the column mapping for that one row -- detected by checking
  whether the "time" cell looks like a clock time or not
- confirmation status spelling varies ("Confrimed" vs "Confirmed") and a
  blank cell means "not yet updated" (Pending), not "TBC"
- mentor names carry placeholders (TBC/NA/N/A/blank) and inconsistent
  formatting (whitespace, periods, case) for the same person
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

EXCEL_EPOCH = date(1899, 12, 30)  # same serial-date epoch Excel and Google Sheets both use

IGNORED_TABS = {"MIS", "Mastersheet"}


class SheetAccessError(Exception):
    """Raised with a stable .code the front end can map to a friendly message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _get_credentials() -> Credentials:
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    try:
        if raw_json:
            import json

            info = json.loads(raw_json)
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        if os.path.exists(key_path):
            return Credentials.from_service_account_file(key_path, scopes=SCOPES)
    except Exception as exc:  # noqa: BLE001 - surfaced as a stable error code below
        raise SheetAccessError(
            "bad_credentials", f"Service account credentials could not be read: {exc}"
        ) from exc
    raise SheetAccessError(
        "missing_credentials",
        "No Google service account credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON "
        "or GOOGLE_SERVICE_ACCOUNT_FILE (see README.md).",
    )


def _open_sheet(sheet_id: str):
    creds = _get_credentials()
    client = gspread.authorize(creds)
    try:
        return client.open_by_key(sheet_id)
    except gspread.exceptions.APIError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            raise SheetAccessError(
                "sheet_not_found",
                "Google Sheets returned 404 for that spreadsheet ID. Double-check "
                "GOOGLE_SHEET_ID.",
            ) from exc
        if status == 403:
            raise SheetAccessError(
                "permission_denied",
                "Google Sheets refused access (403). Share the spreadsheet with the "
                "service account's email address (found inside your JSON key, "
                "\"client_email\") as at least Viewer.",
            ) from exc
        raise SheetAccessError("api_error", f"Google Sheets API error: {exc}") from exc


def _clean_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        t = v.strip()
        return t if t else None
    return str(v)


def _code_of(name: str) -> str:
    return name.split(" - ")[0].strip()


def _date_to_iso(v: Any) -> str | None:
    """A raw Google Sheets date-serial (int/float, UNFORMATTED_VALUE) -> 'YYYY-MM-DD'."""
    if v is None or v == "":
        return None
    try:
        serial = float(v)
    except (TypeError, ValueError):
        return None
    d = EXCEL_EPOCH + timedelta(days=serial)
    return d.strftime("%Y-%m-%d")


def _norm_status(v: Any) -> str:
    if v is None:
        return "Pending"
    s = str(v).strip()
    if s == "":
        return "Pending"
    low = s.lower()
    if "confr" in low or "confi" in low:
        return "Confirmed"
    if s.upper() == "TBC":
        return "TBC"
    return s


_TIME_RE = re.compile(r"\d{1,2}[:.]\d{2}")


def _looks_like_time(raw: Any) -> bool:
    if raw is None:
        return False
    s = str(raw)
    return bool(_TIME_RE.search(s)) or "ist" in s.lower()


def _parse_course_sheet(course_rows: list[list[Any]]) -> list[dict]:
    """course_rows: raw UNFORMATTED_VALUE rows for one per-course tab."""
    if len(course_rows) < 3:
        return []
    course_title = _clean_str(course_rows[0][0] if course_rows[0] else None)
    if not course_title:
        return []
    code = _code_of(course_title)

    def cell(row: list[Any], idx: int) -> Any:
        return row[idx] if idx < len(row) else None

    sessions: list[dict] = []
    for row in course_rows[2:]:
        sl_raw = cell(row, 0)
        if sl_raw is None or sl_raw == "":
            continue
        try:
            sl = int(float(sl_raw))
        except (TypeError, ValueError):
            continue

        time_raw = cell(row, 3)
        is_time_like = _looks_like_time(time_raw) or time_raw is None or time_raw == ""

        if is_time_like:
            sess_date = _date_to_iso(cell(row, 2))
            date_end = None
            time_val = _clean_str(time_raw)
            session_no = _clean_str(cell(row, 4))
            session_name = _clean_str(cell(row, 5))
            mentor = _clean_str(cell(row, 6))
            status = _norm_status(cell(row, 7))
            remarks = _clean_str(cell(row, 8))
        else:
            # date-range row (e.g. "Mini Mocks"): columns shift by one
            sess_date = _date_to_iso(cell(row, 2))
            date_end = _date_to_iso(cell(row, 4))
            time_val = None
            session_no = None
            session_name = _clean_str(cell(row, 5))
            mentor = _clean_str(cell(row, 6))
            status = _norm_status(cell(row, 7))
            remarks = _clean_str(cell(row, 8))

        sessions.append(
            {
                "course": course_title,
                "code": code,
                "sl": sl,
                "date": sess_date,
                "date_end": date_end,
                "time": time_val,
                "session_no": session_no,
                "session_name": session_name,
                "mentor": mentor,
                "status": status,
                "remarks": remarks,
            }
        )
    return sessions


def _parse_mis_sheet(mis_rows: list[list[Any]]) -> list[dict]:
    overview: list[dict] = []

    def cell(row: list[Any], idx: int) -> Any:
        return row[idx] if idx < len(row) else None

    for row in mis_rows[1:]:
        name = _clean_str(cell(row, 1))
        if not name:
            continue
        overview.append(
            {
                "sl": cell(row, 0),
                "course": name,
                "code": _code_of(name),
                "coordinator": _clean_str(cell(row, 2)),
                "team_lead": _clean_str(cell(row, 3)),
                "start": _date_to_iso(cell(row, 4)),
                "end": _date_to_iso(cell(row, 5)),
                "status": _clean_str(cell(row, 6)) or "Not Started",
            }
        )
    return overview


def fetch_workbook_data(sheet_id: str) -> dict:
    """Live-fetches the whole spreadsheet and returns {generated, sessions, courses_overview}."""
    spreadsheet = _open_sheet(sheet_id)
    worksheets = spreadsheet.worksheets()

    sessions: list[dict] = []
    courses_overview: list[dict] = []

    for ws in worksheets:
        title = ws.title
        try:
            rows = ws.get_values(value_render_option="UNFORMATTED_VALUE")
        except gspread.exceptions.APIError as exc:
            raise SheetAccessError("api_error", f"Could not read tab '{title}': {exc}") from exc

        if title == "MIS":
            courses_overview = _parse_mis_sheet(rows)
        elif title in IGNORED_TABS:
            continue
        else:
            sessions.extend(_parse_course_sheet(rows))

    if not sessions:
        raise SheetAccessError(
            "no_sessions_found",
            "Connected to the spreadsheet, but no recognizable session rows were found "
            "in any tab. Check that the per-course tabs still follow the expected layout.",
        )

    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "sessions": sessions,
        "courses_overview": courses_overview,
    }
