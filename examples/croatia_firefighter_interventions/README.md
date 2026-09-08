# Croatian firefighter interventions (HVZ DVOC reports)

Turns Croatia's nightly national fire-service reports into structured records.

## Why scraping

Croatia publishes no open-data feed for firefighter interventions. The national
portal ([data.gov.hr](https://data.gov.hr)) still carries a dataset record,
*Evidencija vatrogasnih intervencija*, but its only resource points at
`http://duzs.hr/o-nama/otvoreni-podaci/` and that domain no longer resolves —
the publishing agency (Državna uprava za zaštitu i spašavanje) was folded into
the Ministry of the Interior in 2019 and the export was never rebuilt. The
underlying database, [UVI](https://hvz.gov.hr/istaknute-teme/informatizacija/sustav-upravljanje-vatrogasnim-intervencijama/101),
is an internal operational system and is not publicly exposed.

What *is* published daily is the Državni vatrogasni operativni centar (DVOC 193)
report on [hvz.gov.hr](https://hvz.gov.hr/vijesti/8). It is narrative Croatian
prose with no API and no RSS, which makes it a good fit for JSON extraction.

## What you get

Each report yields national totals plus one record per narrated incident:

| Field | Example |
| --- | --- |
| `county` | Splitsko-dalmatinska županija |
| `reported_at` | 2026-09-06 13:30 |
| `location` | Biokovsko Selo (Zagvozd) |
| `fuel_type` | trava, nisko raslinje i makija |
| `hectares` | 6.75 |
| `units` | DVD Zagvozd |
| `firefighters` / `vehicles` / `aircraft` | 9 / 4 / 1 |
| `status` | lokaliziran |

Output is `dvoc_reports.ndjson` (full reports) and `dvoc_incidents.csv`
(flattened incidents).

## Two caveats

**It is a digest, not a live feed.** The report covers a 07:00–07:00 window and
appears the following day, so expect roughly 24 hours of lag.

**Totals do not equal the incident list.** The header counts every intervention
nationally; the body narrates only *značajnije* (significant) ones. A night with
106 interventions may narrate eight. Use `total_interventions` for national
counts and `incidents` for incident-level detail — never sum the latter to get
the former.

For fresher, per-incident data, county fire associations publish their own
bulletins roughly twice daily (for example
[Šibensko-kninska županija](https://www.vatrogastvo-sibenik-knin.hr/stranice/intervencije/)),
but each formats its own, so national coverage means one scraper per county.

## Usage

```bash
pip install -r requirements.txt
export FIRECRAWL_API_KEY=fc-...

python croatia_firefighter_interventions.py --pages 1 --limit 5
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--pages` | 1 | Listing pages to walk (about 10 reports per page) |
| `--limit` | 5 | Maximum reports to extract |
| `--out` | `output` | Output directory |

Set `FIRECRAWL_API_URL` to run against a self-hosted instance.
