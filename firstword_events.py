"""FirstWord Pharma events -> firstword_events.ics  (its OWN calendar / Sheet tab)

Standalone, stdlib only. Mirrors the FDA script: own .ics, carry-forward when a
pull fails, and history-archive of past events.

SOURCE: the JSON search endpoint the FirstWord events page itself calls
  POST https://ml.firstwordpharma.com/v1/articles/search
  body: types = FW Perspectives Live, IAV Live, Sponsor Event
Response shape: {"success":true,"data":{"articles":[ ... ]}}; event times are
Unix seconds in extra_fields.event_start_timestamp / event_end_timestamp (UTC).

AUTH (fully automated, no token to copy): the events page gets its own Bearer
token when it loads. This script opens the public events page in headless
Chromium (Playwright), lets the page make its own search call, and reads that
response -- the token is never seen, stored or expired by you. If the page's
own call isn't usable, the token the page used is replayed for our own query.
Optional override: set FIRSTWORD_TOKEN to skip the browser (plain HTTP).

Data path (FIRSTWORD_MODE=auto|browser|token|manual, default auto):
  * auto   : FIRSTWORD_TOKEN set -> plain HTTP; otherwise headless browser.
  * manual : parse the saved snapshot `firstword_events.json` (commit it).
  * No silent fallback to a stale snapshot: a failed pull turns the run red,
    and old events are carried forward / past events archived (as FDA script).

Optional env: FIRSTWORD_WINDOW (default "1 year"), FIRSTWORD_ICS,
FIRSTWORD_SNAPSHOT, FIRSTWORD_TYPES (comma list), FIRSTWORD_UA,
FIRSTWORD_CHROMIUM (path to a Chromium binary; default = Playwright's own).
Legacy snapshot format {"events":[{title,start,end,url,...}]} still works.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

PAGE = "https://firstwordpharma.com/river/events"
API_URL = "https://ml.firstwordpharma.com/v1/articles/search"
API_HOST = "ml.firstwordpharma.com"
TOKEN = os.getenv("FIRSTWORD_TOKEN", "").strip()
WINDOW = os.getenv("FIRSTWORD_WINDOW", "1 year")
TYPES = [t.strip() for t in os.getenv(
    "FIRSTWORD_TYPES", "FW Perspectives Live,IAV Live,Sponsor Event").split(",") if t.strip()]
UA = os.getenv("FIRSTWORD_UA", "FirstWordEventsTracker/1.0 (personal calendar; low-frequency)")
SNAPSHOT = Path(os.getenv("FIRSTWORD_SNAPSHOT", "firstword_events.json"))
ICS_PATH = Path(os.getenv("FIRSTWORD_ICS", "firstword_events.ics"))
MODE = os.getenv("FIRSTWORD_MODE", "auto").lower()
TZ_UTC = dt.timezone.utc


def esc(s: str) -> str:
    return str(s).replace('\\', '\\\\').replace('\n', '\\n').replace(',', '\\,').replace(';', '\\;')


def fold(line: str) -> list[str]:
    """RFC5545 75-octet line limit; continuation line starts with a space."""
    pieces, chunk, n = [], '', 0
    for char in line:
        size = len(char.encode('utf-8'))
        if n + size > 75:
            pieces.append(chunk)
            chunk, n = ' ', 1
        chunk += char
        n += size
    pieces.append(chunk)
    return pieces


def fetch_live() -> str:
    """POST the events search. Raises with a clear message on any failure."""
    if not TOKEN:
        raise RuntimeError("FIRSTWORD_TOKEN is not set")
    body = json.dumps({
        "query_string": None, "type": TYPES, "sort_by": "publication_date",
        "from": 0, "size": 1000, "publication_date": WINDOW,
        "publication_date_end": None, "tags": {},
        "include_related_tags": True, "tags_category_operator": "OR",
    }).encode()
    assert urllib.parse.urlparse(API_URL).hostname == API_HOST
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + TOKEN,
        "Origin": "https://firstwordpharma.com",
        "Referer": "https://firstwordpharma.com/",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(8_000_001)
    except urllib.error.HTTPError as e:
        hint = " (token expired/invalid -- unset FIRSTWORD_TOKEN to use the browser path)" if e.code in (401, 403) else ""
        raise RuntimeError(f"FirstWord API HTTP {e.code}{hint}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"FirstWord API unreachable: {e.reason}")
    if len(raw) > 8_000_000:
        raise ValueError("API response exceeds 8 MB safety limit")
    print(f"FirstWord: fetched live API ({len(raw)} bytes).")
    return raw.decode("utf-8")


def _search_body() -> dict:
    return {
        "query_string": None, "type": TYPES, "sort_by": "publication_date",
        "from": 0, "size": 1000, "publication_date": WINDOW,
        "publication_date_end": None, "tags": {},
        "include_related_tags": True, "tags_category_operator": "OR",
    }


def _has_events(text: str) -> bool:
    """True if text is an API response containing at least one event record."""
    try:
        arts = json.loads(text)["data"]["articles"]
        return any((a.get("extra_fields") or {}).get("event_start_timestamp") for a in arts)
    except Exception:
        return False


DEBUG_DIR = Path(os.getenv("FIRSTWORD_DEBUG_DIR", "fw_debug"))
_REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


def _safe_url(u: str) -> str:
    p = urllib.parse.urlparse(u)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _browser_attempt(p, headless: bool) -> str:
    """One browser attempt. Returns the events JSON text or raises. On failure it
    writes diagnostics (network log, screenshot, page text) to DEBUG_DIR; tokens
    are never logged (only whether an Authorization header was present)."""
    launch = {"headless": headless,
              "args": ["--disable-blink-features=AutomationControlled"]}
    if os.getenv("FIRSTWORD_CHROMIUM"):
        launch["executable_path"] = os.environ["FIRSTWORD_CHROMIUM"]
    browser = p.chromium.launch(**launch)
    seen, net, auth = [], [], {}
    try:
        ctx = browser.new_context(
            locale="en-US", timezone_id="America/New_York",
            viewport={"width": 1366, "height": 900},
            user_agent=os.getenv("FIRSTWORD_BROWSER_UA", _REAL_UA))
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.new_page()

        def on_request(req):
            try:
                if API_HOST in req.url and req.headers.get("authorization"):
                    auth["h"] = req.headers["authorization"]
            except Exception:
                pass

        def on_response(resp):
            try:
                req = resp.request
                host = urllib.parse.urlparse(resp.url).netloc
                if "firstword" in host and req.resource_type in ("xhr", "fetch", "document"):
                    net.append(f"{req.method} {resp.status} {req.resource_type} "
                               f"auth={'Y' if req.headers.get('authorization') else 'N'} "
                               f"{_safe_url(resp.url)}")
                if resp.status == 200 and "json" in (resp.headers.get("content-type") or ""):
                    txt = resp.text()
                    if _has_events(txt):          # ANY endpoint returning events
                        seen.append(txt)
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", lambda r: net.append(
            f"FAILED {r.method} {_safe_url(r.url)} {r.failure}")
            if "firstword" in r.url else None)

        try:
            page.goto(PAGE, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            net.append(f"goto warning: {exc}")
        try:                                       # best-effort cookie-banner dismissal
            for label in ("Accept all", "Accept All", "Accept", "I agree", "Agree",
                          "Allow all", "Got it"):
                btn = page.get_by_role("button", name=label)
                if btn.count():
                    btn.first.click(timeout=2000)
                    break
        except Exception:
            pass
        for i in range(40):                        # up to ~20 s; nudge lazy loaders
            if seen:
                break
            if i in (4, 12, 20):
                try:
                    page.mouse.wheel(0, 1200)
                except Exception:
                    pass
            page.wait_for_timeout(500)

        if seen:
            best = max(seen, key=len)
            print(f"FirstWord: captured page's own events response ({len(best)} bytes).")
            return best
        if auth.get("h"):
            res = page.evaluate("""async ([url, auth, body]) => {
                const r = await fetch(url, {method: "POST", headers: {
                    "authorization": auth, "content-type": "application/json",
                    "accept": "application/json, text/plain, */*"}, body});
                return {status: r.status, text: await r.text()};
            }""", [API_URL, auth["h"], json.dumps(_search_body())])
            if res["status"] == 200 and _has_events(res["text"]):
                print("FirstWord: replayed page auth for own query.")
                return res["text"]
            net.append(f"replay with page auth -> HTTP {res['status']}")

        # ---- failure: write diagnostics --------------------------------------
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            title = page.title()
            body = page.inner_text("body")[:2000]
        except Exception as exc:
            title, body = "?", f"(could not read page: {exc})"
        report = [f"mode: {'headless' if headless else 'headed'}",
                  f"final url: {_safe_url(page.url)}", f"title: {title}",
                  f"auth header seen on API host: {'yes' if auth else 'no'}",
                  "--- firstword network calls ---", *net,
                  "--- visible page text (first 2000 chars) ---", body]
        (DEBUG_DIR / f"report_{'headless' if headless else 'headed'}.txt").write_text(
            "\n".join(report), encoding="utf-8")
        try:
            page.screenshot(path=str(DEBUG_DIR / f"page_{'headless' if headless else 'headed'}.png"),
                            full_page=True)
        except Exception:
            pass
        print("\n".join(["FirstWord DIAGNOSTICS:", *report]), file=sys.stderr)
        raise RuntimeError("page made no usable search call (see diagnostics above / "
                           "the fw_debug artifact: blocked, login wall, or site change)")
    finally:
        browser.close()


def fetch_browser() -> str:
    """Load the public events page in Chromium and capture the events response the
    page itself requests (it fetches its own token). Tries headless, then headed
    (works under xvfb on CI) if the first attempt fails."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright not installed (pip install playwright && "
                           "playwright install --with-deps chromium)")
    errs = []
    with sync_playwright() as p:
        for headless in ((True, False) if os.getenv("DISPLAY") else (True,)):
            try:
                return _browser_attempt(p, headless)
            except Exception as exc:
                errs.append(str(exc))
    raise RuntimeError(" | ".join(errs))


