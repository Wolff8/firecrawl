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

Where the articles come from is up to you:
  - the national log (/media/notizie), exhaustive and hundreds of pages deep;
  - a region preset (--region fvg), which fans out over every provincial
    command and the regional direction of that region;
  - --all, which first discovers all ~120 command and direction sites from
    the site index and then walks each one's notizie-dal-territorio view;
  - --search, which uses Firecrawl's news search to pick up fresh reports
    from local press and feeds them through the same extraction.

The log mixes real interventions with ceremonies, drills and institutional
notices, so the schema carries an is_intervention flag and the CSV keeps only
rows where it is true.

Usage:
    export FIRECRAWL_API_KEY=fc-...
    python italy_vvf_interventions.py --pages 3 --limit 30
    python italy_vvf_interventions.py --region fvg --pages 2
    python italy_vvf_interventions.py --all --limit 200
    python italy_vvf_interventions.py --search "vigili del fuoco incendio" --limit 20
"""

import argparse
import csv
import json
import os
import re
import sys
from typing import Dict, List, Optional
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
SITE_INDEX = "/siti-vvf"
SITE_INDEX_PAGES = 13

# Articles live under /media/notizie/<slug>, /comando-vvf-<city>/notizie/<slug>
# or /direzione-regionale-vigili-del-fuoco-<region>/notizie/<slug>. The
# listing pages themselves (.../notizie, .../notizie-dal-territorio) have no
# trailing slug and so do not match.
ARTICLE_PATH_PATTERN = re.compile(r"^/[a-z0-9-]+(?:/[a-z0-9-]+)?/notizie/[a-z0-9-]+$")

# Command and direction sites as linked from the site index.
SITE_PATH_PATTERN = re.compile(r"^/(?:comando-vvf|direzione-(?:inter)?regionale)[a-z0-9-]*$")

# Every command and direction site exposes its own filtered news view here.
TERRITORY_VIEW = "/notizie-dal-territorio"

REGIONS: Dict[str, List[str]] = {
    "fvg": [
        "/direzione-regionale-vigili-del-fuoco-friuli-venezia-giulia",
        "/comando-vvf-trieste", "/comando-vvf-udine", "/comando-vvf-gorizia", "/comando-vvf-pordenone",
    ],
    "veneto": [
        "/comando-vvf-belluno", "/comando-vvf-venezia", "/comando-vvf-padova",
        "/comando-vvf-verona", "/comando-vvf-vicenza", "/comando-vvf-treviso",
    ],
}

# A published article never changes, so Firecrawl may serve a cached copy
# instead of re-fetching and re-extracting on every run. Seven days in ms.
CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000


class VvfReport(BaseModel):
    """One news article about the Vigili del Fuoco, structured."""

    is_intervention: bool = Field(description=(
        "True only if the article narrates an actual emergency response: a fire, rescue, road accident, "
        "weather damage, hazmat or similar call-out. False for ceremonies, exercises and drills, training, "
        "awards, publications, recruitment or institutional news."
    ))
    category: Optional[str] = Field(None, description="The 'Categoria' printed on the page, e.g. Incendi, Cronaca, Soccorsi")
    published_at: Optional[str] = Field(None, description="Publication date as YYYY-MM-DD")
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


def page_url(path: str, page: int) -> str:
    """Drupal listings paginate with a 0-based ?page= parameter."""
    return urljoin(SITE_ROOT, path) + ("" if page == 0 else "?page={}".format(page))


def links_matching(docs, pattern: re.Pattern) -> List[str]:
    """Collect site-relative paths from scraped documents' links, in order and deduplicated."""
    out: List[str] = []
    seen = set()
    for doc in docs:
        for link in doc.links or []:
            path = urlparse(link).path if link.startswith("http") else link
            if pattern.match(path) and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def discover_sites(firecrawl: Firecrawl) -> List[str]:
    """Read the site index to find every command and regional direction site."""
    pages = [page_url(SITE_INDEX, p) for p in range(SITE_INDEX_PAGES)]
    print("{}Reading site index ({} pages){}".format(Colors.YELLOW, len(pages), Colors.RESET))
    job = firecrawl.batch_scrape(pages, formats=["links"], max_age=CACHE_MAX_AGE_MS)
    sites = links_matching(job.data, SITE_PATH_PATTERN)
    print("  {} command and direction sites".format(len(sites)))
    return sites


