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
import sys
import json
import html
import hashlib
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error
import gzip
import zlib
import socket
import http.cookiejar
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
        "adapter": "q4",
        "ir_host": "https://investors.pfizer.com",
        # CONFIRMED from DevTools Headers tab: GET, params in the query string.
        "endpoint": "https://investors.pfizer.com/feed/Event.svc/GetEventList",
        "method": "GET",
        "query": Q4_DEFAULT_QUERY,
    },
    # CONFIRMED from DevTools: Gilead runs the same Q4 endpoint (GET, query params).
    "gilead": {
        "name": "Gilead",
        "adapter": "q4",
        "ir_host": "https://investors.gilead.com",
        "endpoint": "https://investors.gilead.com/feed/Event.svc/GetEventList",
        "method": "GET",
        "query": Q4_DEFAULT_QUERY,
    },
    # Same shape, just different host/endpoint -- add once each is confirmed.
    # (Confirm each site uses the standard /feed/Event.svc/GetEventList path.)
    "jnj": {
        "name": "Johnson & Johnson",
        "adapter": "q4",
        "ir_host": "https://www.investor.jnj.com",
        "endpoint": "https://www.investor.jnj.com/feed/Event.svc/GetEventList",
        "method": "GET",
        "query": Q4_DEFAULT_QUERY,
    },
    # CONFIRMED Q4 site: BMS returns the standard GetEventListResult envelope
    # (EventId / WebCastLink / TimeZone:"ET" / StartDate "MM/DD/YYYY HH:MM:SS"),
    # LinkToDetailPage under /iframes/events-and-presentations/... -> reuse fetch_q4.
    # Host/path guessed to the standard Q4 pattern; if the live run 404s/405s,
    # re-capture the exact GetEventList request from DevTools and swap it in.
    "bms": {
        "name": "Bristol Myers Squibb",
        "adapter": "q4",
        # Live host confirmed via DevTools: BMS's IR data lives on its Q4 portal
        # (bristolmyers2016ir.q4web.com), NOT the www.bms.com corporate site.
        "ir_host": "https://bristolmyers2016ir.q4web.com",
        "endpoint": "https://bristolmyers2016ir.q4web.com/feed/Event.svc/GetEventList",
        "method": "GET",
        "query": Q4_DEFAULT_QUERY,
    },
    # --- FIRST NON-Q4 SITE. AstraZeneca runs Adobe Experience Manager (AEM),
    # which serves events as JSON at a .eventsListing.json selector. Totally
    # different schema, so it uses its own adapter ("astrazeneca") below.
    # NOTE: the endpoint contains an AEM component id (eventslistingcontain_...).
    # If AZ rebuilds the page that id can change -> re-capture the URL if the
    # fetch ever 404s.
    "astrazeneca": {
        "name": "AstraZeneca",
        "adapter": "astrazeneca",
        "ir_host": "https://www.astrazeneca.com",
        "endpoint": ("https://www.astrazeneca.com/content/astraz/investor-relations/"
                     "events/jcr:content/par/contentwrapper/wrapperPar/"
                     "eventslistingcontain_1772792074.eventsListing.json"),
        "method": "GET",
    },
    # --- SECOND NON-Q4 SITE. Roche runs a purpose-built public REST API on
    # its own domain (api-prod.roche.com). Clean JSON, no auth, but PAGINATED
    # (Total/Page) and pre-filtered to upcoming investor events, so it uses its
    # own "roche" adapter which walks all pages.
    "roche": {
        "name": "Roche",
        "adapter": "roche",
        "ir_host": "https://www.roche.com",
        "endpoint": "https://api-prod.roche.com/externaldatasources/events/upcoming",
        "method": "GET",
        # 'page' is added per-request by fetch_roche(); these are the fixed filters.
        "query": {"category": "investor", "tag": "all"},
    },
    # --- THIRD NON-Q4-API SITE (but Merck IS a Q4 CUSTOMER). Merck renders its
    # Q4 event data SERVER-SIDE into its own WordPress page (mco-q4-* blocks,
    # transcripts on s21.q4cdn.com/488056881, signup iframe on
    # merck2016rd.q4web.com) -- so there is NO GetEventList API exposed here.
    # We scrape the events page HTML instead. Dates only (no times) -> all-day.
    "merck": {
        "name": "Merck",
        "adapter": "merck",
        "ir_host": "https://www.merck.com",
        "endpoint": "https://www.merck.com/investor-relations/events-and-presentations/",
        "method": "GET",
    },
    # --- FOURTH SCRAPE SITE, NEW PLATFORM. Eli Lilly runs the NASDAQ IR
    # "nir-widget" stack on Drupal (profiles/nasdaqir; blocks named
    # nir-widget--event--*). NO JSON API fires -- events are rendered
    # SERVER-SIDE into two HTML tables (table_upcoming_events /
    # table_archived_events). Unlike Merck, each row carries a full
    # date+time+timezone (e.g. "October 29, 2026 at 10:00 AM EDT"), so these
    # are TIMED events (not all-day). We scrape the upcoming table only.
    "lilly": {
        "name": "Eli Lilly",
        "adapter": "lilly",
        "ir_host": "https://investor.lilly.com",
        "endpoint": "https://investor.lilly.com/webcasts-and-presentations",
        "method": "GET",
    },
    # --- SIXTH PLATFORM, but only the THIRD "parse JSON" adapter type (after
    # Q4 and Roche). Novo Nordisk runs Adobe AEM and exposes a public search
    # servlet at /bin/nncorp/investoreventsearch. No auth (the captured
    # 'Authorization;' header is empty). Clean JSON, but PAGINATED via an
    # OFFSET: 'currentresults' is the offset, 'limit' the page size, and the
    # response carries data.numberOfResults (the total) to walk to. Times are
    # naive Copenhagen (CET/CEST) and look like padded placeholder spans
    # (e.g. 04:00->21:55), so -- like Merck/AZ -- we treat them as ALL-DAY.
    "novo": {
        "name": "Novo Nordisk",
        "adapter": "novo",
        "ir_host": "https://www.novonordisk.com",
        "endpoint": "https://www.novonordisk.com/bin/nncorp/investoreventsearch",
        "method": "GET",
        # Fixed filters; 'start'/'end' (date window) and the paging offset are
        # added per-request by fetch_novo(). 'eventpath' points AEM at the
        # investor calendar content fragments.
        "query": {
            "eventpath": ("/content/dam/nncorp/global/en/investors/"
                          "content-fragments/calendar"),
            "location": "",
            "category": "",
            "function": "search",
        },
        "page_size": 50,          # 'limit' per request
        "window_days": 400,       # how far ahead to ask for events
    },
    # --- SECOND INSTANCE OF PLATFORM #5 (Lilly's stack), but a DIFFERENT
    # widget rendering. Amgen also runs the NASDAQ IR "nir-widget" stack on
    # Drupal (theme nir_pid3019; sites/g/files/knoqqb60211) behind Akamai Bot
    # Manager (akam/13/... sensor) -- so it REUSES the Akamai-hardened fetch.
    # BUT unlike Lilly it does NOT render <table>s: events are div.item blocks
    # inside two widgets keyed by CLASS -- block--upcoming-event and
    # block--past-events. Each item has <span class="date-1"> with a
    # MM.DD.YYYY h:mm AM/PM TZ stamp (e.g. "Tuesday, 09.15.2026 11:30 AM EDT")
    # and <a class="event-content title-1">. Times+tz present -> TIMED events.
    # We isolate ONLY the upcoming widget so past events are never ingested.
    "amgen": {
        "name": "Amgen",
        "adapter": "amgen",
        "ir_host": "https://investors.amgen.com",
        "endpoint": "https://investors.amgen.com/news-and-events/events-calendar/",
        "method": "GET",
    },
    # --- THIRD INSTANCE OF PLATFORM #5 (Lilly's stack), and a THIRD widget
    # rendering. AbbVie also runs the NASDAQ IR "nir-widget" Drupal stack, so it
    # REUSES the Akamai-hardened fetch. It is SERVER-RENDERED (no data call
    # fires -- the only JSON in DevTools is a OneTrust cookie blob, a red
    # herring). Two traps vs Amgen: (1) BOTH widgets carry the SAME
    # block--upcoming class, so we isolate on the <h2> heading ("Upcoming
    # events" vs "Past Events"), NOT on class; (2) events are <article
    # node--type-nir-event> blocks whose date is "MM/DD/YY h:mm am/pm TZ"
    # (2-digit year, lowercase am/pm, and the first CENTRAL-time site: CDT/CST).
    "abbvie": {
        "name": "AbbVie",
        "adapter": "abbvie",
        "ir_host": "https://investors.abbvie.com",
        # Confirmed live events-page URL (user-supplied).
        "endpoint": "https://investors.abbvie.com/events-and-presentations/upcoming-events",
        "method": "GET",
    },
    # -- Sanofi -------------------------------------------------------------
    # NEW PLATFORM (#7): Magnolia CMS on React-Router SSR. NO events XHR fires
    # (DevTools shows only OneTrust cookie traffic -- a red herring); the data
    # rides down INSIDE the initial HTML document as a React hydration stream
    # (window.__reactRouterContext.streamController.enqueue("...")). The visible
    # DOM only renders the ACTIVE tab, so we parse the stream, which carries
    # every tab (Quarterly results / Conferences / Presentations / AGM).
    # TRAP: startDate is "...T12:30:00.000Z" but the true time is 11:30Z -- the
    # 'Z' is a LIE (naive Paris/Copenhagen wall-clock mislabelled UTC). The
    # adjacent "timezone" string ('CET (...)') is the reliable label.
    "sanofi": {
        "name": "Sanofi",
        "adapter": "sanofi",
        "ir_host": "https://www.sanofi.com",
        "endpoint": "https://www.sanofi.com/en/investors/news-events/upcoming-events",
        "method": "GET",
    },
}

# ---------------------------------------------------------------------------
# 2. THE BOUNCER'S RULEBOOK
# ---------------------------------------------------------------------------
# Guest list: canonical event types you always want, matched by keywords.
ALLOW_TYPES = {
    "earnings_call":       ["earnings", "quarter", "q1", "q2", "q3", "q4", "full year",
                            "results", "sales"],
    "capital_markets_day": ["capital markets", "investor day", "cmd", "r&d day", "rnd day",
                            "pharma day"],
    "agm":                 ["annual general meeting", "agm", "annual meeting"],
}

