"""
main.py — OpTrack v2
Opportunity hunter: searches the web for real fellowship/accelerator/grant
programs and saves them to Notion.

Usage:
  python main.py           # auto-detects daily vs weekly based on day of week
  python main.py --daily   # light scan (priority queries only)
  python main.py --weekly  # deep scan (all queries)
  python main.py --dry-run # search + evaluate but don't write to Notion
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from core.query_builder import build_queries
from core.deduper import filter_new, mark_seen
from core.prefilter import prefilter
from core.evaluator import batch_evaluate
from core.notion_writer import batch_write
from scrapers.search_engine import batch_search
from scrapers.page_scraper import batch_scrape


def parse_args():
    parser = argparse.ArgumentParser(description="OpTrack v2 — opportunity hunter")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--daily",   action="store_true", help="Light scan (priority queries)")
    group.add_argument("--weekly",  action="store_true", help="Deep scan (all queries)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Notion")
    parser.add_argument("--min-score", type=int, default=5, help="Minimum Claude score to save (default: 5)")
    return parser.parse_args()


def detect_mode() -> str:
    return "weekly" if datetime.now().weekday() == 0 else "daily"


def save_log(log: dict):
    Path("logs").mkdir(exist_ok=True)
    log_path = Path("logs/run_log.json")
    history = []
    try:
        history = json.loads(log_path.read_text())
        if not isinstance(history, list):
            history = [history]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    history.append(log)
    log_path.write_text(json.dumps(history[-50:], indent=2))


def main():
    args = parse_args()

    if args.daily:
        mode = "daily"
    elif args.weekly:
        mode = "weekly"
    else:
        mode = detect_mode()

    print(f"\n{'='*60}")
    print(f"OpTrack v2 — {mode.upper()} scan — {date.today()}")
    print(f"{'='*60}\n")

    # ── Step 1: Build search queries ──────────────────────────────
    queries = build_queries(mode=mode)
    print(f"[QUERIES] Built {len(queries)} search queries ({mode} mode)\n")

    # ── Step 2: Search the web ────────────────────────────────────
    raw_results = batch_search(queries, num_results=3)
    print(f"\n[SEARCH] {len(raw_results)} unique URLs found\n")

    if not raw_results:
        print("No results from search. Check SERPER_API_KEY.")
        save_log({"date": str(date.today()), "mode": mode,
                  "raw": 0, "new": 0, "prefiltered": 0, "accepted": 0, "written": 0})
        sys.exit(0)

    # ── Step 3: Deduplicate against seen URLs ─────────────────────
    new_results = filter_new(raw_results)
    print(f"[DEDUP] {len(new_results)} new URLs (never seen before)\n")

    if not new_results:
        print("No new URLs this run — nothing to evaluate.")
        save_log({"date": str(date.today()), "mode": mode,
                  "raw": len(raw_results), "new": 0, "prefiltered": 0, "accepted": 0, "written": 0})
        sys.exit(0)

    # ── Step 4: Pre-filter obvious junk (no API calls) ────────────
    candidates = prefilter(new_results)
    print(f"\n[PREFILTER] {len(candidates)} candidates remain\n")

    if not candidates:
        print("Everything filtered as junk. Consider loosening prefilter signals.")
        mark_seen(new_results)
        save_log({"date": str(date.today()), "mode": mode,
                  "raw": len(raw_results), "new": len(new_results), "prefiltered": 0, "accepted": 0, "written": 0})
        sys.exit(0)

    # ── Step 5: Scrape pages ──────────────────────────────────────
    scraped = batch_scrape(candidates, delay=0.5)
    print(f"\n[SCRAPE] Done\n")

    # ── Step 6: Claude evaluates each page ───────────────────────
    accepted = batch_evaluate(scraped, min_score=args.min_score)
    print(f"\n[EVAL] {len(accepted)} opportunities accepted\n")

    # ── Step 7: Write to Notion ───────────────────────────────────
    written = 0
    if accepted and not args.dry_run:
        written = batch_write(accepted)
    elif args.dry_run:
        print("[DRY RUN] Skipping Notion write. Accepted items:")
        for item in accepted:
            print(f"  [{item['score']}/10] {item['name']} — {item.get('deadline','no deadline')} — {item.get('url','')[:60]}")

    # ── Step 8: Mark all new URLs as seen (including rejected) ────
    mark_seen(new_results)

    # ── Log run ───────────────────────────────────────────────────
    log = {
        "date":        str(date.today()),
        "mode":        mode,
        "raw":         len(raw_results),
        "new":         len(new_results),
        "prefiltered": len(candidates),
        "accepted":    len(accepted),
        "written":     written,
    }
    save_log(log)

    print(f"\n{'='*60}")
    print(f"Done: {written} new opportunities written to Notion")
    print(f"Run summary: {json.dumps(log)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()