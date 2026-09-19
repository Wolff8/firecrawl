"""
Extract incident reports from the Austrian state police press bulletins.

Each Landespolizeidirektion publishes its press releases on polizei.gv.at:
long German prose, one release per incident, with the district, the road,
the time, the responding fire brigades and rescue services, and the outcome
("Brodersdorf. – Samstagfrüh, 19. September 2026, geriet ein 19-jähriger
Autofahrer von der B65 ab ... Die Einsatzkräfte der Feuerwehr Haselbach und
Feuerwehr Ludersdorf befreiten den 19-Jährigen ..."). There is no API and the
official RSS feeds sit behind a bot challenge, so this example reads the
listings and uses Firecrawl's JSON extraction to structure the releases.

The listing gives every release a number, a timestamp and a topic column; in
Styria the topic is the district, which is what --border keys on. Archive
pages are reached by following the "older" link each page offers, because
the page file names carry a hash that cannot be constructed.

Usage:
    export FIRECRAWL_API_KEY=fc-...
    python austria_police_reports.py --state stmk --pages 3
    python austria_police_reports.py --border --pages 5     # Styria, Carinthia, Burgenland; CSV limited to Slovenian-border districts
    python austria_police_reports.py --all --pages 2         # every state
"""

import argparse
import csv
import html
import json
import os
import re
import sys
from typing import Dict, List, Optional
from urllib.parse import urljoin

from dotenv import load_dotenv
from firecrawl import Firecrawl
from pydantic import BaseModel, Field


class Colors:
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"


SITE_ROOT = "https://www.polizei.gv.at"

STATES: Dict[str, str] = {
    "bgld": "Burgenland", "ktn": "Kärnten", "noe": "Niederösterreich", "ooe": "Oberösterreich",
    "sbg": "Salzburg", "stmk": "Steiermark", "tirol": "Tirol", "vbg": "Vorarlberg", "wien": "Wien",
}

BORDER_STATES = ["stmk", "ktn", "bgld"]

# Districts (Bezirke) along the Slovenian border, plus the two Carinthian
# cities that sit a few kilometres from it. Matched case-insensitively as
# substrings of the district field.
BORDER_DISTRICTS = [
    "leibnitz", "deutschlandsberg", "südoststeiermark", "radkersburg",
    "völkermarkt", "klagenfurt", "villach",
    "jennersdorf",
]

# One release on the listing page.
ROW_PATTERN = re.compile(
    r'class="pa_nr"><p>(?P<nr>.*?)</p>.*?'
    r'class="pa_thema"><p>(?P<thema>.*?)</p>.*?'
    r'<h3>(?P<title>.*?)</h3>.*?'
    r'class="pa_vorspann"><p>(?P<teaser>.*?)</p>.*?'
    r'href="(?P<href>presse[0-9a-f]*\.html\?prid=[0-9a-f]+(?:&(?:amp;)?pro=\d+)?)"',
    re.S,
)
NR_PATTERN = re.compile(r"Nr:\s*(\d+)\s+vom\s+(\d{2})\.(\d{2})\.(\d{4}),\s*(\d{2}:\d{2})")
OLDER_PATTERN = re.compile(r'href="(presse[0-9a-f]*\.html\?pro=(\d+))"')

# A published release never changes, so Firecrawl may serve a cached copy
# instead of re-fetching and re-extracting on every run. Seven days in ms.
CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000


