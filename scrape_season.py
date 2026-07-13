#!/usr/bin/env python3
"""CLI for the AFL scraper.

Examples
--------
Scrape a single match (player stats + play-by-play):
    python scrape_season.py match 7150 --out-dir data/match7150

Scrape an entire season (auto-discovers every match, resumable):
    python scrape_season.py season 2026 --out-dir data/2026

Only the raw play-by-play JSON, nothing else, all files flat in one folder:
    python scrape_season.py raw 2026 --out-dir data/2026_raw

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
from afl_scraper.raw_dump import scrape_match_raw, scrape_season_raw
from afl_scraper.season import scrape_season
from afl_scraper.stats import scrape_match_stats


def _cmd_match(args: argparse.Namespace) -> None:
    result = asyncio.run(scrape_match_stats(args.match_id, out_dir=args.out_dir, headless=not args.headed))
    has_players = bool(result.home_rows or result.away_rows)
    print(f"Player stats: home={len(result.home_rows)} away={len(result.away_rows)} -> {result.csv_paths}")

    code = args.cd_code or result.cd_match_code
    if not code:
        print("Could not find a CD_M... match code on the page; skipping play-by-play scrape. "
              "Pass --cd-code explicitly if you know it.", file=sys.stderr)
        return

    if result.matchplays_blob and result.matchplays_blob.get("matchChains"):
        print("Using play-by-play data captured directly from the match-centre page (not a separate API fetch).")
        plays_result = scrape_match_plays(code, out_dir=args.out_dir, data=result.matchplays_blob)
    else:
        plays_result = scrape_match_plays(code, out_dir=args.out_dir)

    print(f"Play-by-play: {plays_result.n_events} events (source={plays_result.source}) -> {plays_result.csv_paths}")
    if plays_result.n_events == 0:
        if has_players:
            print(f"WARNING: 0 events for code {code!r} even though player stats exist for this match "
                  "(so it has definitely been played) -- the match code is most likely wrong. "
                  "Double-check it against the match-centre page's network traffic in devtools.", file=sys.stderr)
        else:
            print("0 events and no player stats either -- this match probably hasn't been played yet.", file=sys.stderr)


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
    suspect = [o for o in outcomes if o.stats_ok and not o.plays_ok and "code is likely wrong" in o.error]
    if suspect:
        print(f"\n{len(suspect)} match(es) have stats but 0 play events for a match code that's probably "
              f"wrong (see {args.out_dir}/matches_index.csv, n_play_events/cd_match_code columns):")
        for o in suspect[:20]:
            print(f"  - match {o.match_id}: code={o.cd_match_code}")
    failed = [o for o in outcomes if not (o.stats_ok and o.plays_ok) and o not in suspect]
    if failed:
        print(f"\n{len(failed)} other match(es) had issues (see {args.out_dir}/matches_index.csv):")
        for o in failed[:20]:
            print(f"  - match {o.match_id}: {o.error}")
        if len(failed) > 20:
            print(f"  ... and {len(failed) - 20} more")


def _cmd_raw(args: argparse.Namespace) -> None:
    if args.match_id:
        outcome = asyncio.run(scrape_match_raw(args.match_id, out_dir=args.out_dir, headless=not args.headed))
        if outcome.ok:
            print(f"match {outcome.match_id}: {outcome.n_events} events (source={outcome.source}) "
                  f"-> {args.out_dir}/{outcome.cd_match_code}_raw.json")
        else:
            print(f"match {outcome.match_id}: {outcome.error} (code={outcome.cd_match_code})", file=sys.stderr)
        return

    matches = None
    if args.match_ids:
        matches = [MatchRef(match_id=m.strip()) for m in args.match_ids.split(",") if m.strip()]

    outcomes = asyncio.run(scrape_season_raw(
        season=args.season,
        out_dir=args.out_dir,
        headless=not args.headed,
        overwrite=args.overwrite,
        delay_seconds=args.delay,
        match_ids_file=args.match_ids_file,
        matches=matches,
    ))

    n_ok = sum(o.ok for o in outcomes)
    print(f"\n{args.season} season (raw only): {len(outcomes)} matches, {n_ok} with events, "
          f"all raw JSON in {args.out_dir}/")
    failed = [o for o in outcomes if not o.ok]
    if failed:
        print(f"{len(failed)} match(es) had no events (see {args.out_dir}/raw_index.csv):")
        for o in failed[:20]:
            print(f"  - match {o.match_id}: {o.error} (code={o.cd_match_code})")
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

    p_raw = sub.add_parser("raw", help="scrape ONLY raw play-by-play JSON, flat in one folder (no CSVs)")
    p_raw.add_argument("season", type=int, help="season year, e.g. 2026 (ignored if --match-id is given)")
    p_raw.add_argument("--out-dir", default=None, help="directory every <code>_raw.json lands in directly "
                        "(default: data/<season>_raw)")
    p_raw.add_argument("--match-id", default=None, help="scrape just this one match id instead of a whole season")
    p_raw.add_argument("--overwrite", action="store_true", help="re-scrape matches that already have raw JSON")
    p_raw.add_argument("--delay", type=float, default=2.0, help="seconds to sleep between matches (be polite)")
    p_raw.add_argument("--headed", action="store_true", help="show the browser window (for debugging)")
    p_raw.add_argument("--match-ids-file", default=None,
                        help="skip fixture discovery; read match ids from this file instead "
                             "(one per line, optionally 'id,round,home,away')")
    p_raw.add_argument("--match-ids", default=None,
                        help="skip fixture discovery; comma-separated list of match ids")
    p_raw.set_defaults(func=_cmd_raw)

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "season" and args.out_dir is None:
        args.out_dir = f"data/{args.season}"
    if args.cmd == "raw" and args.out_dir is None:
        args.out_dir = f"data/{args.season}_raw"
    args.func(args)


if __name__ == "__main__":
    main()
