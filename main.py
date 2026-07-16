"""
main.py — OpTrack v2 (snippet-first pipeline)

Usage:
  python main.py              # auto daily vs weekly
  python main.py --daily
  python main.py --weekly
  python main.py --reval        # retry failed / queued URLs from SQLite
  python main.py --dry-run
  python main.py --min-score 6  # default 6
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from core.evaluator import enrich_batch, snippet_batch_eval
from core.notion_writer import batch_write
from core.prefilter import prefilter
from core.deduper import clean_url
from core.query_builder import build_queries
from core.snippet_paths import tag_snippet_only
from core import store
from scrapers.page_scraper import batch_scrape
from scrapers.search_engine import batch_search


def parse_args():
    parser = argparse.ArgumentParser(description="OpTrack v2 — opportunity hunter")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--daily", action="store_true", help="Light scan (priority queries)")
    group.add_argument("--weekly", action="store_true", help="Deep scan (all queries)")
    group.add_argument("--reval", action="store_true",
                       help="Re-evaluate queued URLs from SQLite (no Serper)")
    parser.add_argument("--reval-limit", type=int, default=0,
                        help="Max URLs to re-eval (0 = all queued)")
    parser.add_argument("--max-eval", type=int, default=0,
                        help="Max snippet rows per eval pass (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Notion")
    parser.add_argument("--min-score", type=int, default=6,
                        help="Minimum score to save (default: 6)")
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


def _normalize_tracks(items: list[dict]) -> None:
    for item in items:
        tracks = item.get("tracks")
        if tracks:
            item["track"] = "labs" if "labs" in tracks else tracks[0]


def _write_and_mark(accepted: list[dict], dry_run: bool) -> int:
    if not accepted:
        return 0
    _normalize_tracks(accepted)
    written = 0
    if dry_run:
        print("[DRY RUN] Would write:")
        for item in accepted:
            print(f"  [{item['score']}/10] ({item.get('track')}) {item['name'][:50]}")
        written = len(accepted)
    else:
        written = batch_write(accepted)
    for item in accepted:
        store.mark_status(
            item["url"],
            "written",
            name=item.get("name", ""),
            opp_type=item.get("type", ""),
            deadline=item.get("deadline") or "",
            region=item.get("region", ""),
            snippet_score=item.get("snippet_score"),
            full_score=item.get("full_score") or item.get("score"),
        )
    return written


def _persist_snippet_results(
    snippet_accepts: list[dict],
    scrape_queue: list[dict],
    rejected_urls: list[str],
) -> None:
    for item in snippet_accepts:
        store.mark_status(
            item["url"],
            "snippet_scored",
            snippet_only=1,
            snippet_score=item.get("snippet_score"),
            name=item.get("name", ""),
            opp_type=item.get("type", ""),
            deadline=item.get("deadline") or "",
            region=item.get("region", ""),
        )
    for item in scrape_queue:
        store.mark_status(
            item["url"],
            "scrape_queued",
            snippet_score=item.get("snippet_score"),
            snippet_only=0,
        )
    for url in rejected_urls:
        store.mark_status(url, "rejected_snippet")


def _scrape_and_enrich(
    scrape_queue: list[dict],
    min_score: int,
) -> tuple[list[dict], list[dict]]:
    if not scrape_queue:
        return [], []
    scraped = batch_scrape(scrape_queue, delay=0.5)
    for item in scraped:
        store.update_row(
            item["url"],
            scraped_title=item.get("scraped_title", ""),
            scraped_body=item.get("scraped_body", ""),
            scrape_error=item.get("scrape_error") or "",
            status="scraped",
        )
    enriched, failed = enrich_batch(scraped, min_score=min_score)
    for item in failed:
        if item.get("scrape_error"):
            store.mark_status(item["url"], "rejected_full")
        else:
            store.mark_status(item["url"], "scrape_queued")
    return enriched, failed


def run_pipeline(candidates: list[dict], args, mode: str) -> dict:
    """Snippet-first eval for a list of search-result dicts."""
    if not candidates:
        return {"accepted": 0, "written": 0, "prefiltered": 0}

    tag_snippet_only(candidates)
    for item in candidates:
        store.update_row(
            item["url"],
            snippet_only=item.get("snippet_only", False),
        )

    snippet_accepts, scrape_queue, failed = snippet_batch_eval(
        candidates,
        min_score=args.min_score,
        max_eval=args.max_eval,
    )

    rejected_urls = []
    accepted_urls = {i["url"] for i in snippet_accepts + scrape_queue}
    failed_urls = {f["url"] for f in failed}
    for item in candidates:
        url = item["url"]
        if url in accepted_urls or url in failed_urls:
            continue
        rejected_urls.append(url)
    _persist_snippet_results(snippet_accepts, scrape_queue, rejected_urls)

    for item in failed:
        store.mark_status(item["url"], "discovered")

    enriched, _ = _scrape_and_enrich(scrape_queue, args.min_score)
    all_accepted = snippet_accepts + enriched
    written = _write_and_mark(all_accepted, args.dry_run)

    return {
        "mode": mode,
        "prefiltered": len(candidates),
        "snippet_only": len(snippet_accepts),
        "scraped": len(scrape_queue),
        "accepted": len(all_accepted),
        "written": written,
        "failed_eval": len(failed),
    }


def run_reval(args) -> None:
    rows = store.get_reval_candidates()
    if args.reval_limit and args.reval_limit > 0:
        rows = rows[: args.reval_limit]

    if not rows:
        print("No queued URLs in database.")
        sys.exit(0)

    candidates = [store.item_from_row(r) for r in rows]
    print(f"\n{'='*60}")
    print(f"OpTrack v2 — REVAL — {date.today()}")
    print(f"Re-evaluating {len(candidates)} queued URLs")
    print(f"{'='*60}\n")

    stats = run_pipeline(candidates, args, "reval")
    save_log({"date": str(date.today()), **stats})
    print(f"\nDone: {stats['written']} written\n")


def main():
    args = parse_args()
    store.init_db()

    if args.reval:
        run_reval(args)
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

    queries = build_queries(mode=mode)
    n_general = sum(1 for q in queries if q.get("track") == "general")
    n_labs = sum(1 for q in queries if q.get("track") == "labs")
    print(f"[QUERIES] Built {len(queries)} search queries "
          f"({n_general} general, {n_labs} labs)\n")

    raw_results = batch_search(queries, num_results=10)
    print(f"\n[SEARCH] {len(raw_results)} unique URLs found\n")
    if not raw_results:
        print("No results from search. Check SERPER_API_KEY.")
        save_log({"date": str(date.today()), "mode": mode, "raw": 0, "written": 0})
        sys.exit(0)

    new_results = store.filter_new(raw_results)
    inserted = len(new_results)
    print(f"[DEDUP] {inserted} new URLs (not in database)\n")

    if not new_results:
        print("No new URLs this run.")
        save_log({
            "date": str(date.today()), "mode": mode,
            "raw": len(raw_results), "new": 0, "written": 0,
        })
        sys.exit(0)

    candidates = prefilter(new_results)
    print(f"\n[PREFILTER] {len(candidates)} candidates remain\n")

    for item in new_results:
        url = clean_url(item.get("url", ""))
        if not any(clean_url(c.get("url", "")) == url for c in candidates):
            store.mark_status(url, "rejected_prefilter")

    if not candidates:
        save_log({
            "date": str(date.today()), "mode": mode,
            "raw": len(raw_results), "new": len(new_results),
            "prefiltered": 0, "written": 0,
        })
        sys.exit(0)

    stats = run_pipeline(candidates, args, mode)
    log = {
        "date": str(date.today()),
        "raw": len(raw_results),
        "new": len(new_results),
        **stats,
    }
    save_log(log)

    print(f"\n{'='*60}")
    print(f"Done: {stats['written']} new opportunities written to Notion")
    print(f"Run summary: {json.dumps(log)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
