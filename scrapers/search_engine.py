"""
search_engine.py
Fires search queries at Serper.dev (Google Search API).
Free tier: 2,500 queries/month — more than enough for daily + weekly runs.
Returns raw results: title, url, snippet per hit.
"""

import os
import time
import requests

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SERPER_URL = "https://google.serper.dev/search"


def search(
    query: str,
    num_results: int = 10,
    time_filter: str | None = "qdr:m",
    track: str = "general",
) -> list[dict]:
    """Single search. Returns list of {title, url, snippet, source_query, track}."""
    if not SERPER_API_KEY:
        raise EnvironmentError("SERPER_API_KEY not set. Check your .env file.")

    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "num": num_results,
        "gl": "us",
        "hl": "en",
    }
    if time_filter:
        payload["tbs"] = time_filter

    try:
        r = requests.post(SERPER_URL, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"[SEARCH ERR] '{query[:60]}': {e}")
        return []

    return [
        {
            "title":        item.get("title", "").strip(),
            "url":          item.get("link", "").strip(),
            "snippet":      item.get("snippet", "").strip(),
            "source_query": query,
            "track":        track,
        }
        for item in data.get("organic", [])
        if item.get("link")
    ]


def _normalize_query(entry) -> tuple[str, str]:
    """Accept either a plain string or a {'query', 'track'} dict."""
    if isinstance(entry, dict):
        return entry.get("query", ""), entry.get("track", "general")
    return entry, "general"


def batch_search(
    queries: list,
    num_results: int = 10,
    delay: float = 0.4,
    time_filter: str | None = "qdr:m",
) -> list[dict]:
    """
    Run all queries, deduplicate by URL across results.
    Accepts track-tagged query dicts ({"query", "track"}) or plain strings.
    On cross-track URL collisions, the first-seen result keeps a merged
    `tracks` list so attribution is not silently lost.
    Returns flat list of all unique hits.
    """
    all_results: list[dict] = []
    by_url: dict[str, dict] = {}
    total = len(queries)

    for i, entry in enumerate(queries, 1):
        query, track = _normalize_query(entry)
        print(f"[SEARCH {i}/{total}] ({track}) {query}")
        for r in search(query, num_results=num_results, time_filter=time_filter, track=track):
            url = r["url"]
            existing = by_url.get(url)
            if existing is None:
                r["tracks"] = [track]
                by_url[url] = r
                all_results.append(r)
            elif track not in existing["tracks"]:
                existing["tracks"].append(track)
        time.sleep(delay)

    print(f"[SEARCH] Done. {len(all_results)} unique URLs across {total} queries.")
    return all_results
