"""
Read South Tyrol's live volunteer fire-brigade dispatch list.

The Landesverband der Freiwilligen Feuerwehren Südtirols publishes every
call-out of the last 72 hours as a plain table, updated to the minute:
incident number, brigade, district, type of operation, alarm level, dispatch
keyword and timestamp. No names, no API, ten rows per page. Unlike the
national Vigili del Fuoco news log this is a dispatch record, not prose, so
there is nothing to extract with a model: this script scrapes the HTML and
parses the table.

The pages are a TYPO3 list whose pagination links carry a cHash, so page
URLs cannot be constructed; the script follows the links the first page
gives it.

Usage:
    export FIRECRAWL_API_KEY=fc-...
    python suedtirol_live_dispatch.py            # Italian column headers
    python suedtirol_live_dispatch.py --lang de  # German column headers
"""

import argparse
import csv
import json
import os
import sys
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from firecrawl import Firecrawl


class Colors:
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"


SITE_ROOT = "https://www.lfvbz.it"
LIST_PATH = {"it": "/it/corpi-in-intervento.html", "de": "/freiwillige-feuerwehren-im-einsatz.html"}
PAGE_MARKER = "tx_rollfvoperations_oplist%5BcurrentPage%5D="

COLUMNS = ["number", "brigade", "district", "operation_type", "alarm_level", "keyword", "sub_keyword", "reported_at"]


class TableRows(HTMLParser):
    """Collects the cell text of every row of the first table on the page."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._tables_seen = 0
        self._in_table = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._tables_seen += 1
            self._in_table = self._tables_seen == 1
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
        elif self._in_table and tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif self._in_table and tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_table(html: str) -> List[dict]:
    """Turn the dispatch table into records; the header row is dropped."""
    parser = TableRows()
    parser.feed(html)
    records = []
    for row in parser.rows:
        if len(row) != len(COLUMNS) or row[0].lower() in ("nummer", "numero"):
            continue
        records.append(dict(zip(COLUMNS, row)))
    return records


def page_number(url: str) -> int:
    return int(url.split(PAGE_MARKER, 1)[1].split("&", 1)[0])


def page_links(doc) -> List[str]:
    """The pagination links the page itself offers, in page order. Page 1 is the page we already have."""
    urls: List[str] = []
    for link in doc.links or []:
        if PAGE_MARKER not in link:
            continue
        url = urljoin(SITE_ROOT, link)
        if page_number(url) > 1 and url not in urls:
            urls.append(url)
    return sorted(urls, key=page_number)


def fetch_all(firecrawl: Firecrawl, lang: str) -> List[dict]:
    """Scrape page one, then every further page in one batch, and parse them all."""
    first_url = urljoin(SITE_ROOT, LIST_PATH[lang])
    print("{}Scraping {}{}".format(Colors.YELLOW, first_url, Colors.RESET))

    # This is a live list, so never accept a cached copy.
    first = firecrawl.scrape(first_url, formats=["html", "links"], max_age=0)
    records = parse_table(first.html or "")

    more = page_links(first)
    if more:
        print("{}Following {} further page(s){}".format(Colors.YELLOW, len(more), Colors.RESET))
        job = firecrawl.batch_scrape(more, formats=["html"], max_age=0)
        by_url = {doc.metadata_typed.source_url: doc for doc in job.data if doc.metadata_typed.source_url}
        for url in more:
            doc = by_url.get(url)
            if doc and doc.html:
                records.extend(parse_table(doc.html))

    # The same incident appears once per brigade that responded; keep every
    # row but dedupe exact repeats that pagination can produce as the list moves.
    seen = set()
    unique = []
    for record in records:
        key = (record["number"], record["brigade"], record["reported_at"])
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return unique


def write_outputs(records: List[dict], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    ndjson_path = os.path.join(out_dir, "suedtirol_dispatch.ndjson")
    with open(ndjson_path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    csv_path = os.path.join(out_dir, "suedtirol_dispatch.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(records)
    print("{}Wrote {} and {}{}".format(Colors.GREEN, ndjson_path, csv_path, Colors.RESET))


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Read South Tyrol's live volunteer fire-brigade dispatch list.")
    parser.add_argument("--lang", choices=sorted(LIST_PATH), default="it", help="Language of the source page")
    parser.add_argument("--out", default="output", help="Directory for the NDJSON and CSV output")
    args = parser.parse_args()

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("{}FIRECRAWL_API_KEY is not set.{}".format(Colors.RED, Colors.RESET))
        return 1

    firecrawl = Firecrawl(api_key=api_key, api_url=os.getenv("FIRECRAWL_API_URL"))

    records = fetch_all(firecrawl, args.lang)
    if not records:
        print("{}No rows parsed -- the table layout may have changed.{}".format(Colors.RED, Colors.RESET))
        return 1

    print("{}{} call-out row(s) in the last 72 hours{}".format(Colors.CYAN, len(records), Colors.RESET))
    for record in records[:10]:
        print("  {reported_at}  {brigade} ({district})  {operation_type} {alarm_level}  {keyword} / {sub_keyword}".format(**record))

    write_outputs(records, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