class PoliceReport(BaseModel):
    """One press release, structured."""

    is_incident: bool = Field(description=(
        "True if the release reports a specific event at a place and time: a road accident, fire, rescue, "
        "search, missing person, crime, or similar. False for recruiting, statistics, campaigns, "
        "announcements or other institutional news."
    ))
    occurred_at: Optional[str] = Field(None, description=(
        "When the event happened as YYYY-MM-DD HH:MM, taken from the text (e.g. 'Samstagfrüh, 19. September 2026 ... "
        "Gegen 05:15 Uhr'). Omit the time if none is given."
    ))
    district: Optional[str] = Field(None, description="Bezirk, e.g. Leibnitz, Völkermarkt, Graz-Umgebung")
    municipality: Optional[str] = Field(None, description="Town or municipality, usually the dateline before the dash")
    road: Optional[str] = Field(None, description="Road designation if any, e.g. B 65, L107, A2, S36")
    incident_type: Optional[str] = Field(None, description=(
        "Short German label: Verkehrsunfall, Motorradunfall, Brand, Suchaktion, Alpinunfall, Einbruch, "
        "Bedrohung, Betrug, usw."
    ))
    emergency_services: List[str] = Field(default_factory=list, description=(
        "Every responding unit named: fire brigades ('Feuerwehr Haselbach'), rescue services, emergency doctor, "
        "helicopters ('Christophorus 12'), mountain or water rescue."
    ))
    injured: Optional[int] = Field(None, description="Count of injured people. Counts only, never names.")
    fatalities: Optional[int] = Field(None, description="Count of deaths. Counts only, never names.")
    people_rescued: Optional[int] = Field(None, description="Count of people rescued or evacuated. Counts only, never names.")
    summary_de: str = Field(description="One-sentence German summary. No names of persons.")


def listing_url(state: str) -> str:
    return "{}/{}/presse/aussendungen/presse.aspx".format(SITE_ROOT, state)


