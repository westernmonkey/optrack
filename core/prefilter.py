"""
prefilter.py — thin wrapper over filter_policy.
"""

from core.filter_policy import prefilter_item


def is_junk(item: dict) -> tuple[bool, str]:
    return prefilter_item(item)


def prefilter(items: list[dict]) -> list[dict]:
    passed = []
    dropped = 0
    for item in items:
        junk, _reason = is_junk(item)
        if junk:
            dropped += 1
        else:
            passed.append(item)
    total = len(items)
    print(f"[PREFILTER] {len(passed)}/{total} passed ({dropped} dropped)")
    return passed
