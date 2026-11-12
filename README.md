# Ohio Lead Stacker

Finds properties that show up on **two or more** of your lead lists (foreclosure,
probate, tax delinquent, code violation, eviction, divorce) and ranks them.

Franklin records without a parcel ID get one from the Auditor's monthly parcel file (matched by
address, ZIP used to break ties; ambiguous addresses are left alone). See `stacker/franklin_parcels.py`.

Health check: `health.py` (workflow "Scraper health check", daily ~10:47 AM ET) checks every scraper repo's
scheduled runs, writes docs/health.html, and opens a GitHub issue (GitHub emails you) when something fails
or is overdue. Repos are listed in `health.json`.

Owner tenure: each stacked property's last sale date comes from the county parcel data (Franklin Auditor,
Hamilton CAGIS, Cuyahoga Fiscal Officer). Owned 20+ years +8, 10-19 years +4. See `stacker/tenure.py`.

## How the data stays separated

```
scraper repos (each writes only its own data)
   │   publish JSON on GitHub
   ▼
ohio-lead-stacker (read-only: never writes to a scraper repo)
   │   adapters → normalize → match → score
   ▼
docs/stacked.json + docs/stacked_ghl.csv → Stacked Leads dashboard
```

* Each scraper repo keeps working exactly as it does now. Nothing in them changes.
* `sources.json` lists every feed the stacker reads. To add a new scraper, add one
  entry there (and an adapter in `stacker/adapters.py` if its format is new).
* If a feed can't be downloaded, the last good copy in `data/cache/` is used, so
  one broken scraper never empties the stacked list.

## How properties are matched

Within the same county, two records are the same property when they share:

1. a **parcel ID** (reduced to digits; Franklin's `-00` ending is dropped), or
2. a **street address** after normalizing (`1487 Elaine Road Apt D` = `1487 ELAINE RD`,
   `E Fifth Ave` = `E 5TH AVE`, units dropped so units match their building) **and** the
   ZIPs agree. Different ZIPs only merge when the owner names agree (ZIP typos).

Only properties on 2+ *different* lead types are kept.

## Score (0–100)

| | |
|---|---|
| Foreclosure / Probate | +30 each |
| Tax delinquent | +25 |
| Code violation | +20 |
| Eviction | +15 |
| Extra records of the same type | +3 each, max +10 |
| Activity in last 30 / 90 days | +10 / +5 |
| Absentee owner | +5 |
| Owner names differ across lists | −15 (check before calling — may have sold) |

3+ lead types = **Hot**, 2 = **Warm**.

## Run it

```powershell
python run_stacker.py                 # reads the live feeds from GitHub
python -m pytest -q tests             # tests
```

GitHub Actions runs it every morning at ~9:13 AM Eastern, after the scrapers.
Turn on GitHub Pages for this repo (Settings → Pages → Deploy from branch →
`main` / `/docs`) to get the dashboard.
