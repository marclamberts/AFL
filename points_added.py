#!/usr/bin/env python3
"""Estimate player "Points Added" from a folder of raw play-by-play JSON files
(the <CD_M...>_raw.json output of scrape_season.py raw / scrape_season_raw).

Example:
    python points_added.py data/2026_raw --out points_added.csv

See afl_scraper/points_added.py for how the metric is computed and why it needs a
decent number of matches to be trustworthy -- pass --zones-out to see how many
samples went into each field-position zone before trusting the numbers.
"""
from __future__ import annotations

import argparse

from afl_scraper.points_added import compute_points_added, write_csv


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw_dir", help="folder containing <CD_M...>_raw.json files")
    ap.add_argument("--out", default="points_added.csv", help="output CSV path")
    ap.add_argument("--zones-out", default=None,
                     help="optional CSV of the zone value/sample-count table, for sanity-checking")
    ap.add_argument("--n-x", type=int, default=8, help="grid columns (goal-to-goal axis)")
    ap.add_argument("--n-y", type=int, default=4, help="grid rows (boundary-to-boundary axis)")
    ap.add_argument("--min-touches", type=int, default=0, help="drop players below this many touches")
    args = ap.parse_args()

    rows, zone_rows = compute_points_added(args.raw_dir, n_x=args.n_x, n_y=args.n_y)
    if args.min_touches:
        rows = [r for r in rows if r["touches"] >= args.min_touches]

    write_csv(rows, args.out)
    print(f"{len(rows)} players -> {args.out}")
    print("\nTop 10:")
    for r in rows[:10]:
        print(f"  {r['playerId']:>14}  pointsAdded={r['pointsAdded']:>7}  touches={r['touches']:>4}  matches={r['matches']}")
    if len(rows) > 10:
        print("\nBottom 5 (biggest negative contributors):")
        for r in rows[-5:]:
            print(f"  {r['playerId']:>14}  pointsAdded={r['pointsAdded']:>7}  touches={r['touches']:>4}  matches={r['matches']}")

    if args.zones_out:
        write_csv(zone_rows, args.zones_out)
        low_sample = [z for z in zone_rows if z["samples"] < 10]
        print(f"\nZone value table -> {args.zones_out}")
        if low_sample:
            print(f"WARNING: {len(low_sample)}/{len(zone_rows)} zones have fewer than 10 samples -- "
                  "treat this run's numbers as rough until you've scraped more matches.")


if __name__ == "__main__":
    main()
