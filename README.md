# Course Calendar Analytics — Flask webapp

A standalone Flask version of the Course Calendar Analytics dashboard. It
reads the "Course Calendar (2027–2028)" Google Sheet directly, using a
Google service account, instead of going through a viewer's own Google
Drive connection — so it can run as an ordinary webapp with its own URL,
for anyone on the team to open.

Every per-course tab (everything except `MIS` and `Mastersheet`) is read as
a list of sessions; the `MIS` tab is read as the coordinator/team-lead/status
overview per batch. All of the dashboard's tabs — Overview, Pending,
Coordinators, Team Leads, Courses, Batches, Mentors (including the mentor
scheduling-conflict detector), Sessions, Confirmation Rates, and the Month
Calendar — work the same way they did in the Claude-artifact version. If the
sheet can't be reached for any reason, the app falls back to a bundled demo
snapshot (`fallback_data.json`) so the page still loads.

## 1. Create a Google service account and share the sheet with it

1. In the [Google Cloud Console](https://console.cloud.google.com/), create
   a project (or use an existing one) and enable the **Google Sheets API**
   (APIs & Services → Library → search "Google Sheets API" → Enable).
2. Go to **APIs & Services → Credentials → Create Credentials → Service
   account**. Give it any name (e.g. `course-calendar-reader`).
3. Open the new service account, go to the **Keys** tab, **Add key → Create
   new key → JSON**, and download it. This file's `client_email` field
   (something like `course-calendar-reader@your-project.iam.gserviceaccount.com`)
   is the address you share the sheet with next.
4. Open the Course Calendar Google Sheet, click **Share**, and add that
   `client_email` address with **Viewer** access.
5. Copy the spreadsheet ID out of its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_LONG_ID`**`/edit` — that's
   your `GOOGLE_SHEET_ID`.

## 2. Configure

```bash
cp .env.example .env
```

Fill in `.env`:
- `GOOGLE_SHEET_ID` — the spreadsheet ID from step 1.5.
- Either paste the whole downloaded JSON key as `GOOGLE_SERVICE_ACCOUNT_JSON`
  (one line), **or** save the key file as `service_account.json` next to
  `app.py` and leave `GOOGLE_SERVICE_ACCOUNT_FILE` at its default.

Never commit the JSON key file or its contents to version control — add
`service_account.json` and `.env` to `.gitignore`.

## 3. Run it locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# load .env into the shell (or use a tool like python-dotenv / direnv)
export $(grep -v '^#' .env | xargs)   # macOS/Linux
python app.py
```

Open http://127.0.0.1:5000 — it live-fetches the sheet on startup, and you
can also click **Refresh from Google Sheet** any time, or set
`AUTO_REFRESH_MINUTES` to have it refresh itself in the background.

## 4. Deploy it

This is a normal Flask app — any Python host works. A couple of common
options:

**Render / Railway / Fly.io / a VPS**, using `gunicorn`:
```bash
gunicorn -w 2 -b 0.0.0.0:$PORT app:app
```
Set `GOOGLE_SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the whole key
file as the value) as environment variables in the host's dashboard — most
hosts don't let you upload a JSON file directly, which is why
`GOOGLE_SERVICE_ACCOUNT_JSON` exists.

**Docker**, roughly:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "app:app"]
```

## Project layout

```
course-calendar-webapp/
  app.py              Flask routes (/, /api/refresh, /api/data, /api/export.csv)
  data_source.py       Google Sheets service-account reader + parsing logic
  fallback_data.json   bundled demo snapshot, used if the sheet can't be reached
  requirements.txt
  .env.example
  templates/
    index.html          page shell, renders the initial embedded data
  static/
    style.css           all dashboard styling (light + dark mode)
    app.js              all dashboard behavior: filters, tabs, calendar,
                        aggregation, and the mentor conflict detector
```

## How the mentor conflict detector works

For every session with a named mentor (placeholders like `TBC`, `NA`, `N/A`,
`-`, blank are ignored) and a readable date + start time, sessions are
grouped by (mentor, date). Within a group, sessions are sorted by start time
and merged into overlap clusters using an assumed session length (30/45/60/90/120
minutes, selectable in the Mentors tab — the sheet only records a start
time, not a duration). Any cluster with 2+ sessions is a conflict: the same
mentor double-booked across overlapping windows, whether the sessions are
in the same course, a different course, or a different batch entirely. This
logic lives in `static/app.js` (`computeConflicts`) and runs entirely in the
browser against whatever data is currently loaded.

## API endpoints

- `GET /` — the dashboard page.
- `POST /api/refresh` — re-fetches the Google Sheet now; returns the fresh
  data as JSON (also used by the "Refresh from Google Sheet" button).
- `GET /api/data` — returns the currently cached data as JSON.
- `GET /api/export.csv` — a CSV of the sessions, honoring the same filters
  as the UI (query params: `course`, `batch`, `coordinator`, `teamlead`,
  `mentor`, `month`, `status`, `q`).
