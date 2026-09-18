"""
Q4 IR events -> normalized schema -> bouncer filter -> .ics
Proof of concept, driven by the real Pfizer GetEventList payload.

Works for any Q4-hosted IR site (Pfizer, Gilead, JNJ, AbbVie...) by swapping
the endpoint in COMPANIES. Everything after fetch is shared, reusable code.

v2: "conference" is no longer a blanket block. Conferences are now judged by
WHO hosts them -> broker/sell-side conferences are dropped, medical/scientific
congresses (ADA, ESMO, ASCO, AHA...) are kept.
"""

import re
import os
import json
import hashlib
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# 1. CONFIG  (one entry per company; only the fetch layer differs by company)
# ---------------------------------------------------------------------------
# Standard Q4 GetEventList request body. Q4's WCF service ("...svc/GetEventList")
# expects a POST with this JSON. ItemCount:-1 = "all events". Adjust ViewType /
# year if your captured Payload tab shows different values.
# Standard Q4 GetEventList query parameters. CONFIRMED from Pfizer DevTools:
# the endpoint is a GET and the params ride in the query string (NOT a POST body).
# This "upcoming events" filter is what the events landing page itself uses:
#   eventDateFilter=1 + year=-1  ->  all upcoming events, any year
#   pageSize=-1                   ->  return everything (no paging)
Q4_DEFAULT_QUERY = {
    "LanguageId": "1",
    "eventSelection": "1",
    "eventDateFilter": "1",          # 1 = upcoming; 0 = date-agnostic
    "includeFinancialReports": "false",
    "includePresentations": "false",
    "includePressReleases": "false",
    "sortOperator": "1",
    "pageSize": "-1",                # -1 = everything
    "pageNumber": "0",
    "tagList": "",
    "includeTags": "true",
    "year": "-1",                    # -1 = all years
    "excludeSelection": "1",
}

COMPANIES = {
    "pfizer": {
        "name": "Pfizer",
        "ir_host": "https://investors.pfizer.com",
        # CONFIRMED from DevTools Headers tab: GET, params in the query string.
        "endpoint": "https://investors.pfizer.com/feed/Event.svc/GetEventList",
        "method": "GET",
        "query": Q4_DEFAULT_QUERY,
    },
    # Same shape, just different host/endpoint -- add once Pfizer works.
    # (Confirm each site uses the standard /feed/Event.svc/GetEventList path.)
    # "gilead": {"name": "Gilead", "ir_host": "https://investors.gilead.com",
    #            "endpoint": "https://investors.gilead.com/feed/Event.svc/GetEventList",
    #            "method": "GET", "query": Q4_DEFAULT_QUERY},
    # "jnj":    {... "ir_host": "https://www.investor.jnj.com" ...},
    # "abbvie": {... "ir_host": "https://investors.abbvie.com" ...},
}

# ---------------------------------------------------------------------------
# 2. THE BOUNCER'S RULEBOOK
# ---------------------------------------------------------------------------
# Guest list: canonical event types you always want, matched by keywords.
ALLOW_TYPES = {
    "earnings_call":       ["earnings", "quarter", "q1", "q2", "q3", "q4", "full year", "results"],
    "capital_markets_day": ["capital markets", "investor day", "cmd", "r&d day", "rnd day"],
    "agm":                 ["annual general meeting", "agm", "annual meeting"],
}

# Medical / scientific congresses you DO want (pipeline data readouts, symposia).
# Matched on WHOLE WORDS so short acronyms like "ada"/"acc"/"ash" don't fire
# inside unrelated words (e.g. "roadshow", "accelerate").
MEDICAL_CONFERENCES = [
    "asco", "esmo", "ada", "easd", "aha", "acc", "ash", "aacr", "ers", "ats",
    "acr", "eular", "ddw", "obesityweek", "endo", "esc", "croi", "aasld",
    "sabcs", "wclc", "asn", "idweek", "ueg",
    "kidney week", "world congress", "annual congress",
    "scientific session", "scientific sessions", "symposium", "congress",
]