# Medical / scientific congresses you DO want (pipeline data readouts, symposia).
# Matched on WHOLE WORDS so short acronyms like "ada"/"acc"/"ash" don't fire
# inside unrelated words (e.g. "roadshow", "accelerate").
MEDICAL_CONFERENCES = [
    "asco", "esmo", "ada", "easd", "aha", "acc", "ash", "aacr", "ers", "ats",
    "acr", "eular", "ddw", "obesityweek", "endo", "esc", "croi", "aasld",
    "sabcs", "wclc", "asn", "idweek", "ueg", "asbmr", "ash", "eha", "wclc",
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
    # --- additional sell-side hosts seen in AstraZeneca / Roche real payloads ---
    "santander", "kepler", "carnegie", "nordea", "danske", "bnp paribas",
    "baader",
    "capital group", "capital world", "handelsbanken", "daiwa", "rothschild",
    "kantonalbank", "putnam", "db access", "dbaccess", "seb nordic", "seb,",
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
    all_day: bool = False       # AstraZeneca-style date-only events


# ---------------------------------------------------------------------------
# 4. TIMEZONE HANDLING  (the #1 source of pharma-calendar bugs)
# ---------------------------------------------------------------------------
# Legacy fixed table (kept for reference / any external callers). Prefer
# _resolve_offset(), which is DST-aware. Bare labels here are STANDARD-time
# values; the resolver upgrades them to daylight time when the date warrants.
_TZ_OFFSETS = {
    "GMT": 0, "UTC": 0,
    "BST": +1, "CET": +1, "CEST": +2,
    "ET": -5, "EST": -5, "EDT": -4,
    "CT": -6, "CST": -6, "CDT": -5,
    "PT": -8, "PST": -8, "PDT": -7,
}

# Labels whose offset is UNAMBIGUOUS -- an explicit standard/daylight name, or a
# zero-offset zone. Used verbatim, no date logic.
_FIXED_OFFSETS = {
    "GMT": 0, "UTC": 0, "BST": +1, "CEST": +2,
    "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "PST": -8, "PDT": -7,
}
# AMBIGUOUS labels: a bare zone name that could be standard OR daylight depending
# on the event's DATE. Value = (standard_offset, daylight_offset). BMS sends a
# bare "ET"; Novo labels everything "CET" year-round -- both need resolving.
_AMBIG_US = {"ET": (-5, -4), "CT": (-6, -5), "PT": (-8, -7)}
_AMBIG_EU = {"CET": (+1, +2)}   # European Central; explicit "CEST" stays fixed

def _nth_sunday(year: int, month: int, n: int) -> int:
    """Day-of-month of the n-th Sunday of the given month."""
    first = dt.datetime(year, month, 1)
    first_sunday = 1 + ((6 - first.weekday()) % 7)     # Mon=0..Sun=6
    return first_sunday + 7 * (n - 1)

def _last_sunday(year: int, month: int) -> int:
    """Day-of-month of the LAST Sunday of the given month."""
    nextm = dt.datetime(year + 1, 1, 1) if month == 12 else dt.datetime(year, month + 1, 1)
    last = nextm - dt.timedelta(days=1)
    return last.day - ((last.weekday() - 6) % 7)

def _is_us_dst(d: dt.datetime) -> bool:
    """US DST: 2nd Sunday of March 02:00 -> 1st Sunday of November 02:00 (local wall)."""
    start = dt.datetime(d.year, 3, _nth_sunday(d.year, 3, 2), 2, 0)
    end = dt.datetime(d.year, 11, _nth_sunday(d.year, 11, 1), 2, 0)
    return start <= d < end

def _is_eu_dst(d: dt.datetime) -> bool:
    """EU DST: last Sunday of March -> last Sunday of October (local wall ~02:00/03:00)."""
    start = dt.datetime(d.year, 3, _last_sunday(d.year, 3), 2, 0)
    end = dt.datetime(d.year, 10, _last_sunday(d.year, 10), 3, 0)
    return start <= d < end

def _resolve_offset(tz_label: str, naive: dt.datetime) -> Optional[int]:
    """UTC offset (hours) for a timezone label on a SPECIFIC date.

    Explicit labels (EST/EDT/CEST/...) are fixed; bare labels (ET/CT/PT/CET) are
    resolved against the correct DST calendar for the event's OWN date -- so an
    October 'ET' event is EDT (-4) while a January 'ET' event is EST (-5). This
    is what keeps a bare-'ET' site like BMS from landing an hour off in summer."""
    lab = (tz_label or "GMT").strip().upper()
    if lab in _FIXED_OFFSETS:
        return _FIXED_OFFSETS[lab]
    if lab in _AMBIG_US:
        std, dst = _AMBIG_US[lab]
        return dst if _is_us_dst(naive) else std
    if lab in _AMBIG_EU:
        std, dst = _AMBIG_EU[lab]
        return dst if _is_eu_dst(naive) else std
    return None

def _to_utc(date_str: str, tz_label: str) -> Optional[dt.datetime]:
    if not date_str:
        return None
    naive = dt.datetime.strptime(date_str.strip(), "%m/%d/%Y %H:%M:%S")
    offset = _resolve_offset(tz_label, naive)
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
# 5b. ASTRAZENECA ADAPTER  (Adobe Experience Manager, NOT Q4)
# ---------------------------------------------------------------------------
# AZ's AEM feed shape:
#   {"future":[ {"month","year","entry":[ {title, href, date:{timestamp,
#                start:{day}, end:{day}}} ...]} ...], "past":[ ... ]}
# Only "future" is used (forward-looking calendar). Dates are DATE-ONLY
# (timestamps are all midnight), so these become all-day events. The event
# TYPE is encoded as the first comma-segment of the title
# ("Conference, Morgan Stanley...", "ESMO, AZN Meet the Management...") -- which
# is exactly what the shared bouncer already keys on, so no new filter logic
# is needed: broker names/formats drop, medical acronyms + earnings/AGM keep.
def fetch_astrazeneca(cfg: dict) -> dict:
    """Fetch AstraZeneca's AEM events JSON. Raises loudly on any failure so the
    Action goes red instead of silently shipping an empty calendar."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        # AEM only returns JSON for this selector when it looks like the page's
        # own AJAX call:
        "X-Requested-With": "XMLHttpRequest",
        "Referer": cfg["ir_host"].rstrip("/") + "/investor-relations/events.html",
    }
    req = urllib.request.Request(cfg["endpoint"], headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(
            f"{cfg['name']}: HTTP {e.code} from {cfg['endpoint']}\n"
            f"  -> AEM component id may have changed; re-capture the events JSON URL.\n"
            f"  server said: {detail}"
        ) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"{cfg['name']}: could not reach {cfg['endpoint']} ({e.reason})") from None

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"{cfg['name']}: response was not JSON (first 300 chars):\n{body[:300]}"
        ) from None
    if "future" not in parsed and "past" not in parsed:
        raise RuntimeError(
            f"{cfg['name']}: JSON had no 'future'/'past' keys. Got: {list(parsed)[:8]}"
        )
    return parsed


_AZ_TAIL_TAG = re.compile(r"\s*\[[^\]]*\]\s*$")   # strip trailing " [Management]" / " [IR]"

def _az_parse_ts(ts: str):
    """AZ timestamp '2026-10-30T00:00:00.000+0000' -> date-only UTC datetime."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = dt.datetime.strptime(ts.strip(), fmt)
            return d.astimezone(dt.timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            continue
    return None

def _az_end(start: dt.datetime, end_day: int):
    """Build the inclusive end date from a day-of-month, rolling into next
    month when end_day < start.day (e.g. a roadshow spanning a month boundary)."""
    y, m = start.year, start.month
    if end_day < start.day:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    try:
        return start.replace(year=y, month=m, day=end_day)
    except ValueError:
        return None

def parse_astrazeneca(raw_json: dict, cfg: dict) -> list:
    events = []
    today = dt.datetime.now(dt.timezone.utc).date()
    for block in raw_json.get("future", []):
        for e in block.get("entry", []) or []:
            title = _AZ_TAIL_TAG.sub("", (e.get("title") or "").strip()).strip()
            if not title:
                continue
            date_obj = e.get("date") or {}
            start = _az_parse_ts(date_obj.get("timestamp"))
            if not start:
                continue
            if start.date() < today:      # forward-looking only
                continue
            end = None
            end_day = ((date_obj.get("end") or {}).get("day"))
            if end_day:
                try:
                    end = _az_end(start, int(end_day))
                except (TypeError, ValueError):
                    end = None
            href = e.get("href") or ""
            if href.startswith("/"):
                source_url = cfg["ir_host"].rstrip("/") + href
            elif href:
                source_url = href
            else:
                source_url = cfg["ir_host"].rstrip("/") + "/investor-relations/events.html"
            ev = Event(
                company=cfg["name"],
                title=title,
                start_utc=start,
                end_utc=end,
                tz_original="GMT",
                event_type="unknown",
                source_url=source_url,
                webcast_url="",
                location="",
                tags=[],
                all_day=True,
            )
            basis = f"{cfg['name']}|{title}|{start.date()}"
            ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
            events.append(ev)
    return events


# ---------------------------------------------------------------------------
# 5c. ROCHE ADAPTER  (purpose-built public REST API, NOT Q4 / NOT AEM)
# ---------------------------------------------------------------------------
# Roche serves a clean paginated JSON API on its own domain:
#   https://api-prod.roche.com/externaldatasources/events/upcoming
#       ?category=investor&tag=all&page=N
# Shape:  {"Items":[ {..event..} ], "Total": <int>, "Page": <int> }
# Each item uses Title-Case keys with SPACES, e.g.:
#   "Event Title", "Event Tag", "Timezone" (BST/CEST/CET/GMT/EST),
#   "Location", "Link" (relative detail path), "Start Date"/"End Date"
#   as "YYYY-MM-DD HH:MM", and "Category".
# Two wrinkles vs the other adapters:
#   (1) PAGINATION -- one page returns a subset; loop until we've collected
#       "Total" items (or a page comes back empty).
#   (2) date format is "%Y-%m-%d %H:%M" (no seconds) -- own parser below.
def _roche_to_utc(date_str, tz_label):
    """Roche 'YYYY-MM-DD HH:MM' + tz abbreviation -> aware UTC datetime."""
    if not date_str:
        return None
    naive = dt.datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M")
    offset = _resolve_offset(tz_label, naive)
    if offset is None:
        raise ValueError(f"Roche: unknown timezone label {tz_label!r} for {date_str!r}")
    return (naive - dt.timedelta(hours=offset)).replace(tzinfo=dt.timezone.utc)


def fetch_roche(cfg):
    """Fetch ALL pages of Roche's investor events API and return a combined
    {"Items":[...], "Total":N}. Raises loudly on any failure so the Action
    goes red rather than silently shipping an empty calendar."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": cfg["ir_host"].rstrip("/") + "/",
    }
    base_query = dict(cfg.get("query") or {})
    items, total, page = [], None, 1
    MAX_PAGES = 50  # safety valve so a misbehaving API can't loop forever
    while page <= MAX_PAGES:
        q = dict(base_query); q["page"] = str(page)
        url = cfg["endpoint"] + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(
                f"{cfg['name']}: HTTP {e.code} from {url}\n"
                f"  -> re-capture the Roche events API request if the path changed.\n"
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
        if "Items" not in parsed:
            raise RuntimeError(
                f"{cfg['name']}: JSON had no 'Items' key. Got: {list(parsed)[:8]}"
            )
        page_items = parsed.get("Items") or []
        items += page_items
        if total is None:
            total = parsed.get("Total")
        # Stop when we've got everything, or the API returns an empty page.
        if not page_items:
            break
        if isinstance(total, int) and len(items) >= total:
            break
        page += 1
    return {"Items": items, "Total": total if total is not None else len(items)}


def parse_roche(raw_json, cfg):
    events = []
    for e in raw_json.get("Items", []):
        title = (e.get("Event Title") or "").strip()
        if not title:
            continue
        tz = e.get("Timezone") or "GMT"
        start = _roche_to_utc(e.get("Start Date"), tz)
        if not start:
            continue
        end = _roche_to_utc(e.get("End Date"), tz)
        link = e.get("Link") or ""
        if link.startswith("/"):
            source_url = cfg["ir_host"].rstrip("/") + link
        elif link:
            source_url = link
        else:
            source_url = cfg["ir_host"].rstrip("/") + "/investors/events.htm"
        tag = e.get("Event Tag")
        ev = Event(
            company=cfg["name"],
            title=title,
            start_utc=start,
            end_utc=end,
            tz_original=tz,
            event_type="unknown",
            source_url=source_url,
            webcast_url="",
            location=e.get("Location") or "",
            tags=[tag] if tag else [],
        )
        basis = f"{cfg['name']}|{title}|{start}"
        ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# 5d. MERCK ADAPTER  (HTML scrape -- Merck is a Q4 customer but renders events
#     server-side into its own WordPress page, so there is no JSON API to hit)
# ---------------------------------------------------------------------------
# The events page has 3 tabs: "Featured event" (tab-content-1, a duplicate of
# an upcoming event), "Upcoming events" (tab-content-2) and "Past events"
# (tab-content-3). We isolate ONLY the upcoming tab so we never ingest past
# events or the featured duplicate. Each event item looks like:
#   <div class="mco-q4-events-list-block-events-item-container">
#     <div class="mco-q4-events-list-events-item-date"><p>October 26, 2026</p></div>
#     <div class="mco-q4-events-list-events-item-headline">
#       <p class="mco-tag"><small class="tagline">Webcast</small></p>
#       <p><a href="https://www.merck.com/events/...">Investor Event at ESMO 2026</a></p>
#       ...<a href='https://onlinexperiences.com/...'>Webcast</a>...
# Dates are date-only -> all-day events (reuse the AstraZeneca all-day path).
_MERCK_ITEM = "mco-q4-events-list-block-events-item-container"

def fetch_merck(cfg: dict) -> str:
    """Fetch Merck's IR events page HTML. Raises loudly on any failure so the
    Action goes red instead of silently shipping an empty calendar."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(cfg["endpoint"], headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{cfg['name']}: HTTP {e.code} from {cfg['endpoint']}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"{cfg['name']}: could not reach {cfg['endpoint']} ({e.reason})") from None
    if _MERCK_ITEM not in body:
        raise RuntimeError(
            f"{cfg['name']}: page markup changed (no '{_MERCK_ITEM}' blocks found). "
            f"Re-inspect the events page HTML."
        )
    return body

def _merck_strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()

def parse_merck(raw_html: str, cfg: dict) -> list:
    events = []
    if not raw_html:
        return events
    today = dt.datetime.now(dt.timezone.utc).date()

    # Isolate ONLY the "Upcoming events" tab (content-2), stopping at the "Past
    # events" tab (content-3). This drops past events AND the featured duplicate.
    start = raw_html.find("mccberg-tab-content-2")
    if start == -1:
        return events
    end = raw_html.find("mccberg-tab-content-3", start)
    region = raw_html[start:end] if end != -1 else raw_html[start:]

    for ch in region.split(_MERCK_ITEM)[1:]:
        dm = re.search(r"events-item-date.*?<p>\s*(.*?)\s*</p>", ch, re.S)
        if not dm:
            continue
        date_txt = _merck_strip_tags(dm.group(1))
        try:
            start_dt = dt.datetime.strptime(date_txt, "%B %d, %Y").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if start_dt.date() < today:                 # forward-looking only
            continue
        # Title + detail-page URL: the anchor pointing at /events/.
        tm = re.search(r'<a\s+href="([^"]*/events/[^"]*)"[^>]*>\s*(.*?)\s*</a>', ch, re.S)
        if not tm:
            continue
        source_url = html.unescape(tm.group(1)).strip()
        title = _merck_strip_tags(tm.group(2))
        if not title:
            continue
        tag_m = re.search(r'class="tagline">\s*(.*?)\s*</small>', ch, re.S)
        tag = _merck_strip_tags(tag_m.group(1)) if tag_m else ""
        # Webcast link (Merck uses single-quoted hrefs for webcast/PDF links;
        # the title/Add-to-Calendar links use double quotes). Skip document links.
        webcast = ""
        wc_m = re.search(r"href='([^']+)'", ch)
        if wc_m:
            cand = html.unescape(wc_m.group(1)).strip()
            if not cand.lower().endswith((".pdf", ".xlsx", ".doc", ".docx")):
                webcast = cand
        ev = Event(
            company=cfg["name"],
            title=title,
            start_utc=start_dt,
            end_utc=None,
            tz_original="GMT",
            event_type="unknown",
            source_url=source_url,
            webcast_url=webcast,
            location="",
            tags=[tag] if tag else [],
            all_day=True,
        )
        basis = f"{cfg['name']}|{title}|{start_dt.date()}"
        ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        events.append(ev)
    return events



# ---------------------------------------------------------------------------
# 5e. ELI LILLY ADAPTER  (HTML scrape -- NASDAQ IR "nir-widget" stack on Drupal;
#     no JSON API fires, events are server-rendered into HTML tables)
# ---------------------------------------------------------------------------
# The page has two tables: "Upcoming Events" (table_upcoming_events) and
# "Archived Events" (table_archived_events). We isolate ONLY the upcoming table
# so past events are never ingested. Each <tr> looks like:
#   <div class="nir-widget--field nir-widget--event--date"> October 29, 2026 at 10:00 AM EDT </div>
#   <div class="field-nir-event-title"><div class="field__item">Q3 2026 Earnings Call</div></div>
#   <div class="normal-webcast-link field__item"><a href="https://..." ...>Listen to webcast</a></div>
#   ...the "Add to Google Calendar" link carries the detail page URL:
#      details=Event Details: https://investor.lilly.com/events/event-details/...
# Unlike Merck, rows carry a full time + tz label (EDT/EST) -> TIMED events.
_LILLY_MARKER = "nir-widget--event--date"

def fetch_lilly(cfg: dict) -> str:
    """Fetch Lilly's webcasts & presentations page HTML.

    investor.lilly.com sits behind Akamai Bot Manager (note the `akam/13/...`
    sensor + pixel in the page). A bare urllib GET with minimal headers gets
    *tarpitted* -- the TCP connection opens but the body never streams back,
    surfacing as `TimeoutError: The read operation timed out` (NOT an HTTP
    4xx/5xx). We defeat the basic tier three ways:
      1. Send a complete, self-consistent Chrome header set (UA + client hints
         + Sec-Fetch-* + Accept-Encoding), so passive fingerprinting passes.
      2. Carry a cookie jar across attempts, so the `ak_bmsc`/`bm_sv` cookies
         Akamai sets on first contact are echoed back on the retry.
      3. Retry with backoff and catch the read-timeout explicitly, so a stall
         becomes a clean red error instead of an uncaught traceback.
    Raises loudly (RuntimeError) on definitive failure so the Action goes red
    rather than shipping an empty calendar.
    """
    url = cfg["endpoint"]
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",          # decompressed below
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": ('"Google Chrome";v="153", "Not_A Brand";v="8", '
                      '"Chromium";v="153"'),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
    }

    # An opener with its own cookie jar => Akamai's set-cookie survives to retry.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def _read_body(resp):
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return raw.decode("utf-8", "replace")

    last_err = None
    body = None
    for attempt in range(1, 4):            # up to 3 tries
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=45) as resp:
                body = _read_body(resp)
            break
        except urllib.error.HTTPError as ex:
            # A real HTTP status (e.g. 403 challenge) -> report it, do not retry.
            raise RuntimeError(
                f"{cfg['name']}: HTTP {ex.code} from {url} "
                f"(Akamai challenge? headers may need a session token)"
            ) from None
        except (TimeoutError, socket.timeout) as ex:
            last_err = f"read timed out (attempt {attempt}/3)"
        except urllib.error.URLError as ex:
            last_err = f"{ex.reason} (attempt {attempt}/3)"
        if attempt < 3:
            import time as _t
            _t.sleep(2 * attempt)          # 2s, 4s backoff; 2nd hit carries cookie

    if body is None:
        raise RuntimeError(
            f"{cfg['name']}: could not fetch {url} -- {last_err}. "
            f"Akamai Bot Manager is tarpitting the request even with a full "
            f"browser header set + cookie jar; this site likely needs a "
            f"headless-browser fetch (Playwright) rather than urllib."
        )

    if _LILLY_MARKER not in body:
        # Got a body but not the events -> almost certainly an Akamai interstitial.
        interstitial = ("_abck" in body or "ak_bmsc" in body
                        or "Access Denied" in body or "bm-verify" in body)
        hint = (" (looks like an Akamai challenge page, not the events page)"
                if interstitial else "")
        raise RuntimeError(
            f"{cfg['name']}: page markup changed (no '{_LILLY_MARKER}' blocks "
            f"found){hint}. Re-inspect the webcasts page HTML."
        )
    return body

