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
import html
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
    # "abbvie": {... "ir_host": "https://investors.abbvie.com" ...},
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
    offset = _TZ_OFFSETS.get((tz_label or "GMT").strip().upper())
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

# Adapter registry: maps each company's "adapter" key to its (fetch, parse) pair.
ADAPTERS = {
    "q4": (fetch_q4, parse_q4),
    "astrazeneca": (fetch_astrazeneca, parse_astrazeneca),
    "roche": (fetch_roche, parse_roche),
    "merck": (fetch_merck, parse_merck),
}
# Empty-payload shapes per adapter (used as the OFFLINE fallback).
_EMPTY_RAW = {
    "q4": {"GetEventListResult": []},
    "astrazeneca": {"future": [], "past": []},
    "roche": {"Items": [], "Total": 0},
    "merck": "",
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
    }

    all_kept, all_parsed = [], []
    for key, cfg in COMPANIES.items():
        adapter = cfg.get("adapter", "q4")
        fetch_fn, parse_fn = ADAPTERS[adapter]

        if OFFLINE:
            raw = OFFLINE_RAW.get(key, _EMPTY_RAW[adapter])
        else:
            raw = fetch_fn(cfg)   # raises loudly on failure -> Action goes red

        parsed = parse_fn(raw, cfg)
        all_parsed += parsed
        kept_here = 0

        print("=" * 78)
        print(f"BOUNCER DECISIONS  --  {cfg['name']}  "
              f"[{adapter}]  ({'offline' if OFFLINE else 'LIVE'})")
        print("=" * 78)
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
    print(f"Wrote {len(all_kept)} event(s) to the .ics "
          f"(dropped {len(all_parsed)-len(all_kept)} across {len(COMPANIES)} company(ies)).")
    print("=" * 78)