def fetch_input() -> tuple[str, str]:
    if MODE not in ("auto", "manual", "browser", "token"):
        raise ValueError("FIRSTWORD_MODE must be auto, browser, token, or manual")
    if MODE == "token" or (MODE == "auto" and TOKEN):
        return fetch_live(), "live API (token)"
    if MODE in ("auto", "browser"):
        return fetch_browser(), "live API (browser)"
    if SNAPSHOT.is_file():
        return SNAPSHOT.read_text(encoding="utf-8"), str(SNAPSHOT)
    raise FileNotFoundError(f"No snapshot at {SNAPSHOT}")


def parse_date(value: str) -> tuple[dt.date | dt.datetime, bool]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing date/time string')
    value = value.strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return dt.date.fromisoformat(value), True
    stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Timed event must have an explicit UTC offset: ' + value)
    return stamp.astimezone(TZ_UTC), False


def iso_value(value: dt.date | dt.datetime, all_day: bool) -> str:
    if all_day:
        return value.strftime('%Y%m%d')
    return value.strftime('%Y%m%dT%H%M%SZ')


def read_previous(path: Path) -> list[tuple[str, dt.date, list[str]]]:
    """Read our own previous ICS; retain exact physical VEVENT lines."""
    if not path.exists():
        return []
    text = path.read_text(encoding='utf-8')
    if 'BEGIN:VCALENDAR' not in text or 'END:VCALENDAR' not in text:
        raise ValueError(f'Existing {path} is not a complete VCALENDAR; refusing overwrite')
    results, block = [], None
    for line in text.splitlines():
        if line == 'BEGIN:VEVENT':
            if block is not None:
                raise ValueError('Nested VEVENT in previous ICS')
            block = [line]
        elif block is not None:
            block.append(line)
            if line == 'END:VEVENT':
                joined = []
                for physical in block:
                    if physical.startswith((' ', '\t')) and joined:
                        joined[-1] += physical[1:]
                    else:
                        joined.append(physical)
                uid = next((l[4:] for l in joined if l.startswith('UID:')), '')
                start = next((l.split(':', 1)[1] for l in joined
                              if l.startswith('DTSTART:') or l.startswith('DTSTART;VALUE=DATE:')), '')
                if not uid or not re.match(r'^\d{8}', start):
                    raise ValueError('Previous ICS contains event without UID or DTSTART')
                results.append((uid, dt.datetime.strptime(start[:8], '%Y%m%d').date(), block))
                block = None
    if block is not None:
        raise ValueError('Incomplete VEVENT in previous ICS')
    return results


