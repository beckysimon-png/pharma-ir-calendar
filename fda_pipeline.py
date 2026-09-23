"""
FDA Advisory Committee calendar -> fda_events.ics  (its OWN calendar / Sheet tab)

Scope: keep ONLY meetings whose FDA "Center" column is
    - Center for Biologics Evaluation and Research (CBER), or
    - Center for Drug Evaluation and Research (CDER).
Everything else (Medical Devices / CDRH panels, Science Board, NCTR, etc.) is dropped.

Source = the OFFICIAL FDA page (Option A, no third parties):
    https://www.fda.gov/advisory-committees/advisory-committee-calendar
The calendar renders a table:  Start Date | End Date | Meeting | Contributing Office | Center
so the keep/drop decision is literally "read the Center cell".

Data path (mirrors the AstraZeneca pattern you already run):
  * LIVE  : fetch_fda() GETs the calendar page and parse_fda() reads the table.
  * MANUAL: if a saved snapshot file `fda_calendar.html` exists in the repo,
            it is parsed INSTEAD of the network (paste the page source, commit).
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
    # Re-emit verbatim VEVENT blocks retained from the PREVIOUS .ics: both
    # carried-forward events (a company that FAILED this run) and archived PAST
    # events (aged out of the live fetch but kept as history). Stable UIDs make
    # each a no-op update, never a duplicate.
    for block in (carried_vevents or []):
        lines += block
    lines.append("END:VCALENDAR")
    with open(path, "w") as f:
        f.write("\r\n".join(lines) + "\r\n")




CALENDAR_URL = "https://www.fda.gov/advisory-committees/advisory-committee-calendar"
ICS_PATH     = "fda_events.ics"
SNAPSHOT     = "fda_calendar.html"   # optional manual paste of the page source

# The two centers we keep. Match is substring + case-insensitive so minor
# wording ("Center for Drug Evaluation and Research (CDER)") still matches.
KEEP_CENTERS = {
    "cber": "center for biologics evaluation and research",
    "cder": "center for drug evaluation and research",
}

# Fallback ONLY when the Center cell is blank: map well-known committee names to
# their center. (The live table almost always fills Center, so this is a safety net.)
_COMMITTEE_CENTER = {
    # --- CDER (drugs) ---
    "oncologic drugs": "cder", "antimicrobial drugs": "cder", "arthritis": "cder",
    "cardiovascular and renal drugs": "cder", "dermatologic and ophthalmic": "cder",
    "drug safety and risk management": "cder", "endocrinologic and metabolic": "cder",
    "gastrointestinal drugs": "cder", "nonprescription drugs": "cder",
    "pharmacy compounding": "cder", "peripheral and central nervous system": "cder",
    "psychopharmacologic drugs": "cder", "pulmonary-allergy drugs": "cder",
    "bone, reproductive and urologic": "cder", "anesthetic and analgesic": "cder",
    # --- CBER (biologics) ---
    "vaccines and related biological products": "cber",
    "blood products": "cber", "cellular, tissue, and gene therapies": "cber",
    "allergenic products": "cber",
}


def _center_of(center_cell: str, meeting_title: str) -> str | None:
    """Return 'cber' / 'cder' if this row is one we keep, else None."""
    c = center_cell.lower()
    for tag, needle in KEEP_CENTERS.items():
        if needle in c:
            return tag
    # Fallback: Center cell blank/garbled -> infer from committee name.
    if not center_cell.strip():
        t = meeting_title.lower()
        for name, tag in _COMMITTEE_CENTER.items():
            if name in t:
                return tag
    return None


def _clean(cell: str) -> str:
    """Strip tags/entities/whitespace from one HTML cell."""
    txt = re.sub(r"<[^>]+>", " ", cell)
    return re.sub(r"\s+", " ", html.unescape(txt)).strip()


def _first_href(cell: str) -> str:
    m = re.search(r'href="([^"]+)"', cell)
    if not m:
        return ""
    href = html.unescape(m.group(1)).strip()
    if href.startswith("/"):
        href = "https://www.fda.gov" + href
    return href


def _parse_date(s: str):
    """FDA dates are MM/DD/YYYY (start/end). Return a date, or None."""
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def fetch_fda(cfg: dict | None = None) -> str:
    """Return the calendar HTML. Prefer a committed snapshot (manual refresh);
    otherwise GET the live page. Raises on a non-200 live response so the run
    turns red and carry-forward protects prior events."""
    if os.path.exists(SNAPSHOT):
        with open(SNAPSHOT, encoding="utf-8") as f:
            data = f.read()
        if "advisory" not in data.lower():
            raise RuntimeError(
                f"FDA: snapshot '{SNAPSHOT}' does not look like the calendar page "
                "(no 'advisory' text). Re-save the full page source and commit.")
        print(f"FDA: using saved snapshot '{SNAPSHOT}' ({len(data)} bytes).")
        return data

    import requests  # only needed on the live path
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
    }
    r = requests.get(CALENDAR_URL, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"FDA: GET {CALENDAR_URL} -> HTTP {r.status_code}")
    print(f"FDA: fetched live calendar ({len(r.text)} bytes).")
    return r.text


def parse_fda(raw_html: str, cfg: dict | None = None) -> list:
    """Parse the calendar table; keep only CBER/CDER rows as all-day Events."""
    events = []
    if not raw_html:
        return events

    # Isolate the calendar table (the one whose header carries a 'Center' column).
    # Be tolerant: walk every <tr>, require >=5 cells, map positionally.
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", raw_html, re.S | re.I)
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
        if len(cells) < 5:
            continue
        start_txt = _clean(cells[0])
        end_txt   = _clean(cells[1])
        meeting   = _clean(cells[2])
        center    = _clean(cells[4])
        start_d = _parse_date(start_txt)
        if not start_d:          # header row or malformed -> skip
            continue
        tag = _center_of(center, meeting)
        if not tag:              # not CBER/CDER -> DROP
            continue
        end_d = _parse_date(end_txt) or start_d
        source_url = _first_href(cells[2])
        start_utc = dt.datetime(start_d.year, start_d.month, start_d.day,
                                tzinfo=dt.timezone.utc)
        end_utc = dt.datetime(end_d.year, end_d.month, end_d.day,
                              tzinfo=dt.timezone.utc)
        ev = Event(
            company="FDA " + tag.upper(),
            title=meeting,
            start_utc=start_utc,
            end_utc=end_utc,
            tz_original="ET",
            event_type="advisory_committee",
            source_url=source_url,
            location="Silver Spring, MD / Virtual",
            tags=[tag.upper()],
            all_day=True,
        )
        basis = f"FDA|{meeting}|{start_d}"
        ev.uid = hashlib.sha1(basis.encode()).hexdigest()[:16] + "@ir-calendar"
        events.append(ev)
    return events


def main():
    offline = os.environ.get("FDA_OFFLINE") == "1"
    print("=" * 78)
    print(f"FDA ADVISORY COMMITTEE CALENDAR (CBER + CDER only)  "
          f"({'offline' if offline else 'LIVE'})")
    print("=" * 78)

    failed = False
    try:
        raw = os.environ.get("FDA_OFFLINE_HTML", "") if offline else fetch_fda()
        parsed = parse_fda(raw)
    except Exception as exc:
        failed = True
        parsed = []
        print(f"  !! FDA fetch/parse FAILED -> {type(exc).__name__}: {exc}")
        print("     (calendar still written from carry-forward / archive)")

    for ev in parsed:
        print(f"[KEEP {ev.tags[0]:4}] {ev.start_utc:%Y-%m-%d}  {ev.title}")

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
