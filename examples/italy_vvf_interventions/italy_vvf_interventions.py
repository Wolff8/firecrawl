"""
Extract Italian firefighter (Vigili del Fuoco) interventions from vigilfuoco.it.

Italy publishes no per-incident open data for fire-service interventions:
opendata.vigilfuoco.it carries aggregate statistics only, and the national
CKAN portal (dati.gov.it) has nothing beyond station locations and a 2009
municipal summary. What the Corpo Nazionale does publish, continuously, is
its news log on vigilfuoco.it -- one narrated article per notable intervention
("Alle 12.30 del 24 marzo, una squadra del comando di Gorizia è intervenuta a
Mariano del Friuli per un incidente stradale..."). It is Italian prose with no
API, so this example uses Firecrawl's JSON extraction to structure it.

Two things to know about the source before you build on it:
  - The national log (/media/notizie) is exhaustive and paginated hundreds of
    pages deep, but it mixes real interventions with ceremonies, drills,
    awards and recruitment notices. The schema carries an explicit
    is_intervention flag and the CSV keeps only rows where it is true.
  - Every provincial command and regional direction has its own filtered
    listing (/comando-vvf-<city>/notizie-dal-territorio). Some post daily,
    others go quiet for months, so for coverage prefer the national log and
    use --listing when you want one territory.

Usage:
    export FIRECRAWL_API_KEY=fc-...
    python italy_vvf_interventions.py --pages 2 --limit 20
    python italy_vvf_interventions.py --listing /comando-vvf-udine/notizie-dal-territorio
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


SITE_ROOT = "https://www.vigilfuoco.it"
NATIONAL_LISTING = "/media/notizie"

# Articles live under /media/notizie/<slug>, /comando-vvf-<city>/notizie/<slug>
# or /direzione-regionale-vigili-del-fuoco-<region>/notizie/<slug>. The
# listing pages themselves (.../notizie, .../notizie-dal-territorio) have no
# trailing slug and so do not match.
ARTICLE_PATH_PATTERN = re.compile(r"^/[a-z0-9-]+(?:/[a-z0-9-]+)?/notizie/[a-z0-9-]+$")

# A published article never changes, so Firecrawl may serve a cached copy
# instead of re-fetching and re-extracting on every run. Seven days in ms.
CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000


class VvfReport(BaseModel):
    """One vigilfuoco.it news article, structured."""

    is_intervention: bool = Field(description=(
        "True only if the article narrates an actual emergency response: a fire, rescue, road accident, "
        "weather damage, hazmat or similar call-out. False for ceremonies, exercises and drills, training, "
        "awards, publications, recruitment or institutional news."
    ))
    category: Optional[str] = Field(None, description="The 'Categoria' printed on the page, e.g. Incendi, Cronaca, Soccorsi")
    published_at: Optional[str] = Field(None, description="'Data di pubblicazione' as YYYY-MM-DD")
    occurred_at: Optional[str] = Field(None, description=(
        "When the call-out happened as YYYY-MM-DD HH:MM, from the body text (e.g. 'Alle 12.30 del 24 marzo'). "
        "Infer the year from the publication date. Omit the time if the body gives none."
    ))
    command: Optional[str] = Field(None, description="Responding command as written, e.g. 'Comando di Gorizia'")
    region: Optional[str] = Field(None, description="Italian region, e.g. Friuli Venezia Giulia")
    municipality: Optional[str] = Field(None, description="Municipality (comune) where the intervention took place")
    address: Optional[str] = Field(None, description="Street, road or locality if given, e.g. 'viale D'Annunzio', 'SR 251'")
    intervention_type: Optional[str] = Field(None, description=(
        "Short Italian label: incendio abitazione, incendio vegetazione, incidente stradale, soccorso persona, "
        "recupero animale, maltempo, fuga di gas, ecc."
    ))
    squads: Optional[int] = Field(None, description="Number of squadre deployed")
    vehicles: List[str] = Field(default_factory=list, description="Vehicle types named, e.g. 'autoscala', 'autopompa', 'autobotte'")
    people_rescued: Optional[int] = Field(None, description="Count of people brought to safety. Counts only, never names.")
    people_injured: Optional[int] = Field(None, description="Count of injured people. Counts only, never names.")
    fatalities: Optional[int] = Field(None, description="Count of deaths. Counts only, never names.")
    summary: str = Field(description="One-sentence Italian summary of what happened")


def discover_article_urls(firecrawl: Firecrawl, listing: str, pages: int) -> List[str]:
    """Walk a vigilfuoco.it listing (Drupal, 0-based ?page=) and collect article URLs, newest first."""
    urls: List[str] = []
    seen = set()

    for page in range(pages):
        url = urljoin(SITE_ROOT, listing) + ("" if page == 0 else "?page={}".format(page))
        print("{}Listing {}{}".format(Colors.YELLOW, url, Colors.RESET))

        doc = firecrawl.scrape(url, formats=["links"])
        for link in doc.links or []:
            path = urlparse(link).path if link.startswith("http") else link
            if not ARTICLE_PATH_PATTERN.match(path):
                continue
            article = urljoin(SITE_ROOT, path)
            if article not in seen:
                seen.add(article)
                urls.append(article)

    return urls


def source_of(url: str) -> str:
    """First path segment: 'media', 'comando-vvf-udine', 'direzione-regionale-...'."""
    return urlparse(url).path.strip("/").split("/")[0]


def extract_reports(firecrawl: Firecrawl, urls: List[str]) -> List[dict]:
    """Extract every article in one batch-scrape job.

    Firecrawl scrapes the whole list server-side and runs the same JSON
    extraction on each page, so this is one call instead of one per URL.
    max_age lets already-seen articles come back from cache.
    """
    job = firecrawl.batch_scrape(
        urls,
        formats=[{"type": "json", "schema": VvfReport}],
        only_main_content=True,
        max_age=CACHE_MAX_AGE_MS,
    )

    by_url = {}
    for doc in job.data:
        if not doc.json:
            continue
        url = doc.metadata_typed.source_url or ""
        report = dict(doc.json)
        report["source_url"] = url
        report["source"] = source_of(url)
        by_url[url] = report

    # Keep the listing's order (newest first); the batch may return in any order.
    reports = [by_url[u] for u in urls if u in by_url]

    credits = "" if job.credits_used is None else " ({} credits)".format(job.credits_used)
    print("{}Batch {}: {}/{} scraped, {} article(s) extracted{}{}".format(
        Colors.CYAN, job.status, job.completed, job.total, len(reports), credits, Colors.RESET))
    return reports


def write_outputs(reports: List[dict], out_dir: str) -> int:
    """Write all articles as NDJSON and the real interventions as a flat CSV."""
    os.makedirs(out_dir, exist_ok=True)

    ndjson_path = os.path.join(out_dir, "vvf_reports.ndjson")
    with open(ndjson_path, "w", encoding="utf-8") as handle:
        for report in reports:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")

    csv_path = os.path.join(out_dir, "vvf_interventions.csv")
    columns = [
        "occurred_at", "published_at", "category", "intervention_type", "region", "command",
        "municipality", "address", "squads", "vehicles", "people_rescued", "people_injured",
        "fatalities", "summary", "source", "source_url",
    ]
    kept = 0
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for report in reports:
            if not report.get("is_intervention"):
                continue
            row = {key: report.get(key) for key in columns}
            row["vehicles"] = "; ".join(report.get("vehicles") or [])
            writer.writerow(row)
            kept += 1

    print("{}Wrote {} and {} ({} intervention(s)){}".format(Colors.GREEN, ndjson_path, csv_path, kept, Colors.RESET))
    return kept


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Extract Vigili del Fuoco interventions from vigilfuoco.it.")
    parser.add_argument("--listing", default=NATIONAL_LISTING,
                        help="Listing path to walk: the national log, or e.g. /comando-vvf-udine/notizie-dal-territorio")
    parser.add_argument("--pages", type=int, default=1, help="Listing pages to walk (about 10 articles per page)")
    parser.add_argument("--limit", type=int, default=10, help="Maximum articles to extract")
    parser.add_argument("--out", default="output", help="Directory for the NDJSON and CSV output")
    args = parser.parse_args()

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("{}FIRECRAWL_API_KEY is not set.{}".format(Colors.RED, Colors.RESET))
        return 1

    # api_url lets this run against a self-hosted instance.
    firecrawl = Firecrawl(api_key=api_key, api_url=os.getenv("FIRECRAWL_API_URL"))

    urls = discover_article_urls(firecrawl, args.listing, args.pages)
    if not urls:
        print("{}No articles found -- the listing layout may have changed.{}".format(Colors.RED, Colors.RESET))
        return 1

    selected = urls[: args.limit]
    print("{}Found {} article(s), extracting {}{}".format(Colors.CYAN, len(urls), len(selected), Colors.RESET))

    reports = extract_reports(firecrawl, selected)
    if not reports:
        print("{}Nothing extracted.{}".format(Colors.RED, Colors.RESET))
        return 1

    for report in reports:
        flag = "INTERVENTO" if report.get("is_intervention") else "altro     "
        print("  {} {} {} -- {}".format(
            flag, report.get("occurred_at") or report.get("published_at") or "?",
            report.get("municipality") or "?", report.get("summary") or ""))

    write_outputs(reports, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