# J.P. Morgan is handled SEPARATELY (see JPM_NAMES): its January Healthcare
# Conference is a must-keep, but JPM also hosts smaller broker confs in other
# months that we drop. So JPM is NOT in the generic broker list below.
JPM_NAMES = ["j.p. morgan", "jp morgan", "jpmorgan", "jpm ", "j p morgan"]

# Sell-side / broker-hosted investor conferences you do NOT want.
# Bank names are distinctive, so plain substring matching is safe here.
BROKER_NAMES = [
    "all stars",
    "morgan stanley", "goldman", "jefferies", "citi", "bank of america",
    "bofa", "barclays", "ubs", "cowen", "td cowen", "leerink", "evercore",
    "piper", "wells fargo", "guggenheim", "bmo", "berenberg", "deutsche bank",
    "hsbc", "wolfe", "truist", "baird", "stifel", "raymond james", "redburn",
    "bernstein", "oppenheimer", "mizuho", "cantor", "needham", "william blair",
]

# Always-drop formats, regardless of who hosts them.
HARD_BLOCK_KEYWORDS = [
    "broker", "non-deal", "roadshow", "fireside", "bus tour", "field trip",
]


def _has_word(hay: str, needle: str) -> bool:
    """Whole-word / phrase match so short acronyms don't match inside words."""
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", hay) is not None


# ---------------------------------------------------------------------------
# 3. NORMALIZED EVENT SCHEMA (vendor-agnostic; every adapter emits this)
# ---------------------------------------------------------------------------
@dataclass
class Event:
    company: str
    title: str
    start_utc: dt.datetime
    end_utc: Optional[dt.datetime]
    tz_original: str
    event_type: str          # from classify()
    source_url: str
    webcast_url: str = ""
    location: str = ""
    tags: list = field(default_factory=list)
    uid: str = ""


# ---------------------------------------------------------------------------
# 4. TIMEZONE HANDLING  (the #1 source of pharma-calendar bugs)
# ---------------------------------------------------------------------------
_TZ_OFFSETS = {
    "GMT": 0, "UTC": 0,
    "BST": +1, "CET": +1, "CEST": +2,
    "ET": -5, "EST": -5, "EDT": -4,
    "CT": -6, "CST": -6, "CDT": -5,
    "PT": -8, "PST": -8, "PDT": -7,
}

def _to_utc(date_str: str, tz_label: str) -> Optional[dt.datetime]:
    if not date_str:
        return None
    naive = dt.datetime.strptime(date_str.strip(), "%m/%d/%Y %H:%M:%S")
    offset = _TZ_OFFSETS.get((tz_label or "GMT").strip().upper())
    if offset is None:
        raise ValueError(f"Unknown timezone label {tz_label!r} for {date_str!r}")
    return (naive - dt.timedelta(hours=offset)).replace(tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# 4b. LIVE FETCH  (calls the real Q4 endpoint; returns raw GetEventList JSON)
# ---------------------------------------------------------------------------
def fetch_q4(cfg: dict) -> dict:
    """Fetch raw GetEventList JSON from a company\'s live Q4 endpoint.

    Raises on any non-200 / bad payload so the GitHub Action fails LOUDLY
    (our "0 events returned" alarm) instead of silently shipping an empty
    calendar. Set env PHARMA_OFFLINE=1 to skip the network and use samples.
    """
    method = (cfg.get("method") or "GET").upper()
    endpoint = cfg["endpoint"]
    # Real browser-like headers. The live Pfizer request sent X-Requested-With
    # and a normal User-Agent -- send the same so the WCF service + any WAF
    # treat us like the site's own AJAX call.
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        # Events landing page is the natural referer for this AJAX call:
        "Referer": cfg["ir_host"].rstrip("/") + "/Investors/news-events/default.aspx",
    }

    data = None
    url = endpoint
    if method == "GET":
        query = cfg.get("query") or {}
        if query:
            url = endpoint + "?" + urllib.parse.urlencode(query)
    else:  # POST fallback (kept for sites that use it)
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(cfg.get("body") or {}).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(
            f"{cfg['name']}: HTTP {e.code} from {url}\n"
            f"  -> the endpoint/method/body likely differ from the standard Q4 pattern.\n"
            f"  -> paste me the DevTools Headers (Request URL + Method) and Payload tabs.\n"
            f"  server said: {detail}"
        ) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"{cfg['name']}: could not reach {url} ({e.reason})") from None

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"{cfg['name']}: response was not JSON (first 300 chars):\n{body[:300]}"
        ) from None

    # Q4 sometimes wraps the result in {"d": {...}} for WCF endpoints.
    if isinstance(parsed, dict) and "d" in parsed and "GetEventListResult" not in parsed:
        parsed = parsed["d"]
    if "GetEventListResult" not in parsed:
        raise RuntimeError(
            f"{cfg['name']}: JSON had no 'GetEventListResult' key. Got keys: "
            f"{list(parsed)[:8]}"
        )
    return parsed