def _lilly_strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()

def _lilly_parse_ts(date_txt: str):
    """'October 29, 2026 at 10:00 AM EDT' -> (utc_datetime, tz_label).
    Returns (None, '') if unparseable (so a malformed row is skipped, not fatal)."""
    m = re.match(r"(.+?)\s+at\s+(\d{1,2}:\d{2}\s*[AaPp][Mm])\s+([A-Za-z]{2,4})\s*$",
                 date_txt.strip())
    if not m:
        return None, ""
    day_s = m.group(1).strip()
    time_s = re.sub(r"\s+", " ", m.group(2).strip().upper())
    tz = m.group(3).strip().upper()
    try:
        naive = dt.datetime.strptime(f"{day_s} {time_s}", "%B %d, %Y %I:%M %p")
    except ValueError:
        return None, ""
    offset = _resolve_offset(tz, naive)
    if offset is None:
        return None, ""
    return (naive - dt.timedelta(hours=offset)).replace(tzinfo=dt.timezone.utc), tz

def parse_lilly(raw_html: str, cfg: dict) -> list:
    events = []
    if not raw_html:
        return events
    today = dt.datetime.now(dt.timezone.utc).date()

    # Isolate ONLY the "Upcoming Events" table; stop at the archived table.
    start = raw_html.find("table_upcoming_events")
    if start == -1:
        return events
    end = raw_html.find("table_archived_events", start)
    region = raw_html[start:end] if end != -1 else raw_html[start:]

    for row in region.split("<tr>")[1:]:
        dm = re.search(r'nir-widget--event--date"\s*>(.*?)</div>', row, re.S)
        if not dm:
            continue
        start_dt, tz = _lilly_parse_ts(_lilly_strip_tags(dm.group(1)))
        if start_dt is None:
            continue
        if start_dt.date() < today:                    # forward-looking only
            continue
        tm = re.search(r'field-nir-event-title.*?field__item"\s*>(.*?)</div>', row, re.S)
        if not tm:
            continue
        title = _lilly_strip_tags(tm.group(1))
        if not title:
            continue
        # Webcast link (skip the document/asset links, which sit elsewhere).
        webcast = ""
        wc = re.search(r'normal-webcast-link[^>]*>\s*<a\s+href="([^"]+)"', row, re.S)
        if wc:
            webcast = html.unescape(wc.group(1)).strip()
        # Detail-page URL from the "Add to Google Calendar" link, if present.
        src = re.search(r'details=Event Details:\s*(https?://[^&"]+)', row)
        source_url = (html.unescape(src.group(1)).strip() if src
                      else cfg["ir_host"] + "/webcasts-and-presentations")
        ev = Event(
            company=cfg["name"],
            title=title,
            start_utc=start_dt,
            end_utc=None,
            tz_original=tz,
            event_type="unknown",
            source_url=source_url,
            webcast_url=webcast,
            location="",
            tags=[],
            all_day=False,
        )
        basis = f"{cfg['name']}|{title}|{start_dt.date()}"
        ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 5g. AMGEN ADAPTER  (HTML scrape -- SAME NASDAQ IR "nir-widget" Drupal stack
#     as Lilly, SAME Akamai front, but a DIFFERENT widget rendering: div.item
#     lists, not <table>s. New parser; SHARED Akamai-hardened fetch.)
# ---------------------------------------------------------------------------
# Layout (from live source):
#   <div class="block--upcoming-event ...">   <-- upcoming widget (may be empty:
#       ... <div class="nir-widget--list"> ...     "More events are coming soon.")
#           <div class="item column ...">
#               <span class="date-1"> Tuesday, 09.15.2026 11:30 AM EDT </span>
#               <a class="event-content title-1" href="/events/event-details/...">TITLE</a>
#   <div class="block--past-events ...">      <-- past widget (IGNORED)
# The AZ/Merck-style "no upcoming events" state renders the upcoming widget with
# "More events are coming soon." and no item blocks -> we correctly emit 0.
_AMGEN_MARKER = "block--upcoming-event"       # always present, even when empty

def _fetch_nir_html(cfg: dict, marker: str) -> str:
    """Shared Akamai-hardened GET for the NASDAQ IR nir-widget Drupal stack
    (used by Amgen; Lilly keeps its own copy so it is provably unchanged).
    Same defenses as fetch_lilly: full Chrome header set, cookie jar across
    attempts, retry+backoff, explicit read-timeout catch, gzip/deflate decode.
    Raises loudly (RuntimeError) on definitive failure -> Action goes red."""
    url = cfg["endpoint"]
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": ('"Google Chrome";v="153", "Not_A Brand";v="8", '
                      '"Chromium";v="153"'),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
    }
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def _read_body(resp):
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return raw.decode("utf-8", "replace")

    last_err = None
    body = None
    for attempt in range(1, 4):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=45) as resp:
                body = _read_body(resp)
            break
        except urllib.error.HTTPError as ex:
            raise RuntimeError(
                f"{cfg['name']}: HTTP {ex.code} from {url} "
                f"(Akamai challenge? headers may need a session token)"
            ) from None
        except (TimeoutError, socket.timeout):
            last_err = f"read timed out (attempt {attempt}/3)"
        except urllib.error.URLError as ex:
            last_err = f"{ex.reason} (attempt {attempt}/3)"
        if attempt < 3:
            import time as _t
            _t.sleep(2 * attempt)

    if body is None:
        raise RuntimeError(
            f"{cfg['name']}: could not fetch {url} -- {last_err}. "
            f"Akamai Bot Manager is tarpitting the request even with a full "
            f"browser header set + cookie jar; this site likely needs a "
            f"headless-browser fetch (Playwright) rather than urllib."
        )
    if marker not in body:
        interstitial = ("_abck" in body or "ak_bmsc" in body
                        or "Access Denied" in body or "bm-verify" in body)
        hint = (" (looks like an Akamai challenge page, not the events page)"
                if interstitial else "")
        raise RuntimeError(
            f"{cfg['name']}: page markup changed (no '{marker}' block "
            f"found){hint}. Re-inspect the events-calendar page HTML."
        )
    return body

