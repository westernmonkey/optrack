"""
snippet_paths.py — Madison/WI event fast-path tagging.
OFY is discovery-only: always requires full-page confirmation.
"""

from core.filter_policy import is_snippet_only as policy_is_snippet_only


def is_wisconsin_snippet_only(item: dict) -> bool:
    from core.filter_policy import is_wisconsin_snippet_only as fn
    return fn(item)


def is_ofy_snippet_only(item: dict) -> bool:
    from core.filter_policy import is_ofy_snippet_only as fn
    return fn(item)


def is_snippet_only(item: dict) -> bool:
    return policy_is_snippet_only(item)


def tag_snippet_only(items: list[dict]) -> list[dict]:
    for item in items:
        item["snippet_only"] = is_snippet_only(item)
    n = sum(1 for i in items if i.get("snippet_only"))
    if n:
        print(f"[SNIPPET] {n}/{len(items)} tagged snippet-only (WI events)")
    return items