def _clean(s) -> str:
    return re.sub(r"[ \t]+", " ", str(s or "")).strip()


def _api_rows(obj) -> list[dict] | None:
    """FirstWord API response -> normalised rows, or None if not that shape."""
    if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
        arts = obj["data"].get("articles")
    elif isinstance(obj, dict) and "articles" in obj:
        arts = obj.get("articles")
    else:
        return None
    if not isinstance(arts, list):
        return None
    rows = []
    for a in arts:
        x = a.get("extra_fields") or {}
        try:
            s, e = int(x.get("event_start_timestamp")), x.get("event_end_timestamp")
            e = int(e) if e else None
        except (TypeError, ValueError):
            print(f"FirstWord: skipped '{a.get('title')}' (no event_start_timestamp)",
                  file=sys.stderr)
            continue
        start = dt.datetime.fromtimestamp(s, TZ_UTC)
        end = dt.datetime.fromtimestamp(e, TZ_UTC) if e else None
        link = _clean(x.get("cta_url")) or PAGE
        bits = []
        if _clean(x.get("teaser") or a.get("body")):
            bits.append(_clean(x.get("teaser") or a.get("body"))[:900])
        bits.append("Type: " + _clean(a.get("type")))
        if _clean(x.get("sponsors")):
            bits.append("Sponsor: " + _clean(x["sponsors"]))
        elif _clean(x.get("location")):
            bits.append("Presented by: " + _clean(x["location"]))
        if _clean(x.get("speakers")):
            bits.append("Speakers: " + _clean(x["speakers"]))
        bits.append(f"{_clean(x.get('cta')) or 'Link'}: {link}")
        rows.append(dict(id="fw-" + str(a.get("id")), title=_clean(a.get("title")),
                         start=start, end=end, url=link, location="Online webinar",
                         description="\n".join(bits), _dt=True))
    return rows


