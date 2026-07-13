"""Scrape the play-by-play event feed for a single match from sapi.afl.com.au.

Fixes vs. the original notebook cell:
- the WMCTok token is refreshed automatically on a 401 instead of just raising and
  telling you to "re-run this cell"; that's not workable inside a season-long loop.
- network calls go through common.retry with backoff instead of failing the whole
  season scrape on one transient error.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .common import (
    DEFAULT_USER_AGENT,
    fingerprint_event,
    flatten_dict,
    parse_mmss,
    retry,
    write_csv,
    write_ndjson,
)

TOKEN_URL = "https://api.afl.com.au/cfs/afl/WMCTok"
MATCH_PLAYS_URL = "https://sapi.afl.com.au/afl/matchPlays/{code}"


class TokenExpiredError(RuntimeError):
    pass


def get_afl_token(timeout: float = 15.0) -> str:
    def _fetch() -> str:
        r = requests.post(TOKEN_URL, timeout=timeout)
        r.raise_for_status()
        tok = r.json().get("token")
        if not tok:
            raise RuntimeError("No 'token' in WMCTok response")
        return tok

    return retry(_fetch, attempts=3, delay=2.0)


def fetch_json_with_token(url: str, token: str, timeout: float = 30.0) -> Any:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": DEFAULT_USER_AGENT,
        "x-media-mis-token": token,
        "Origin": "https://www.afl.com.au",
        "Referer": "https://www.afl.com.au/",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code == 401:
        raise TokenExpiredError(f"401 Unauthorized fetching {url}")
    r.raise_for_status()
    return r.json()


def fetch_match_plays_json(code: str, token: Optional[str] = None) -> Tuple[Any, str]:
    """Fetch the raw matchPlays JSON, refreshing the token once if it has expired."""
    token = token or get_afl_token()
    url = MATCH_PLAYS_URL.format(code=code)
    try:
        data = retry(lambda: fetch_json_with_token(url, token), attempts=2, delay=2.0)
    except TokenExpiredError:
        token = get_afl_token()
        data = retry(lambda: fetch_json_with_token(url, token), attempts=2, delay=2.0)
    return data, token


def _iter_nodes_with_path(root: Any, path: str = "$") -> Iterable[Tuple[str, Any]]:
    yield path, root
    if isinstance(root, dict):
        for k, v in root.items():
            if isinstance(v, (dict, list)):
                yield from _iter_nodes_with_path(v, f"{path}.{k}")
    elif isinstance(root, list):
        for i, v in enumerate(root):
            if isinstance(v, (dict, list)):
                yield from _iter_nodes_with_path(v, f"{path}[{i}]")


def _eventish_score(d: Dict[str, Any]) -> int:
    keys = " ".join(map(str, d.keys())).lower()
    score = 0
    for hint in ("period", "quarter", "qtr", "time", "display", "type", "event", "team", "player", "x", "y", "score"):
        if hint in keys:
            score += 1
    return score


def collect_event_lists(root: Any) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Return [(jsonpath, list_of_event_dicts)] for every list that looks like play events."""
    found: List[Tuple[str, List[Dict[str, Any]]]] = []
    for p, node in _iter_nodes_with_path(root):
        if isinstance(node, list) and node and isinstance(node[0], dict):
            sample = node[:5]
            avg = sum(_eventish_score(x) for x in sample) / len(sample)
            if avg >= 2:
                found.append((p, node))
    priority = (".$.plays", ".$.matchPlays", ".$.events", ".$.data")
    def rank(item):
        p, _ = item
        return 0 if any(p.endswith(k[1:]) for k in priority) else 1
    found.sort(key=rank)
    return found


def row_period(flat: Dict[str, Any]) -> int:
    for k in ("period.number", "quarter", "qtr", "period"):
        v = flat.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return 0


def row_seconds(flat: Dict[str, Any]) -> float:
    for k in ("period.secondsRemaining", "secondsRemaining", "seconds", "timeSeconds"):
        v = flat.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.isdigit():
            return float(v)
    for k in ("displayTime", "period.displayTime", "time", "clock"):
        v = flat.get(k)
        if v is not None:
            return parse_mmss(str(v))
    return float("inf")


