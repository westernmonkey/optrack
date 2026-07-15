"""
query_builder.py
Builds Google search queries from keywords.yaml for two tracks:
  general → fellowships, scholarships, student programs (not networking-first)
  labs    → clinical AI / digital health research labs + student programs

Daily mode:  priority combos (+ lab targets) only — fits Serper free tier.
Weekly mode: full types x regions sweep on top of the daily set.

Each query is returned as a dict: {"query": str, "track": str}.
"""

import yaml
from datetime import datetime


def load_keywords():
    with open("config/keywords.yaml") as f:
        return yaml.safe_load(f)


def _dedup_add(queries: list[dict], seen: set, track: str, query: str):
    query = query.strip()
    if not query:
        return
    key = (track, query.lower())
    if key in seen:
        return
    seen.add(key)
    queries.append({"query": query, "track": track})


def _build_general(config: dict, mode: str, year: int, queries: list[dict], seen: set):
    track_cfg = config.get("tracks", {}).get("general", {})

    for combo in track_cfg.get("priority_combos", []):
        opp, region = combo
        if region.lower() != "global":
            _dedup_add(queries, seen, "general", f"{opp} {region} {year}")
            _dedup_add(queries, seen, "general", f"{opp} {region} apply")
        else:
            _dedup_add(queries, seen, "general", f"{opp} {year} apply")
            _dedup_add(queries, seen, "general", f"{opp} {year} open applications")

    if mode == "daily":
        return

    priority_types = track_cfg.get("opportunity_types", [])[:10]
    for opp in priority_types:
        for region in config.get("regions", []):
            _dedup_add(queries, seen, "general", f"{opp} {region} {year}")


def _build_labs(config: dict, mode: str, year: int, queries: list[dict], seen: set):
    track_cfg = config.get("tracks", {}).get("labs", {})

    # Prestige / program combos, same shape as general priority_combos.
    for combo in track_cfg.get("priority_combos", []):
        opp, region = combo
        if region.lower() in ("global", "united states"):
            _dedup_add(queries, seen, "labs", f"{opp} {year}")
            _dedup_add(queries, seen, "labs", f"{opp} application")
        else:
            _dedup_add(queries, seen, "labs", f"{opp} {region} {year}")
            _dedup_add(queries, seen, "labs", f"{opp} {region} apply")

    # Named lab targets: one focused query each in daily mode.
    targets = track_cfg.get("targets", [])
    for target in targets:
        name, location = target
        _dedup_add(queries, seen, "labs", f"{name} undergraduate research {year}")

    if mode == "daily":
        return

    # Weekly: expand each lab target across its opportunity types.
    opp_types = track_cfg.get("opportunity_types", [])
    for target in targets:
        name, location = target
        for opp in opp_types:
            _dedup_add(queries, seen, "labs", f"{name} {opp}")


def build_queries(mode: str = "weekly") -> list[dict]:
    """
    Build track-tagged queries for both the general and labs tracks.

    mode='daily'  → priority combos + lab targets only
    mode='weekly' → full types x regions / lab target x opp-type sweep

    Returns a deduplicated list of {"query": str, "track": str} dicts.
    Dedup is per (track, query) so the same string can exist in both tracks.
    """
    config = load_keywords()
    year = datetime.now().year
    queries: list[dict] = []
    seen: set = set()

    _build_general(config, mode, year, queries, seen)
    _build_labs(config, mode, year, queries, seen)

    return queries


if __name__ == "__main__":
    for m in ("daily", "weekly"):
        qs = build_queries(m)
        general = [q for q in qs if q["track"] == "general"]
        labs = [q for q in qs if q["track"] == "labs"]
        print(f"{m}: {len(qs)} total ({len(general)} general, {len(labs)} labs)")
    print("\nSample labs queries:")
    for q in build_queries("daily"):
        if q["track"] == "labs":
            print(f"  {q['query']}")
