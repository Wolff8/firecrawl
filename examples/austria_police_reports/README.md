# Austrian police incident reports (Landespolizeidirektionen)

Turns the state police press bulletins on polizei.gv.at into structured
incident records, with a preset for the districts along the Slovenian border.

## Why scraping

Every Landespolizeidirektion publishes its press releases on
[polizei.gv.at](https://www.polizei.gv.at/stmk/presse/aussendungen/presse.aspx):
one release per incident, in long German prose — the district, the road, the
time, which fire brigades and rescue services responded, how many people were
hurt, whether a helicopter flew. There is no API. The BMI's official RSS feeds
exist but sit behind a bot challenge, and the archive pages have hashed file
names, so the example reads the listings and follows each page's own "older"
link back through the archive.

## What you get

| Field | Example |
| --- | --- |
| `is_incident` | true |
| `occurred_at` | 2026-09-19 05:15 |
| `state` / `district` | Steiermark / Graz-Umgebung |
| `municipality` / `road` | Brodersdorf / B 65 |
| `incident_type` | Verkehrsunfall |
| `emergency_services` | Feuerwehr Haselbach; Feuerwehr Ludersdorf; Rettungsteam; Notärztin |
| `injured` / `fatalities` / `people_rescued` | 1 / 0 / 1 |
| `summary_de` | Pkw kam von der B 65 ab und prallte gegen ein Mehrparteienhaus; Lenker schwer verletzt. |
| `report_no` / `published_at` | 466856 / 2026-09-19 09:05 |

The listing itself already gives the release number, timestamp, title and
teaser, and in Styria the topic column is the district. That listing metadata
is parsed from the HTML with no model involved and merged with the extracted
fields.

Output is `police_reports.ndjson` (every release) and `police_incidents.csv`
(only `is_incident: true`; with `--border`, only border districts). Person
fields are counts only; the schema never asks for names.

## Slovenian border preset

`--border` walks Styria, Carinthia and Burgenland and limits the CSV to
releases whose district matches one of: Leibnitz, Deutschlandsberg,
Südoststeiermark, Radkersburg, Völkermarkt, Klagenfurt (city and Land),
Villach (city and Land), Jennersdorf. The district comes from the extraction,
or from the listing topic when Styria fills it in.

## How it uses Firecrawl

Listing pages are scraped as `html` with `max_age=0` (they change through the
day) and parsed for rows and the "older" link. The releases are then pulled in
a single **`batch_scrape`** call — one job that scrapes every URL server-side
and runs the same JSON extraction on each — with **`max_age`** set to seven
days, since a published release never changes. The job reports how many URLs
completed and the credits it used.

## Two caveats

**Not every release is an incident.** Recruiting drives, statistics and
campaigns share the feed. The `is_incident` flag is the gate.

**Only Styria labels districts on the listing.** Carinthia and Burgenland
file everything under "Aktuelle Meldungen", so for them the border filter
relies on the district the extraction reads from the text.

## Usage

```bash
pip install -r requirements.txt
export FIRECRAWL_API_KEY=fc-...

python austria_police_reports.py --state stmk --pages 3
python austria_police_reports.py --border --pages 5 --limit 100
python austria_police_reports.py --all --pages 2 --limit 100
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--state` / `--border` / `--all` | `--state stmk` | Which states to walk (mutually exclusive) |
| `--pages` | 1 | Listing pages to walk per state (about 6 releases per page) |
| `--limit` | 30 | Maximum releases to extract |
| `--out` | `output` | Output directory |

Set `FIRECRAWL_API_URL` to run against a self-hosted instance.
