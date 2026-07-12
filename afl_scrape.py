"""
Scrape AFL match-chain (play-by-play) data for multiple matches.

Refactors the notebook's single-match "ONE-CELL: AFL matchPlays" scraper into
reusable functions, adds a loop over many match IDs, and a helper to discover
a Champion Data matchId (e.g. "CD_M20250142207") from a public match-centre
URL (e.g. "https://www.afl.com.au/afl/matches/7150") by sniffing the page's
network traffic with Playwright.

Run this on a machine with normal internet access -- it will not work from a
network-restricted sandbox.

Usage:
    # If you already have Champion Data matchIds:
    python afl_scrape.py --match-ids CD_M20250142207 CD_M20250142208

    # If you only have match-centre URLs (numeric IDs from afl.com.au/fixture):
    python afl_scrape.py --centre-urls https://www.afl.com.au/afl/matches/7150 \\
                                        https://www.afl.com.au/afl/matches/7151

    # Or read either from a text file, one per line:
    python afl_scrape.py --match-ids-file ids.txt
    python afl_scrape.py --centre-urls-file urls.txt

Each match writes 4 files into --out-dir (default "data"):
    <matchId>_raw.json          the raw API response
    <matchId>_plays.csv         one row per match chain (JSON-encoded events)
    <matchId>_plays_all.csv     one row per individual event, flattened
    <matchId>_plays_all.ndjson  same, as newline-delimited JSON
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests

TOKEN_URL = "https://api.afl.com.au/cfs/afl/WMCTok"
PLAYS_URL = "https://sapi.afl.com.au/afl/matchPlays/{match_id}"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)


# ---------------- Token + HTTP ----------------

def get_afl_token(timeout: float = 15.0) -> str:
    r = requests.post(TOKEN_URL, timeout=timeout)
    r.raise_for_status()
    tok = r.json().get("token")
    if not tok:
        raise RuntimeError("No 'token' in WMCTok response")
    return tok


def fetch_json_with_token(url: str, token: str, timeout: float = 30.0) -> Any:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
        "x-media-mis-token": token,
        "Origin": "https://www.afl.com.au",
        "Referer": "https://www.afl.com.au/",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code == 401:
        raise RuntimeError("401 Unauthorized: token expired/invalid.")
    r.raise_for_status()
    return r.json()


# ---------------- JSON traversal ----------------

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


def deep_find_keys(node: Any, targets: Set[str], found: Dict[str, Any]) -> None:
    """DFS that records the first occurrence of each key in `targets`."""
    if not (targets - set(found.keys())):
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k in targets and k not in found:
                found[k] = v
            deep_find_keys(v, targets, found)
    elif isinstance(node, list):
        for v in node:
            deep_find_keys(v, targets, found)


def _eventish_score(d: Dict[str, Any]) -> int:
    keys = " ".join(map(str, d.keys())).lower()
    score = 0
    for hint in ("period", "quarter", "qtr", "time", "display", "type", "event", "team", "player", "x", "y", "score"):
        if hint in keys:
            score += 1
    return score


def collect_event_lists(root: Any) -> List[Tuple[str, List[Dict[str, Any]]]]:
    found: List[Tuple[str, List[Dict[str, Any]]]] = []
    for p, node in _iter_nodes_with_path(root):
        if isinstance(node, list) and node and isinstance(node[0], dict):
            sample = node[:5]
            avg = sum(_eventish_score(x) for x in sample) / len(sample)
            if avg >= 2:
                found.append((p, node))
    priority = (".$.plays", ".$.matchPlays", ".$.events", ".$.data")
    found.sort(key=lambda item: 0 if any(item[0].endswith(k[1:]) for k in priority) else 1)
    return found


# ---------------- Flattening / Sorting / Dedup ----------------

def flatten_dict(d: Dict[str, Any], parent: str = "", sep: str = ".") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in d.items():
        nk = f"{parent}{sep}{k}" if parent else k
        if isinstance(v, dict):
            out.update(flatten_dict(v, nk, sep))
        elif isinstance(v, list):
            if v and all(isinstance(x, dict) for x in v):
                out[nk] = json.dumps(v, ensure_ascii=False)
            else:
                out[nk] = "|".join(map(str, v))
        else:
            out[nk] = v
    return out


def fingerprint_event(e: Dict[str, Any]) -> str:
    if "id" in e and isinstance(e["id"], (int, str)):
        return f"id::{e['id']}"
    canon = json.dumps(e, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha1::" + hashlib.sha1(canon.encode("utf-8")).hexdigest()


def parse_mmss(s: str) -> float:
    if not isinstance(s, str):
        return float("inf")
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    try:
        return float(s)
    except Exception:
        return float("inf")


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
    all_keys: Set[str] = set()
    for r in flat_rows:
        all_keys.update(r.keys())
    ordered = [k for k in preferred if k in all_keys]
    remaining = sorted(all_keys - set(ordered))
    return ordered + remaining


def write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    import csv

    if not rows:
        open(path, "w", encoding="utf-8").close()
        return
    cols = choose_columns(rows)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def write_ndjson(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------- Single-match scrape ----------------

def scrape_match_plays(match_id: str, token: str, out_dir: str = "data") -> Dict[str, str]:
    """Fetch one match's play-by-play data and write the 4 output files.

    Returns a dict of the paths written.
    """
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, match_id)

    data = fetch_json_with_token(PLAYS_URL.format(match_id=match_id), token)

    raw_path = f"{base}_raw.json"
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
        primary_csv_path = f"{base}_plays.csv"
        write_csv(primary_flat, primary_csv_path)

    seen: Set[str] = set()
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

    all_csv = f"{base}_plays_all.csv"
    all_ndjson = f"{base}_plays_all.ndjson"
    write_csv(all_flat, all_csv)
    write_ndjson([e for _, arr in lists for e in arr], all_ndjson)

    return {
        "raw": raw_path,
        "plays": primary_csv_path or "",
        "plays_all_csv": all_csv,
        "plays_all_ndjson": all_ndjson,
    }


# ---------------- Multi-match loop ----------------

def scrape_many(match_ids: List[str], out_dir: str = "data", pause_seconds: float = 1.5) -> Dict[str, Dict[str, str]]:
    """Scrape play-by-play data for many matches, refreshing the token on 401.

    Skips (and reports) any match that fails, instead of aborting the batch.
    """
    results: Dict[str, Dict[str, str]] = {}
    token = get_afl_token()

    for i, match_id in enumerate(match_ids, 1):
        print(f"[{i}/{len(match_ids)}] {match_id} ...", end=" ", flush=True)
        try:
            try:
                paths = scrape_match_plays(match_id, token, out_dir)
            except RuntimeError as e:
                if "401" not in str(e):
                    raise
                token = get_afl_token()  # refresh once and retry
                paths = scrape_match_plays(match_id, token, out_dir)
            results[match_id] = paths
            print("ok")
        except Exception as e:
            print(f"FAILED: {e}")
            results[match_id] = {"error": str(e)}

        if i < len(match_ids):
            time.sleep(pause_seconds)  # be polite to the API

    return results


# ---------------- Discover matchId from a match-centre URL ----------------

async def discover_match_id(centre_url: str) -> Optional[str]:
    """Visit a public match-centre page (e.g. .../afl/matches/7150) and sniff
    its network traffic for the Champion Data matchId (e.g. "CD_M...").

    Requires: pip install playwright && playwright install chromium
    """
    from playwright.async_api import async_playwright

    captured: List[Any] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT, locale="en-AU")
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

        page.on("response", lambda resp: asyncio.create_task(grab(resp)))
        await page.goto(centre_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        await asyncio.sleep(5)
        await browser.close()

    found: Dict[str, Any] = {}
    targets = {"matchId"}
    for blob in captured:
        deep_find_keys(blob, targets, found)
        if targets.issubset(found.keys()):
            break
    return found.get("matchId")


async def discover_many(centre_urls: List[str]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for url in centre_urls:
        print(f"resolving matchId for {url} ...", end=" ", flush=True)
        try:
            mid = await discover_match_id(url)
            print(mid or "not found")
        except Exception as e:
            mid = None
            print(f"FAILED: {e}")
        out[url] = mid
    return out


# ---------------- CLI ----------------

def _read_lines(path: str) -> List[str]:
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--match-ids", nargs="*", default=[], help="Champion Data matchIds, e.g. CD_M20250142207")
    ap.add_argument("--match-ids-file", help="text file, one matchId per line")
    ap.add_argument("--centre-urls", nargs="*", default=[], help="match-centre URLs to resolve matchIds from")
    ap.add_argument("--centre-urls-file", help="text file, one match-centre URL per line")
    ap.add_argument("--out-dir", default="data", help="output directory (default: data)")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds to sleep between matches")
    args = ap.parse_args()

    match_ids = list(args.match_ids)
    if args.match_ids_file:
        match_ids += _read_lines(args.match_ids_file)

    centre_urls = list(args.centre_urls)
    if args.centre_urls_file:
        centre_urls += _read_lines(args.centre_urls_file)

    if centre_urls:
        resolved = asyncio.run(discover_many(centre_urls))
        match_ids += [mid for mid in resolved.values() if mid]

    if not match_ids:
        ap.error("no matchIds given or resolved -- pass --match-ids / --centre-urls (or the *-file variants)")

    match_ids = list(dict.fromkeys(match_ids))  # dedupe, keep order
    results = scrape_many(match_ids, out_dir=args.out_dir, pause_seconds=args.pause)

    ok = sum(1 for r in results.values() if "error" not in r)
    print(f"\nDone: {ok}/{len(results)} matches scraped into {args.out_dir}/")


if __name__ == "__main__":
    main()
