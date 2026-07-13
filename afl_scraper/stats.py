"""Scrape player box-score stats for a single AFL Match Centre page.

Replaces the two near-identical "scrape_match_to_csv" copies that used to live in
notebook cells 0 and 3 (the second one also hardcoded a /Users/user/ output path,
which meant it only ever worked on the original author's laptop).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import DEFAULT_USER_AGENT, deep_find_keys, find_cd_match_code, get_in, write_csv

PLAYER_STATS_BASE_COLS = [
    "name", "number", "position", "playerId", "teamId", "teamSide",
    "goals", "behinds", "kicks", "handballs", "disposals", "marks", "hitouts", "tackles",
    "centreClearances", "stoppageClearances", "totalClearances",
    "inside50s", "rebound50s",
    "freesFor", "freesAgainst",
    "contestedPossessions", "uncontestedPossessions",
    "marksInside50", "contestedMarks", "onePercenters", "bounces", "intercepts", "turnovers",
    "scoreInvolvements", "goalAssists", "shotsAtGoal",
    "disposalEfficiency", "metresGained", "dreamTeamPoints", "ratingPoints",
    "timeOnGroundPercentage", "rowLastUpdated",
    "effectiveKicks", "kickEfficiency", "kickToHandballRatio", "effectiveDisposals",
]


def match_url(match_id: Any, base_url: str = "https://www.afl.com.au/afl/matches") -> str:
    return f"{base_url.rstrip('/')}/{match_id}"


def full_name(entry: dict) -> Optional[str]:
    given = get_in(entry, ["playerStats", "player", "playerName", "givenName"])
    sur = get_in(entry, ["playerStats", "player", "playerName", "surname"])
    if given or sur:
        return " ".join([x for x in [given, sur] if x]).strip()
    given = get_in(entry, ["player", "player", "playerName", "givenName"])
    sur = get_in(entry, ["player", "player", "playerName", "surname"])
    if given or sur:
        return " ".join([x for x in [given, sur] if x]).strip()
    nm = get_in(entry, ["playerStats", "player", "playerName"]) or get_in(entry, ["player", "player", "playerName"])
    return str(nm).strip() if nm else None


def flatten_row(entry: dict, side_label: str) -> dict:
    stats = get_in(entry, ["playerStats", "stats"], {}) or {}
    ext = stats.get("extendedStats") or {}
    cl = stats.get("clearances") or {}

    row = {
        "name": full_name(entry),
        "number": get_in(entry, ["player", "jumperNumber"]),
        "position": get_in(entry, ["player", "player", "position"]),
        "playerId": get_in(entry, ["playerStats", "player", "playerId"])
        or get_in(entry, ["player", "player", "playerId"]),
        "teamId": entry.get("teamId") or get_in(entry, ["playerStats", "teamId"]),
        "teamSide": side_label,
        "goals": stats.get("goals"),
        "behinds": stats.get("behinds"),
        "kicks": stats.get("kicks"),
        "handballs": stats.get("handballs"),
        "disposals": stats.get("disposals"),
        "marks": stats.get("marks"),
        "hitouts": stats.get("hitouts"),
        "tackles": stats.get("tackles"),
        "centreClearances": cl.get("centreClearances"),
        "stoppageClearances": cl.get("stoppageClearances"),
        "totalClearances": cl.get("totalClearances"),
        "inside50s": stats.get("inside50s"),
        "rebound50s": stats.get("rebound50s"),
        "freesFor": stats.get("freesFor"),
        "freesAgainst": stats.get("freesAgainst"),
        "contestedPossessions": stats.get("contestedPossessions"),
        "uncontestedPossessions": stats.get("uncontestedPossessions"),
        "metresGained": stats.get("metresGained") if "metresGained" in stats else stats.get("metersGained"),
        "marksInside50": stats.get("marksInside50"),
        "contestedMarks": stats.get("contestedMarks"),
        "onePercenters": stats.get("onePercenters"),
        "bounces": stats.get("bounces"),
        "intercepts": stats.get("intercepts"),
        "turnovers": stats.get("turnovers"),
        "scoreInvolvements": stats.get("scoreInvolvements"),
        "goalAssists": stats.get("goalAssists"),
        "shotsAtGoal": stats.get("shotsAtGoal"),
        "disposalEfficiency": stats.get("disposalEfficiency"),
        "dreamTeamPoints": stats.get("dreamTeamPoints"),
        "ratingPoints": stats.get("ratingPoints"),
        "timeOnGroundPercentage": get_in(entry, ["playerStats", "timeOnGroundPercentage"]),
        "rowLastUpdated": get_in(entry, ["playerStats", "lastUpdated"]),
    }
    for k in ("effectiveKicks", "kickEfficiency", "kickToHandballRatio", "effectiveDisposals"):
        if k in ext:
            row[k] = ext[k]
    return row


@dataclass
class MatchStatsResult:
    match_id: str
    home_rows: List[Dict[str, Any]]
    away_rows: List[Dict[str, Any]]
    cd_match_code: Optional[str]
    csv_paths: Dict[str, str]
    # Populated if the match-centre page itself fetched the play-by-play feed while
    # loading (e.g. for a timeline widget) -- this is literally the JSON the browser
    # session received, so it's a more trustworthy source than re-deriving the match
    # code and re-fetching matchPlays ourselves via a separate token (see plays.py).
    matchplays_blob: Optional[Dict[str, Any]] = None


async def _capture_match_json(url: str, headless: bool = True, settle_seconds: float = 5.0) -> List[Any]:
    from playwright.async_api import async_playwright  # imported lazily so this module is importable without it

    captured: List[Any] = []
    pending: List[asyncio.Task] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=DEFAULT_USER_AGENT, locale="en-AU")
        page = await context.new_page()

        async def grab(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "")
                rtype = resp.request.resource_type
                if ("application/json" in ct.lower()) or (rtype in ("xhr", "fetch")):
                    txt = await resp.text()
                    if txt:
                        try:
                            captured.append(json.loads(txt))
                        except Exception:
                            pass
            except Exception:
                pass

        def on_response(resp):
            pending.append(asyncio.create_task(grab(resp)))

        page.on("response", on_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        await asyncio.sleep(settle_seconds)
        # Make sure every in-flight response handler has actually finished writing
        # into `captured` before we close the browser out from under it.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        await browser.close()

    return captured


async def scrape_match_stats(match_id: Any, out_dir: str = ".", headless: bool = True) -> MatchStatsResult:
    """Scrape one AFL Match Centre page and write home/away/all player-stats CSVs."""
    match_id = str(match_id)
    url = match_url(match_id)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    captured = await _capture_match_json(url, headless=headless)

    targets = {"homeTeamPlayerStats", "awayTeamPlayerStats"}
    found: Dict[str, Any] = {}
    for blob in captured:
        deep_find_keys(blob, targets, found)
        if targets.issubset(found.keys()):
            break

    home = found.get("homeTeamPlayerStats") or []
    away = found.get("awayTeamPlayerStats") or []

    home_rows = [flatten_row(x, "home") for x in home]
    away_rows = [flatten_row(x, "away") for x in away]
    all_rows = home_rows + away_rows

    home_csv = str(out_path / f"match{match_id}_home_player_stats.csv")
    away_csv = str(out_path / f"match{match_id}_away_player_stats.csv")
    all_csv = str(out_path / f"match{match_id}_all_player_stats.csv")

    write_csv(home_rows, home_csv, PLAYER_STATS_BASE_COLS)
    write_csv(away_rows, away_csv, PLAYER_STATS_BASE_COLS)
    write_csv(all_rows, all_csv, PLAYER_STATS_BASE_COLS)

    cd_code = find_cd_match_code(captured)
    matchplays_blob = find_live_matchplays_blob(captured)
    if matchplays_blob and not cd_code:
        cd_code = matchplays_blob.get("matchId")

    return MatchStatsResult(
        match_id=match_id,
        home_rows=home_rows,
        away_rows=away_rows,
        cd_match_code=cd_code,
        csv_paths={"home": home_csv, "away": away_csv, "all": all_csv},
        matchplays_blob=matchplays_blob,
    )


def find_live_matchplays_blob(captured: List[Any]) -> Optional[Dict[str, Any]]:
    """Look for a matchPlays-shaped response (top-level "matchChains" key) among what
    the match-centre page itself fetched. Prefer one with actual events in it; fall
    back to an empty one (still useful for diagnosing "wrong code" vs "no data yet")."""
    empty_fallback = None
    for blob in captured:
        if isinstance(blob, dict) and "matchChains" in blob:
            if blob.get("matchChains"):
                return blob
            empty_fallback = empty_fallback or blob
    return empty_fallback
