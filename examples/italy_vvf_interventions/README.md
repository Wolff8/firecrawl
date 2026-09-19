# Italian firefighter interventions

Two scripts, two kinds of source:

| Script | Source | What it is |
| --- | --- | --- |
| `italy_vvf_interventions.py` | [vigilfuoco.it](https://www.vigilfuoco.it/media/notizie) news log | Narrated reports from the Corpo Nazionale dei Vigili del Fuoco, structured with JSON extraction |
| `suedtirol_live_dispatch.py` | [lfvbz.it](https://www.lfvbz.it/it/corpi-in-intervento.html) dispatch list | South Tyrol's live 72-hour call-out table, parsed as-is |

## Why scraping

Italy publishes no per-incident open data for fire-service call-outs. The
Corpo's own [opendata.vigilfuoco.it](https://opendata.vigilfuoco.it) holds
aggregate statistics only, and the national CKAN portal
([dati.gov.it](https://dati.gov.it)) returns nothing beyond station locations
and one municipal summary from 2009. The mountain rescue service (CNSAS) sits
behind a bot challenge and is deliberately not scraped.

What *is* published is the Corpo's news log — one narrated article per notable
intervention, hundreds of pages deep, with the responding command, the time,
the municipality, the vehicles and the outcome as Italian prose — and, for the
autonomous province of Bolzano, a real dispatch record from the volunteer
brigades' federation.

## Vigili del Fuoco news log

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

### Where the articles come from

| Mode | Coverage |
| --- | --- |
| default | The national log `/media/notizie`, newest first |
| `--listing PATH` (repeatable) | Any listing, e.g. `/comando-vvf-udine/notizie-dal-territorio` |
| `--region fvg` / `--region veneto` | Every provincial command of the region plus its regional direction |
| `--all` | Reads the site index to discover all ~120 command and direction sites, then walks each one |
| `--search QUERY` | Firecrawl news search over the last week, so local-press reports go through the same extraction |

### How it uses Firecrawl

Listing pages are scraped for `links` in a single **`batch_scrape`** — with
`--all` that is a hundred-odd listings times `--pages`, fetched together rather
than one after another, with `max_age=0` because listings change daily. The
articles are then pulled in a second `batch_scrape` that runs the same JSON
extraction on each page server-side, with **`max_age`** set to seven days so
articles seen on an earlier run come from cache. `--search` uses
**`search(sources=["news"], tbs="qdr:w")`** to find reports the Corpo has not
written up itself. Each job reports how many URLs completed and the credits it
used.

### Two caveats

**The log is not only interventions.** Ceremonies, exercises, awards and
recruitment notices sit in the same feed. The `is_intervention` flag is the
gate; check the NDJSON if a row you expected is missing from the CSV.

**Territorial listings vary in freshness.** Some commands post daily, others go
quiet for months. For coverage walk the national log or use `--all`; use
`--region` or `--listing` when you want one territory.

## South Tyrol live dispatch list

The Landesverband der Freiwilligen Feuerwehren Südtirols lists every call-out
of the last 72 hours, updated to the minute. It is a table, not prose, so this
script parses it directly — no model involved:

| Field | Example |
| --- | --- |
| `number` | F260900684 |
| `brigade` / `district` | Wiesen / Wipptal |
| `operation_type` / `alarm_level` | Technischer Einsatz / 5 |
| `keyword` / `sub_keyword` | TECHNISCH MITTEL / PERSON EINGEKLEMMT |
| `reported_at` | 19.09.2026 - 22:11 |

The same incident number appears once per brigade that responded. Pagination
links carry a TYPO3 `cHash`, so the script follows the links the first page
offers instead of building URLs. `--lang it` (default) or `--lang de` picks
the column-header language; the keywords themselves are always German.

Common keywords: `BRAND` fire, `TECHNISCH` technical (non-fire),
`VU MIT VERLETZTEN PERSONEN` road accident with injured, `PERSON EINGEKLEMMT`
person trapped, `MELDERALARM` automatic alarm, `TUEROEFFNUNG` door opening,
`STRASSENREINIGUNG` road clearing, `SUCHAKTION` search. `KLEIN` / `MITTEL` /
`GROSS` are the scale.

Output is `suedtirol_dispatch.ndjson` and `suedtirol_dispatch.csv`.

## Usage

```bash
pip install -r requirements.txt
export FIRECRAWL_API_KEY=fc-...

python italy_vvf_interventions.py --pages 3 --limit 30
python italy_vvf_interventions.py --region fvg --pages 2 --limit 50
python italy_vvf_interventions.py --all --limit 200
python italy_vvf_interventions.py --search "vigili del fuoco incendio" --limit 20

python suedtirol_live_dispatch.py
```

| Flag | Script | Default | Meaning |
| --- | --- | --- | --- |
| `--listing` / `--region` / `--all` / `--search` | vvf | national log | Where to get articles (mutually exclusive) |
| `--pages` | vvf | 1 | Listing pages to walk per listing (about 10 articles per page) |
| `--limit` | vvf | 10 | Maximum articles to extract |
| `--lang` | suedtirol | `it` | Source page language |
| `--out` | both | `output` | Output directory |

Set `FIRECRAWL_API_URL` to run against a self-hosted instance.
