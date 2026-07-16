"""
snippet_paths.py — detect URLs eligible for snippet-only eval (no scrape).
"""

from core.prefilter import WISCONSIN_EVENT_SIGNALS, get_domain

HEALTH_SIGNALS = [
    "healthtech", "health tech", "digital health", "clinical ai", "clinical informatics",
    "medtech", "med tech", "hospital innovation", "health data", "health informatics",
    "biomedical informatics", "health innovation", "healthcare innovation",
]

WI_QUERY_SIGNALS = [
    "madison", "wisconsin", "milwaukee",
    "networking", "meetup", "conference", "summit", "forum", "symposium",
]

OFY_DOMAIN = "opportunitiesforyouth.org"

OFY_TITLE_SIGNALS = [
    "apply", "fellowship", "scholarship", "internship", "grant", "program",
    "fully funded", "call for", "applications open", "deadline",
]


def _combined_text(item: dict) -> str:
    title = (item.get("title") or "").lower()
    snippet = (item.get("snippet") or "").lower()
    query = (item.get("source_query") or "").lower()
    url = (item.get("url") or "").lower()
    return f"{title} {snippet} {query} {url}"


def _has_health_signal(text: str) -> bool:
    return any(sig in text for sig in HEALTH_SIGNALS)


def _is_wisconsin_context(item: dict, text: str) -> bool:
    if "madison" in text or "wisconsin" in text:
        return True
    query = (item.get("source_query") or "").lower()
    return "madison" in query or "wisconsin" in query or "milwaukee" in query


def _is_wi_event_query(item: dict) -> bool:
    query = (item.get("source_query") or "").lower()
    if not any(loc in query for loc in ("madison", "wisconsin", "milwaukee")):
        return False
    return any(sig in query for sig in WI_QUERY_SIGNALS)


def is_wisconsin_snippet_only(item: dict) -> bool:
    text = _combined_text(item)
    if not _is_wisconsin_context(item, text):
        return False
    if not any(sig in text for sig in WISCONSIN_EVENT_SIGNALS):
        return False
    if _has_health_signal(text) or _is_wi_event_query(item):
        return True
    return False


def is_ofy_snippet_only(item: dict) -> bool:
    domain = get_domain(item.get("url", ""))
    if domain != OFY_DOMAIN and not domain.endswith(f".{OFY_DOMAIN}"):
        return False
    title = (item.get("title") or "").lower()
    snippet = (item.get("snippet") or "").lower()
    combined = f"{title} {snippet}"
    if not any(sig in combined for sig in OFY_TITLE_SIGNALS):
        return False
    return _has_health_signal(combined) or any(
        h in combined for h in ("health", "clinical", "medical", "who", "nih")
    )


def is_snippet_only(item: dict) -> bool:
    return is_wisconsin_snippet_only(item) or is_ofy_snippet_only(item)


def tag_snippet_only(items: list[dict]) -> list[dict]:
    for item in items:
        item["snippet_only"] = is_snippet_only(item)
    n = sum(1 for i in items if i.get("snippet_only"))
    if n:
        print(f"[SNIPPET] {n}/{len(items)} tagged snippet-only (no scrape if accepted)")
    return items
