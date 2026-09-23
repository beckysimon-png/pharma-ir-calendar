"""
FDA Advisory Committee meetings -> fda_events.ics  (its OWN calendar / Sheet tab)

Scope: keep ONLY meetings run by
    - Center for Biologics Evaluation and Research (CBER), or
    - Center for Drug Evaluation and Research (CDER).
Everything else (Medical Devices / CDRH panels, Science Board, NCTR, etc.) is dropped.

Source = the FEDERAL REGISTER API (official U.S. Government, Option A, no third
parties, no API key):  https://www.federalregister.gov/api/v1/documents.json
FDA must publish each advisory-committee "Notice of Meeting" here >=15 days ahead,
and each notice carries the AGENDA -- the drug, application number, sponsor and
indication -- which the bare fda.gov calendar does NOT. That agenda text is put
in the event title + Details so a row is genuinely useful, not just a pointer.

Data path (mirrors the AstraZeneca pattern you already run):
  * LIVE  : fetch_fda() queries the FR API; parse_fda() reads the JSON.
  * MANUAL: if a saved snapshot file `fda_fr.json` exists in the repo, it is
            parsed INSTEAD of the network (paste the API response, commit).
  * Failure is non-fatal: carry-forward + history-archive keep prior events.

Self-contained: the proven Event / write_ics / _events_to_retain are INLINED
below, so this module has ZERO dependency on q4_pipeline.py.
"""
import os, re, sys, html, hashlib
import datetime as dt

from dataclasses import dataclass, field
from typing import Optional

# ===========================================================================
# SELF-CONTAINED CORE  (inlined from q4_pipeline.py so this module is FULLY
# STANDALONE -- no import from the pharma pipeline. A change/break in
# q4_pipeline.py can never affect the FDA calendar.)
# ===========================================================================
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
    notes: str = ""             # free-text agenda (FDA: drug/indication) -> Details
    docket: str = ""            # FDA docket number, if any


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