def discover_article_urls(firecrawl: Firecrawl, listings: List[str], pages: int) -> List[str]:
    """Scrape every listing page in one batch and collect article URLs.

    Listings are fetched together rather than one after another: with --all
    that is a hundred-odd sites times --pages, which is exactly what a batch
    job is for. Order is preserved, so the first listing's newest articles
    come first.
    """
    urls = [page_url(listing, p) for listing in listings for p in range(pages)]
    print("{}Scraping {} listing page(s) across {} listing(s){}".format(
        Colors.YELLOW, len(urls), len(listings), Colors.RESET))

    # Listings change daily, so they are never served from cache. A site
    # without a territory view returns 404, which the batch simply skips.
    job = firecrawl.batch_scrape(urls, formats=["links"], max_age=0, ignore_invalid_urls=True)

    by_url = {doc.metadata_typed.source_url: doc for doc in job.data if doc.metadata_typed.source_url}
    ordered = [by_url[u] for u in urls if u in by_url]
    return [urljoin(SITE_ROOT, path) for path in links_matching(ordered, ARTICLE_PATH_PATTERN)]


def search_article_urls(firecrawl: Firecrawl, query: str, limit: int) -> List[str]:
    """Use Firecrawl's news search to find fresh reports from any outlet.

    Local press reports the same call-outs the Corpo does, often sooner. The
    results go through the same extraction, and the is_intervention flag
    filters out whatever is not an actual response.
    """
    print("{}Searching news for: {}{}".format(Colors.YELLOW, query, Colors.RESET))
    result = firecrawl.search(query, sources=["news"], tbs="qdr:w", limit=limit)
    urls: List[str] = []
    for hit in result.news or []:
        url = getattr(hit, "url", None)
        if url and url not in urls:
            urls.append(url)
    print("  {} result(s)".format(len(urls)))
    return urls


def source_of(url: str) -> str:
    """Host for external articles, else the first path segment ('media', 'comando-vvf-udine', ...)."""
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != urlparse(SITE_ROOT).netloc:
        return parsed.netloc
    return parsed.path.strip("/").split("/")[0]


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
        ignore_invalid_urls=True,
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

    # Keep discovery order (newest first); the batch may return in any order.
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
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--listing", action="append",
                       help="Listing path to walk (repeatable), e.g. /comando-vvf-udine/notizie-dal-territorio")
    where.add_argument("--region", choices=sorted(REGIONS), help="Walk every command of a region plus its direction")
    where.add_argument("--all", action="store_true", help="Discover every command and direction site and walk them all")
    where.add_argument("--search", metavar="QUERY", help="Find fresh reports via Firecrawl news search instead")
    parser.add_argument("--pages", type=int, default=1, help="Listing pages to walk per listing (about 10 articles per page)")
    parser.add_argument("--limit", type=int, default=10, help="Maximum articles to extract")
    parser.add_argument("--out", default="output", help="Directory for the NDJSON and CSV output")
    args = parser.parse_args()

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("{}FIRECRAWL_API_KEY is not set.{}".format(Colors.RED, Colors.RESET))
        return 1

    # api_url lets this run against a self-hosted instance.
    firecrawl = Firecrawl(api_key=api_key, api_url=os.getenv("FIRECRAWL_API_URL"))

    if args.search:
        urls = search_article_urls(firecrawl, args.search, args.limit)
    else:
        if args.all:
            listings = [site + TERRITORY_VIEW for site in discover_sites(firecrawl)]
        elif args.region:
            listings = [site + TERRITORY_VIEW for site in REGIONS[args.region]]
        else:
            listings = args.listing or [NATIONAL_LISTING]
        urls = discover_article_urls(firecrawl, listings, args.pages)

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
