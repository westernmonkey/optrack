"""
main.py — OpTrack v2 (snippet-first pipeline)

Usage:
  python main.py              # auto daily vs weekly
  python main.py --daily
  python main.py --weekly
  python main.py --reval
  python main.py --dry-run
  python main.py --min-score 6
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from core.deduper import clean_url
from core.evaluator import (
    collect_snippet_rejects,
    enrich_batch,
    remaining_daily_budget,
    snippet_batch_eval,
)
from core.filter_policy import MIN_SCORE
from core.notion_writer import batch_write
from core.prefilter import prefilter
from core.query_builder import build_queries
from core.snippet_paths import tag_snippet_only
from core import store
from scrapers.page_scraper import batch_scrape
from scrapers.search_engine import SerperFatalError, batch_search


def parse_args():
    parser = argparse.ArgumentParser(description="OpTrack v2 — opportunity hunter")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--daily", action="store_true", help="Light scan")
    group.add_argument("--weekly", action="store_true", help="Deep scan")
    group.add_argument(
        "--reval",
        action="store_true",
        help="Retry queued / retryable URLs from SQLite (no Serper)",
    )
    parser.add_argument(
        "--reval-limit",
        type=int,
        default=0,
        help="Max URLs to re-eval (0 = all queued)",
    )
    parser.add_argument(
        "--reval-stage",
        choices=["all", "snippet", "scrape", "notion"],
        default="all",
        help="Which retry stage to process",
    )
    parser.add_argument(
        "--max-eval",
        type=int,
        default=0,
        help="Max snippet rows per eval pass (0 = all)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Notion")
    parser.add_argument(
        "--min-score",
        type=int,
        default=MIN_SCORE,
        help=f"Minimum score to save (default: {MIN_SCORE})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every search query",
    )
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


def _write_and_mark(accepted: list[dict], dry_run: bool) -> dict:
    """
    Write accepted items. Dry-run never mutates terminal state.
    Returns counts: written, notion_retry, dry_run.
    """
    if not accepted:
        return {"written": 0, "notion_retry": 0, "dry_run": 0}

    _normalize_tracks(accepted)

    if dry_run:
        print("[DRY RUN] Would write:")
        for item in accepted:
            print(
                f"  [{item['score']}/10] ({item.get('track')}) "
                f"{item['name'][:50]}"
            )
        return {"written": 0, "notion_retry": 0, "dry_run": len(accepted)}

    results = batch_write(accepted)
    written = 0
    notion_retry = 0
    by_url = {item["url"]: item for item in accepted}

    for result in results:
        url = result["url"]
        item = by_url.get(url, {})
        if result["ok"]:
            store.mark_status(
                url,
                "written",
                name=item.get("name", ""),
                opp_type=item.get("type", ""),
                deadline=item.get("deadline") or "",
                region=item.get("region", ""),
                snippet_score=item.get("snippet_score"),
                full_score=item.get("full_score") or item.get("score"),
                notion_page_id=result.get("page_id") or "",
                last_error="",
            )
            written += 1
        else:
            store.mark_retry(
                url,
                "notion_retry",
                result.get("error") or "notion_write_failed",
            )
            notion_retry += 1

    return {"written": written, "notion_retry": notion_retry, "dry_run": 0}


def _persist_snippet_results(
    candidates: list[dict],
    snippet_accepts: list[dict],
    scrape_queue: list[dict],
    retryable: list[dict],
) -> None:
    accept_urls = {i["url"] for i in snippet_accepts}
    scrape_urls = {i["url"] for i in scrape_queue}
    retry_urls = {i["url"] for i in retryable}

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
            last_model=item.get("last_model", ""),
        )

    for item in scrape_queue:
        store.mark_status(
            item["url"],
            "scrape_queued",
            snippet_score=item.get("snippet_score"),
            snippet_only=0,
            last_model=item.get("last_model", ""),
        )

    for item in retryable:
        store.mark_retry(
            item["url"],
            "eval_retry",
            item.get("last_error") or "eval_failed",
            model=item.get("last_model", ""),
        )

    for item in collect_snippet_rejects(candidates):
        url = item["url"]
        if url in accept_urls or url in scrape_urls or url in retry_urls:
            continue
        store.mark_rejected(
            url,
            "rejected_snippet",
            item.get("rejection_reason") or "rejected",
            snippet_score=item.get("snippet_score"),
        )


def _scrape_and_enrich(
    scrape_queue: list[dict],
    min_score: int,
) -> tuple[list[dict], dict]:
    if not scrape_queue:
        return [], {"scraped": 0, "accepted": 0, "rejected": 0, "retry": 0}

    scraped = batch_scrape(scrape_queue, delay=0.5)
    for item in scraped:
        store.update_row(
            item["url"],
            scraped_title=item.get("scraped_title", ""),
            scraped_body=item.get("scraped_body", ""),
            scrape_error=item.get("scrape_error") or "",
            status="scraped",
        )

    enriched, rejects, retryable = enrich_batch(scraped, min_score=min_score)

    for item in rejects:
        store.mark_rejected(
            item["url"],
            "rejected_full",
            item.get("rejection_reason") or "rejected",
            full_score=item.get("full_score"),
            snippet_score=item.get("snippet_score"),
        )

    for item in retryable:
        err = item.get("last_error") or item.get("scrape_error") or "enrich_failed"
        if item.get("scrape_error") and item.get("scrape_retryable", True):
            store.mark_retry(item["url"], "scrape_retry", f"scrape:{err}")
        elif item.get("scrape_error"):
            store.mark_rejected(
                item["url"],
                "rejected_full",
                f"scrape:{item.get('scrape_error')}",
            )
        else:
            store.mark_retry(
                item["url"],
                "scrape_retry",
                err,
                model=item.get("last_model", ""),
            )

    return enriched, {
        "scraped": len(scraped),
        "accepted": len(enriched),
        "rejected": len(rejects),
        "retry": len(retryable),
    }


def run_pipeline(candidates: list[dict], args, mode: str) -> dict:
    if not candidates:
        return {
            "mode": mode,
            "prefiltered": 0,
            "snippet_only": 0,
            "scraped": 0,
            "accepted": 0,
            "written": 0,
            "notion_retry": 0,
            "failed_eval": 0,
            "openrouter_budget": remaining_daily_budget(),
        }

    tag_snippet_only(candidates)
    for item in candidates:
        store.update_row(
            item["url"],
            snippet_only=item.get("snippet_only", False),
        )

    snippet_accepts, scrape_queue, retryable = snippet_batch_eval(
        candidates,
        min_score=args.min_score,
        max_eval=args.max_eval,
    )
    _persist_snippet_results(candidates, snippet_accepts, scrape_queue, retryable)

    enriched, scrape_stats = _scrape_and_enrich(scrape_queue, args.min_score)
    all_accepted = snippet_accepts + enriched
    write_stats = _write_and_mark(all_accepted, args.dry_run)

    return {
        "mode": mode,
        "prefiltered": len(candidates),
        "snippet_only": len(snippet_accepts),
        "scraped": scrape_stats["scraped"],
        "accepted": len(all_accepted),
        "written": write_stats["written"],
        "notion_retry": write_stats["notion_retry"],
        "dry_run_count": write_stats["dry_run"],
        "failed_eval": len(retryable) + scrape_stats["retry"],
        "openrouter_budget": remaining_daily_budget(),
    }


def _retry_notion_only(rows: list[dict], dry_run: bool) -> dict:
    items = []
    for row in rows:
        item = store.item_from_row(row)
        if not item.get("name"):
            item["name"] = item.get("title") or "Unnamed"
        if item.get("score") is None:
            item["score"] = item.get("full_score") or item.get("snippet_score") or 6
        items.append(item)
    return _write_and_mark(items, dry_run)


def run_reval(args) -> None:
    rows = store.get_by_stage(args.reval_stage, limit=args.reval_limit)
    if not rows:
        print("No queued / retryable URLs in database.")
        sys.exit(0)

    print(f"\n{'=' * 60}")
    print(f"OpTrack v2 — REVAL ({args.reval_stage}) — {date.today()}")
    print(f"Processing {len(rows)} URLs")
    print(f"{'=' * 60}\n")

    if args.reval_stage == "notion":
        write_stats = _retry_notion_only(rows, args.dry_run)
        stats = {
            "mode": "reval",
            "stage": "notion",
            "prefiltered": len(rows),
            "accepted": len(rows),
            **write_stats,
        }
        save_log({"date": str(date.today()), **stats})
        print(f"\nDone: {write_stats['written']} written\n")
        return

    # Stage-aware: scrape stage skips snippet eval for scrape_queued/retry
    if args.reval_stage == "scrape":
        scrape_items = []
        snippet_items = []
        for row in rows:
            item = store.item_from_row(row)
            if row.get("status") in ("scrape_queued", "scrape_retry", "scraped"):
                scrape_items.append(item)
            else:
                snippet_items.append(item)

        stats = {
            "mode": "reval",
            "stage": "scrape",
            "prefiltered": len(rows),
            "snippet_only": 0,
            "accepted": 0,
            "written": 0,
            "notion_retry": 0,
            "failed_eval": 0,
        }
        if snippet_items:
            part = run_pipeline(snippet_items, args, "reval")
            for k, v in part.items():
                if isinstance(v, int) and k in stats:
                    stats[k] = stats.get(k, 0) + v
        if scrape_items:
            enriched, scrape_stats = _scrape_and_enrich(scrape_items, args.min_score)
            write_stats = _write_and_mark(enriched, args.dry_run)
            stats["scraped"] = scrape_stats["scraped"]
            stats["accepted"] = stats.get("accepted", 0) + len(enriched)
            stats["written"] += write_stats["written"]
            stats["notion_retry"] += write_stats["notion_retry"]
            stats["failed_eval"] += scrape_stats["retry"]
        save_log({"date": str(date.today()), **stats})
        print(f"\nDone: {stats['written']} written\n")
        return

    candidates = [store.item_from_row(r) for r in rows]
    # Notion-retry rows: only write
    notion_rows = [c for c in candidates if c.get("status") == "notion_retry"]
    other = [c for c in candidates if c.get("status") != "notion_retry"]

    stats = {
        "mode": "reval",
        "stage": args.reval_stage,
        "prefiltered": 0,
        "snippet_only": 0,
        "scraped": 0,
        "accepted": 0,
        "written": 0,
        "notion_retry": 0,
        "failed_eval": 0,
    }
    if other:
        part = run_pipeline(other, args, "reval")
        stats.update({k: part.get(k, stats.get(k)) for k in stats})
        stats["mode"] = "reval"
    if notion_rows:
        write_stats = _retry_notion_only(notion_rows, args.dry_run)
        stats["written"] = stats.get("written", 0) + write_stats["written"]
        stats["notion_retry"] = (
            stats.get("notion_retry", 0) + write_stats["notion_retry"]
        )

    save_log({"date": str(date.today()), **stats})
    print(f"\nDone: {stats.get('written', 0)} written\n")


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

    print(f"\n{'=' * 60}")
    print(f"OpTrack v2 — {mode.upper()} scan — {date.today()}")
    print(f"{'=' * 60}\n")

    queries = build_queries(mode=mode)
    n_general = sum(1 for q in queries if q.get("track") == "general")
    n_labs = sum(1 for q in queries if q.get("track") == "labs")
    n_wi = sum(1 for q in queries if q.get("track") == "wi_events")
    print(
        f"[QUERIES] Built {len(queries)} search queries "
        f"({n_general} general, {n_labs} labs, {n_wi} wi_events)\n"
    )

    try:
        raw_results, search_stats = batch_search(
            queries, num_results=10, verbose=args.verbose
        )
    except SerperFatalError as e:
        print(f"[SEARCH] Aborting run: {e}")
        save_log({
            "date": str(date.today()),
            "mode": mode,
            "raw": 0,
            "written": 0,
            "error": str(e),
            "search": {"fatal": True},
        })
        sys.exit(1)

    print(f"\n[SEARCH] {len(raw_results)} unique URLs found\n")
    if not raw_results:
        print("No results from search.")
        save_log({
            "date": str(date.today()),
            "mode": mode,
            "raw": 0,
            "written": 0,
            "search": search_stats,
        })
        sys.exit(0)

    # Upsert all discoveries (updates metadata on rediscovery)
    new_results = store.filter_new(raw_results)
    store.upsert_discovered(raw_results)
    print(f"[DEDUP] {len(new_results)} new URLs (not previously in database)\n")

    if not new_results:
        print("No new URLs this run.")
        save_log({
            "date": str(date.today()),
            "mode": mode,
            "raw": len(raw_results),
            "new": 0,
            "written": 0,
            "search": search_stats,
            "status_counts": store.count_by_status(),
        })
        sys.exit(0)

    candidates = prefilter(new_results)
    print(f"\n[PREFILTER] {len(candidates)} candidates remain\n")

    cand_urls = {clean_url(c.get("url", "")) for c in candidates}
    for item in new_results:
        url = clean_url(item.get("url", ""))
        if url not in cand_urls:
            from core.prefilter import is_junk
            _, reason = is_junk(item)
            store.mark_rejected(url, "rejected_prefilter", reason or "prefilter")

    if not candidates:
        save_log({
            "date": str(date.today()),
            "mode": mode,
            "raw": len(raw_results),
            "new": len(new_results),
            "prefiltered": 0,
            "written": 0,
            "search": search_stats,
            "status_counts": store.count_by_status(),
        })
        sys.exit(0)

    stats = run_pipeline(candidates, args, mode)
    log = {
        "date": str(date.today()),
        "raw": len(raw_results),
        "new": len(new_results),
        "search": search_stats,
        "status_counts": store.count_by_status(),
        **stats,
    }
    save_log(log)

    print(f"\n{'=' * 60}")
    print(f"Done: {stats['written']} opportunities written to Notion")
    if args.dry_run:
        print(f"Dry-run would-write: {stats.get('dry_run_count', 0)}")
    print(f"Status counts: {json.dumps(store.count_by_status())}")
    print(f"Run summary: {json.dumps({k: v for k, v in log.items() if k != 'status_counts'})}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
