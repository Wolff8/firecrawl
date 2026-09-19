# Italian firefighter interventions (Vigili del Fuoco news log)

Turns the Corpo Nazionale dei Vigili del Fuoco's intervention reports into
structured records.

## Why scraping

Italy publishes no per-incident open data for fire-service call-outs. The
Corpo's own [opendata.vigilfuoco.it](https://opendata.vigilfuoco.it) holds
aggregate statistics only, and the national CKAN portal
([dati.gov.it](https://dati.gov.it)) returns nothing beyond station locations
and one municipal summary from 2009.

What *is* published, continuously, is the news log on
[vigilfuoco.it](https://www.vigilfuoco.it/media/notizie): one narrated article
per notable intervention, hundreds of pages deep, with the responding command,
the time, the municipality, the vehicles and the outcome written as Italian
prose. There is no API, which makes it a good fit for JSON extraction.

## What you get

Each article yields one record. Real interventions are flagged and flattened:

| Field | Example |
| --- | --- |
| `is_intervention` | true |
| `category` | Cronaca |
| `occurred_at` | 2026-03-24 12:30 |
| `command` | Comando di Gorizia |
| `region` / `municipality` | Friuli Venezia Giulia / Mariano del Friuli |
| `intervention_type` | incidente stradale |
| `squads` / `vehicles` | 1 / autopompa |
| `people_rescued` / `people_injured` / `fatalities` | 2 / 2 / 0 |
| `summary` | Due occupanti estricati dopo un incidente contro il muro di un'abitazione. |

Output is `vvf_reports.ndjson` (every article, including the ones flagged as
not interventions) and `vvf_interventions.csv` (only `is_intervention: true`).
Person fields are counts only; the schema never asks for names.

## How it uses Firecrawl

The listing page is scraped for `links` to discover article URLs (Drupal,
0-based `?page=`). The articles are then pulled in a single **`batch_scrape`**
call — one job that scrapes every URL server-side and runs the same JSON
extraction on each. Because a published article never changes, the batch runs
with **`max_age`** set to seven days, so articles seen on an earlier run come
straight from Firecrawl's cache. The job reports how many URLs completed and
the credits it used.

## Two caveats

**The log is not only interventions.** Ceremonies, exercises, awards and
recruitment notices sit in the same feed. The `is_intervention` flag is the
gate; check the NDJSON if a row you expected is missing from the CSV.

**Territorial listings vary in freshness.** Every provincial command and
regional direction has its own filtered view
(`/comando-vvf-<city>/notizie-dal-territorio`), but some post daily and others
go quiet for months. For coverage walk the national log; use `--listing` when
you want one territory.

## Usage

```bash
pip install -r requirements.txt
export FIRECRAWL_API_KEY=fc-...

python italy_vvf_interventions.py --pages 2 --limit 20
python italy_vvf_interventions.py --listing /comando-vvf-udine/notizie-dal-territorio --pages 3
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--listing` | `/media/notizie` | Listing path to walk (national log or a command's `notizie-dal-territorio`) |
| `--pages` | 1 | Listing pages to walk (about 10 articles per page) |
| `--limit` | 10 | Maximum articles to extract |
| `--out` | `output` | Output directory |

Set `FIRECRAWL_API_URL` to run against a self-hosted instance.
