"""Shared helpers used by the match-stats, match-plays and fixture scrapers.

Centralising these here removes the duplicate copies of deep_find_keys /
get_in / flatten_dict / write_csv that used to live in every notebook cell.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, TypeVar

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)

T = TypeVar("T")


def retry(fn: Callable[[], T], attempts: int = 3, delay: float = 2.0,
          backoff: float = 2.0, on_error: Optional[Callable[[Exception, int], None]] = None) -> T:
    """Call fn() up to `attempts` times with exponential backoff, re-raising the last error."""
    last_exc: Optional[Exception] = None
    wait = delay
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - deliberately broad, this is a generic retry helper
            last_exc = e
            if on_error:
                on_error(e, i)
            if i < attempts:
                time.sleep(wait)
                wait *= backoff
    assert last_exc is not None
    raise last_exc


def deep_find_keys(node: Any, targets: Set[str], found: Dict[str, Any]) -> None:
    """Depth-first search that records the first occurrence of each key in `targets`."""
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


def find_string_matching(node: Any, pattern: "re.Pattern[str]") -> Optional[str]:
    """Depth-first search for the first string leaf value matching `pattern`."""
    if isinstance(node, str):
        return node if pattern.match(node) else None
    if isinstance(node, dict):
        for v in node.values():
            hit = find_string_matching(v, pattern)
            if hit:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = find_string_matching(v, pattern)
            if hit:
                return hit
    return None


CD_MATCH_CODE_RE = re.compile(r"^CD_M\d+$")


def find_cd_match_code(blobs: Iterable[Any]) -> Optional[str]:
    """Look through captured JSON blobs for a Champion Data match code (e.g. CD_M20250142207).

    The match-centre page embeds this code somewhere in its JSON payloads; it's what the
    matchPlays endpoint (sapi.afl.com.au/afl/matchPlays/<code>) expects. Field names for it
    vary release to release, so we scan for the value shape instead of a fixed key path.
    """
    for blob in blobs:
        hit = find_string_matching(blob, CD_MATCH_CODE_RE)
        if hit:
            return hit
    return None


def get_in(obj: Any, path: List[str], default: Any = None) -> Any:
    cur = obj
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


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


def parse_mmss(s: Any) -> float:
    if not isinstance(s, str):
        return float("inf")
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    try:
        return float(s)
    except Exception:
        return float("inf")


def write_csv(rows: List[Dict[str, Any]], path: str, base_cols: Optional[List[str]] = None) -> None:
    """Write rows to CSV. Columns are `base_cols` (in order) followed by any extra keys found."""
    if not rows:
        open(path, "w", encoding="utf-8").close()
        return
    base_cols = base_cols or []
    dyn: List[str] = []
    for r in rows:
        for k in r.keys():
            if k not in base_cols and k not in dyn:
                dyn.append(k)
    cols = base_cols + dyn
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def write_ndjson(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
