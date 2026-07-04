#!/usr/bin/env python3
"""
Import skipped_eval_urls.json → URL enrich → prefilter → heuristic parse → Notion.
No LLM. Run: python scripts/import_skipped.py [--dry-run] [--limit N]
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from core.heuristic_parser import enrich_from_url, is_url_junk, parse_item
from core.notion_writer import batch_write
from core.prefilter import prefilter

SKIPPED_PATH = ROOT / "data" / "skipped_eval_urls.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    args = parser.parse_args()

    pending = json.loads(SKIPPED_PATH.read_text())
    if args.limit:
        batch_urls = pending[: args.limit]
        remaining = pending[args.limit :]
    else:
        batch_urls = pending
        remaining = []

    print(f"\n{'='*60}")
    print(f"OpTrack — DIRECT IMPORT — {date.today()}")
    print(f"URLs: {len(batch_urls)} (remaining after: {len(remaining)})")
    print(f"{'='*60}\n")

    items = []
    url_junk = 0
    for url in batch_urls:
        if is_url_junk(url):
            url_junk += 1
            continue
        item = {"url": url, "title": "", "snippet": "", "track": "general", "source_query": "skipped-import"}
        enrich_from_url(item)
        items.append(item)

    print(f"[URL JUNK] Dropped {url_junk} obvious job/news URLs from path/domain\n")

    candidates = prefilter(items)
    print(f"[PREFILTER] {len(candidates)}/{len(items)} passed\n")

    parsed = []
    for item in candidates:
        result = parse_item(item)
        if result:
            parsed.append(result)

    print(f"[PARSE] {len(parsed)} items ready for Notion\n")

    written = 0
    if parsed and not args.dry_run:
        written = batch_write(parsed)
    elif args.dry_run:
        print("[DRY RUN] Would write:")
        for p in parsed[:25]:
            print(f"  [{p['score']}] ({p['track']}) {p['name'][:55]} — {p['type']} — {p['region']}")
        if len(parsed) > 25:
            print(f"  ... and {len(parsed) - 25} more")

    if not args.dry_run:
        written_urls = {p["url"] for p in parsed}
        if args.limit:
            unprocessed = [u for u in batch_urls if u not in written_urls]
            SKIPPED_PATH.write_text(json.dumps(remaining + unprocessed, indent=2))
            left = len(remaining) + len(unprocessed)
        else:
            SKIPPED_PATH.write_text(json.dumps([], indent=2))
            left = 0
        print(f"\nDone: {written} written to Notion | {left} left in queue")
    else:
        print(f"\nDry run complete. {len(parsed)} would be written.")


if __name__ == "__main__":
    main()