def _ics_dt(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

def _read_ics_vevents(path: str) -> dict:
    """Parse an existing pharma_events.ics into {company_name: [vevent_block,...]}.

    Each block is the verbatim list of lines from BEGIN:VEVENT..END:VEVENT, so a
    carried-forward event is re-emitted byte-for-byte (same UID -> Google treats
    it as the SAME event returning, never a duplicate). Company is read from the
    SUMMARY, which we write as 'SUMMARY:<company>: <title>'. Missing file / parse
    trouble -> empty dict (we simply have nothing to carry forward)."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (FileNotFoundError, OSError):
        return out
    block = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line == "BEGIN:VEVENT":
            block = [line]
        elif block is not None:
            block.append(line)
            if line == "END:VEVENT":
                company = None
                for bl in block:
                    if bl.startswith("SUMMARY:"):
                        summ = bl[len("SUMMARY:"):]
                        # company is the prefix before the first ': ' (our own
                        # names never contain ': '); tolerate escaped chars.
                        idx = summ.find(": ")
                        if idx != -1:
                            company = summ[:idx].replace("\\,", ",").replace(
                                "\\;", ";").replace("\\\\", "\\").strip()
                        break
                if company:
                    out.setdefault(company, []).append(block)
                block = None
    return out


def _block_uid(block: list):
    for bl in block:
        if bl.startswith("UID:"):
            return bl[len("UID:"):].strip()
    return None


def _block_start_date(block: list):
    """Date (UTC) of a VEVENT block's DTSTART, from either a DATE value
    (DTSTART;VALUE=DATE:YYYYMMDD) or a timed one (DTSTART:YYYYMMDDT...Z).
    Returns a datetime.date, or None if unparseable."""
    for bl in block:
        if bl.startswith("DTSTART"):
            val = bl.split(":", 1)[1].strip() if ":" in bl else ""
            m = re.match(r"(\d{4})(\d{2})(\d{2})", val)
            if m:
                try:
                    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    return None
    return None


def _read_ics_all_blocks(path: str) -> list:
    """Every VEVENT block in the file as (uid, start_date, block). Missing file
    / parse trouble -> [] (we simply have nothing to retain)."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (FileNotFoundError, OSError):
        return out
    block = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line == "BEGIN:VEVENT":
            block = [line]
        elif block is not None:
            block.append(line)
            if line == "END:VEVENT":
                out.append((_block_uid(block), _block_start_date(block), block))
                block = None
    return out


def _events_to_retain(path: str, fresh_uids: set, failed_names: set, today):
    """Decide which VEVENT blocks from the PREVIOUS .ics to re-emit this run.

    Two independent reasons to retain a previous event, both deduped by UID and
    never allowed to clash with a freshly-built event:
      (a) carry-forward -- if a company FAILED its fetch this run, keep ALL of
          its previous events (past AND future); a transient blip must not blank
          them.
      (b) history archive -- for EVERY company, keep genuinely-PAST events
          (start < today) that have aged out of the live fetch window, so the
          calendar/Sheet accumulates a permanent historical record. FUTURE
          events are deliberately NOT archived, so a real cancellation of an
          upcoming event still propagates as a deletion via the fresh fetch.

    Returns (blocks, carry_counts, archived_count)."""
    carried, seen = [], set()
    carry_counts = {}
    if failed_names:
        prev_by_co = _read_ics_vevents(path)
        for name in failed_names:
            n = 0
            for block in prev_by_co.get(name, []):
                uid = _block_uid(block)
                if uid in fresh_uids or uid in seen:
                    continue
                carried.append(block); seen.add(uid); n += 1
            carry_counts[name] = n
    archived = 0
    for uid, sdate, block in _read_ics_all_blocks(path):
        if sdate is None or sdate >= today:
            continue                       # only archive genuinely-past events
        if uid in fresh_uids or uid in seen:
            continue                       # already present this run
        carried.append(block); seen.add(uid); archived += 1
    return carried, carry_counts, archived


def write_ics(events: list, path: str, carried_vevents: list = None) -> None:
    now = _ics_dt(dt.datetime.now(dt.timezone.utc))
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//pharma-ir-calendar//EN", "CALSCALE:GREGORIAN"]
    for ev in events:
        desc_bits = []
        if getattr(ev, "notes", ""):
            desc_bits.append(f"Agenda: {ev.notes}")
        desc_bits.append(f"Type: {ev.event_type}")
        if getattr(ev, "docket", ""):
            desc_bits.append(f"Docket: {ev.docket}")
        desc_bits.append(f"Source: {ev.source_url}")
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
        url_out = ev.webcast_url or ev.source_url
        if url_out:
            lines.append(f"URL:{_esc(url_out)}")
        lines.append("END:VEVENT")
    # Re-emit verbatim VEVENT blocks retained from the PREVIOUS .ics: both
    # carried-forward events (a company that FAILED this run) and archived PAST
    # events (aged out of the live fetch but kept as history). Stable UIDs make
    # each a no-op update, never a duplicate.
    for block in (carried_vevents or []):
        lines += block
    lines.append("END:VCALENDAR")
    with open(path, "w") as f:
        f.write("\r\n".join(lines) + "\r\n")




# ===========================================================================
# SOURCE = the FEDERAL REGISTER API  (official U.S. Government; no third party,
# no API key, no scraping of fda.gov's Akamai-blocked host).
#
# By law FDA must publish every advisory-committee "Notice of Meeting" in the
# Federal Register at least 15 days before the meeting -- and, unlike the
# JS-rendered fda.gov calendar (which is just committee + date), each notice
# carries the AGENDA: the drug, application number, sponsor and indication.
# federalregister.gov is a DIFFERENT system from fda.gov, so the GitHub-runner
# fetch has a real chance of working; a snapshot fallback mirrors the AZ pattern.
#
# Because the FR notice has no structured "Center" field, the CBER-vs-CDER
# decision is made from the committee name (and any explicit center mention in
# the text) -- see _center_of_notice().
# ===========================================================================
import json

FR_API   = "https://www.federalregister.gov/api/v1/documents.json"
ICS_PATH = "fda_events.ics"
SNAPSHOT = "fda_fr.json"      # optional manual paste of the API JSON response

# Explicit center wording, if the notice happens to name it.
_CENTER_WORDS = {
    "cber": ("center for biologics evaluation and research", "(cber)"),
    "cder": ("center for drug evaluation and research", "(cder)"),
}

# The committee-name -> center map does the PRIMARY work (FR notices carry no
# structured Center column). Keys are lowercase substrings of the committee name.
_COMMITTEE_CENTER = {
    # --- CDER (drugs) ---
    "oncologic drugs": "cder",
    "antimicrobial drugs": "cder",
    "anti-infective drugs": "cder",
    "antiviral drugs": "cder",
    "arthritis": "cder",
    "cardiovascular and renal drugs": "cder",
    "dermatologic and ophthalmic": "cder",
    "drug safety and risk management": "cder",
    "endocrinologic and metabolic": "cder",
    "gastrointestinal drugs": "cder",
    "medical imaging drugs": "cder",
    "nonprescription drugs": "cder",
    "obstetrics, reproductive and urologic": "cder",
    "bone, reproductive and urologic": "cder",
    "peripheral and central nervous system": "cder",
    "pharmacy compounding": "cder",
    "psychopharmacologic drugs": "cder",
    "pulmonary-allergy drugs": "cder",
    "anesthetic and analgesic": "cder",
    # --- CBER (biologics) ---
    "vaccines and related biological products": "cber",
    "blood products": "cber",
    "cellular, tissue, and gene therapies": "cber",
    "allergenic products": "cber",
}

# Documents that were skipped -- surfaced in the run log so nothing vanishes silently.
_UNDATED   = []   # matched a CBER/CDER committee but no meeting date could be read
_CANCELLED = []   # cancellation / postponement notices (can't retro-delete)


def _center_of_notice(title: str, abstract: str):
    """Return 'cber' / 'cder' if this notice is one we keep, else None."""
    text = (title + " " + abstract).lower()
    for tag, needles in _CENTER_WORDS.items():
        if any(n in text for n in needles):
            return tag
    for name, tag in _COMMITTEE_CENTER.items():
        if name in text:
            return tag
    return None


_MONTH = ("January|February|March|April|May|June|July|August|September|"
          "October|November|December")
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

# Cross-month range: "September 30 to October 1, 2026"
_RE_CROSS = re.compile(
    rf"({_MONTH})\s+(\d{{1,2}})\s*(?:-|\u2013|\u2014|to|through)\s*"
    rf"({_MONTH})\s+(\d{{1,2}}),?\s+(\d{{4}})", re.I)
# Same-month range: "July 23-24, 2026" / "October 1 and 2, 2026"
_RE_RANGE = re.compile(
    rf"({_MONTH})\s+(\d{{1,2}})\s*(?:-|\u2013|\u2014|to|through|and|&)\s*"
    rf"(\d{{1,2}}),?\s+(\d{{4}})", re.I)
# Single: "April 30, 2026"
_RE_SINGLE = re.compile(rf"({_MONTH})\s+(\d{{1,2}}),?\s+(\d{{4}})", re.I)


def _d(y, mo_name_or_num, day):
    mo = mo_name_or_num if isinstance(mo_name_or_num, int) else _MONTHS[mo_name_or_num.lower()]
    return dt.date(int(y), int(mo), int(day))


def _dates_from_text(text: str):
    """Return (start_date, end_date) for the FIRST date/range in text, else (None, None)."""
    if not text:
        return None, None
    m = _RE_CROSS.search(text)
    if m:
        return _d(m.group(5), m.group(1), m.group(2)), _d(m.group(5), m.group(3), m.group(4))
    m = _RE_RANGE.search(text)
    if m:
        return _d(m.group(4), m.group(1), m.group(2)), _d(m.group(4), m.group(1), m.group(3))
    m = _RE_SINGLE.search(text)
    if m:
        d0 = _d(m.group(3), m.group(1), m.group(2))
        return d0, d0
    return None, None


def _meeting_clause(text: str) -> str:
    """The sentence describing when the meeting is HELD (avoids grabbing the
    comment-deadline date that also appears in the DATES field)."""
    for sent in re.split(r"(?<=[.;])\s+", text or ""):
        low = sent.lower()
        if "held" in low or ("meeting" in low and " on " in low):
            return sent
    return ""


def _extract_meeting_dates(dates_field: str, abstract: str, title: str):
    """Meeting date, preferring the 'meeting will be held on ...' clause."""
    # 1) the held-clause inside the structured DATES field
    s, e = _dates_from_text(_meeting_clause(dates_field))
    if s:
        return s, e
    # 2) any date in the DATES field
    s, e = _dates_from_text(dates_field)
    if s:
        return s, e
    # 3) the held-clause in the abstract, then any date in title
    s, e = _dates_from_text(_meeting_clause(abstract))
    if s:
        return s, e
    return _dates_from_text(title)


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


# Phrases that mark PURE BOILERPLATE (no real agenda) -> yields no topic.
_BOILER = re.compile(
    r"announces a forthcoming public advisory committee meeting|"
    r"general function of the committee|"
    r"will be open to the public|"
    r"provide advice and recommendations to the agency", re.I)

# Sponsor company: "from/submitted by <Company ...><corporate marker>" (comma-tolerant)
_SPONSOR = re.compile(
    r"\b(?:from|submitted by|sponsored by)\s+"
    r"([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Za-z0-9&.\-]+){0,4}?"
    r"\s*,?\s*(?:Inc|LLC|Ltd|Corporation|Corp|Company|Co|Therapeutics|"
    r"Pharmaceuticals|Pharma|Biosciences|Sciences|Biotech|GmbH|AG|plc)\.?)\b")

# Trailing GENERIC corporate suffixes to trim (keep Therapeutics/Pharmaceuticals).
_GENERIC_SUFFIX = re.compile(
    r"[,\s]+(?:Inc|LLC|Ltd|Corporation|Corp|Company|Co|GmbH|AG|plc)\.?$", re.I)

# Application number: optional-paren (BLA)/BLA/NDA/sNDA/ANDA/EUA + number + optional S-004
_APPNO = re.compile(
    r"\(?\s*(s?(?:BLA|NDA|ANDA)|EUA)\s*\)?\s*#?\s*(\d{4,6})((?:\s*S-?\d+)?)", re.I)

# Branded drug: "for BRANDNAME (generic)"
_DRUG = re.compile(r"\bfor\s+([A-Z][A-Za-z0-9\-]{2,})\s*\(([a-z][A-Za-z0-9\- ]+)\)")


def _clean_sponsor(s: str) -> str:
    s = s.strip().rstrip(".,")
    prev = None
    while prev != s:                      # strip possibly-stacked generic suffixes
        prev = s
        s = _GENERIC_SUFFIX.sub("", s).strip().rstrip(".,")
    return s


def _extract_topic(abstract: str) -> str:
    """Concise 'company + drug' phrase for the event TITLE.

    Priority: (branded drug / application no.) + sponsor when the abstract
    names them; else a real 'discuss ...' clause; else '' (boilerplate notice)
    so the title falls back to just the committee name -- NEVER a dumped
    paragraph of FDA filler."""
    a = _clean_text(abstract)
    if not a:
        return ""
    drug = _DRUG.search(a)
    appno = _APPNO.search(a)
    sponsor = _SPONSOR.search(a)

    drug_txt = f"{drug.group(1)} ({drug.group(2).strip()})" if drug else ""
    app_txt = ""
    if appno:
        app_txt = (f"{appno.group(1).upper()} {appno.group(2)}"
                   f"{appno.group(3).replace(' ', '')}").strip()
    spon_txt = _clean_sponsor(sponsor.group(1)) if sponsor else ""

    left, right = spon_txt, (drug_txt or app_txt)
    if left and right:
        return f"{left} \u2014 {right}"
    if right:
        return right
    if left:
        return left

    m = re.search(r"(?:will |to )?discuss(?:ion)?"
                  r"(?: and make recommendations)?(?: on| of| the)?\s+(.+)", a, re.I)
    if m:
        clause = re.split(r"(?<=[.;])\s", m.group(1))[0].strip().rstrip(".")
        clause = re.sub(r"^the\s+", "", clause, flags=re.I)
        if clause and not _BOILER.search(clause):
            return (clause[:87] + "...") if len(clause) > 90 else clause
    return ""


def _agenda_note(abstract: str) -> str:
    """Fuller agenda text for the Sheet's Details column (capped)."""
    a = _clean_text(abstract)
    return a[:600] + ("..." if len(a) > 600 else "")


def _committee_name(title: str) -> str:
    """The committee name = the part of the FR title before the first ';'."""
    return _clean_text(title.split(";")[0])


def fetch_fda(cfg: dict | None = None) -> str:
    """Return the Federal Register API JSON (as text). Prefer a committed
    snapshot (`fda_fr.json`) for a guaranteed-offline manual refresh; otherwise
    query the live API. Raises on failure so the run turns red and
    carry-forward/history-archive protect prior events."""
    if os.path.exists(SNAPSHOT):
        with open(SNAPSHOT, encoding="utf-8") as f:
            data = f.read()
        try:
            n = len(json.loads(data).get("results", []))
        except Exception as e:
            raise RuntimeError(
                f"FDA: snapshot '{SNAPSHOT}' is not valid JSON ({e}). "
                "Re-save the whole Federal Register API response and commit.")
        print(f"FDA: using saved snapshot '{SNAPSHOT}' ({n} document(s)).")
        return data

    # Live path: Python standard library only (urllib) -> ZERO dependencies.
    import urllib.request, urllib.error, urllib.parse
    params = [
        ("conditions[agencies][]", "food-and-drug-administration"),
        ("conditions[type][]", "NOTICE"),
        ("conditions[term]", "advisory committee meeting"),
        ("per_page", "200"),
        ("order", "newest"),
    ]
    for fld in ("document_number", "title", "abstract", "publication_date",
                "html_url", "type", "docket_ids", "dates", "agencies"):
        params.append(("fields[]", fld))
    url = FR_API + "?" + urllib.parse.urlencode(params)
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            charset = resp.headers.get_content_charset() or "utf-8"
            text = resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"FDA: GET Federal Register API -> HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"FDA: GET Federal Register API failed -> {e.reason}")
    if status != 200:
        raise RuntimeError(f"FDA: GET Federal Register API -> HTTP {status}")
    print(f"FDA: fetched Federal Register API ({len(text)} bytes).")
    return text


def parse_fda(raw_json, cfg: dict | None = None) -> list:
    """Parse FR API JSON; keep only CBER/CDER advisory-committee meeting notices,
    each enriched with agenda / drug / docket."""
    events = []
    _UNDATED.clear(); _CANCELLED.clear()
    if not raw_json:
        return events
    obj = raw_json if isinstance(raw_json, dict) else json.loads(raw_json)
    docs = obj.get("results", []) if isinstance(obj, dict) else (obj or [])

    seen = set()
    for d in docs:
        title = _clean_text(d.get("title"))
        abstract = _clean_text(d.get("abstract"))
        low = (title + " " + abstract).lower()

        # Must be an advisory-committee MEETING notice.
        if "advisory committee" not in low and "advisory panel" not in low:
            continue
        if "meeting" not in low:
            continue

        tag = _center_of_notice(title, abstract)
        if not tag:                       # not CBER/CDER -> DROP
            continue

        # Cancellations/postponements: can't retro-delete a published event; log.
        if re.search(r"\b(cancellation|canceled|cancelled|postpone)", low):
            _CANCELLED.append(_committee_name(title))
            continue

        start_d, end_d = _extract_meeting_dates(
            d.get("dates") or "", abstract, title)
        if not start_d:                   # no reliable meeting date -> skip + log
            _UNDATED.append(_committee_name(title))
            continue
        end_d = end_d or start_d

        committee = _committee_name(title)
        basis = f"FDA|{committee}|{start_d}"
        uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        if uid in seen:                   # amended notice for same meeting -> newest wins
            continue
        seen.add(uid)

        topic = _extract_topic(abstract)
        dockets = d.get("docket_ids") or []
        ev = Event(
            company="FDA " + tag.upper(),
            title=committee + (f" \u2014 {topic}" if topic else ""),
            start_utc=dt.datetime(start_d.year, start_d.month, start_d.day,
                                  tzinfo=dt.timezone.utc),
            end_utc=dt.datetime(end_d.year, end_d.month, end_d.day,
                                tzinfo=dt.timezone.utc),
            tz_original="ET",
            event_type="advisory_committee",
            source_url=_clean_text(d.get("html_url")),
            location="Silver Spring, MD / Virtual",
            tags=[tag.upper()],
            all_day=True,
            notes=_agenda_note(abstract),
            docket=dockets[0] if dockets else "",
        )
        ev.uid = uid
        events.append(ev)
    return events


def main():
    offline = os.environ.get("FDA_OFFLINE") == "1"
    print("=" * 78)
    print(f"FDA ADVISORY COMMITTEE MEETINGS via Federal Register (CBER + CDER only)  "
          f"({'offline' if offline else 'LIVE'})")
    print("=" * 78)

    failed = False
    try:
        raw = os.environ.get("FDA_OFFLINE_JSON", "") if offline else fetch_fda()
        parsed = parse_fda(raw)
    except Exception as exc:
        failed = True
        parsed = []
        print(f"  !! FDA fetch/parse FAILED -> {type(exc).__name__}: {exc}")
        print("     (calendar still written from carry-forward / archive)")

    for ev in sorted(parsed, key=lambda e: e.start_utc):
        print(f"[KEEP {ev.tags[0]:4}] {ev.start_utc:%Y-%m-%d}  {ev.title}")
    if _CANCELLED:
        print(f"  note: skipped {len(_CANCELLED)} cancellation/postponement "
              f"notice(s): {', '.join(_CANCELLED)}")
    if _UNDATED:
        print(f"  note: skipped {len(_UNDATED)} CBER/CDER notice(s) with no "
              f"readable meeting date (check manually): {', '.join(_UNDATED)}")

    fresh_uids = {ev.uid for ev in parsed}
    failed_names = {"FDA CBER", "FDA CDER"} if failed else set()
    today = dt.datetime.now(dt.timezone.utc).date()
    carried, carry_counts, archived = _events_to_retain(
        ICS_PATH, fresh_uids, failed_names, today)
    for name, n in carry_counts.items():
        print(f"  carry-forward: retained {n} previous event(s) for {name}.")
    if archived:
        print(f"  history archive: retained {archived} past meeting(s).")

    write_ics(parsed, ICS_PATH, carried_vevents=carried)
    print(f"\nWrote {len(parsed)} CBER/CDER meeting(s) to {ICS_PATH}.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
