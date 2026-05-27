"""
deduper.py
Tracks seen URLs across runs using a JSON file committed to the repo.
Strips query params and fragments so utm_source= variants don't sneak through.
"""

import json
from pathlib import Path
from urllib.parse import urlparse, urlunparse

SEEN_PATH = Path("data/seen_urls.json")


def clean_url(url: str) -> str:
    """Strip query string and fragment — keeps the canonical path."""
    try:
        p = urlparse(url)
        return urlunparse(p._replace(query="", fragment=""))
    except Exception:
        return url


def load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def filter_new(items: list[dict]) -> list[dict]:
    """Return only items whose cleaned URL hasn't been seen before."""
    seen = load_seen()
    return [item for item in items if clean_url(item.get("url", "")) not in seen]


def mark_seen(items: list[dict]) -> None:
    """Persist newly processed URLs to disk."""
    seen = load_seen()
    for item in items:
        seen.add(clean_url(item.get("url", "")))
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=2))
