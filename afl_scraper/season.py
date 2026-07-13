"""Drive a full-season scrape: discover every match, then scrape stats + plays for each.

Designed to be safe to re-run: matches already scraped (their CSVs already on disk)
are skipped unless overwrite=True, so a season run that dies partway through (rate
limiting, a bad match page, a killed process) can just be re-invoked.
"""
from __future__ import annotations

import asyncio
import csv
import glob
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .fixture import MatchRef, discover_season_matches, load_match_ids_file
from .plays import scrape_match_plays
from .stats import scrape_match_stats

logger = logging.getLogger("afl_scraper.season")


@dataclass
class MatchOutcome:
    match_id: str
    round: Optional[str] = None
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    stats_ok: bool = False
    plays_ok: bool = False
    cd_match_code: Optional[str] = None
    error: str = ""


def _match_dir(out_dir: Path, match_id: str) -> Path:
    return out_dir / str(match_id)


def _already_scraped(match_dir: Path, match_id: str) -> bool:
    stats_done = (match_dir / f"match{match_id}_all_player_stats.csv").exists()
    plays_done = any(match_dir.glob("CD_M*_plays_all.csv"))
    return stats_done and plays_done


async def scrape_one_match(
    ref: MatchRef,
    out_dir: Path,
    headless: bool = True,
    overwrite: bool = False,
    token: Optional[str] = None,
) -> MatchOutcome:
    outcome = MatchOutcome(match_id=ref.match_id, round=ref.round, home_team=ref.home_team, away_team=ref.away_team)
    match_dir = _match_dir(out_dir, ref.match_id)

    if not overwrite and _already_scraped(match_dir, ref.match_id):
        outcome.stats_ok = True
        outcome.plays_ok = True
        logger.info("match %s already scraped, skipping", ref.match_id)
        return outcome

    try:
        stats_result = await scrape_match_stats(ref.match_id, out_dir=str(match_dir), headless=headless)
        outcome.stats_ok = True
        outcome.cd_match_code = stats_result.cd_match_code
    except Exception as e:  # noqa: BLE001
        outcome.error = f"stats: {e}"
        logger.warning("match %s stats scrape failed: %s", ref.match_id, e)
        return outcome

    if not outcome.cd_match_code:
        outcome.error = "could not find CD_M... match code on the stats page; skipped plays scrape"
        logger.warning("match %s: %s", ref.match_id, outcome.error)
        return outcome

    try:
        plays_result = await asyncio.to_thread(scrape_match_plays, outcome.cd_match_code, str(match_dir), token)
        outcome.plays_ok = True
        _ = plays_result
    except Exception as e:  # noqa: BLE001
        outcome.error = f"plays: {e}"
        logger.warning("match %s plays scrape failed: %s", ref.match_id, e)

    return outcome


def _write_matches_index(out_dir: Path, outcomes: List[MatchOutcome]) -> None:
    path = out_dir / "matches_index.csv"
    cols = ["match_id", "round", "home_team", "away_team", "stats_ok", "plays_ok", "cd_match_code", "error"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for o in outcomes:
            w.writerow({
                "match_id": o.match_id, "round": o.round or "",
                "home_team": o.home_team or "", "away_team": o.away_team or "",
                "stats_ok": o.stats_ok, "plays_ok": o.plays_ok,
                "cd_match_code": o.cd_match_code or "", "error": o.error,
            })


def _concat_csvs(pattern: str, out_path: Path) -> int:
    """Concatenate every CSV matching `pattern` (glob) into one file, header taken from the first."""
    paths = sorted(glob.glob(pattern))
    n_rows = 0
    header: Optional[List[str]] = None
    with open(out_path, "w", newline="", encoding="utf-8") as out_f:
        writer = None
        for p in paths:
            if not Path(p).stat().st_size:
                continue
            with open(p, newline="", encoding="utf-8") as in_f:
                reader = csv.reader(in_f)
                rows = list(reader)
            if not rows:
                continue
            if header is None:
                header = rows[0]
                writer = csv.writer(out_f)
                writer.writerow(header)
            for row in rows[1:]:
                writer.writerow(row)
                n_rows += 1
    return n_rows


async def scrape_season(
    season: int,
    out_dir: str,
    headless: bool = True,
    overwrite: bool = False,
    delay_seconds: float = 2.0,
    match_ids_file: Optional[str] = None,
    matches: Optional[List[MatchRef]] = None,
) -> List[MatchOutcome]:
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

    logger.info("season %s: %d matches to scrape -> %s", season, len(refs), out_path)

    outcomes: List[MatchOutcome] = []
    token: Optional[str] = None
    for i, ref in enumerate(refs):
        outcome = await scrape_one_match(ref, out_path, headless=headless, overwrite=overwrite, token=token)
        outcomes.append(outcome)
        _write_matches_index(out_path, outcomes)  # keep the index fresh so progress is visible mid-run
        if i < len(refs) - 1:
            time.sleep(delay_seconds)

    n_stats_ok = sum(o.stats_ok for o in outcomes)
    n_plays_ok = sum(o.plays_ok for o in outcomes)
    logger.info("season %s done: %d/%d stats ok, %d/%d plays ok", season, n_stats_ok, len(outcomes), n_plays_ok, len(outcomes))

    n_players = _concat_csvs(str(out_path / "*" / "match*_all_player_stats.csv"), out_path / f"season_{season}_player_stats.csv")
    n_plays = _concat_csvs(str(out_path / "*" / "CD_M*_plays_all.csv"), out_path / f"season_{season}_plays.csv")
    logger.info("season %s: wrote %d player-stat rows and %d play rows to season-level CSVs", season, n_players, n_plays)

    return outcomes