def fetch_amgen(cfg: dict) -> str:
    return _fetch_nir_html(cfg, _AMGEN_MARKER)

def _amgen_strip(fragment: str) -> str:
    return re.sub(r"\s+", " ",
                  html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()

def _amgen_parse_ts(date_txt: str):
    """'Tuesday, 09.15.2026 11:30 AM EDT' -> (utc_datetime, tz_label, all_day).
    Time+tz are optional; date-only rows fall back to an all-day event.
    Returns (None, '', False) if unparseable (row skipped, not fatal)."""
    s = _amgen_strip(date_txt)
    m = re.match(
        r"(?:[A-Za-z]+,\s*)?"                       # optional 'Tuesday, '
        r"(\d{1,2})\.(\d{1,2})\.(\d{4})"            # MM.DD.YYYY
        r"(?:\s+(\d{1,2}:\d{2})\s*([AaPp][Mm])"     # optional  h:mm AM/PM
        r"\s+([A-Za-z]{2,4}))?\s*$",                # optional  TZ
        s)
    if not m:
        return None, "", False
    mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group(4):                                  # timed event
        tz = m.group(6).upper()
        try:
            naive = dt.datetime.strptime(
                f"{yr}-{mo:02d}-{day:02d} {m.group(4)} {m.group(5).upper()}",
                "%Y-%m-%d %I:%M %p")
        except ValueError:
            return None, "", False
        offset = _resolve_offset(tz, naive)
        if offset is None:
            return None, "", False
        return ((naive - dt.timedelta(hours=offset)).replace(tzinfo=dt.timezone.utc),
                tz, False)
    # date-only -> all-day, pin to noon UTC so no tz shift can move the day
    try:
        d = dt.datetime(yr, mo, day, 12, 0, tzinfo=dt.timezone.utc)
    except ValueError:
        return None, "", False
    return d, "", True

def parse_amgen(raw_html: str, cfg: dict) -> list:
    events = []
    if not raw_html:
        return events
    today = dt.datetime.now(dt.timezone.utc).date()

    # Isolate ONLY the upcoming widget; stop at the past-events widget.
    start = raw_html.find("block--upcoming-event")
    if start == -1:
        return events
    end = raw_html.find("block--past-events", start)
    region = raw_html[start:end] if end != -1 else raw_html[start:]

    for chunk in region.split('class="item column')[1:]:
        dm = re.search(r'date-1"\s*>(.*?)</span>', chunk, re.S)
        if not dm:
            continue
        start_dt, tz, all_day = _amgen_parse_ts(dm.group(1))
        if start_dt is None:
            continue
        if start_dt.date() < today:                    # forward-looking only
            continue
        tm = re.search(r'class="event-content title-1"(.*?)>(.*?)</a>', chunk, re.S)
        if not tm:
            continue
        title = _amgen_strip(tm.group(2))
        if not title:
            continue
        href_m = re.search(r'href="([^"]+)"', tm.group(1))
        source_url = cfg["ir_host"] + "/news-and-events/events-calendar/"
        if href_m:
            href = html.unescape(href_m.group(1)).strip()
            source_url = href if href.startswith("http") else cfg["ir_host"] + href
        ev = Event(
            company=cfg["name"],
            title=title,
            start_utc=start_dt,
            end_utc=None,
            tz_original=tz,
            event_type="unknown",
            source_url=source_url,
            webcast_url="",
            location="",
            tags=[],
            all_day=all_day,
        )
        basis = f"{cfg['name']}|{title}|{start_dt.date()}"
        ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# 5g. ABBVIE ADAPTER  (NASDAQ IR nir-widget on Drupal; server-rendered)
# ---------------------------------------------------------------------------
# Third rendering variant of Lilly's platform. Reuses the shared Akamai-hardened
# GET (_fetch_nir_html). Two AbbVie-specific traps:
#   (1) BOTH the upcoming AND past widgets carry class="block--upcoming ...", so
#       we CANNOT split on class like Amgen. We split on the <h2> heading text
#       ("Upcoming events" vs "Past Events") to isolate ONLY future events.
#   (2) Each event is an <article ... node--type-nir-event ...> with:
#         title: <div class="field-nir-event-title">...<a href=...>TITLE</a>
#         date : <div class="... nir-widget--event--date"> MM/DD/YY h:mm am/pm TZ
#       Dates use a 2-DIGIT year, lowercase am/pm, and CENTRAL time (CDT/CST).
_ABBVIE_MARKER = "block--nir-events__widget"   # present even when empty


def fetch_abbvie(cfg: dict) -> str:
    return _fetch_nir_html(cfg, _ABBVIE_MARKER)


def _abbvie_strip(fragment: str) -> str:
    return re.sub(r"\s+", " ",
                  html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _abbvie_parse_ts(date_txt: str):
    """'09/15/26 9:00 am CDT' -> (utc_datetime, tz_label, all_day).
    2-digit year (+2000), lowercase am/pm, Central tz. Time+tz optional; a
    date-only row falls back to an all-day event. Returns (None,'',False) if
    unparseable (row skipped, not fatal)."""
    s = _abbvie_strip(date_txt)
    m = re.match(
        r"(\d{1,2})/(\d{1,2})/(\d{2,4})"            # MM/DD/YY  (or YYYY)
        r"(?:\s+(\d{1,2}:\d{2})\s*([AaPp][Mm])"     # optional  h:mm am/pm
        r"\s+([A-Za-z]{2,4}))?\s*$",                # optional  TZ
        s)
    if not m:
        return None, "", False
    mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:                                    # 2-digit year -> 20xx
        yr += 2000
    if m.group(4):                                  # timed event
        tz = m.group(6).upper()
        try:
            naive = dt.datetime.strptime(
                f"{yr}-{mo:02d}-{day:02d} {m.group(4)} {m.group(5).upper()}",
                "%Y-%m-%d %I:%M %p")
        except ValueError:
            return None, "", False
        offset = _resolve_offset(tz, naive)
        if offset is None:
            return None, "", False
        return ((naive - dt.timedelta(hours=offset)).replace(tzinfo=dt.timezone.utc),
                tz, False)
    # date-only -> all-day, pinned to noon UTC so no tz shift moves the day
    try:
        d = dt.datetime(yr, mo, day, 12, 0, tzinfo=dt.timezone.utc)
    except ValueError:
        return None, "", False
    return d, "", True


def parse_abbvie(raw_html: str, cfg: dict) -> list:
    events = []
    if not raw_html:
        return events
    today = dt.datetime.now(dt.timezone.utc).date()

    # Isolate ONLY the "Upcoming events" widget. Both widgets share the
    # block--upcoming class here, so we split on the <h2> headings instead:
    # everything between "Upcoming events" and the following "Past Events".
    low = raw_html.lower()
    up = low.find("upcoming events")
    if up == -1:
        return events
    past = low.find("past events", up + len("upcoming events"))
    region = raw_html[up:past] if past != -1 else raw_html[up:]

    for chunk in re.split(r"<article\b", region, flags=re.I)[1:]:
        if "node--type-nir-event" not in chunk:
            continue
        dm = re.search(r'nir-widget--event--date"\s*>(.*?)</div>', chunk, re.S)
        if not dm:
            continue
        start_dt, tz, all_day = _abbvie_parse_ts(dm.group(1))
        if start_dt is None:
            continue
        if start_dt.date() < today:                 # forward-looking only
            continue
        tm = re.search(
            r'field-nir-event-title.*?<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
            chunk, re.S)
        if not tm:
            continue
        title = _abbvie_strip(tm.group(2))
        if not title:
            continue
        href = html.unescape(tm.group(1)).strip()
        source_url = href if href.startswith("http") else cfg["ir_host"] + href
        ev = Event(
            company=cfg["name"],
            title=title,
            start_utc=start_dt,
            end_utc=None,
            tz_original=tz,
            event_type="unknown",
            source_url=source_url,
            webcast_url="",
            location="",
            tags=[],
            all_day=all_day,
        )
        basis = f"{cfg['name']}|{title}|{start_dt.date()}"
        ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        events.append(ev)
    return events


# 6. THE BOUNCER  (classify + decide keep/drop)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 5f. NOVO NORDISK ADAPTER  (paginated JSON on Adobe AEM; offset-based)
# ---------------------------------------------------------------------------
# Response shape:
#   {"data": {"resultBeanList": [ {eventTitle, eventDescription, eventStartDate,
#             eventEndDate, eventLocation}, ... ], "numberOfResults": N},
#    "status": 200, "message": "success"}
# We loop, bumping 'currentresults' by page_size, until we've collected
# numberOfResults items (or a page comes back empty). Times are treated as
# ALL-DAY: the naive Copenhagen timestamps are unreliable placeholder spans,
# so we trust the DATE only.
def _novo_date(iso_str):
    """'2026-09-21T04:00:00.000' -> a date. Store all-day events at 12:00Z on
    that calendar date so no timezone shift can ever move the day."""
    if not iso_str:
        return None
    d = dt.datetime.strptime(iso_str.strip()[:10], "%Y-%m-%d").date()
    return dt.datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=dt.timezone.utc)


def fetch_novo(cfg: dict) -> dict:
    """Walk every page of Novo's AEM event-search servlet and return a combined
    {"data": {"resultBeanList": [...], "numberOfResults": N}}. Raises loudly on
    failure so the Action goes red rather than shipping an empty calendar."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": cfg["ir_host"].rstrip("/") + "/investors/financial-calendar.html",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    today = dt.date.today()
    window = int(cfg.get("window_days", 400))
    page_size = int(cfg.get("page_size", 50))
    base_query = dict(cfg.get("query") or {})
    base_query["start"] = today.isoformat()
    base_query["end"] = (today + dt.timedelta(days=window)).isoformat()
    base_query["limit"] = str(page_size)

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    items, total, offset = [], None, 0
    MAX_PAGES = 50  # safety valve so a bad offset param can't loop forever
    for _ in range(MAX_PAGES):
        q = dict(base_query); q["currentresults"] = str(offset)
        url = cfg["endpoint"] + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=30) as resp:
                blob = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(
                f"{cfg['name']}: HTTP {e.code} from {url}\n"
                f"  -> re-capture the Novo investoreventsearch request if the path changed.\n"
                f"  server said: {detail}"
            ) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            raise RuntimeError(
                f"{cfg['name']}: could not reach {url} "
                f"({getattr(e, 'reason', e)})"
            ) from None

        if enc == "gzip":
            blob = gzip.decompress(blob)
        elif enc == "deflate":
            blob = zlib.decompress(blob)
        body = blob.decode("utf-8", "replace")

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"{cfg['name']}: response was not JSON (first 300 chars):\n{body[:300]}"
            ) from None

        data = parsed.get("data") or {}
        page_items = data.get("resultBeanList") or []
        if total is None:
            total = data.get("numberOfResults")
        items += page_items

        if not page_items:
            break
        if isinstance(total, int) and len(items) >= total:
            break
        offset += page_size

    return {"data": {"resultBeanList": items,
                     "numberOfResults": total if total is not None else len(items)}}


def parse_novo(raw_json: dict, cfg: dict) -> list:
    events = []
    rows = (raw_json.get("data") or {}).get("resultBeanList") or []
    for e in rows:
        title = (e.get("eventTitle") or "").strip()
        if not title:
            continue
        start = _novo_date(e.get("eventStartDate"))
        if not start:
            continue
        end = _novo_date(e.get("eventEndDate")) or start
        ev = Event(
            company=cfg["name"],
            title=html.unescape(title),
            start_utc=start,
            end_utc=end,
            tz_original="CET",         # Copenhagen; all-day so only used by JPM month check
            event_type="unknown",
            source_url=cfg["ir_host"].rstrip("/") + "/investors/financial-calendar.html",
            webcast_url="",
            location=html.unescape((e.get("eventLocation") or "").strip()),
            all_day=True,              # naive placeholder times -> trust the date only
        )
        basis = f"{cfg['name']}|{ev.title}|{start:%Y-%m-%d}"
        ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        events.append(ev)
    return events


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
    # approximate local instant from UTC; DST edges never fall on a month
    # boundary, so this is exact for the Jan-JPM decision.
    approx = ev.start_utc.replace(tzinfo=None)
    offset = _resolve_offset(ev.tz_original, approx)
    if offset is None:
        offset = 0
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
        desc_bits = [f"Type: {ev.event_type}", f"Source: {ev.source_url}"]
        if ev.webcast_url:
            desc_bits.append(f"Webcast: {ev.webcast_url}")
        lines += ["BEGIN:VEVENT", f"UID:{ev.uid}", f"DTSTAMP:{now}"]
        if ev.all_day:
            # RFC5545 all-day: DATE values, DTEND is EXCLUSIVE (day after).
            start_d = ev.start_utc.astimezone(dt.timezone.utc).date()
            end_d = (ev.end_utc or ev.start_utc).astimezone(dt.timezone.utc).date()
            dtend_d = end_d + dt.timedelta(days=1)
            lines += [
                f"DTSTART;VALUE=DATE:{start_d:%Y%m%d}",
                f"DTEND;VALUE=DATE:{dtend_d:%Y%m%d}",
            ]
        else:
            end = ev.end_utc or (ev.start_utc + dt.timedelta(hours=1))
            lines += [
                f"DTSTART:{_ics_dt(ev.start_utc)}",
                f"DTEND:{_ics_dt(end)}",
            ]
        lines += [
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

BMS_SAMPLE = {"GetEventListResult": [
    # real captured BMS row (Q3 2026 results call)
    {"EventId": 1815, "Title": "Bristol Myers Squibb Q3 2026 Results Conference Call",
     "TimeZone": "ET", "StartDate": "10/29/2026 08:00:00", "EndDate": "10/29/2026 09:00:00",
     "IsWebcast": True, "WebCastLink": "https://event.choruscall.com/mediaframe/webcast.html?webcastid=GlsfwVZY",
     "Location": "",
     "LinkToDetailPage": "/iframes/events-and-presentations/event-details/2026/Bristol-Myers-Squibb-Q3-2026-Results-Conference-Call/default.aspx",
     "TagsList": None},
    # litmus: broker conf -> DROP
    {"EventId": 1816, "Title": "Morgan Stanley Global Healthcare Conference",
     "TimeZone": "ET", "StartDate": "09/08/2026 13:00:00", "EndDate": "09/08/2026 13:40:00",
     "IsWebcast": True, "WebCastLink": "https://example.com/ms", "Location": "",
     "LinkToDetailPage": "/iframes/events-and-presentations/event-details/2026/ms/default.aspx",
     "TagsList": None},
    # litmus: January JPM -> KEEP (exception)
    {"EventId": 1817, "Title": "45th Annual J.P. Morgan Healthcare Conference",
     "TimeZone": "PST", "StartDate": "01/12/2027 09:00:00", "EndDate": "01/12/2027 09:40:00",
     "IsWebcast": True, "WebCastLink": "https://example.com/jpm", "Location": "San Francisco, CA",
     "LinkToDetailPage": "/iframes/events-and-presentations/event-details/2027/jpm/default.aspx",
     "TagsList": None},
]}

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

# --- AstraZeneca offline litmus sample (real AEM shape, future-only subset) ---
AZ_SAMPLE = {
    "future": [
        {"month": "October", "year": "2026", "entry": [
            {"title": "Call Series, Barclays Cardio Pipeline Investor Call, Virtual [Management]",
             "href": "", "date": {"timestamp": "2026-10-05T00:00:00.000+0000", "start": {"day": "5"}}},
            {"title": "ASBMR, AZN Meet the Management, Virtual [Management]",
             "href": "", "date": {"timestamp": "2026-10-12T00:00:00.000+0000", "start": {"day": "12"}}},
            {"title": "ESMO, AZN Meet the Management, Madrid, Hybrid [Management]",
             "href": "/content/astraz/investor-relations/meet-the-management-event-at-esmo-2026.html",
             "date": {"timestamp": "2026-10-26T00:00:00.000+0000", "start": {"day": "26"}}},
            {"title": "AZN 9M and Q3 2026 results",
             "href": "", "date": {"timestamp": "2026-10-30T00:00:00.000+0000", "start": {"day": "30"}}},
        ]},
        {"month": "November", "year": "2026", "entry": [
            {"title": "Conference, Guggenheim Healthcare Innovation Conference, Boston [Management]",
             "href": "", "date": {"timestamp": "2026-11-10T00:00:00.000+0000", "start": {"day": "10"}}},
            {"title": "Conference, Jefferies Healthcare Conference, London [Management]",
             "href": "", "date": {"timestamp": "2026-11-16T00:00:00.000+0000",
                                   "start": {"day": "16"}, "end": {"day": "17"}}},
            {"title": "Bus Tour, UBS Pharma Bus Tour, London [Management]",
             "href": "", "date": {"timestamp": "2026-11-25T00:00:00.000+0000", "start": {"day": "25"}}},
        ]},
    ],
    "past": [],
}

# --- Roche offline litmus sample: the REAL page-1 payload you captured ---
ROCHE_SAMPLE = {"Items": [
    {"Event Title": "BofA Global Healthcare Conference", "Event Tag": "Conference",
     "Timezone": "BST", "Location": "London, UK", "Link": "",
     "Category": "Investor", "Start Date": "2026-09-23 08:00", "End Date": "2026-09-23 10:00"},
    {"Event Title": "Barclays EU MedTech & Life Sciences Investor Trip 2026 (virtual event)",
     "Event Tag": "Investor Relations event", "Timezone": "CEST", "Location": "Basel, CH", "Link": "",
     "Category": "Investor", "Start Date": "2026-09-25 10:00", "End Date": "2026-09-25 12:00"},
    {"Event Title": "Roche Pharma Day 2026", "Event Tag": "Investor Relations event",
     "Timezone": "BST", "Location": "London UK", "Link": "/investors/updates/inv-update-2026-09-18",
     "Category": "Investor", "Start Date": "2026-09-28 09:00", "End Date": "2026-09-28 11:00"},
    {"Event Title": "Roche 3rd Quarter Sales 2026 (virtual event)", "Event Tag": "Results presentation",
     "Timezone": "CEST", "Location": "Basel, Switzerland", "Link": "",
     "Category": "Investor", "Start Date": "2026-10-22 14:00", "End Date": "2026-10-22 15:30"},
    {"Event Title": "Berenberg Pharma CFO Series 2026 (virtual event)", "Event Tag": "Investor Relations event",
     "Timezone": "CET", "Location": "Basel, CH", "Link": "",
     "Category": "Investor", "Start Date": "2026-11-02 15:00", "End Date": "2026-11-02 17:00"},
    {"Event Title": "Z\u00fcrcher Kantonalbank 7th Swiss Equity Conference", "Event Tag": "Conference",
     "Timezone": "CET", "Location": "Zurich, CH", "Link": "",
     "Category": "Investor", "Start Date": "2026-11-05 08:00", "End Date": "2026-11-05 10:00"},
    {"Event Title": "UBS European Conference", "Event Tag": "Conference",
     "Timezone": "GMT", "Location": "London, UK", "Link": "",
     "Category": "Investor", "Start Date": "2026-11-10 08:00", "End Date": "2026-11-10 10:00"},
    {"Event Title": "UBS Global Healthcare Conference", "Event Tag": "Conference",
     "Timezone": "EST", "Location": "Palm Beach, US", "Link": "",
     "Category": "Investor", "Start Date": "2026-11-10 08:00", "End Date": "2026-11-10 10:00"},
    {"Event Title": "Jefferies Global Healthcare Conference", "Event Tag": "Conference",
     "Timezone": "GMT", "Location": "London, UK", "Link": "",
     "Category": "Investor", "Start Date": "2026-11-17 08:00", "End Date": "2026-11-17 10:00"},
    {"Event Title": "Bernstein 5th Annual Diabetes Disruptors Conference (virtual event)",
     "Event Tag": "Conference", "Timezone": "CET", "Location": "Basel, CH", "Link": "",
     "Category": "Investor", "Start Date": "2026-11-20 15:30", "End Date": "2026-11-20 17:30"},
], "Total": 15, "Page": 1}

# --- Merck offline litmus sample: an "Upcoming events" tab (content-2) plus
# a Featured tab (content-1) and a Past tab (content-3) that MUST be ignored.
MERCK_SAMPLE = """<div class="mccberg-tab-content mccberg-tab-content-1 open"><div class="mco-q4-featured-event-block"><h3>Investor Event at ESMO 2026</h3><p>October 26, 2026</p></div></div><div class="mccberg-tab-content mccberg-tab-content-2"><div class="mco-q4-events-list-block"><div class="mco-q4-events-list-block-events-item-container"><div class="mco-q4-events-list-events-item-date"><p>October 26, 2026</p></div><div class="mco-q4-events-list-events-item-headline"><p class="mco-tag"><small class="tagline">Webcast</small></p><p><a href="https://www.merck.com/events/investor-event-at-esmo-2026/" target="_blank">Investor Event at ESMO 2026</a></p><div class="mco-q4-events-list-events-item-link-containers"><a class="calendar-link " href="https://www.merck.com/ical?event_id=1 ">Add to Calendar</a>
      <a href='https://onlinexperiences.com/scripts/Server.nxp?ShowUUID=ABC'>Webcast</a></div></div></div><div class="mco-q4-events-list-block-events-item-container"><div class="mco-q4-events-list-events-item-date"><p>October 29, 2026</p></div><div class="mco-q4-events-list-events-item-headline"><p class="mco-tag"><small class="tagline">Earnings</small></p><p><a href="https://www.merck.com/events/q3-2026-earnings-call/" target="_blank">Q3 2026 Earnings Call</a></p><div class="mco-q4-events-list-events-item-link-containers"><a class="calendar-link " href="https://www.merck.com/ical?event_id=1 ">Add to Calendar</a></div></div></div><div class="mco-q4-events-list-block-events-item-container"><div class="mco-q4-events-list-events-item-date"><p>February 02, 2027</p></div><div class="mco-q4-events-list-events-item-headline"><p class="mco-tag"><small class="tagline">Earnings</small></p><p><a href="https://www.merck.com/events/q4-2026-earnings-call/" target="_blank">Q4 2026 Earnings Call</a></p><div class="mco-q4-events-list-events-item-link-containers"><a class="calendar-link " href="https://www.merck.com/ical?event_id=1 ">Add to Calendar</a></div></div></div><div class="mco-q4-events-list-block-events-item-container"><div class="mco-q4-events-list-events-item-date"><p>November 18, 2026</p></div><div class="mco-q4-events-list-events-item-headline"><p class="mco-tag"><small class="tagline">Webcast</small></p><p><a href="https://www.merck.com/events/morgan-stanley-healthcare-conference/" target="_blank">Morgan Stanley 24th Annual Global Healthcare Conference</a></p><div class="mco-q4-events-list-events-item-link-containers"><a class="calendar-link " href="https://www.merck.com/ical?event_id=1 ">Add to Calendar</a>
      <a href='https://cc.webcasts.com/morg007/'>Webcast</a></div></div></div></div></div><div class="mccberg-tab-content mccberg-tab-content-3"><div class="mco-q4-events-list-block"><div class="mco-q4-events-list-block-events-item-container"><div class="mco-q4-events-list-events-item-date"><p>September 09, 2026</p></div><div class="mco-q4-events-list-events-item-headline"><p class="mco-tag"><small class="tagline">Webcast</small></p><p><a href="https://www.merck.com/events/wells-fargo/" target="_blank">Wells Fargo Healthcare Conference (PAST)</a></p><div class="mco-q4-events-list-events-item-link-containers"><a class="calendar-link " href="https://www.merck.com/ical?event_id=1 ">Add to Calendar</a></div></div></div></div></div>"""

# --- Eli Lilly offline litmus sample: real "nir-widget" markup. The UPCOMING
# table carries 5 future events (earnings / JPM-January / medical / 2 brokers);
# the ARCHIVED table's past row MUST be ignored (table isolation + date guard).
LILLY_SAMPLE = """
<table class="nirtable table_upcoming_events">
  <thead><tr><th>Date</th><th>Event Details</th><th>Remind me</th></tr></thead>
  <tbody>
  <tr>
    <td><div class="nir-widget--field nir-widget--event--date">


          October 29, 2026 at 10:00 AM EDT

      </div></td>
    <td>
      <div class="nir-widget--field nir-widgets--event--title">
        <div class="field-nir-event-title"><div class="field__item">Q3 2026 Earnings Call</div></div>
      </div>
      <div class="add-gcal field__item"><a href="https://www.google.com/calendar/render?action=TEMPLATE&amp;text=Eli Lilly and Company - Q3 2026 Earnings Call&amp;dates=20261029T140000Z/20261029T140000Z&amp;details=Event Details: https://investor.lilly.com/events/event-details/q3-2026-earnings-call&amp;location=&amp;trp=false" class="add-gcal">Add to Google Calendar</a></div>
    </td>
  </tr>
  <tr>
    <td><div class="nir-widget--field nir-widget--event--date"> November 18, 2026 at 9:00 AM EST </div></td>
    <td>
      <div class="nir-widget--field nir-widgets--event--title">
        <div class="field-nir-event-title"><div class="field__item">Morgan Stanley 24th Annual Global Healthcare Conference</div></div>
      </div>
      <div class="nir-widget--field nir-widget--event--webcast">
        <div class="field-nir-event-url"><div class="normal-webcast-link field__item"><a href="https://event.webcasts.com/starthere.jsp?ei=999001" target="_blank">Listen to webcast</a></div></div>
      </div>
    </td>
  </tr>
  <tr>
    <td><div class="nir-widget--field nir-widget--event--date"> January 12, 2027 at 5:15 PM EST </div></td>
    <td>
      <div class="nir-widget--field nir-widgets--event--title">
        <div class="field-nir-event-title"><div class="field__item">45th Annual J.P. Morgan Healthcare Conference</div></div>
      </div>
      <div class="nir-widget--field nir-widget--event--webcast">
        <div class="field-nir-event-url"><div class="normal-webcast-link field__item"><a href="https://jpmorgan.metameetings.net/events/healthcare27/sessions/eli-lilly/webcast" target="_blank">Listen to webcast</a></div></div>
      </div>
    </td>
  </tr>
  <tr>
    <td><div class="nir-widget--field nir-widget--event--date"> December 1, 2026 at 8:00 AM EST </div></td>
    <td>
      <div class="nir-widget--field nir-widgets--event--title">
        <div class="field-nir-event-title"><div class="field__item">Citi&#039;s 2026 Global Healthcare Conference</div></div>
      </div>
    </td>
  </tr>
  <tr>
    <td><div class="nir-widget--field nir-widget--event--date"> June 5, 2027 at 7:00 PM EDT </div></td>
    <td>
      <div class="nir-widget--field nir-widgets--event--title">
        <div class="field-nir-event-title"><div class="field__item">Lilly Investor Event at American Diabetes Association&#039;s (ADA) 87th Scientific Sessions</div></div>
      </div>
      <div class="nir-widget--field nir-widget--event--webcast">
        <div class="field-nir-event-url"><div class="normal-webcast-link field__item"><a href="https://edge.media-server.com/mmc/p/ada87demo" target="_blank">Listen to webcast</a></div></div>
      </div>
    </td>
  </tr>
  </tbody>
</table>

<table class="nirtable table_archived_events">
  <thead><tr><th>Date</th><th>Event Details</th></tr></thead>
  <tbody>
  <tr>
    <td><div class="nir-widget--field nir-widget--event--date"> September 14, 2026 at 1:50 PM EDT </div></td>
    <td>
      <div class="nir-widget--field nir-widgets--event--title">
        <div class="field-nir-event-title"><div class="field__item">Morgan Stanley 23rd Annual Global Healthcare Conference (PAST - MUST BE IGNORED)</div></div>
      </div>
    </td>
  </tr>
  </tbody>
</table>
"""

NOVO_SAMPLE = {"data": {"resultBeanList": [
    {"eventTitle": "Capital Markets Day 2026",
     "eventDescription": "<p>Full review of strategy, operations and financial targets.</p>",
     "eventStartDate": "2026-09-21T04:00:00.000",
     "eventEndDate": "2026-09-21T21:55:00.000",
     "eventLocation": "Access Online"},
    {"eventTitle": "Q3 2026 quarterly results",
     "eventDescription": "<p>Financial results for the first nine months of 2026</p>",
     "eventStartDate": "2026-11-04T06:30:00.000",
     "eventEndDate": "2026-11-04T22:12:00.000",
     "eventLocation": "Access Online"},
    {"eventTitle": "Goldman Sachs 47th Annual Global Healthcare Conference",
     "eventDescription": "<p>Broker-hosted -- should be DROPPED.</p>",
     "eventStartDate": "2026-12-08T08:00:00.000",
     "eventEndDate": "2026-12-08T20:00:00.000",
     "eventLocation": "New York"}
], "numberOfResults": 3}}


AMGEN_SAMPLE = """
<div class="block--upcoming-event block--nir-events__widget block block-nir-events block-nir-events__widget">
  <div class="nir-widget">
    <div class="nir-widget--label">Upcoming Events</div>
    <div class="nir-widget--content">
      <div class="nir-widget--list">
        <div class="row recent-news-items upcoming-events">

<div class="item column col-sm-12 col-md-12">
  <p class="press-release-container">
      <span>EVENT</span>
      <span class="date-1">
            Wednesday, 10.28.2026 4:30 PM EDT
      </span>
      <a class="news-right-arrow link-1" href="/events/event-details/amgen-q3-2026-earnings"><img src="/arrow.svg" alt="Arrow"></a>
  </p>
  <a class="event-content title-1"
      href="/events/event-details/amgen-q3-2026-earnings"
      data-clik-type="promo"
      data-click-text="Amgen Q3 2026 Financial Results Conference Call">Amgen Q3 2026 Financial Results Conference Call</a>
</div>

<div class="item column col-sm-12 col-md-12">
  <p class="press-release-container">
      <span>EVENT</span>
      <span class="date-1"> Monday, 01.12.2027 9:00 AM EST </span>
      <a class="news-right-arrow link-1" href="/events/event-details/jpm-2027"><img src="/arrow.svg" alt="Arrow"></a>
  </p>
  <a class="event-content title-1" href="/events/event-details/jpm-2027">45th Annual J.P. Morgan Healthcare Conference</a>
</div>

<div class="item column col-sm-12 col-md-12">
  <p class="press-release-container">
      <span>EVENT</span>
      <span class="date-1"> Monday, 10.19.2026 8:00 AM EDT </span>
      <a class="news-right-arrow link-1" href="/events/event-details/esmo-2026"><img src="/arrow.svg" alt="Arrow"></a>
  </p>
  <a class="event-content title-1" href="/events/event-details/esmo-2026">Amgen Investor Event at ESMO 2026 Congress</a>
</div>

<div class="item column col-sm-12 col-md-12">
  <p class="press-release-container">
      <span>EVENT</span>
      <span class="date-1"> Thursday, 06.10.2027 8:00 AM EDT </span>
      <a class="news-right-arrow link-1" href="/events/event-details/gs-2027"><img src="/arrow.svg" alt="Arrow"></a>
  </p>
  <a class="event-content title-1" href="/events/event-details/gs-2027">Goldman Sachs 48th Annual Global Healthcare Conference</a>
</div>

        </div>
      </div>
    </div>
  </div>
</div>

<div class="block--past-events block--nir-events__widget block block-nir-events block-nir-events__widget">
  <div class="nir-widget">
    <div class="nir-widget--label">Past Events</div>
    <div class="nir-widget--content">
      <div class="nir-widget--list">
        <div class="row recent-news-items upcoming-events">

<div class="item column col-sm-12 col-md-12">
  <p class="press-release-container">
      <span>EVENT</span>
      <span class="date-1"> Tuesday, 09.15.2026 11:30 AM EDT </span>
      <a class="news-right-arrow link-1" href="/events/event-details/ms-past"><img src="/arrow.svg" alt="Arrow"></a>
  </p>
  <a class="event-content title-1" href="/events/event-details/ms-past">Morgan Stanley 24th Annual Global Healthcare Conference (PAST - MUST BE IGNORED)</a>
</div>

<div class="item column col-sm-12 col-md-12">
  <p class="press-release-container">
      <span>EVENT</span>
      <span class="date-1"> Thursday, 09.10.2026 11:00 AM EDT </span>
      <a class="news-right-arrow link-1" href="/events/event-details/wf-past"><img src="/arrow.svg" alt="Arrow"></a>
  </p>
  <a class="event-content title-1" href="/events/event-details/wf-past">Wells Fargo 21st Annual Healthcare Conference (PAST - MUST BE IGNORED)</a>
</div>

        </div>
      </div>
    </div>
  </div>
</div>
"""


ABBVIE_SAMPLE = """
<div class="block--upcoming block--nir-events__widget block--6031 block block-nir-events block-nir-events__widget">
  <div class="nir-widget">
    <h2>
      Upcoming events
    </h2>
    <div class="nir-widget--content">
      <div class="nir-widget--list">

<article class="clearfix node node--nir-event--nir-widget-list node--type-nir-event node--view-mode-nir-widget-list">
    <div class="nir-widget--field nir-widgets--event--title ccbnTxtBold">
      <div class="field-nir-event-title">
        <div class="field__item"><a href="/events/event-details/abbvie-third-quarter-2026-earnings-conference-call" hreflang="en">AbbVie to Host Third-Quarter 2026 Earnings Conference Call</a></div>
      </div>
    </div>
    <div class="nir-widget--field nir-widget--event--date">
	10/30/26 8:00 am CDT
    </div>
</article>

<article class="clearfix node node--nir-event--nir-widget-list node--type-nir-event node--view-mode-nir-widget-list">
    <div class="nir-widget--field nir-widgets--event--title ccbnTxtBold">
      <div class="field-nir-event-title">
        <div class="field__item"><a href="/events/event-details/jpm-2027" hreflang="en">45th Annual J.P. Morgan Healthcare Conference</a></div>
      </div>
    </div>
    <div class="nir-widget--field nir-widget--event--date">
	01/11/27 9:00 am PST
    </div>
</article>

<article class="clearfix node node--nir-event--nir-widget-list node--type-nir-event node--view-mode-nir-widget-list">
    <div class="nir-widget--field nir-widgets--event--title ccbnTxtBold">
      <div class="field-nir-event-title">
        <div class="field__item"><a href="/events/event-details/esmo-2026" hreflang="en">AbbVie Investor Webcast at ESMO 2026 Congress</a></div>
      </div>
    </div>
    <div class="nir-widget--field nir-widget--event--date">
	10/19/26 8:00 am CDT
    </div>
</article>

<article class="clearfix node node--nir-event--nir-widget-list node--type-nir-event node--view-mode-nir-widget-list">
    <div class="nir-widget--field nir-widgets--event--title ccbnTxtBold">
      <div class="field-nir-event-title">
        <div class="field__item"><a href="/events/event-details/goldman-2026" hreflang="en">Goldman Sachs 47th Annual Global Healthcare Conference</a></div>
      </div>
    </div>
    <div class="nir-widget--field nir-widget--event--date">
	11/12/26 2:00 pm CST
    </div>
</article>

      </div>
    </div>
  </div>
</div>

<div class="block--upcoming block--nir-events__widget block--6031 block block-nir-events block-nir-events__widget">
  <div class="nir-widget">
    <h2>
      Past Events
    </h2>
    <div class="nir-widget--content">
      <div class="nir-widget--list">

<article class="clearfix node node--nir-event--nir-widget-list node--type-nir-event node--view-mode-nir-widget-list">
    <div class="nir-widget--field nir-widgets--event--title ccbnTxtBold">
      <div class="field-nir-event-title">
        <div class="field__item"><a href="/events/event-details/morgan-stanley-past" hreflang="en">Morgan Stanley 24th Annual Global Healthcare Conference (PAST - MUST BE IGNORED)</a></div>
      </div>
    </div>
    <div class="nir-widget--field nir-widget--event--date">
	09/15/26 9:00 am CDT
    </div>
</article>

<article class="clearfix node node--nir-event--nir-widget-list node--type-nir-event node--view-mode-nir-widget-list">
    <div class="nir-widget--field nir-widgets--event--title ccbnTxtBold">
      <div class="field-nir-event-title">
        <div class="field__item"><a href="/events/event-details/wells-fargo-past" hreflang="en">Wells Fargo 21st Annual Healthcare Conference (PAST - MUST BE IGNORED)</a></div>
      </div>
    </div>
    <div class="nir-widget--field nir-widget--event--date">
	09/09/26 9:15 am CDT
    </div>
</article>

      </div>
    </div>
  </div>
</div>
"""


# ---------------------------------------------------------------------------
# 5h. SANOFI ADAPTER  (Magnolia CMS + React-Router SSR hydration stream)
# ---------------------------------------------------------------------------
# The events live in a turbo-stream FLAT ARRAY inside the page document:
#   enqueue("[ {\"_37\":121, ...}, \"title\", \"startDate\", ... ]")
# A turbo-stream object {"_K": V} means {arr[K]: arr[V]} -- both K and V are
# indices into the flat array. We: (1) pull + double-decode the array, (2) find
# the "events" list (a string "events" followed by a list of ints pointing to
# event objects), (3) deref each object into {title, startDate, endDate,
# timezone, type, cta}. Timed vs all-day + the fake-'Z' are handled below.
_SANOFI_MARKER = "streamController.enqueue"


def fetch_sanofi(cfg: dict) -> str:
    # Plain GET; reuses the shared hardened fetcher (full browser headers,
    # cookie jar, retry, gzip). No Akamai here, but the fetcher is generic.
    return _fetch_nir_html(cfg, _SANOFI_MARKER)


def _sanofi_extract_arr(html_body: str):
    """Pull the enqueue("...") payload and double-decode it to the flat list."""
    m = re.search(r'streamController\.enqueue\(("(?:[^"\\]|\\.)*")\)',
                  html_body, re.S)
    if not m:
        return None
    try:
        inner = json.loads(m.group(1))     # unwrap the JS string literal
        arr = json.loads(inner)            # -> flat turbo-stream array
    except ValueError:
        return None
    return arr if isinstance(arr, list) else None


def _sanofi_deref(arr, dct) -> dict:
    """Resolve one turbo-stream object {"_K":V} -> {arr[K]: arr[V]}."""
    out = {}
    for k, v in dct.items():
        if not (isinstance(k, str) and k.startswith("_")):
            continue
        try:
            key = arr[int(k[1:])]
        except (ValueError, IndexError):
            continue
        val = arr[v] if isinstance(v, int) and 0 <= v < len(arr) else v
        if isinstance(key, str):
            out[key] = val
    return out


def _sanofi_event_dt(start_iso: str, tz_field: str):
    """(utc, tz_label, all_day).

    If a timezone string is present the event is TIMED: the ISO carries a FAKE
    'Z' (naive Paris/Copenhagen wall-clock mislabelled UTC), so we strip the Z
    and resolve the REAL offset from the tz label ('CET (...)' -> 'CET') on the
    event's own date (DST-aware). No tz field -> the timestamp is an unreliable
    publish stamp, so we trust the DATE only (all-day, pinned to noon UTC)."""
    if not start_iso or len(start_iso) < 10:
        return None, "", False
    if tz_field:
        label = re.split(r"[\s(]", tz_field.strip(), maxsplit=1)[0].upper() or "CET"
        try:
            naive = dt.datetime.strptime(start_iso[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None, "", False
        offset = _resolve_offset(label, naive)
        if offset is None:
            return None, "", False
        return ((naive - dt.timedelta(hours=offset)).replace(tzinfo=dt.timezone.utc),
                label, False)
    try:
        d = dt.datetime.strptime(start_iso[:10], "%Y-%m-%d")
    except ValueError:
        return None, "", False
    return dt.datetime(d.year, d.month, d.day, 12, 0, tzinfo=dt.timezone.utc), "", True


def _sanofi_link(arr, cta_val) -> str:
    """Best-effort detail URL from the (nested) cta object."""
    if isinstance(cta_val, dict):
        for _, v in _sanofi_deref(arr, cta_val).items():
            if isinstance(v, str) and v.startswith("/"):
                return v
    return ""


def parse_sanofi(raw, cfg: dict) -> list:
    events = []
    if not raw:
        return events
    arr = raw if isinstance(raw, list) else _sanofi_extract_arr(raw)
    if not arr:
        return events
    # Locate the events list: string "events" immediately followed by a list of
    # ints pointing to event objects (dicts). Guard against the "events" string
    # used elsewhere as a plain key.
    ev_dicts = []
    for i in range(len(arr) - 1):
        if arr[i] == "events" and isinstance(arr[i + 1], list):
            objs = [arr[j] for j in arr[i + 1]
                    if isinstance(j, int) and 0 <= j < len(arr)
                    and isinstance(arr[j], dict)]
            if objs:
                ev_dicts = objs
                break
    for d in ev_dicts:
        f = _sanofi_deref(arr, d)
        title = html.unescape(str(f.get("title", ""))).strip()
        if not title:
            continue
        status = f.get("status")
        if isinstance(status, str) and status.lower() not in ("upcoming", ""):
            continue
        start_iso = f.get("startDate") if isinstance(f.get("startDate"), str) else ""
        tz_field = f.get("timezone") if isinstance(f.get("timezone"), str) else ""
        start_dt, tz, all_day = _sanofi_event_dt(start_iso, tz_field)
        if start_dt is None:
            continue
        end_dt = None
        end_iso = f.get("endDate")
        if not all_day and isinstance(end_iso, str) and len(end_iso) >= 19:
            end_dt, _, _ = _sanofi_event_dt(end_iso, tz_field)
        href = _sanofi_link(arr, f.get("cta"))
        source_url = (href if href.startswith("http") else cfg["ir_host"] + href) \
            if href else cfg["endpoint"]
        ev = Event(
            company=cfg["name"], title=title, start_utc=start_dt, end_utc=end_dt,
            tz_original=tz, event_type="unknown", source_url=source_url,
            webcast_url="", location="", tags=[], all_day=all_day,
        )
        basis = f"{cfg['name']}|{title}|{start_dt.date()}"
        ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        events.append(ev)
    return events


# Offline sample: a STRUCTURALLY FAITHFUL turbo-stream document built from the
# 8 real Sanofi events (same key names + fake-'Z' start stamps the live page
# uses). Q3 results carries a timezone (-> timed); the rest have none (-> all-
# day). Proves the decoder + fake-Z resolver + bouncer end to end.
def _sanofi_build_sample() -> str:
    arr = [None]  # index 0 placeholder

    def add(v):
        arr.append(v)
        return len(arr) - 1

    k_title = add("title"); k_start = add("startDate"); k_end = add("endDate")
    k_tz = add("timezone"); k_type = add("type"); k_status = add("status")
    k_cta = add("cta"); k_link = add("link")

    rows = [
        # title, start(fake-Z), end, timezone, type, detail-link
        ("Third quarter 2026 results", "2026-10-30T12:30:00.000Z",
         "2026-10-30T13:30:00.000Z", "CET (7:30am - 8:30am EDT)", "Quarterly results",
         "/en/investors/financial-results-and-events/financial-results/q3-2026-results"),
        ("Annual General Meeting 2027", "2027-04-28T06:28:51.000Z", None, None,
         "Annual general meetings",
         "/en/investors/financial-results-and-events/general-meetings/annual-general-meeting-2027"),
        ("Santander Virtual Pharma Day \u2013 virtual", "2026-09-28T08:16:28.000Z",
         None, None, "Conferences",
         "/en/investors/broker-conferences/2026-santander-virtual-pharma-day-virtual"),
        ("Goldman Sach\u2019s China Biopharma Bus Tour \u2013 Shanghai",
         "2026-09-24T08:15:36.000Z", None, None, "Conferences",
         "/en/investors/broker-conferences/2026-goldman-sachs-china-biopharma-bus-tour-shanghai"),
        ("Baader Investment Conference \u2013 Munich", "2026-09-24T08:14:43.000Z",
         None, None, "Conferences",
         "/en/investors/broker-conferences/2026-baader-investment-conference-munich"),
        ("UBS Pharma Bus Tour \u2013 Paris", "2026-09-23T08:13:35.000Z",
         None, None, "Conferences",
         "/en/investors/broker-conferences/2026-ubs-pharma-bus-tour-paris"),
        ("Bank of America Global Healthcare Conference - London",
         "2026-09-22T08:12:51.000Z", None, None, "Conferences",
         "/en/investors/broker-conferences/2026-bank-of-america-global-healthcare-conference-london"),
        ("BNP Paribas Sustainability Conference \u2013 Paris",
         "2026-09-17T08:12:05.000Z", None, None, "Conferences",
         "/en/investors/broker-conferences/2026-bnp-paribas-sustainability-conference-paris"),
    ]
    ev_idx = []
    for title, start, end, tz, typ, link in rows:
        i_title = add(title); i_start = add(start); i_type = add(typ)
        i_status = add("upcoming")
        i_link = add(link); i_cta = add({("_%d" % k_link): i_link})
        d = {("_%d" % k_title): i_title, ("_%d" % k_start): i_start,
             ("_%d" % k_type): i_type, ("_%d" % k_status): i_status,
             ("_%d" % k_cta): i_cta}
        if end is not None:
            d[("_%d" % k_end)] = add(end)
        if tz is not None:
            d[("_%d" % k_tz)] = add(tz)
        ev_idx.append(add(d))

    add("events"); add(ev_idx)
    inner = json.dumps(arr)
    return ('<!DOCTYPE html><html><body>'
            '<div class="dotcom-event-content-list"></div>'
            '<script>window.__reactRouterContext.streamController.enqueue('
            + json.dumps(inner) + ');</script></body></html>')


SANOFI_SAMPLE = _sanofi_build_sample()


# Adapter registry: maps each company's "adapter" key to its (fetch, parse) pair.
ADAPTERS = {
    "q4": (fetch_q4, parse_q4),
    "astrazeneca": (fetch_astrazeneca, parse_astrazeneca),
    "roche": (fetch_roche, parse_roche),
    "merck": (fetch_merck, parse_merck),
    "lilly": (fetch_lilly, parse_lilly),
    "novo": (fetch_novo, parse_novo),
    "amgen": (fetch_amgen, parse_amgen),
    "abbvie": (fetch_abbvie, parse_abbvie),
    "sanofi": (fetch_sanofi, parse_sanofi),
}
# Empty-payload shapes per adapter (used as the OFFLINE fallback).
_EMPTY_RAW = {
    "q4": {"GetEventListResult": []},
    "astrazeneca": {"future": [], "past": []},
    "roche": {"Items": [], "Total": 0},
    "merck": "",
    "lilly": "",
    "novo": {"data": {"resultBeanList": [], "numberOfResults": 0}},
    "amgen": "",
    "abbvie": "",
    "sanofi": "",
}


if __name__ == "__main__":
    OFFLINE = os.environ.get("PHARMA_OFFLINE") == "1"

    # In OFFLINE test mode we feed the pipeline the REAL captured payloads
    # (Pfizer GetEventList + litmus events, and the AstraZeneca AEM sample) so we
    # can prove behavior without a network. In LIVE mode each company is fetched
    # through its own adapter.
    OFFLINE_RAW = {
        "pfizer": {"GetEventListResult":
                   PFIZER_SAMPLE["GetEventListResult"] + EXTRA_SAMPLE["GetEventListResult"]},
        "astrazeneca": AZ_SAMPLE,
        "roche": ROCHE_SAMPLE,
        "merck": MERCK_SAMPLE,
        "lilly": LILLY_SAMPLE,
        "novo": NOVO_SAMPLE,
        "amgen": AMGEN_SAMPLE,
        "abbvie": ABBVIE_SAMPLE,
        "sanofi": SANOFI_SAMPLE,
        "bms": BMS_SAMPLE,
    }

    all_kept, all_parsed = [], []
    failures = []  # (company_name, exception) for any company that blew up
    for key, cfg in COMPANIES.items():
        adapter = cfg.get("adapter", "q4")
        fetch_fn, parse_fn = ADAPTERS[adapter]

        print("=" * 78)
        print(f"BOUNCER DECISIONS  --  {cfg['name']}  "
              f"[{adapter}]  ({'offline' if OFFLINE else 'LIVE'})")
        print("=" * 78)

        # --- per-company isolation --------------------------------------------
        # A fetch/parse failure for ONE company must not blank the whole
        # calendar. Catch it, log loudly, record it, and carry on so every
        # other company still writes its events to the .ics. Any failure still
        # turns the Action RED at the very end (non-zero exit) so you're alerted.
        try:
            if OFFLINE:
                raw = OFFLINE_RAW.get(key, _EMPTY_RAW[adapter])
            else:
                raw = fetch_fn(cfg)
            parsed = parse_fn(raw, cfg)
        except Exception as exc:
            failures.append((cfg["name"], exc))
            print(f"  !! {cfg['name']}: FETCH/PARSE FAILED -> "
                  f"{type(exc).__name__}: {exc}")
            print(f"  {cfg['name']}: fetched 0 raw event(s), kept 0  (ERRORED, "
                  f"skipped so the rest of the calendar still builds).")
            continue

        all_parsed += parsed
        kept_here = 0
        for ev in parsed:
            keep, reason = should_post(ev)
            flag = "KEEP" if keep else "DROP"
            print(f"[{flag}]  {ev.start_utc:%Y-%m-%d}  {ev.title}")
            print(f"        -> {reason}")
            if keep:
                all_kept.append(ev)
                kept_here += 1
        # Self-diagnosing one-liner: exactly why a company contributes 0 events.
        print(f"  {cfg['name']}: fetched {len(parsed)} raw event(s), kept {kept_here}.")

    write_ics(all_kept, "pharma_events.ics")
    print("\n" + "=" * 78)
    ok_companies = len(COMPANIES) - len(failures)
    print(f"Wrote {len(all_kept)} event(s) to the .ics "
          f"(dropped {len(all_parsed)-len(all_kept)} across {ok_companies} healthy "
          f"company(ies); {len(failures)} errored).")
    print("=" * 78)

    # The calendar was still written for the healthy companies above. But if ANY
    # company errored, exit non-zero so the GitHub Action goes RED and alerts you
    # -- a broken endpoint can no longer hide behind a green run.
    if failures:
        print("\n" + "!" * 78)
        print(f"{len(failures)} company(ies) FAILED this run "
              f"(calendar still written for the other {ok_companies}):")
        for name, exc in failures:
            print(f"   - {name}: {type(exc).__name__}: {exc}")
        print("!" * 78)
        sys.exit(1)
