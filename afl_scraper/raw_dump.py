"""Scrape *only* the raw play-by-play JSON (matchChains) for one match or a whole
season, with every file landing flat in a single output directory -- no player-stats
CSVs, no flattened plays CSVs/NDJSON, just one `<CD_M...>_raw.json` per match.

A match page still has to be loaded once per match to discover its CD_M... code
(there's no season-wide list of those), but nothing derived from the player-stats
side of the page is written to disk in this mode.
"""
from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .fixture import MatchRef, discover_season_matches, load_match_ids_file
from .plays import fetch_match_plays_json
from .stats import _capture_match_json, find_cd_match_code, find_live_matchplays_blob, match_url

logger = logging.getLogger("afl_scraper.raw_dump")


@dataclass
class RawDumpOutcome:
    match_id: str
    round: Optional[str] = None
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    cd_match_code: Optional[str] = None
    n_events: int = 0
    source: str = ""  # "page" (captured live) or "api" (separate token fetch)
    ok: bool = False
    error: str = ""


async def scrape_match_raw(
    match_id: str,
    out_dir: str = ".",
    headless: bool = True,
    token: Optional[str] = None,
) -> RawDumpOutcome:
    """Load one match page just long enough to find its code + raw event JSON, and
    write `<code>_raw.json` straight into `out_dir` (no subfolder, no other files)."""
    match_id = str(match_id)
    outcome = RawDumpOutcome(match_id=match_id)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    try:
        captured = await _capture_match_json(match_url(match_id), headless=headless)
    except Exception as e:  # noqa: BLE001
        outcome.error = f"page load: {e}"
        return outcome

    live_blob = find_live_matchplays_blob(captured)
    cd_code = find_cd_match_code(captured) or (live_blob.get("matchId") if live_blob else None)
    outcome.cd_match_code = cd_code

    if not cd_code:
        outcome.error = "could not find a CD_M... match code on the match page"
        return outcome

    if live_blob and live_blob.get("matchChains"):
        data = live_blob
        outcome.source = "page"
    else:
        try:
            data, _token = fetch_match_plays_json(cd_code, token=token)
            outcome.source = "api"
        except Exception as e:  # noqa: BLE001
            outcome.error = f"matchPlays fetch: {e}"
            return outcome

    raw_path = out_path / f"{cd_code}_raw.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    outcome.n_events = len(data.get("matchChains") or []) if isinstance(data, dict) else 0
    outcome.ok = outcome.n_events > 0
    if not outcome.ok:
        outcome.error = "0 matchChains events (match may not have been played yet, or the code may be wrong)"
    return outcome


def _load_previous_outcomes(out_dir: Path) -> Dict[str, RawDumpOutcome]:
    path = out_dir / "raw_index.csv"
    if not path.exists():
        return {}
    prev: Dict[str, RawDumpOutcome] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            prev[row["match_id"]] = RawDumpOutcome(
                match_id=row["match_id"],
                round=row.get("round") or None,
                home_team=row.get("home_team") or None,
                away_team=row.get("away_team") or None,
                cd_match_code=row.get("cd_match_code") or None,
                n_events=int(row.get("n_events") or 0),
                source=row.get("source", ""),
                ok=row.get("ok") == "True",
                error=row.get("error", ""),
            )
    return prev


def _write_index(out_dir: Path, outcomes: List[RawDumpOutcome]) -> None:
    path = out_dir / "raw_index.csv"
    cols = ["match_id", "round", "home_team", "away_team", "cd_match_code", "n_events", "source", "ok", "error"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for o in outcomes:
            w.writerow({
                "match_id": o.match_id, "round": o.round or "",
                "home_team": o.home_team or "", "away_team": o.away_team or "",
                "cd_match_code": o.cd_match_code or "", "n_events": o.n_events,
                "source": o.source, "ok": o.ok, "error": o.error,
            })


async def scrape_season_raw(
    season: int,
    out_dir: str,
    headless: bool = True,
    overwrite: bool = False,
    delay_seconds: float = 2.0,
    match_ids_file: Optional[str] = None,
    matches: Optional[List[MatchRef]] = None,
) -> List[RawDumpOutcome]:
    """Raw-only, flat-folder version of season.scrape_season: every `<code>_raw.json`
    lands directly in `out_dir`, with no per-match subfolders and no CSVs."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if matches is not None:
        refs = matches
    elif match_ids_file:
        refs = load_match_ids_file(match_ids_file)
    else:
        refs = await discover_season_matches(season, headless=headless)

    if not refs:
        raise RuntimeError(
            f"No matches discovered for season {season}. The fixture-page heuristics in "
            "afl_scraper/fixture.py may need adjusting for the current site, or pass "
            "match_ids_file / matches explicitly. See README.md."
        )

    logger.info("season %s (raw only): %d matches -> %s", season, len(refs), out_path)

    previous = _load_previous_outcomes(out_path)

    outcomes: List[RawDumpOutcome] = []
    for i, ref in enumerate(refs):
        prev = previous.get(ref.match_id)
        if not overwrite and prev is not None and prev.ok:
            logger.info("match %s already scraped, skipping", ref.match_id)
            outcomes.append(prev)
        else:
            outcome = await scrape_match_raw(ref.match_id, out_dir=str(out_path), headless=headless)
            outcome.round, outcome.home_team, outcome.away_team = ref.round, ref.home_team, ref.away_team
            outcomes.append(outcome)
            if not outcome.ok:
                logger.warning("match %s: %s", ref.match_id, outcome.error)

        _write_index(out_path, outcomes)
        if i < len(refs) - 1:
            time.sleep(delay_seconds)

    n_ok = sum(o.ok for o in outcomes)
    logger.info("season %s (raw only) done: %d/%d matches with events", season, n_ok, len(outcomes))

    return outcomes