def clean(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def parse_listing(page_html: str, base_url: str, state: str) -> List[dict]:
    """The releases on one listing page, newest first, with what the listing already knows about them."""
    rows: List[dict] = []
    for match in ROW_PATTERN.finditer(page_html):
        nr = NR_PATTERN.search(clean(match.group("nr")))
        thema = clean(match.group("thema")).rstrip("| ").strip()
        rows.append({
            "source_url": urljoin(base_url, html.unescape(match.group("href"))),
            "state": STATES[state],
            "report_no": nr.group(1) if nr else None,
            "published_at": "{}-{}-{} {}".format(nr.group(4), nr.group(3), nr.group(2), nr.group(5)) if nr else None,
            # Styria fills this column with the district; other states use a generic topic.
            "listing_topic": thema or None,
            "title": clean(match.group("title")),
            "teaser": clean(match.group("teaser")),
        })
    return rows


def older_page(page_html: str, base_url: str, wanted: int) -> Optional[str]:
    """The link to archive page `wanted`, if this page offers one."""
    for href, number in OLDER_PATTERN.findall(page_html):
        if int(number) == wanted:
            return urljoin(base_url, html.unescape(href))
    return None


def discover_releases(firecrawl: Firecrawl, states: List[str], pages: int) -> List[dict]:
    """Walk each state's listing back through its archive and collect release rows."""
    rows: List[dict] = []
    seen = set()
    for state in states:
        url = listing_url(state)
        for page in range(pages):
            print("{}Listing {} page {}{}".format(Colors.YELLOW, STATES[state], page, Colors.RESET))
            # Listings change through the day, so never accept a cached copy.
            doc = firecrawl.scrape(url, formats=["html"], max_age=0)
            page_html = doc.html or ""
            for row in parse_listing(page_html, url, state):
                if row["source_url"] not in seen:
                    seen.add(row["source_url"])
                    rows.append(row)
            url = older_page(page_html, url, page + 1)
            if not url:
                break
    return rows


def extract_reports(firecrawl: Firecrawl, rows: List[dict]) -> List[dict]:
    """Extract every release in one batch-scrape job and merge in the listing metadata.

    Firecrawl scrapes the whole list server-side and runs the same JSON
    extraction on each page, so this is one call instead of one per URL.
    max_age lets already-seen releases come back from cache.
    """
    urls = [row["source_url"] for row in rows]
    job = firecrawl.batch_scrape(
        urls,
        formats=[{"type": "json", "schema": PoliceReport}],
        only_main_content=True,
        max_age=CACHE_MAX_AGE_MS,
        ignore_invalid_urls=True,
    )

    extracted = {doc.metadata_typed.source_url: doc.json for doc in job.data if doc.json and doc.metadata_typed.source_url}
    reports: List[dict] = []
    for row in rows:
        data = extracted.get(row["source_url"])
        if not data:
            continue
        report = dict(row)
        report.update(data)
        if not report.get("district") and row["listing_topic"] and is_border_district(row["listing_topic"]):
            report["district"] = row["listing_topic"]
        reports.append(report)

    credits = "" if job.credits_used is None else " ({} credits)".format(job.credits_used)
    print("{}Batch {}: {}/{} scraped, {} release(s) extracted{}{}".format(
        Colors.CYAN, job.status, job.completed, job.total, len(reports), credits, Colors.RESET))
    return reports


def is_border_district(text: Optional[str]) -> bool:
    low = (text or "").lower()
    return any(district in low for district in BORDER_DISTRICTS)


def write_outputs(reports: List[dict], out_dir: str, border_only: bool) -> int:
    """Write every release as NDJSON and the incidents (optionally border-only) as a flat CSV."""
    os.makedirs(out_dir, exist_ok=True)

    ndjson_path = os.path.join(out_dir, "police_reports.ndjson")
    with open(ndjson_path, "w", encoding="utf-8") as handle:
        for report in reports:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")

    csv_path = os.path.join(out_dir, "police_incidents.csv")
    columns = [
        "occurred_at", "published_at", "state", "district", "municipality", "road", "incident_type",
        "emergency_services", "injured", "fatalities", "people_rescued", "summary_de", "title",
        "report_no", "source_url",
    ]
    kept = 0
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for report in reports:
            if not report.get("is_incident"):
                continue
            if border_only and not (is_border_district(report.get("district")) or is_border_district(report.get("listing_topic"))):
                continue
            row = {key: report.get(key) for key in columns}
            row["emergency_services"] = "; ".join(report.get("emergency_services") or [])
            writer.writerow(row)
            kept += 1

    print("{}Wrote {} and {} ({} incident(s){}){}".format(
        Colors.GREEN, ndjson_path, csv_path, kept, ", border districts only" if border_only else "", Colors.RESET))
    return kept


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Extract incident reports from Austrian state police press bulletins.")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--state", action="append", choices=sorted(STATES), help="State to walk (repeatable)")
    where.add_argument("--border", action="store_true",
                       help="Styria, Carinthia and Burgenland; CSV limited to districts on the Slovenian border")
    where.add_argument("--all", action="store_true", help="Every state")
    parser.add_argument("--pages", type=int, default=1, help="Listing pages to walk per state (about 6 releases per page)")
    parser.add_argument("--limit", type=int, default=30, help="Maximum releases to extract")
    parser.add_argument("--out", default="output", help="Directory for the NDJSON and CSV output")
    args = parser.parse_args()

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("{}FIRECRAWL_API_KEY is not set.{}".format(Colors.RED, Colors.RESET))
        return 1

    # api_url lets this run against a self-hosted instance.
    firecrawl = Firecrawl(api_key=api_key, api_url=os.getenv("FIRECRAWL_API_URL"))

    if args.all:
        states = sorted(STATES)
    elif args.border:
        states = BORDER_STATES
    else:
        states = args.state or ["stmk"]

    rows = discover_releases(firecrawl, states, args.pages)
    if not rows:
        print("{}No releases found -- the listing layout may have changed.{}".format(Colors.RED, Colors.RESET))
        return 1

    selected = rows[: args.limit]
    print("{}Found {} release(s), extracting {}{}".format(Colors.CYAN, len(rows), len(selected), Colors.RESET))

    reports = extract_reports(firecrawl, selected)
    if not reports:
        print("{}Nothing extracted.{}".format(Colors.RED, Colors.RESET))
        return 1

    for report in reports:
        flag = "EINSATZ" if report.get("is_incident") else "info   "
        print("  {} {} {} / {} -- {}".format(
            flag, report.get("occurred_at") or report.get("published_at") or "?",
            report.get("district") or report.get("listing_topic") or "?",
            report.get("municipality") or "?", report.get("summary_de") or ""))

    write_outputs(reports, args.out, border_only=args.border)
    return 0


if __name__ == "__main__":
    sys.exit(main())
