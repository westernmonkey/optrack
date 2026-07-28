"""
search_engine.py — Serper.dev Google Search with fail-fast and metrics.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from core.deduper import clean_url

SERPER_URL = "https://google.serper.dev/search"


class SerperFatalError(Exception):
    """Auth / out-of-credit — stop the entire run."""


def _api_key() -> str:
    return os.environ.get("SERPER_API_KEY", "").strip()


def _is_fatal_error(status: int, body: str) -> bool:
    if status in (401, 403):
        return True
    lower = (body or "").lower()
    return any(
        phrase in lower
        for phrase in (
            "not enough credits",
            "insufficient credits",
            "out of credits",
            "invalid api key",
            "unauthorized",
        )
    )


def search(
    query: str,
    num_results: int = 10,
    time_filter: str | None = "qdr:m",
    track: str = "general",
) -> list[dict]:
    """Single search. Raises SerperFatalError on credit/auth failure."""
    key = _api_key()
    if not key:
        raise EnvironmentError("SERPER_API_KEY not set. Check your .env file.")

    headers = {
        "X-API-KEY": key,
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "q": query,
        "num": num_results,
        "gl": "us",
        "hl": "en",
    }
    if time_filter:
        payload["tbs"] = time_filter

    try:
        r = requests.post(SERPER_URL, headers=headers, json=payload, timeout=15)
    except requests.RequestException as e:
        print(f"[SEARCH ERR] '{query[:60]}': {e}")
        return []

    if _is_fatal_error(r.status_code, r.text):
        raise SerperFatalError(
            f"Serper fatal {r.status_code}: {r.text[:200]}"
        )

    if r.status_code != 200:
        print(f"[SEARCH ERR] '{query[:60]}': HTTP {r.status_code}")
        return []

    data = r.json()
    return [
        {
            "title": item.get("title", "").strip(),
            "url": item.get("link", "").strip(),
            "snippet": item.get("snippet", "").strip(),
            "source_query": query,
            "track": track,
        }
        for item in data.get("organic", [])
        if item.get("link")
    ]


def _normalize_query(entry) -> tuple[str, str, object]:
    """
    Accept plain string or dict with query/track/time_filter.
    Returns (query, track, time_filter) where time_filter may be:
      _USE_DEFAULT — use batch default
      None — no freshness filter
      str — explicit tbs value
    """
    _USE_DEFAULT = object()
    if isinstance(entry, dict):
        tf = entry["time_filter"] if "time_filter" in entry else _USE_DEFAULT
        return (
            entry.get("query", ""),
            entry.get("track", "general"),
            tf,
        )
    return entry, "general", _USE_DEFAULT


def batch_search(
    queries: list,
    num_results: int = 10,
    delay: float = 0.4,
    time_filter: str | None = "qdr:m",
    *,
    verbose: bool = False,
) -> tuple[list[dict], dict]:
    """
    Run all queries, dedupe by canonical URL, merge tracks/queries.
    Returns (results, stats).
    Raises SerperFatalError on out-of-credit / auth.
    Refuses to treat all-failed as a legitimate zero-result run
    (stats['all_failed']=True).
    """
    _USE_DEFAULT = object()
    all_results: list[dict] = []
    by_url: dict[str, dict] = {}
    total = len(queries)
    ok_queries = 0
    failed_queries = 0
    fatal = False

    for i, entry in enumerate(queries, 1):
        if isinstance(entry, dict):
            query = entry.get("query", "")
            track = entry.get("track", "general")
            tf_override = entry["time_filter"] if "time_filter" in entry else _USE_DEFAULT
        else:
            query, track, tf_override = entry, "general", _USE_DEFAULT

        if not query:
            continue
        tf = time_filter if tf_override is _USE_DEFAULT else tf_override
        if verbose:
            print(f"[SEARCH {i}/{total}] ({track}) {query}")
        elif i == 1 or i % 25 == 0 or i == total:
            print(f"[SEARCH] {i}/{total} queries…")

        try:
            hits = search(query, num_results=num_results, time_filter=tf, track=track)
            ok_queries += 1
        except SerperFatalError:
            fatal = True
            failed_queries += 1
            print(f"[SEARCH] Fatal Serper error after {ok_queries} ok / "
                  f"{failed_queries} failed — aborting remaining queries")
            break
        except Exception as e:
            failed_queries += 1
            print(f"[SEARCH ERR] '{query[:60]}': {e}")
            hits = []

        for r in hits:
            canon = clean_url(r["url"]) or r["url"]
            r["url"] = canon
            existing = by_url.get(canon)
            if existing is None:
                r["tracks"] = [track]
                r["source_queries"] = [query]
                by_url[canon] = r
                all_results.append(r)
            else:
                if track not in existing["tracks"]:
                    existing["tracks"].append(track)
                sqs = existing.setdefault("source_queries", [])
                if query not in sqs:
                    sqs.append(query)
                if len(r.get("snippet") or "") > len(existing.get("snippet") or ""):
                    existing["snippet"] = r["snippet"]
                if len(r.get("title") or "") > len(existing.get("title") or ""):
                    existing["title"] = r["title"]

        time.sleep(delay)

    all_failed = total > 0 and ok_queries == 0
    stats = {
        "queries": total,
        "ok_queries": ok_queries,
        "failed_queries": failed_queries,
        "unique_urls": len(all_results),
        "all_failed": all_failed,
        "fatal": fatal,
    }
    print(
        f"[SEARCH] Done. {len(all_results)} unique URLs | "
        f"{ok_queries}/{total} queries ok"
        + (" | FATAL" if fatal else "")
        + (" | ALL FAILED" if all_failed else "")
    )
    if all_failed and not fatal:
        raise SerperFatalError(
            f"All {total} Serper queries failed — refusing empty run"
        )
    return all_results, stats
