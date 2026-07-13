"""Estimate each player's "Points Added" from a folder of raw play-by-play
(matchChains) JSON files, using a field-position expected-value model in the
spirit of NFL's EPA / basketball's win-probability-added.

How it works
------------
1. Build a "zone value" table: bin the ground into a coarse x/y grid, and for every
   touch a possessing team made, record how many points *that team's chain*
   eventually produced (0 for a turnover/stoppage, 1 for a behind/rushed behind,
   6 for a goal). Average per zone -> "expected points from here".
2. Walk each chain's touches in order. Every transition from one zone to the next is
   credited to the player who made the earlier touch, as the change in expected value
   it produced. The final transition in a chain uses the *actual* result (0/1/6)
   instead of a zone estimate, since we know exactly what happened there.

This needs a reasonable sample to be trustworthy. With only a couple of matches the
zone table is noisy -- `build_zone_values` reports a sample count per zone so you can
judge that yourself (see the `--zones-out` CLI flag). Re-run as more matches get
scraped; accuracy only improves, no code changes needed.
"""
from __future__ import annotations

import csv
import glob
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Points awarded to the possessing team (chain["teamId"]) for each chain outcome.
# Everything else (turnover, ballUpCall, outOfBounds, endQuarter) is worth 0.
CHAIN_POINTS = {"goal": 6, "behind": 1, "rushed": 1}


@dataclass
class Touch:
    match_id: str
    chain_id: str
    order: int
    player_id: str
    x: float
    y: float


@dataclass
class Chain:
    match_id: str
    chain_id: str
    team_id: Optional[str]
    points: int
    touches: List[Touch]


def load_chains(raw_dir: str, pattern: str = "CD_M*_raw.json") -> List[Chain]:
    """Load every matchChain from every raw JSON file in `raw_dir` matching `pattern`.

    Only touches made *by the possessing team* (chain["teamId"]) are kept -- the
    trailing touch some chains carry from the opposing team (e.g. the spoil that
    causes a rushed behind, or the mark that causes a turnover) belongs conceptually
    to the next chain, not this one, and would otherwise contaminate the zone values
    for the wrong team's perspective.
    """
    chains: List[Chain] = []
    for path in sorted(glob.glob(str(Path(raw_dir) / pattern))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        match_id = data.get("matchId") or Path(path).stem
        for ci, chain in enumerate(data.get("matchChains") or []):
            team = chain.get("teamId")
            points = CHAIN_POINTS.get(chain.get("finalState"), 0)
            chain_id = f"{match_id}#{ci}"
            touches = []
            for ev in chain.get("stats") or []:
                if ev.get("teamId") != team or not ev.get("playerId"):
                    continue
                x, y = ev.get("x"), ev.get("y")
                if x is None or y is None:
                    continue
                touches.append(Touch(
                    match_id=match_id, chain_id=chain_id, order=ev.get("displayOrder", 0),
                    player_id=ev["playerId"], x=float(x), y=float(y),
                ))
            chains.append(Chain(match_id=match_id, chain_id=chain_id, team_id=team, points=points, touches=touches))
    return chains


def _make_zoner(chains: List[Chain], n_x: int, n_y: int):
    xs = [t.x for c in chains for t in c.touches]
    ys = [t.y for c in chains for t in c.touches]
    if not xs:
        raise ValueError("No touches found -- check raw_dir/pattern point at real <CD_M...>_raw.json files.")
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    def zone(x: float, y: float) -> Tuple[int, int]:
        xi = min(n_x - 1, max(0, int((x - x_min) / (x_max - x_min + 1e-9) * n_x)))
        yi = min(n_y - 1, max(0, int((y - y_min) / (y_max - y_min + 1e-9) * n_y)))
        return (xi, yi)

    return zone


def build_zone_values(
    chains: List[Chain], n_x: int = 8, n_y: int = 4
) -> Tuple[Dict[Tuple[int, int], float], Dict[Tuple[int, int], int], "callable"]:
    """Expected points per (x,y) zone, plus a sample-count table for judging reliability."""
    zone = _make_zoner(chains, n_x, n_y)
    sums: Dict[Tuple[int, int], float] = defaultdict(float)
    counts: Dict[Tuple[int, int], int] = defaultdict(int)
    for c in chains:
        for t in c.touches:
            z = zone(t.x, t.y)
            sums[z] += c.points
            counts[z] += 1
    values = {z: sums[z] / counts[z] for z in counts}
    return values, counts, zone


@dataclass
class PlayerTotals:
    player_id: str
    points_added: float = 0.0
    touches: int = 0
    matches: Set[str] = field(default_factory=set)


def compute_points_added(
    raw_dir: str, n_x: int = 8, n_y: int = 4, pattern: str = "CD_M*_raw.json"
) -> Tuple[List[dict], List[dict]]:
    chains = load_chains(raw_dir, pattern)
    values, counts, zone = build_zone_values(chains, n_x, n_y)

    totals: Dict[str, PlayerTotals] = {}

    def credit(player_id: str, match_id: str, delta: float) -> None:
        p = totals.setdefault(player_id, PlayerTotals(player_id=player_id))
        p.points_added += delta
        p.touches += 1
        p.matches.add(match_id)

    for c in chains:
        touches = c.touches
        for i in range(len(touches) - 1):
            v_before = values[zone(touches[i].x, touches[i].y)]
            v_after = values[zone(touches[i + 1].x, touches[i + 1].y)]
            credit(touches[i].player_id, c.match_id, v_after - v_before)
        if touches:
            # Final action in the chain: credited against the *actual* result (0/1/6),
            # not a zone estimate, since we know exactly what happened here.
            v_before = values[zone(touches[-1].x, touches[-1].y)]
            credit(touches[-1].player_id, c.match_id, c.points - v_before)

    rows = [
        {"playerId": p.player_id, "pointsAdded": round(p.points_added, 2),
         "touches": p.touches, "matches": len(p.matches)}
        for p in totals.values()
    ]
    rows.sort(key=lambda r: r["pointsAdded"], reverse=True)

    zone_rows = [
        {"zoneX": z[0], "zoneY": z[1], "avgValue": round(v, 3), "samples": counts[z]}
        for z, v in sorted(values.items())
    ]
    return rows, zone_rows


def write_csv(rows: List[dict], path: str) -> None:
    if not rows:
        open(path, "w", encoding="utf-8").close()
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