# ---------------------------------------------------------------------------
# 5. Q4 ADAPTER  (turns raw GetEventList JSON into Event objects)
# ---------------------------------------------------------------------------
def parse_q4(raw_json: dict, cfg: dict) -> list:
    events = []
    for e in raw_json.get("GetEventListResult", []):
        tz = e.get("TimeZone") or "GMT"
        detail = e.get("LinkToDetailPage") or ""
        source_url = cfg["ir_host"] + detail if detail.startswith("/") else (detail or cfg["ir_host"])
        ev = Event(
            company=cfg["name"],
            title=(e.get("Title") or "").strip(),
            start_utc=_to_utc(e.get("StartDate"), tz),
            end_utc=_to_utc(e.get("EndDate"), tz),
            tz_original=tz,
            event_type="unknown",
            source_url=source_url,
            webcast_url=e.get("WebCastLink") or "",
            location=e.get("Location") or "",
            tags=e.get("TagsList") or [],
        )
        eid = e.get("EventId")
        basis = f"{cfg['name']}|{eid if eid else ev.title}|{ev.start_utc}"
        ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# 6. THE BOUNCER  (classify + decide keep/drop)
# ---------------------------------------------------------------------------
def classify(ev: Event) -> str:
    """Return a canonical type from the allow-list, or 'unknown'."""
    hay = (ev.title + " " + " ".join(ev.tags)).lower()
    # Medical congress? (word-boundary match on society names/keywords)
    if any(_has_word(hay, m) for m in MEDICAL_CONFERENCES):
        return "medical_conference"
    for etype, keywords in ALLOW_TYPES.items():
        if any(k in hay for k in keywords):
            return etype
    return "unknown"

def _local_month(ev: Event) -> int:
    """Month in the event's ORIGINAL local time (not UTC), so a timezone
    shift can never bump a mid-January event into Dec/Feb."""
    offset = _TZ_OFFSETS.get((ev.tz_original or "GMT").strip().upper(), 0)
    return (ev.start_utc + dt.timedelta(hours=offset)).month

def should_post(ev: Event) -> tuple:
    """(keep?, reason). Broker + hard-block checks win over everything."""
    hay = ev.title.lower()

    # 0. J.P. MORGAN SPECIAL CASE (date-aware).
    #    Keep the January Healthcare Conference; drop JPM confs any other month.
    if any(j in hay for j in JPM_NAMES):
        if _local_month(ev) == 1:
            ev.event_type = "jpm_healthcare"
            return True, "allowed (J.P. Morgan Healthcare Conf - January exception)"
        return False, "blocked (J.P. Morgan broker conf outside January)"

    # 1. Sell-side / broker-hosted conference -> always drop.
    broker = next((b for b in BROKER_NAMES if b in hay), None)
    if broker:
        return False, f"blocked (broker/sell-side host: {broker.strip()!r})"

    # 2. Low-value formats -> always drop.
    blocked = next((k for k in HARD_BLOCK_KEYWORDS if k in hay), None)
    if blocked:
        return False, f"blocked (keyword: {blocked!r})"

    # 3. Classify against the guest list (incl. medical congresses).
    ev.event_type = classify(ev)
    if ev.event_type != "unknown":
        return True, f"allowed ({ev.event_type})"

    # 4. Not sure -> safe mode: skip but log for a human to eyeball.
    return False, "not on allow-list (safe mode: skip + log)"


