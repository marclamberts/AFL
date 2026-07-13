"""Discover every match in an AFL season so it can be scraped end to end.

There's no single documented "give me the whole season" endpoint, so this uses the
same trick as the rest of this codebase: load the fixture page in a real browser,
capture every JSON response it fires off, and heuristically pick out anything that
looks like a list of matches (same idea as plays.collect_event_lists, just scored
against match-shaped keys instead of event-shaped ones). We also scan the rendered
DOM for /afl/matches/<id> links as a second, independent source, since match tiles
are sometimes rendered from data that never appears as its own XHR.

Network access to afl.com.au is blocked from this sandbox, so none of this has been
exercised against the live site — see README.md for how to validate/adjust it from
an environment that can actually reach afl.com.au, and use `--match-ids-file` / the
`known_matches` param below to bypass discovery entirely if the heuristics miss.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .common import DEFAULT_USER_AGENT, get_in

MATCH_LINK_RE = re.compile(r"/afl/matches/(\d+)")


@dataclass
class MatchRef:
    match_id: str
    round: Optional[str] = None
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    utc_start_time: Optional[str] = None

    def year(self) -> Optional[int]:
        if not self.utc_start_time:
            return None
        m = re.match(r"^(\d{4})-", self.utc_start_time)
        return int(m.group(1)) if m else None


def _match_list_score(d: Dict[str, Any]) -> int:
    keys = " ".join(map(str, d.keys())).lower()
    score = 0
    for hint in ("round", "home", "away", "venue", "date", "starttime", "utc", "match", "team"):
        if hint in keys:
            score += 1
    return score


def _iter_nodes(root: Any) -> Iterable[Any]:
    yield root
    if isinstance(root, dict):
        for v in root.values():
            if isinstance(v, (dict, list)):
                yield from _iter_nodes(v)
    elif isinstance(root, list):
        for v in root:
            if isinstance(v, (dict, list)):
                yield from _iter_nodes(v)


def _collect_match_lists(root: Any) -> List[List[Dict[str, Any]]]:
    found = []
    for node in _iter_nodes(root):
        if isinstance(node, list) and node and isinstance(node[0], dict):
            sample = node[:5]
            avg = sum(_match_list_score(x) for x in sample) / len(sample)
            if avg >= 2:
                found.append(node)
    return found


def _extract_match_ref(entry: Dict[str, Any]) -> Optional[MatchRef]:
    match_id = (
        entry.get("matchId")
        or entry.get("id")
        or get_in(entry, ["match", "id"])
        or get_in(entry, ["match", "matchId"])
    )
    # Some feeds give match ids as e.g. "CD_M20260142207" (Champion Data code) rather
    # than the short numeric id the /afl/matches/<id> URL wants; only accept plain ints.
    if match_id is None or not str(match_id).isdigit():
        return None

    home = (
        get_in(entry, ["homeTeam", "name", "abbreviation"])
        or get_in(entry, ["homeTeam", "abbreviation"])
        or get_in(entry, ["homeTeam", "name"])
    )
    away = (
        get_in(entry, ["awayTeam", "name", "abbreviation"])
        or get_in(entry, ["awayTeam", "abbreviation"])
        or get_in(entry, ["awayTeam", "name"])
    )
    round_name = entry.get("roundName") or entry.get("round") or get_in(entry, ["round", "name"])
    start = entry.get("utcStartTime") or entry.get("date") or entry.get("startDateTime")

    return MatchRef(
        match_id=str(match_id),
        round=str(round_name) if round_name is not None else None,
        home_team=str(home) if home else None,
        away_team=str(away) if away else None,
        utc_start_time=str(start) if start else None,
    )


async def _load_page_json_and_links(url: str, headless: bool = True, settle_seconds: float = 5.0) -> Tuple[List[Any], List[str]]:
    from playwright.async_api import async_playwright

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
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        hrefs: List[str] = await page.eval_on_selector_all(
            "a[href*='/afl/matches/']", "els => els.map(e => e.getAttribute('href'))"
        )

        await browser.close()

    return captured, hrefs


def candidate_fixture_urls(season_id: Any) -> List[str]:
    """Best-effort guesses at a URL that lists the whole season; try them in order.

    `season_id` is whatever value the site's own "Season" query param expects. That's
    often *not* the calendar year -- AFL/Champion Data assign an internal, arbitrary
    compSeason id to each season (e.g. 2026 might be id 85), unrelated to the year.
    """
    return [
        f"https://www.afl.com.au/fixture?Season={season_id}",
        f"https://www.afl.com.au/fixture/{season_id}",
        "https://www.afl.com.au/fixture",
    ]


async def discover_season_matches(
    season: int,
    headless: bool = True,
    extra_urls: Optional[List[str]] = None,
    season_id: Optional[Any] = None,
) -> List[MatchRef]:
    """Discover every match belonging to `season` by crawling fixture page(s).

    `season` (e.g. 2026) is the calendar year, used for labeling and as a fallback
    year-filter. `season_id` is the site's own internal season identifier if it
    differs from the calendar year (e.g. 85) -- pass it explicitly once you've found
    it (devtools > Network on the fixture page, look at the "Season" query param).

    Tries each candidate URL in turn, merging whatever it finds (both JSON-derived
    MatchRefs and bare match-id links from the DOM). When `season_id` is given and
    differs from `season`, every match found is kept as-is -- a compSeason id has no
    relationship to the calendar year, so filtering by parsed year would just discard
    real matches. Without an explicit `season_id`, falls back to the old behaviour of
    keeping only matches whose parsed start-time year matches `season`.
    """
    sid = season if season_id is None else season_id
    urls = (extra_urls or []) + candidate_fixture_urls(sid)

    by_id: Dict[str, MatchRef] = {}
    link_only_ids: set = set()

    for url in urls:
        try:
            blobs, hrefs = await _load_page_json_and_links(url, headless=headless)
        except Exception:
            continue

        for blob in blobs:
            for lst in _collect_match_lists(blob):
                for entry in lst:
                    ref = _extract_match_ref(entry)
                    if ref is None:
                        continue
                    if ref.match_id not in by_id:
                        by_id[ref.match_id] = ref

        for href in hrefs:
            m = MATCH_LINK_RE.search(href or "")
            if m:
                link_only_ids.add(m.group(1))

        if by_id or link_only_ids:
            # Got something from this candidate URL; no need to try the rest too.
            break

    if season_id is not None and season_id != season:
        matches = list(by_id.values())
    else:
        matches = [ref for ref in by_id.values() if ref.year() in (None, season)]
    known_ids = {m.match_id for m in matches}
    for mid in link_only_ids - known_ids:
        matches.append(MatchRef(match_id=mid))

    matches.sort(key=lambda m: (m.round or "", m.match_id))
    return matches


def load_match_ids_file(path: str) -> List[MatchRef]:
    """Manual override: one match id per line (optionally 'id,round,home,away')."""
    refs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            refs.append(MatchRef(
                match_id=parts[0],
                round=parts[1] if len(parts) > 1 else None,
                home_team=parts[2] if len(parts) > 2 else None,
                away_team=parts[3] if len(parts) > 3 else None,
            ))
    return refs
