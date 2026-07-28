"""
query_builder.py
Builds Google search queries from keywords.yaml.

Tracks:
  general   — fellowships / scholarships / student programs
  labs      — clinical AI / digital health research labs
  wi_events — Madison/Wisconsin healthtech networking & conferences

Daily mode: bounded high-value set.
Weekly mode: expands type × region and lab target sweeps.

Each query: {"query": str, "track": str, "time_filter": str|None}
"""

from __future__ import annotations

from datetime import datetime

import yaml


def load_keywords():
    with open("config/keywords.yaml") as f:
        return yaml.safe_load(f)


def _dedup_add(
    queries: list[dict],
    seen: set,
    track: str,
    query: str,
    time_filter: str | None = "qdr:m",
):
    query = query.strip()
    if not query:
        return
    key = (track, query.lower())
    if key in seen:
        return
    seen.add(key)
    queries.append({
        "query": query,
        "track": track,
        "time_filter": time_filter,
    })


def _freshness_for(opp: str) -> str | None:
    """Annual fellowships/REUs often live on older pages — no qdr:m."""
    lower = opp.lower()
    evergreen = (
        "fellowship", "reu", "scholarship", "summer research",
        "undergraduate research", "traineeship", "nlm ",
    )
    if any(tok in lower for tok in evergreen):
        return None
    return "qdr:m"


def _build_general(config: dict, mode: str, year: int, queries: list, seen: set):
    track_cfg = config.get("tracks", {}).get("general", {})
    daily_limit = int(track_cfg.get("daily_limit", 40))

    for combo in track_cfg.get("priority_combos", []):
        if mode == "daily" and sum(1 for q in queries if q["track"] == "general") >= daily_limit:
            break
        opp, region = combo
        tf = _freshness_for(opp)
        if region.lower() != "global":
            _dedup_add(queries, seen, "general", f"{opp} {region} {year}", tf)
            _dedup_add(queries, seen, "general", f"{opp} {region} apply", tf)
        else:
            _dedup_add(queries, seen, "general", f"{opp} {year} apply", tf)
            _dedup_add(queries, seen, "general", f"{opp} {year} open applications", tf)

    for query in track_cfg.get("site_queries", []):
        if mode == "daily" and sum(1 for q in queries if q["track"] == "general") >= daily_limit:
            break
        _dedup_add(queries, seen, "general", query, None)

    if mode == "daily":
        return

    priority_types = track_cfg.get("opportunity_types", [])[:10]
    for opp in priority_types:
        tf = _freshness_for(opp)
        for region in config.get("regions", []):
            _dedup_add(queries, seen, "general", f"{opp} {region} {year}", tf)


def _build_labs(config: dict, mode: str, year: int, queries: list, seen: set):
    track_cfg = config.get("tracks", {}).get("labs", {})
    daily_limit = int(track_cfg.get("daily_limit", 30))

    for combo in track_cfg.get("priority_combos", []):
        if mode == "daily" and sum(1 for q in queries if q["track"] == "labs") >= daily_limit:
            break
        opp, region = combo
        tf = _freshness_for(opp)
        if region.lower() in ("global", "united states"):
            _dedup_add(queries, seen, "labs", f"{opp} {year}", tf)
            _dedup_add(queries, seen, "labs", f"{opp} application", tf)
        else:
            _dedup_add(queries, seen, "labs", f"{opp} {region} {year}", tf)
            _dedup_add(queries, seen, "labs", f"{opp} {region} apply", tf)

    targets = track_cfg.get("targets", [])
    for target in targets:
        if mode == "daily" and sum(1 for q in queries if q["track"] == "labs") >= daily_limit:
            break
        name, _location = target
        _dedup_add(
            queries, seen, "labs",
            f"{name} undergraduate research {year}",
            None,
        )

    if mode == "daily":
        return

    opp_types = track_cfg.get("opportunity_types", [])
    for target in targets:
        name, _location = target
        for opp in opp_types:
            _dedup_add(queries, seen, "labs", f"{name} {opp}", None)


def _build_wi_events(config: dict, mode: str, year: int, queries: list, seen: set):
    track_cfg = config.get("tracks", {}).get("wi_events", {})
    if not track_cfg:
        return
    daily_limit = int(track_cfg.get("daily_limit", 12))
    combos = track_cfg.get("priority_combos", [])
    for combo in combos:
        if mode == "daily" and sum(1 for q in queries if q["track"] == "wi_events") >= daily_limit:
            break
        opp, region = combo
        _dedup_add(
            queries, seen, "wi_events",
            f"{opp} {region} {year}",
            "qdr:m",
        )
        _dedup_add(
            queries, seen, "wi_events",
            f"{opp} {region} register",
            "qdr:m",
        )


def build_queries(mode: str = "weekly") -> list[dict]:
    config = load_keywords()
    year = datetime.now().year
    queries: list[dict] = []
    seen: set = set()

    _build_general(config, mode, year, queries, seen)
    _build_labs(config, mode, year, queries, seen)
    _build_wi_events(config, mode, year, queries, seen)
    return queries


if __name__ == "__main__":
    for m in ("daily", "weekly"):
        qs = build_queries(m)
        by = {}
        for q in qs:
            by[q["track"]] = by.get(q["track"], 0) + 1
        print(f"{m}: {len(qs)} total {by}")
