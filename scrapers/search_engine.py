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


def search(query: str, num_results: int = 10) -> list[dict]:
    """Single search. Returns list of {title, url, snippet, source_query}."""
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
        }
        for item in data.get("organic", [])
        if item.get("link")
    ]


def batch_search(queries: list[str], num_results: int = 10, delay: float = 0.4) -> list[dict]:
    """
    Run all queries, deduplicate by URL across results.
    Returns flat list of all unique hits.
    """
    all_results: list[dict] = []
    seen_urls: set[str] = set()
    total = len(queries)

    for i, query in enumerate(queries, 1):
        print(f"[SEARCH {i}/{total}] {query}")
        for r in search(query, num_results=num_results):
            url = r["url"]
            if url not in seen_urls:
                seen_urls.add(url)
                all_results.append(r)
        time.sleep(delay)

    print(f"[SEARCH] Done. {len(all_results)} unique URLs across {total} queries.")
    return all_results
