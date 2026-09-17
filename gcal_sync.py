"""
gcal_sync.py  -  Push bouncer-approved events into a shared Google Calendar.

Drop this next to q4_pipeline.py. It reuses the Event objects that
q4_pipeline already produces, so nothing about the fetch/normalize/bouncer
logic changes -- this just replaces write_ics() as the OUTPUT step.

Requires (on your machine, NOT in this sandbox):
    pip install google-api-python-client google-auth

Setup (see chat): a Service Account JSON key + a team calendar shared with
that service account's email ("Make changes to events").

Why a Service Account: it runs unattended (cron / cloud scheduler) with no
human clicking "allow" every week.
"""

import datetime as dt
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _service(service_account_json: str):
    creds = service_account.Credentials.from_service_account_file(
        service_account_json, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _to_gcal_event(ev) -> dict:
    """Map our normalized Event -> Google Calendar event body.

    KEY TRICK: we set the Google event's `id` from our own stable uid.
    That makes the whole sync idempotent -- re-running never duplicates,
    and it lets us find/update/delete THIS exact event later.
    Google ids must be lowercase base32hex (a-v, 0-9), 5-1024 chars.
    """
    end = ev.end_utc or (ev.start_utc + dt.timedelta(hours=1))
    gid = "".join(c for c in ev.uid.split("@")[0].lower() if c in "0123456789abcdefghijklmnopqrstuv")
    gid = ("ir" + gid).ljust(5, "0")

    desc = [f"Type: {ev.event_type}", f"Source: {ev.source_url}"]
    if ev.webcast_url:
        desc.append(f"Webcast: {ev.webcast_url}")

    body = {
        "id": gid,
        "summary": f"{ev.company}: {ev.title}",
        "description": "  |  ".join(desc),
        "start": {"dateTime": ev.start_utc.astimezone(dt.timezone.utc).isoformat()},
        "end":   {"dateTime": end.astimezone(dt.timezone.utc).isoformat()},
        # tag every event we own so we can safely clean up only OUR events
        "extendedProperties": {"private": {"managedBy": "ir-calendar-bot",
                                           "eventType": ev.event_type}},
    }
    if ev.location:
        body["location"] = ev.location
    if ev.webcast_url:
        body["source"] = {"title": "Webcast", "url": ev.webcast_url}
    return body


def sync_google_calendar(kept_events, dropped_events, calendar_id, service_account_json):
    """Upsert kept events; delete any previously-posted event that is now
    dropped (e.g. a broker meeting you decided to exclude, or one that
    disappeared from the IR page). This is the delete/skip control you wanted.
    """
    svc = _service(service_account_json)

    # 1. Upsert everything the bouncer KEPT.
    for ev in kept_events:
        body = _to_gcal_event(ev)
        try:
            svc.events().update(calendarId=calendar_id,
                                eventId=body["id"], body=body).execute()
            print(f"[updated] {body['summary']}")
        except Exception:
            # not there yet -> create it
            try:
                svc.events().insert(calendarId=calendar_id, body=body).execute()
                print(f"[created] {body['summary']}")
            except Exception as e:
                print(f"[ERROR ] {body['summary']}: {e}")

    # 2. Delete anything we previously posted that is now on the DROP list.
    for ev in dropped_events:
        gid = _to_gcal_event(ev)["id"]
        try:
            svc.events().delete(calendarId=calendar_id, eventId=gid).execute()
            print(f"[deleted] {ev.company}: {ev.title}  (now dropped)")
        except Exception:
            pass  # wasn't on the calendar; nothing to remove


def purge_managed_events(calendar_id, service_account_json):
    """Nuclear option: delete every event this bot ever created on the
    calendar (matched by our private tag). Useful while testing."""
    svc = _service(service_account_json)
    page = None
    while True:
        resp = svc.events().list(
            calendarId=calendar_id,
            privateExtendedProperty="managedBy=ir-calendar-bot",
            pageToken=page, maxResults=250,
        ).execute()
        for item in resp.get("items", []):
            svc.events().delete(calendarId=calendar_id, eventId=item["id"]).execute()
            print(f"[purged] {item.get('summary')}")
        page = resp.get("nextPageToken")
        if not page:
            break


# ---------------------------------------------------------------------------
# How to call it from q4_pipeline.py's __main__ (replace the write_ics block):
#
#   from gcal_sync import sync_google_calendar
#
#   kept, dropped = [], []
#   for ev in parse_q4(all_raw, cfg):
#       keep, reason = should_post(ev)
#       (kept if keep else dropped).append(ev)
#
#   sync_google_calendar(
#       kept_events=kept,
#       dropped_events=dropped,
#       calendar_id="YOUR_TEAM_CALENDAR_ID@group.calendar.google.com",
#       service_account_json="service_account.json",
#   )
# ---------------------------------------------------------------------------
