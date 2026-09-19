"""
Extract Croatian firefighter intervention data from the HVZ DVOC daily reports.

Croatia has no open-data feed for firefighter interventions. The national
open data portal (data.gov.hr) carries a record for "Evidencija vatrogasnih
intervencija", but its only resource points at duzs.hr -- a domain that no
longer resolves, since the publishing agency was dissolved in 2019.

What does still get published, every day, is the Državni vatrogasni
operativni centar (DVOC 193) report on hvz.gov.hr. It is free prose, so this
example uses Firecrawl's JSON extraction to turn it into structured records.

Two things to know about the source before you build on it:
  - It is a ~24h digest, not a live feed. The report for a given night is
    published the following day.
  - It covers "značajnije" (significant) interventions. The header totals
    count every intervention nationally, but only notable ones are narrated,
    so len(incidents) is normally much smaller than total_interventions.

Usage:
    export FIRECRAWL_API_KEY=fc-...
    python croatia_firefighter_interventions.py --pages 1 --limit 5
"""

import argparse
import csv
import json
import os
import re
import sys
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from firecrawl import Firecrawl
from pydantic import BaseModel, Field


class Colors:
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"


SITE_ROOT = "https://hvz.gov.hr"
LISTING_URL = SITE_ROOT + "/vijesti/8"
REPORT_PATH_PATTERN = re.compile(r"^/vijesti/dvoc-[a-z0-9-]+/\d+$")

# A published DVOC report never changes, so Firecrawl may serve a cached copy
# instead of re-fetching and re-extracting on every run. Seven days in ms.
CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000


class Incident(BaseModel):
    """A single narrated intervention within a DVOC report."""

    county: str = Field(description="Croatian county (županija) the ŽVOC reporting the incident belongs to")
    reported_at: Optional[str] = Field(None, description="When the call was received, as YYYY-MM-DD HH:MM. Infer the year from the report period.")
    location: str = Field(description="Place name(s) given for the incident")
    fuel_type: Optional[str] = Field(None, description="What burned, e.g. 'trava, nisko raslinje i makija'. Null for non-fire interventions.")
    hectares: Optional[float] = Field(None, description="Burned area in hectares, null if unestimated")
    units: List[str] = Field(default_factory=list, description="Fire units involved, e.g. 'JVP Knin', 'DVD Zagvozd', 'IVP Šibenik'")
    firefighters: Optional[int] = Field(None, description="Number of firefighters deployed")
    vehicles: Optional[int] = Field(None, description="Number of fire vehicles deployed")
    aircraft: Optional[int] = Field(None, description="Number of firefighting aircraft deployed")
    status: Optional[str] = Field(None, description="One of: aktivan, lokaliziran, ugašen, nepoznato")


class DvocReport(BaseModel):
    """One nightly DVOC 193 report (07:00-07:00 window)."""

    period: Optional[str] = Field(None, description="Reporting period as printed, e.g. '06. / 07. rujna 2026.'")
    total_interventions: Optional[int] = Field(None, description="Total interventions recorded nationally in the period")
    total_organizations: Optional[int] = Field(None, description="Number of fire organizations that took part")
    total_firefighters: Optional[int] = Field(None, description="Total firefighters deployed")
    total_vehicles: Optional[int] = Field(None, description="Total fire vehicles deployed")
    total_aircraft: Optional[int] = Field(None, description="Total firefighting aircraft deployed")
    total_hectares: Optional[float] = Field(None, description="Total burned area in hectares")
    incidents: List[Incident] = Field(default_factory=list, description="Every individually narrated intervention in the report")


def discover_report_urls(firecrawl: Firecrawl, pages: int) -> List[str]:
    """Walk the HVZ news listing and collect DVOC report URLs, newest first."""
    urls: List[str] = []
    seen = set()

    for page in range(1, pages + 1):
        listing = LISTING_URL if page == 1 else "{}?page={}".format(LISTING_URL, page)
        print("{}Listing {}{}".format(Colors.YELLOW, listing, Colors.RESET))

        doc = firecrawl.scrape(listing, formats=["links"])
        for link in doc.links or []:
            # Links come back absolute, but tolerate root-relative hrefs too.
            path = urlparse(link).path if link.startswith("http") else link
            if not REPORT_PATH_PATTERN.match(path):
                continue
            url = urljoin(SITE_ROOT, path)
            if url not in seen:
                seen.add(url)
                urls.append(url)

    # Article ids increase over time, so this is newest-first regardless of
    # the order links happened to appear in the page.
    urls.sort(key=lambda u: int(u.rsplit("/", 1)[1]), reverse=True)
    return urls