# ---------------------------------------------------------------------------
# 7. .ICS WRITER  (only events that passed the bouncer)
# ---------------------------------------------------------------------------
def _ics_dt(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

def write_ics(events: list, path: str) -> None:
    now = _ics_dt(dt.datetime.now(dt.timezone.utc))
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//pharma-ir-calendar//EN", "CALSCALE:GREGORIAN"]
    for ev in events:
        end = ev.end_utc or (ev.start_utc + dt.timedelta(hours=1))
        desc_bits = [f"Type: {ev.event_type}", f"Source: {ev.source_url}"]
        if ev.webcast_url:
            desc_bits.append(f"Webcast: {ev.webcast_url}")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ev.uid}",
            f"DTSTAMP:{now}",
            f"DTSTART:{_ics_dt(ev.start_utc)}",
            f"DTEND:{_ics_dt(end)}",
            f"SUMMARY:{_esc(ev.company + ': ' + ev.title)}",
            f"DESCRIPTION:{_esc('  |  '.join(desc_bits))}",
        ]
        if ev.location:
            lines.append(f"LOCATION:{_esc(ev.location)}")
        if ev.webcast_url:
            lines.append(f"URL:{_esc(ev.webcast_url)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    with open(path, "w") as f:
        f.write("\r\n".join(lines) + "\r\n")


# ---------------------------------------------------------------------------
# 8. RUN IT on the REAL Pfizer payload + medical-congress test cases
# ---------------------------------------------------------------------------
PFIZER_SAMPLE = json.loads(r'''
{"GetEventListResult":[{"Attachments":[{"DocumentType":"file","Extension":"PDF","Size":"186 KB","Title":"Event Announcement","Type":"Document","Url":"https://s206.q4cdn.com/795948973/files/doc_events/2026/Sep/22/Pfizer-Invites-Public-JPM-US-All-Stars-2026-FINAL2.pdf"}],"Body":"","EventId":2102,"IsWebcast":true,"GlobalTimeZoneId":47,"LanguageId":1,"LinkToDetailPage":"/Investors/news-events/event-details/2026/JP-Morgan-US-All-Stars-2026-Conference-2026-GLyN0E8ncO/default.aspx","LinkToUrl":"","Location":"","SeoName":"JP-Morgan-US-All-Stars-2026-Conference-2026-GLyN0E8ncO","TagsList":["webcast","healthcare"],"TimeZone":"GMT","Title":"J.P. Morgan U.S. All Stars 2026 Conference","WebCastLink":"https://kvgo.com/jpm/pfizer-september-2026","EndDate":"09/22/2026 12:50:00","StartDate":"09/22/2026 11:50:00"}]}
''')

EXTRA_SAMPLE = {"GetEventListResult": [
    {"EventId": 2103, "Title": "Pfizer Fourth Quarter 2026 Earnings Call",
     "TimeZone": "EST", "StartDate": "01/27/2027 08:00:00", "EndDate": "01/27/2027 09:00:00",
     "IsWebcast": True, "WebCastLink": "https://example.com/pfe-q4", "Location": "",
     "LinkToDetailPage": "/Investors/news-events/event-details/2027/q4/default.aspx", "TagsList": ["webcast"]},
    {"EventId": 2104, "Title": "Pfizer 2027 R&D Day",
     "TimeZone": "EDT", "StartDate": "05/14/2027 13:00:00", "EndDate": "05/14/2027 17:00:00",
     "IsWebcast": True, "WebCastLink": "https://example.com/pfe-rnd", "Location": "New York, NY",
     "LinkToDetailPage": "/Investors/news-events/event-details/2027/rndday/default.aspx", "TagsList": ["webcast"]},
    # --- the new litmus tests: medical congresses we WANT to keep ---
    {"EventId": 2105, "Title": "Pfizer Data Presentations at ESMO Congress 2026",
     "TimeZone": "CEST", "StartDate": "10/17/2026 08:00:00", "EndDate": "10/21/2026 17:00:00",
     "IsWebcast": False, "WebCastLink": "", "Location": "Berlin, Germany",
     "LinkToDetailPage": "/Investors/news-events/event-details/2026/esmo/default.aspx", "TagsList": ["oncology"]},
    {"EventId": 2106, "Title": "Investor Call: ADA 2026 Scientific Sessions Data Review",
     "TimeZone": "EDT", "StartDate": "06/20/2026 12:00:00", "EndDate": "06/20/2026 13:00:00",
     "IsWebcast": True, "WebCastLink": "https://example.com/ada", "Location": "",
     "LinkToDetailPage": "/Investors/news-events/event-details/2026/ada/default.aspx", "TagsList": ["webcast"]},
    # --- broker-hosted conferences we should still DROP ---
    {"EventId": 2107, "Title": "UBS Global Healthcare Conference",
     "TimeZone": "EDT", "StartDate": "05/19/2026 14:00:00", "EndDate": "05/19/2026 14:40:00",
     "IsWebcast": True, "WebCastLink": "https://example.com/ubs", "Location": "",
     "LinkToDetailPage": "/Investors/news-events/event-details/2026/ubs/default.aspx", "TagsList": ["webcast"]},
    {"EventId": 2108, "Title": "Goldman Sachs 47th Annual Global Healthcare Conference",
     "TimeZone": "EDT", "StartDate": "06/09/2026 15:20:00", "EndDate": "06/09/2026 16:00:00",
     "IsWebcast": True, "WebCastLink": "https://example.com/gs", "Location": "",
     "LinkToDetailPage": "/Investors/news-events/event-details/2026/gs/default.aspx", "TagsList": ["webcast"]},
    # --- JANUARY J.P. Morgan Healthcare Conference: the must-KEEP exception ---
    {"EventId": 2109, "Title": "44th Annual J.P. Morgan Healthcare Conference",
     "TimeZone": "PST", "StartDate": "01/13/2027 09:00:00", "EndDate": "01/13/2027 09:40:00",
     "IsWebcast": True, "WebCastLink": "https://example.com/jpm-jan", "Location": "San Francisco, CA",
     "LinkToDetailPage": "/Investors/news-events/event-details/2027/jpm/default.aspx", "TagsList": ["webcast"]},
    # --- JPM conference in another month: still DROP ---
    {"EventId": 2110, "Title": "J.P. Morgan European Healthcare Conference",
     "TimeZone": "GMT", "StartDate": "09/03/2026 10:00:00", "EndDate": "09/03/2026 10:40:00",
     "IsWebcast": True, "WebCastLink": "https://example.com/jpm-sep", "Location": "London",
     "LinkToDetailPage": "/Investors/news-events/event-details/2026/jpm-eu/default.aspx", "TagsList": ["webcast"]},
]}

if __name__ == "__main__":
    OFFLINE = os.environ.get("PHARMA_OFFLINE") == "1"

    # In OFFLINE test mode we feed the pipeline the REAL Pfizer payload you
    # captured, plus the litmus-test events, so we can prove behavior without
    # a network. In LIVE mode we call fetch_q4() for every company.
    OFFLINE_RAW = {
        "pfizer": {"GetEventListResult":
                   PFIZER_SAMPLE["GetEventListResult"] + EXTRA_SAMPLE["GetEventListResult"]}
    }

    all_kept, all_parsed = [], []
    for key, cfg in COMPANIES.items():
        if OFFLINE:
            raw = OFFLINE_RAW.get(key, {"GetEventListResult": []})
        else:
            raw = fetch_q4(cfg)   # raises loudly on failure -> Action goes red

        parsed = parse_q4(raw, cfg)
        all_parsed += parsed
        print("=" * 78)
        print(f"BOUNCER DECISIONS  --  {cfg['name']}  ({'offline' if OFFLINE else 'LIVE'})")
        print("=" * 78)
        for ev in parsed:
            keep, reason = should_post(ev)
            flag = "KEEP" if keep else "DROP"
            print(f"[{flag}]  {ev.start_utc:%Y-%m-%d %H:%MZ}  {ev.title}")
            print(f"        -> {reason}")
            if keep:
                all_kept.append(ev)

    write_ics(all_kept, "pharma_events.ics")
    print("\n" + "=" * 78)
    print(f"Wrote {len(all_kept)} event(s) to the .ics "
          f"(dropped {len(all_parsed)-len(all_kept)} across {len(COMPANIES)} company(ies)).")
    print("=" * 78)
