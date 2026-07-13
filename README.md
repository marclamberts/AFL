# AFL Scraper

Scrapes player box-score stats and play-by-play event data from the AFL Match
Centre (afl.com.au / sapi.afl.com.au), for a single match or a whole season.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

Single match:

```bash
python scrape_season.py match 8189 --out-dir data/match8189
```

Whole season (auto-discovers every match, writes one subfolder per match plus
season-level `season_<year>_player_stats.csv` and `season_<year>_plays.csv`):

```bash
python scrape_season.py season 2026 --out-dir data/2026
```

Raw play-by-play JSON only, nothing else, every `<CD_M...>_raw.json` flat in
one folder (no subfolders, no CSVs):

```bash
python scrape_season.py raw 2026 --season-id 85 --out-dir data/2026_raw
```

(or `--match-id 8189` on that same command for just one match). Progress and
resume for this mode are tracked in `raw_index.csv` in the output directory.

### `--season-id`: the fixture page's "Season" filter isn't the calendar year

AFL's site assigns its own internal id to each season for the fixture page's
"Season" query param, and that id has no relationship to the calendar year
(2026 turned out to be site id `85`, for example). `season` (2026) is still
used for output paths and labeling; pass the site's real id separately with
`--season-id` so fixture discovery hits the right URL:

```bash
python scrape_season.py season 2026 --season-id 85 --out-dir data/2026
python scrape_season.py raw 2026 --season-id 85 --out-dir data/2026_raw
```

Find the id yourself via devtools > Network on `https://www.afl.com.au/fixture`
and looking at what value the "Season" request parameter actually holds.
Without `--season-id`, discovery just tries the calendar year as-is (the
original, less reliable behavior).

Re-running the same command resumes: matches that *fully* succeeded last time
(per the previous run's `matches_index.csv`) are skipped unless you pass
`--overwrite`. Matches that only partially worked -- stats scraped but 0 play
events, or not played yet -- are retried automatically, since that's exactly
what a re-run should pick up. Progress is written to `matches_index.csv` in
the output directory after every match (including an `n_play_events` column),
so you can check status while a long run is still going.

### Raw event data (matchPlays) comes back empty

Player stats and play-by-play (`matchChains`) are two independent things to
scrape, and it's common for stats to work while plays don't. There are two
different reasons `matchChains: []` shows up, and they need different fixes:

1. **The match genuinely hasn't been played yet.** No player stats either --
   nothing to do but wait and retry later.
2. **The match code is wrong.** Player stats *do* exist (so the match has
   definitely been played), but the auto-detected `CD_M...` code the scraper
   is feeding into the play-by-play endpoint doesn't actually correspond to
   this match. `matches_index.csv` will show `stats_ok=True`, `plays_ok=False`,
   `n_play_events=0`, and an error containing "code is likely wrong" -- that's
   case 2, not case 1.

To make case 2 less likely in the first place, the scraper first checks
whether the match-centre page itself already fetched non-empty play-by-play
data while loading (captured the same passive way player stats are) and uses
that directly if present, instead of re-deriving the code and re-fetching via
a separate token+API call. If a match still shows up as "code is likely
wrong", pass the correct code manually:

```bash
python scrape_season.py match 8189 --cd-code CD_M20260141903 --out-dir data/match8189
```

(find the real code by opening the match-centre page in a browser, devtools
> Network, and looking for a request to `sapi.afl.com.au/afl/matchPlays/...`).

### If season discovery finds nothing

There is no documented "list every match in a season" endpoint, so
`afl_scraper/fixture.py` crawls the fixture page and heuristically picks out
match-shaped data (same approach the rest of this scraper uses for unknown
JSON shapes). It has **not** been exercised against the live site from this
repo's dev environment, which has no network access to afl.com.au. If it
comes back empty, or picks up the wrong season:

- adjust `candidate_fixture_urls()` / `_extract_match_ref()` in
  `afl_scraper/fixture.py` to match whatever the fixture page actually
  returns (open devtools > Network on `https://www.afl.com.au/fixture` and
  see what XHR responses look like), or
- bypass discovery entirely with a manual list:

  ```bash
  python scrape_season.py season 2026 --out-dir data/2026 --match-ids-file ids.txt
  ```

  where `ids.txt` has one match id per line (optionally
  `id,round,home,away`).

## Layout

- `afl_scraper/common.py` - shared JSON-tree helpers, CSV/NDJSON writers, retry helper.
- `afl_scraper/stats.py` - Playwright scrape of a match page's player stats.
- `afl_scraper/plays.py` - Champion Data play-by-play feed (token fetch + event flattening).
- `afl_scraper/fixture.py` - season match discovery.
- `afl_scraper/season.py` - orchestrates discovery + per-match scraping, resume, season CSVs.
- `afl_scraper/raw_dump.py` - raw-only variant: just `<code>_raw.json` per match, flat in one folder.
- `afl_scraper/pitch.py` - AFL oval pitch plotting + scatter overlay for x/y event data.
- `scrape_season.py` - CLI.
- `AFL Scraper.ipynb` - interactive notebook version of the same workflow.