def choose_columns(flat_rows: List[Dict[str, Any]]) -> List[str]:
    preferred = [
        "id",
        "period", "period.number", "quarter", "qtr",
        "displayTime", "period.displayTime", "time", "clock", "period.secondsRemaining",
        "type", "eventType", "playType", "subType", "result", "outcome",
        "x", "y", "position.x", "position.y",
        "teamId", "team.id", "team.abbrev", "team.name",
        "playerId", "player.id", "player.name", "player.playerName.givenName", "player.playerName.surname",
        "homeScore", "awayScore", "score", "scoreValue",
        "_sourcePath",
    ]
    all_keys: set = set()
    for r in flat_rows:
        all_keys.update(r.keys())
    ordered = [k for k in preferred if k in all_keys]
    remaining = sorted(all_keys - set(ordered))
    return ordered + remaining


@dataclass
class MatchPlaysResult:
    cd_match_code: str
    n_events: int
    csv_paths: Dict[str, str]
    source: str = "api"  # "api" (fetched via token) or "page" (captured from the match-centre page itself)
    suspect_empty: bool = False  # 0 events from a fresh API fetch -- may be a wrong/mismatched code


def scrape_match_plays(
    code: Optional[str] = None,
    out_dir: str = ".",
    token: Optional[str] = None,
    data: Optional[Any] = None,
) -> MatchPlaysResult:
    """Fetch (or accept already-fetched) play-by-play data for one match and flatten it to CSV.

    Pass `data` when you've already captured the matchPlays JSON some other way (e.g.
    stats.scrape_match_stats picked it up passively from the match-centre page's own
    network traffic) -- that's the literal JSON a real browser session received, so
    it's more trustworthy than re-deriving `code` and re-fetching it ourselves here.
    Otherwise `code` (the CD_M... match code) is required and this fetches it directly.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    source = "page"
    if data is None:
        if not code:
            raise ValueError("scrape_match_plays needs either `code` or pre-fetched `data`")
        data, _token = fetch_match_plays_json(code, token=token)
        source = "api"
    code = code or (data.get("matchId") if isinstance(data, dict) else None) or "match"

    raw_path = str(out_path / f"{code}_raw.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    lists = collect_event_lists(data)

    primary_csv_path = None
    if lists:
        primary_path, primary_list = lists[0]
        primary_flat = []
        for ev in primary_list:
            flat = flatten_dict(ev)
            flat["_sourcePath"] = primary_path
            primary_flat.append(flat)
        primary_csv_path = str(out_path / f"{code}_plays.csv")
        write_csv(primary_flat, primary_csv_path)

    seen: set = set()
    all_flat: List[Dict[str, Any]] = []
    for p, arr in lists:
        for ev in arr:
            fp = fingerprint_event(ev)
            if fp in seen:
                continue
            seen.add(fp)
            flat = flatten_dict(ev)
            flat["_sourcePath"] = p
            all_flat.append(flat)

    all_flat.sort(key=lambda r: (row_period(r), row_seconds(r)))

    all_csv = str(out_path / f"{code}_plays_all.csv")
    all_json = str(out_path / f"{code}_plays_all.ndjson")
    write_csv(all_flat, all_csv)
    write_ndjson([e for _, arr in lists for e in arr], all_json)

    csv_paths = {"raw": raw_path, "all": all_csv, "ndjson": all_json}
    if primary_csv_path:
        csv_paths["primary"] = primary_csv_path

    # A fresh API fetch (source == "api") that comes back with 0 events is ambiguous: it
    # could mean the match hasn't been played yet, or that `code` doesn't actually match
    # this match. If it came straight off the match-centre page instead, 0 events just
    # means the page hasn't loaded any chains -- same ambiguity, caller decides what to do.
    suspect_empty = len(all_flat) == 0

    return MatchPlaysResult(
        cd_match_code=code, n_events=len(all_flat), csv_paths=csv_paths,
        source=source, suspect_empty=suspect_empty,
    )
