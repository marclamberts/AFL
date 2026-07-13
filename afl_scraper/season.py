"""Drive a full-season scrape: discover every match, then scrape stats + plays for each.

Designed to be safe to re-run: matches that fully succeeded last time (per the previous
run's matches_index.csv) are skipped unless overwrite=True, so a season run that dies
partway through (rate limiting, a bad match page, a killed process) can just be
re-invoked. Matches that only partially succeeded -- e.g. stats scraped but the
play-by-play came back empty/suspect, or the match hadn't been played yet -- are
retried automatically, since that's exactly the case a re-run is meant to pick up.
"""
from __future__ import annotations

import asyncio
import csv
import glob
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

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
    n_play_events: int = 0
    error: str = ""


def _match_dir(out_dir: Path, match_id: str) -> Path:
    return out_dir / str(match_id)


def _load_previous_outcomes(out_dir: Path) -> Dict[str, MatchOutcome]:
    path = out_dir / "matches_index.csv"
    if not path.exists():
        return {}
    prev: Dict[str, MatchOutcome] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            prev[row["match_id"]] = MatchOutcome(
                match_id=row["match_id"],
                round=row.get("round") or None,
                home_team=row.get("home_team") or None,
                away_team=row.get("away_team") or None,
                stats_ok=row.get("stats_ok") == "True",
                plays_ok=row.get("plays_ok") == "True",
                cd_match_code=row.get("cd_match_code") or None,
                n_play_events=int(row.get("n_play_events") or 0),
                error=row.get("error", ""),
            )
    return prev


async def scrape_one_match(
    ref: MatchRef,
    out_dir: Path,
    headless: bool = True,
    overwrite: bool = False,
    token: Optional[str] = None,
    previous: Optional[MatchOutcome] = None,
) -> MatchOutcome:
    outcome = MatchOutcome(match_id=ref.match_id, round=ref.round, home_team=ref.home_team, away_team=ref.away_team)
    match_dir = _match_dir(out_dir, ref.match_id)

    if not overwrite and previous is not None and previous.stats_ok and previous.plays_ok:
        logger.info("match %s already fully scraped, skipping", ref.match_id)
        return previous

    try:
        stats_result = await scrape_match_stats(ref.match_id, out_dir=str(match_dir), headless=headless)
        outcome.stats_ok = True
        outcome.cd_match_code = stats_result.cd_match_code
    except Exception as e:  # noqa: BLE001
        outcome.error = f"stats: {e}"
        logger.warning("match %s stats scrape failed: %s", ref.match_id, e)
        return outcome

    has_players = bool(stats_result.home_rows or stats_result.away_rows)
    live_blob = stats_result.matchplays_blob

    try:
        if live_blob and live_blob.get("matchChains"):
            # The match-centre page itself already fetched non-empty play-by-play data;
            # use that literal payload instead of re-deriving the code and re-fetching,
            # since it's guaranteed to be what a real browser session actually got.
            plays_result = scrape_match_plays(outcome.cd_match_code, str(match_dir), data=live_blob)
        elif outcome.cd_match_code:
            plays_result = await asyncio.to_thread(scrape_match_plays, outcome.cd_match_code, str(match_dir), token)
        else:
            outcome.error = "could not find a CD_M... match code or live matchChains data on the stats page; skipped plays scrape"
            logger.warning("match %s: %s", ref.match_id, outcome.error)
            return outcome
    except Exception as e:  # noqa: BLE001
        outcome.error = f"plays: {e}"
        logger.warning("match %s plays scrape failed: %s", ref.match_id, e)
        return outcome

    outcome.n_play_events = plays_result.n_events
    if plays_result.n_events > 0:
        outcome.plays_ok = True
    elif has_players:
        # Stats exist, so the match has definitely been played -- 0 play events despite
        # that is suspicious, most likely `cd_match_code` doesn't actually match this
        # match rather than "no data yet". Flag it instead of silently marking it done.
        outcome.plays_ok = False
        outcome.error = (
            f"plays: matchPlays returned 0 events for code {outcome.cd_match_code!r} even though "
            "player stats exist for this match (so it has been played) -- the auto-detected "
            "match code is likely wrong for this match; verify it manually"
        )
        logger.warning("match %s: %s", ref.match_id, outcome.error)
    else:
        # No player stats either -- most likely this match just hasn't been played yet.
        outcome.plays_ok = False
        outcome.error = "plays: 0 events and no player stats -- match probably hasn't been played yet"
        logger.info("match %s: %s", ref.match_id, outcome.error)

    return outcome


def _write_matches_index(out_dir: Path, outcomes: List[MatchOutcome]) -> None:
    path = out_dir / "matches_index.csv"
    cols = ["match_id", "round", "home_team", "away_team", "stats_ok", "plays_ok", "n_play_events", "cd_match_code", "error"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for o in outcomes:
            w.writerow({
                "match_id": o.match_id, "round": o.round or "",
                "home_team": o.home_team or "", "away_team": o.away_team or "",
                "stats_ok": o.stats_ok, "plays_ok": o.plays_ok, "n_play_events": o.n_play_events,
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

    previous_outcomes = _load_previous_outcomes(out_path)

    outcomes: List[MatchOutcome] = []
    token: Optional[str] = None
    for i, ref in enumerate(refs):
        outcome = await scrape_one_match(
            ref, out_path, headless=headless, overwrite=overwrite, token=token,
            previous=previous_outcomes.get(ref.match_id),
        )
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