def make_events(payload: str) -> list[dict]:
    obj = json.loads(payload)
    rows = _api_rows(obj)
    if rows is None:                       # legacy {"events":[...]} / bare list
        rows = obj.get("events") if isinstance(obj, dict) else obj
    if not isinstance(rows, list) or not rows:
        raise ValueError("No events found in input; refusing to erase calendar")
    out, seen = [], set()
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"Event {i} must be an object")
        title = str(row.get("title") or "").strip()
        if not title:
            raise ValueError(f"Event {i} has no title")
        if row.get("_dt"):                 # API rows: already UTC datetimes
            start, all_day = row["start"], False
            end = row["end"] or (start + dt.timedelta(hours=1))
        else:
            start, all_day = parse_date(row.get("start") or row.get("date"))
            end_str = row.get("end")
            end, is_date = parse_date(end_str) if end_str else (start, all_day)
            if all_day != is_date:
                raise ValueError(f"Event {i} mixes dates and datetimes")
            if not all_day and end == start:
                end = start + dt.timedelta(hours=1)
            if all_day:
                end = end + dt.timedelta(days=1)   # all-day DTEND is exclusive
        if end < start:
            raise ValueError(f"Event {i} ends before it starts")
        link = str(row.get("url") or PAGE).strip()
        if urllib.parse.urlparse(link).scheme not in ("https", "http"):
            raise ValueError(f"Event {i} URL must be absolute http(s)")
        source_id = str(row.get("id") or (link if link != PAGE else
                         title.casefold() + "|" + start.isoformat()))
        uid = hashlib.sha256(("firstword|" + source_id).encode()).hexdigest()[:24] + "@firstword-events"
        if uid in seen:                    # duplicate id in feed -> first wins
            continue
        seen.add(uid)
        out.append(dict(uid=uid, title=title, start=start, end=end,
                        all_day=all_day, url=link,
                        location=str(row.get("location") or ""),
                        description=str(row.get("description") or "")))
    return out


def write_calendar(events: list[dict], previous: list, *, failed: bool) -> tuple[int, int]:
    now = dt.datetime.now(TZ_UTC)
    today = now.date()
    fresh_ids = {e['uid'] for e in events}
    kept, seen = [], set()
    for uid, start, block in previous:
        if uid in fresh_ids or uid in seen:
            continue
        if failed or start < today:  # failure: keep everything; success: archive past only
            kept.append(block)
            seen.add(uid)
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'CALSCALE:GREGORIAN',
             'PRODID:-//MedicalMeetingTracker//FirstWord Events//EN']
    for e in events:
        raw = ['BEGIN:VEVENT', 'UID:' + e['uid'],
               'DTSTAMP:' + iso_value(now, False)]
        if e['all_day']:
            raw += ['DTSTART;VALUE=DATE:' + iso_value(e['start'], True),
                    'DTEND;VALUE=DATE:' + iso_value(e['end'], True)]
        else:
            raw += ['DTSTART:' + iso_value(e['start'], False),
                    'DTEND:' + iso_value(e['end'], False)]
        raw += ['SUMMARY:' + esc('FirstWord: ' + e['title']),
                'DESCRIPTION:' + esc(e['description'] + ('\n' if e['description'] else '') + 'Source: ' + e['url']),
                'URL:' + esc(e['url'])]
        if e['location']:
            raw.append('LOCATION:' + esc(e['location']))
        raw.append('END:VEVENT')
        for line in raw:
            lines.extend(fold(line))
    for block in kept:
        lines.extend(block)
    lines.append('END:VCALENDAR')
    ICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.firstword-', suffix='.ics', dir=ICS_PATH.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write('\r\n'.join(lines) + '\r\n')
        os.replace(tmp, ICS_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return len(events), len(kept)


def main() -> int:
    try:
        previous = read_previous(ICS_PATH)
    except Exception as exc:
        print(f'ERROR: cannot safely read previous calendar: {exc}', file=sys.stderr)
        return 1
    try:
        raw, source = fetch_input()
        events = make_events(raw)
    except Exception as exc:
        fresh, retained = write_calendar([], previous, failed=True)
        print(f'ERROR: fetch/parse failed ({exc}); kept {retained} old event(s) in {ICS_PATH}', file=sys.stderr)
        return 1
    fresh, retained = write_calendar(events, previous, failed=False)
    print(f'FirstWord: {fresh} current, {retained} archived past -> {ICS_PATH} (source: {source})')
    if source == str(SNAPSHOT):
        print('Snapshot mode: refresh the JSON file, or use FIRSTWORD_MODE=auto for live pulls.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
