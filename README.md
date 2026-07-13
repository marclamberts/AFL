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
python scrape_season.py match 7150 --out-dir data/match7150
```

Whole season (auto-discovers every match, writes one subfolder per match plus
season-level `season_<year>_player_stats.csv` and `season_<year>_plays.csv`):

```bash
python scrape_season.py season 2026 --out-dir data/2026
```

Re-running the same command resumes: matches that already have output on disk
are skipped unless you pass `--overwrite`. Progress is written to
`matches_index.csv` in the output directory after every match, so you can
check status (and per-match errors) while a long run is still going.

If a match's Champion Data code can't be auto-detected, its play-by-play
scrape is skipped (stats are still saved) and the reason is recorded in
`matches_index.csv`.

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
- `afl_scraper/pitch.py` - AFL oval pitch plotting + scatter overlay for x/y event data.
- `scrape_season.py` - CLI.
- `AFL Scraper.ipynb` - interactive notebook version of the same workflow.
