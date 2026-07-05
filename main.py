"""
main.py — OpTrack v2
Opportunity hunter: searches the web for real fellowship/grant
programs and saves them to Notion.

Usage:
  python main.py              # auto-detects daily vs weekly based on day of week
  python main.py --daily      # light scan (priority queries only)
  python main.py --weekly     # deep scan (all queries)
  python main.py --dry-run    # search + evaluate but don't write to Notion
  python main.py --reval-skipped          # eval URLs in data/skipped_eval_urls.json (no Serper)
  python main.py --reval-skipped --reval-limit 100
  python main.py --reval-pending          # eval saved scrape queue (no Serper, no re-scrape)
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

SKIPPED_PATH = Path("data/skipped_eval_urls.json")
PENDING_PATH = Path("data/pending_eval.json")


def parse_args():
    parser = argparse.ArgumentParser(description="OpTrack v2 — opportunity hunter")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--daily",   action="store_true", help="Light scan (priority queries)")
    group.add_argument("--weekly",  action="store_true", help="Deep scan (all queries)")
    group.add_argument("--reval-skipped", action="store_true",
                        help="Re-evaluate URLs saved in data/skipped_eval_urls.json (no Serper search)")
    group.add_argument("--reval-pending", action="store_true",
                        help="Re-evaluate scraped pages in data/pending_eval.json (no Serper, no scrape)")
    parser.add_argument("--reval-limit", type=int, default=0,
                        help="Max URLs to re-eval (0 = all in queue)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Notion")
    parser.add_argument("--min-score", type=int, default=4,
                        help="Minimum LLM score to save (default: 4)")
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


def load_skipped_urls() -> list[str]:
    if not SKIPPED_PATH.exists():
        return []
    data = json.loads(SKIPPED_PATH.read_text())
    return data if isinstance(data, list) else []


def save_skipped_urls(urls: list[str]) -> None:
    SKIPPED_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKIPPED_PATH.write_text(json.dumps(sorted(set(urls)), indent=2))


def save_pending_eval(scraped: list[dict]) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(scraped, indent=2))
    save_skipped_urls([item["url"] for item in scraped if item.get("url")])
    print(f"[SAVE] {len(scraped)} scraped pages → data/pending_eval.json")


def _reval_batch(items: list, limit: int) -> tuple[list, list]:
    if limit and limit > 0:
        return items[:limit], items[limit:]
    return items, []


def _finish_reval(args, accepted: list[dict], remaining_urls: list[str], mode: str, total: int) -> None:
    for item in accepted:
        item.setdefault("track", "general")
        tracks = item.get("tracks")
        if tracks:
            item["track"] = "labs" if "labs" in tracks else tracks[0]

    written = 0
    if accepted and not args.dry_run:
        written = batch_write(accepted)
    elif args.dry_run and accepted:
        print("[DRY RUN] Would write:")
        for item in accepted:
            print(f"  [{item['score']}/10] ({item.get('track', 'general')}) {item['name']} "
                  f"— {item.get('url', '')[:60]}")

    save_skipped_urls(remaining_urls)
    if not remaining_urls and PENDING_PATH.exists():
        PENDING_PATH.unlink()

    save_log({
        "date": str(date.today()),
        "mode": mode,
        "queued": total,
        "processed": total - len(remaining_urls),
        "remaining": len(remaining_urls),
        "accepted": len(accepted),
        "written": written,
    })
    print(f"\nDone: {written} written | {len(remaining_urls)} URLs still queued")
    if remaining_urls:
        print(f"Run again to process the rest.\n")
    else:
        print()


def run_reval_pending(args) -> None:
    if not PENDING_PATH.exists():
        print("No data/pending_eval.json — nothing to re-evaluate.")
        print("Run a normal scan first; the queue is saved automatically after scrape.")
        sys.exit(0)

    pending = json.loads(PENDING_PATH.read_text())
    batch, remaining_items = _reval_batch(pending, args.reval_limit)
    remaining_urls = [item["url"] for item in remaining_items if item.get("url")]

    print(f"\n{'='*60}")
    print(f"OpTrack v2 — REVAL PENDING — {date.today()}")
    print(f"Evaluating {len(batch)}/{len(pending)} saved pages (no search, no scrape)")
    print(f"{'='*60}\n")

    accepted = batch_evaluate(batch, min_score=args.min_score)
    if remaining_items:
        PENDING_PATH.write_text(json.dumps(remaining_items, indent=2))
    _finish_reval(args, accepted, remaining_urls, "reval-pending", len(pending))


def run_reval_skipped(args) -> None:
    pending = load_skipped_urls()
    if not pending:
        print("No URLs in data/skipped_eval_urls.json — nothing to re-evaluate.")
        sys.exit(0)

    batch_urls, remaining = _reval_batch(pending, args.reval_limit)
    print(f"\n{'='*60}")
    print(f"OpTrack v2 — REVAL SKIPPED — {date.today()}")
    print(f"Processing {len(batch_urls)}/{len(pending)} skipped URLs")
    print(f"{'='*60}\n")

    candidates = [
        {"url": url, "track": "general", "title": "", "snippet": "", "source_query": "reval-skipped"}
        for url in batch_urls
    ]
    scraped = batch_scrape(candidates, delay=0.5)
    accepted = batch_evaluate(scraped, min_score=args.min_score)
    _finish_reval(args, accepted, remaining, "reval-skipped", len(pending))


def main():
    args = parse_args()

    if args.reval_skipped:
        run_reval_skipped(args)
        return

    if args.reval_pending:
        run_reval_pending(args)
        return

    if args.daily:
        mode = "daily"
    elif args.weekly:
        mode = "weekly"
    else:
        mode = detect_mode()

    print(f"\n{'='*60}")
    print(f"OpTrack v2 — {mode.upper()} scan — {date.today()}")
    print(f"{'='*60}\n")

    # ── Step 1: Build search queries (general + labs tracks) ──────
    queries = build_queries(mode=mode)
    n_general = sum(1 for q in queries if q.get("track") == "general")
    n_labs = sum(1 for q in queries if q.get("track") == "labs")
    print(f"[QUERIES] Built {len(queries)} search queries "
          f"({n_general} general, {n_labs} labs) ({mode} mode)\n")

    # ── Step 2: Search the web ────────────────────────────────────
    raw_results = batch_search(queries, num_results=10)
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
    save_pending_eval(scraped)

    # ── Step 6: LLM evaluates each page ────────────────────────────
    accepted = batch_evaluate(scraped, min_score=args.min_score)
    print(f"\n[EVAL] {len(accepted)} opportunities accepted\n")

    # Normalize the track label for output: prefer 'labs' on cross-track URLs.
    for item in accepted:
        tracks = item.get("tracks")
        if tracks:
            item["track"] = "labs" if "labs" in tracks else tracks[0]

    accepted_by_track = {
        "general": sum(1 for it in accepted if it.get("track") == "general"),
        "labs":    sum(1 for it in accepted if it.get("track") == "labs"),
    }
    print(f"[EVAL] By track — general: {accepted_by_track['general']}, "
          f"labs: {accepted_by_track['labs']}\n")

    # ── Step 7: Write to Notion ───────────────────────────────────
    written = 0
    if accepted and not args.dry_run:
        written = batch_write(accepted)
    elif args.dry_run:
        print("[DRY RUN] Skipping Notion write. Accepted items:")
        for item in accepted:
            print(f"  [{item['score']}/10] ({item.get('track','general')}) {item['name']} "
                  f"— {item.get('deadline','no deadline')} — {item.get('url','')[:60]}")

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
        "by_track":    accepted_by_track,
    }
    save_log(log)

    print(f"\n{'='*60}")
    print(f"Done: {written} new opportunities written to Notion")
    print(f"Run summary: {json.dumps(log)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()