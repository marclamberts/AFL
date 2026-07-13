#!/usr/bin/env python3
"""CLI for the AFL scraper.

Examples
--------
Scrape a single match (player stats + play-by-play):
    python scrape_season.py match 7150 --out-dir data/match7150

Scrape an entire season (auto-discovers every match, resumable):
    python scrape_season.py season 2026 --out-dir data/2026

If fixture discovery doesn't find anything (see afl_scraper/fixture.py for why
that's a real risk -- it hasn't been validated against the live site), supply
your own list of match ids instead:
    python scrape_season.py season 2026 --out-dir data/2026 --match-ids-file ids.txt
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from afl_scraper.fixture import MatchRef
from afl_scraper.plays import scrape_match_plays
from afl_scraper.season import scrape_season
from afl_scraper.stats import scrape_match_stats


def _cmd_match(args: argparse.Namespace) -> None:
    result = asyncio.run(scrape_match_stats(args.match_id, out_dir=args.out_dir, headless=not args.headed))
    print(f"Player stats: home={len(result.home_rows)} away={len(result.away_rows)} -> {result.csv_paths}")

    code = args.cd_code or result.cd_match_code
    if not code:
        print("Could not find a CD_M... match code on the page; skipping play-by-play scrape. "
              "Pass --cd-code explicitly if you know it.", file=sys.stderr)
        return

    plays_result = scrape_match_plays(code, out_dir=args.out_dir)
    print(f"Play-by-play: {plays_result.n_events} events -> {plays_result.csv_paths}")


def _cmd_season(args: argparse.Namespace) -> None:
    matches = None
    if args.match_ids:
        matches = [MatchRef(match_id=m.strip()) for m in args.match_ids.split(",") if m.strip()]

    outcomes = asyncio.run(scrape_season(
        season=args.season,
        out_dir=args.out_dir,
        headless=not args.headed,
        overwrite=args.overwrite,
        delay_seconds=args.delay,
        match_ids_file=args.match_ids_file,
        matches=matches,
    ))

    n_stats_ok = sum(o.stats_ok for o in outcomes)
    n_plays_ok = sum(o.plays_ok for o in outcomes)
    print(f"\n{args.season} season: {len(outcomes)} matches discovered, "
          f"{n_stats_ok} stats scraped, {n_plays_ok} plays scraped.")
    failed = [o for o in outcomes if not (o.stats_ok and o.plays_ok)]
    if failed:
        print(f"{len(failed)} match(es) had issues (see {args.out_dir}/matches_index.csv):")
        for o in failed[:20]:
            print(f"  - match {o.match_id}: {o.error}")
        if len(failed) > 20:
            print(f"  ... and {len(failed) - 20} more")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_match = sub.add_parser("match", help="scrape a single match")
    p_match.add_argument("match_id", help="numeric AFL match id, e.g. 7150")
    p_match.add_argument("--out-dir", default=".", help="directory to write CSVs to")
    p_match.add_argument("--cd-code", default=None, help="Champion Data match code (e.g. CD_M20260142207) "
                          "for the play-by-play feed, if auto-detection fails")
    p_match.add_argument("--headed", action="store_true", help="show the browser window (for debugging)")
    p_match.set_defaults(func=_cmd_match)

    p_season = sub.add_parser("season", help="scrape every match in a season")
    p_season.add_argument("season", type=int, help="season year, e.g. 2026")
    p_season.add_argument("--out-dir", default=None, help="directory to write per-match subfolders + season CSVs to "
                           "(default: data/<season>)")
    p_season.add_argument("--overwrite", action="store_true", help="re-scrape matches that already have output")
    p_season.add_argument("--delay", type=float, default=2.0, help="seconds to sleep between matches (be polite)")
    p_season.add_argument("--headed", action="store_true", help="show the browser window (for debugging)")
    p_season.add_argument("--match-ids-file", default=None,
                           help="skip fixture discovery; read match ids from this file instead "
                                "(one per line, optionally 'id,round,home,away')")
    p_season.add_argument("--match-ids", default=None,
                           help="skip fixture discovery; comma-separated list of match ids")
    p_season.set_defaults(func=_cmd_season)

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "season" and args.out_dir is None:
        args.out_dir = f"data/{args.season}"
    args.func(args)


if __name__ == "__main__":
    main()
