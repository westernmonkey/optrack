"""
query_builder.py
Builds Google search queries from keywords.yaml.
Daily mode: priority combos only (~30 queries).
Weekly mode: full sweep (~200 queries).
"""

import yaml
from datetime import datetime


def load_keywords():
    with open("config/keywords.yaml") as f:
        return yaml.safe_load(f)


def build_queries(mode: str = "weekly") -> list[str]:
    """
    mode='daily'  → priority combos only (fits Serper free tier comfortably)
    mode='weekly' → full types × regions sweep
    Returns a deduplicated list of search query strings.
    """
    config = load_keywords()
    year = datetime.now().year
    queries = []
    seen = set()

    def add(q: str):
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)

    # Priority combos always come first
    for combo in config.get("priority_combos", []):
        opp, region = combo
        if region.lower() != "global":
            add(f"{opp} {region} {year}")
            add(f"{opp} {region} apply")
        else:
            add(f"{opp} {year} apply")
            add(f"{opp} {year} open applications")

    if mode == "daily":
        return queries

    # Weekly: add full types × regions matrix
    for opp in config.get("opportunity_types", []):
        for region in config.get("regions", []):
            if region.lower() != "global":
                add(f"{opp} {region} {year}")
            else:
                add(f"{opp} {year} apply")

    # Solo type searches (no region — catches global listings)
    for opp in config.get("opportunity_types", []):
        add(f"{opp} {year} open applications")
        add(f"{opp} {year} deadline")

    return queries


if __name__ == "__main__":
    daily = build_queries("daily")
    weekly = build_queries("weekly")
    print(f"Daily queries:  {len(daily)}")
    print(f"Weekly queries: {len(weekly)}")
    for q in daily[:10]:
        print(f"  {q}")
