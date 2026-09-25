"""
Course Calendar Analytics — Flask webapp.

Serves the same dashboard UI that was built as a Claude artifact, but reads
its data by connecting directly to a Google Sheet with a service account,
instead of asking a viewer's Google Drive connector for the file.

Routes:
  GET  /                -> the dashboard page, with the current cached data
                            embedded so it renders instantly with no extra request
  POST /api/refresh      -> re-fetch the sheet now, update the cache, return the
                            fresh data as JSON (also refreshes the CSV export cache)
  GET  /api/data         -> return the currently cached data as JSON
  GET  /api/export.csv   -> a server-rendered CSV of the (optionally filtered)
                            sessions, honoring the same filters as the UI

Configuration (see .env.example / README.md):
  GOOGLE_SHEET_ID              spreadsheet ID from its URL
  GOOGLE_SERVICE_ACCOUNT_JSON  the service account key, as a JSON string
  GOOGLE_SERVICE_ACCOUNT_FILE  or, instead, a path to the key file (default:
                                service_account.json next to this file)
  AUTO_REFRESH_MINUTES         if set, background auto-refresh interval
  PORT / HOST / FLASK_DEBUG    ordinary Flask server settings
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request

import data_source

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FALLBACK_PATH = os.path.join(BASE_DIR, "fallback_data.json")

app = Flask(__name__)

_cache_lock = threading.Lock()
_cache = {
    "sessions": [],
    "courses_overview": [],
    "generated": None,
    "source": "fallback",  # "live" | "fallback"
    "error": None,
    "last_synced": None,  # ISO timestamp of the last successful live sync
}


def _load_fallback() -> dict:
    with open(FALLBACK_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        "sessions": data.get("sessions", []),
        "courses_overview": data.get("courses_overview", []),
        "generated": data.get("generated"),
    }


def _sheet_id() -> str | None:
    return os.environ.get("GOOGLE_SHEET_ID")


def refresh_cache(is_startup: bool = False) -> dict:
    """Tries a live fetch; on any failure, falls back to the bundled snapshot
    (only on startup, or if we've never synced live before) or keeps
    whatever is already cached (on a manual/auto refresh after a good sync)."""
    sheet_id = _sheet_id()
    if not sheet_id:
        with _cache_lock:
            if _cache["generated"] is None:
                _cache.update(_load_fallback())
                _cache["source"] = "fallback"
            _cache["error"] = (
                "GOOGLE_SHEET_ID is not set — showing the bundled demo snapshot. "
                "See README.md to connect a live spreadsheet."
            )
            return dict(_cache)

    try:
        result = data_source.fetch_workbook_data(sheet_id)
        with _cache_lock:
            _cache["sessions"] = result["sessions"]
            _cache["courses_overview"] = result["courses_overview"]
            _cache["generated"] = result["generated"]
            _cache["source"] = "live"
            _cache["error"] = None
            _cache["last_synced"] = datetime.now(timezone.utc).isoformat()
            return dict(_cache)
    except data_source.SheetAccessError as exc:
        with _cache_lock:
            if _cache["generated"] is None:
                _cache.update(_load_fallback())
                _cache["source"] = "fallback"
            _cache["error"] = f"{exc.code}: {exc}"
            return dict(_cache)
    except Exception as exc:  # noqa: BLE001 - keep serving whatever we have
        with _cache_lock:
            if _cache["generated"] is None:
                _cache.update(_load_fallback())
                _cache["source"] = "fallback"
            _cache["error"] = f"unexpected_error: {exc}"
            return dict(_cache)


def get_cache() -> dict:
    with _cache_lock:
        return dict(_cache)


# ---------------------------------------------------------------------------
# Server-side mirror of enough of app.js's derived-data logic to support the
# CSV export filters. The interactive dashboard's own filtering, grouping,
# and conflict detection all still run client-side in static/app.js exactly
# as before — this is only for /api/export.csv.
# ---------------------------------------------------------------------------

MENTOR_PLACEHOLDERS = {"tbc", "na", "n/a", "tba", "-", "none"}


def _mentor_key_of(raw):
    if not raw:
        return None
    collapsed = " ".join(str(raw).strip().split()).replace(".", "").lower()
    if not collapsed or collapsed in MENTOR_PLACEHOLDERS:
        return None
    return collapsed


def _build_batch_overview_map(sessions, courses_overview):
    by_course = {o["course"]: o for o in courses_overview if o.get("course")}
    by_code = {}
    for o in courses_overview:
        by_code.setdefault(o.get("code"), o)
    batch_map = {}
    for s in sessions:
        b = s.get("course")
        if b in batch_map:
            continue
        ov = by_course.get(b) or by_code.get(s.get("code"))
        batch_map[b] = ov
    return batch_map


def _session_coordinator(s, batch_map):
    ov = batch_map.get(s.get("course"))
    return (ov or {}).get("coordinator") if ov else None


def _session_team_lead(s, batch_map):
    ov = batch_map.get(s.get("course"))
    return (ov or {}).get("team_lead") if ov else None


def _filtered_sessions(sessions, batch_map, args):
    course = args.get("course", "all")
    batch = args.get("batch", "all")
    coordinator = args.get("coordinator", "all")
    teamlead = args.get("teamlead", "all")
    mentor = args.get("mentor", "all")
    month = args.get("month", "all")
    status = args.get("status", "all")
    query = (args.get("q") or "").strip().lower()

    out = []
    for s in sessions:
        if course != "all" and s.get("code") != course:
            continue
        if batch != "all" and s.get("course") != batch:
            continue
        if coordinator != "all":
            c = _session_coordinator(s, batch_map) or "__unassigned__"
            if c != coordinator:
                continue
        if teamlead != "all":
            t = _session_team_lead(s, batch_map) or "__unassigned__"
            if t != teamlead:
                continue
        if mentor != "all" and _mentor_key_of(s.get("mentor")) != mentor:
            continue
        if month != "all":
            d = s.get("date")
            if not d or d[:7] != month:
                continue
        if status != "all" and s.get("status") != status:
            continue
        if query:
            hay = " ".join(
                str(v)
                for v in [
                    s.get("session_name"),
                    s.get("mentor"),
                    s.get("session_no"),
                    s.get("course"),
                    s.get("code"),
                    _session_coordinator(s, batch_map),
                    _session_team_lead(s, batch_map),
                ]
                if v
            ).lower()
            if query not in hay:
                continue
        out.append(s)
    return out


@app.route("/")
def index():
    cache = get_cache()
    payload = {
        "generated": cache["generated"],
        "sessions": cache["sessions"],
        "courses_overview": cache["courses_overview"],
    }
    return render_template(
        "index.html",
        session_data_json=json.dumps(payload),
        initial_source=cache["source"],
        initial_error=cache["error"],
    )


@app.route("/api/data")
def api_data():
    cache = get_cache()
    return jsonify(
        {
            "generated": cache["generated"],
            "sessions": cache["sessions"],
            "courses_overview": cache["courses_overview"],
            "source": cache["source"],
            "error": cache["error"],
            "last_synced": cache["last_synced"],
        }
    )


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    cache = refresh_cache()
    status = 200 if cache["source"] == "live" else 502
    return (
        jsonify(
            {
                "ok": cache["source"] == "live",
                "generated": cache["generated"],
                "sessions": cache["sessions"],
                "courses_overview": cache["courses_overview"],
                "source": cache["source"],
                "error": cache["error"],
                "last_synced": cache["last_synced"],
            }
        ),
        status,
    )


@app.route("/api/export.csv")
def api_export_csv():
    cache = get_cache()
    sessions = cache["sessions"]
    batch_map = _build_batch_overview_map(sessions, cache["courses_overview"])
    rows = _filtered_sessions(sessions, batch_map, request.args)
    rows = sorted(rows, key=lambda s: (s.get("date") or "9999-99-99", s.get("sl") or 0))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Batch", "Course", "SL", "Date", "Date end", "Time", "Session no",
            "Session name", "Mentor", "Status", "Remarks", "Coordinator", "Team lead",
        ]
    )
    for s in rows:
        writer.writerow(
            [
                s.get("course"), s.get("code"), s.get("sl"), s.get("date"),
                s.get("date_end"), s.get("time"), s.get("session_no"),
                s.get("session_name"), s.get("mentor"), s.get("status"),
                s.get("remarks"), _session_coordinator(s, batch_map),
                _session_team_lead(s, batch_map),
            ]
        )

    filename = f"course-calendar-sessions-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _auto_refresh_loop(minutes: float):
    while True:
        time.sleep(minutes * 60)
        try:
            refresh_cache()
        except Exception:  # noqa: BLE001 - never let the background loop die
            pass


refresh_cache(is_startup=True)

_auto_minutes = os.environ.get("AUTO_REFRESH_MINUTES")
if _auto_minutes:
    try:
        _minutes = float(_auto_minutes)
        if _minutes > 0:
            threading.Thread(target=_auto_refresh_loop, args=(_minutes,), daemon=True).start()
    except ValueError:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "127.0.0.1")
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