def extract_reports(firecrawl: Firecrawl, urls: List[str]) -> List[dict]:
    """Extract every DVOC report in one batch-scrape job.

    Firecrawl scrapes the whole list server-side and runs the same JSON
    extraction on each page, so this is one call instead of one per URL.
    max_age lets already-seen reports come back from cache.
    """
    job = firecrawl.batch_scrape(
        urls,
        formats=[{"type": "json", "schema": DvocReport}],
        only_main_content=True,
        max_age=CACHE_MAX_AGE_MS,
    )

    reports: List[dict] = []
    for doc in job.data:
        if not doc.json:
            continue
        report = dict(doc.json)
        report["source_url"] = doc.metadata_typed.source_url
        reports.append(report)

    # The batch may return documents in any order; keep output newest-first by
    # the numeric article id that ends each report URL.
    def article_id(report: dict) -> int:
        url = report.get("source_url") or ""
        tail = url.rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else 0

    reports.sort(key=article_id, reverse=True)

    credits = "" if job.credits_used is None else " ({} credits)".format(job.credits_used)
    print("{}Batch {}: {}/{} scraped, {} report(s) extracted{}{}".format(
        Colors.CYAN, job.status, job.completed, job.total, len(reports), credits, Colors.RESET))
    return reports


def write_outputs(reports: List[dict], out_dir: str) -> None:
    """Write reports as NDJSON and their incidents as a flat CSV."""
    os.makedirs(out_dir, exist_ok=True)

    ndjson_path = os.path.join(out_dir, "dvoc_reports.ndjson")
    with open(ndjson_path, "w", encoding="utf-8") as handle:
        for report in reports:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")

    csv_path = os.path.join(out_dir, "dvoc_incidents.csv")
    columns = [
        "period", "source_url", "county", "reported_at", "location",
        "fuel_type", "hectares", "units", "firefighters", "vehicles",
        "aircraft", "status",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for report in reports:
            for incident in report.get("incidents") or []:
                row = {key: incident.get(key) for key in columns}
                row["period"] = report.get("period")
                row["source_url"] = report.get("source_url")
                row["units"] = "; ".join(incident.get("units") or [])
                writer.writerow(row)

    print("{}Wrote {} and {}{}".format(Colors.GREEN, ndjson_path, csv_path, Colors.RESET))


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Extract Croatian DVOC firefighter intervention reports.")
    parser.add_argument("--pages", type=int, default=1, help="How many listing pages to walk (10 reports per page)")
    parser.add_argument("--limit", type=int, default=5, help="Maximum reports to extract")
    parser.add_argument("--out", default="output", help="Directory for the NDJSON and CSV output")
    args = parser.parse_args()

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("{}FIRECRAWL_API_KEY is not set.{}".format(Colors.RED, Colors.RESET))
        return 1

    # api_url lets this run against a self-hosted instance.
    firecrawl = Firecrawl(api_key=api_key, api_url=os.getenv("FIRECRAWL_API_URL"))

    urls = discover_report_urls(firecrawl, args.pages)
    if not urls:
        print("{}No DVOC reports found -- the listing layout may have changed.{}".format(Colors.RED, Colors.RESET))
        return 1

    selected = urls[: args.limit]
    print("{}Found {} report(s), extracting {}{}".format(Colors.CYAN, len(urls), len(selected), Colors.RESET))

    reports = extract_reports(firecrawl, selected)
    if not reports:
        print("{}Nothing extracted.{}".format(Colors.RED, Colors.RESET))
        return 1

    for report in reports:
        print("  {} -- {} narrated incident(s) of {} total".format(
            report.get("period") or "?",
            len(report.get("incidents") or []),
            report.get("total_interventions") or "?",
        ))

    write_outputs(reports, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
