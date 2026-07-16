"""
store.py — SQLite URL state for OpTrack v2.

Replaces seen_urls.json, pending_eval.json, and skipped_eval_urls.json.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.deduper import clean_url

DB_PATH = Path("data/optrack.db")
SEEN_JSON_PATH = Path("data/seen_urls.json")

STATUSES = frozenset({
    "discovered",
    "rejected_prefilter",
    "snippet_scored",
    "rejected_snippet",
    "scrape_queued",
    "scraped",
    "written",
    "rejected_full",
})

TERMINAL_STATUSES = frozenset({
    "rejected_prefilter",
    "rejected_snippet",
    "written",
    "rejected_full",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                url TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'discovered',
                title TEXT DEFAULT '',
                snippet TEXT DEFAULT '',
                source_query TEXT DEFAULT '',
                track TEXT DEFAULT 'general',
                tracks_json TEXT DEFAULT '[]',
                snippet_score INTEGER,
                full_score INTEGER,
                opp_type TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                region TEXT DEFAULT '',
                name TEXT DEFAULT '',
                scraped_title TEXT DEFAULT '',
                scraped_body TEXT DEFAULT '',
                scrape_error TEXT DEFAULT '',
                snippet_only INTEGER DEFAULT 0,
                first_seen TEXT NOT NULL,
                last_updated TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(status)"
        )
        conn.commit()
    migrate_seen_json()


def migrate_seen_json() -> None:
    if not SEEN_JSON_PATH.exists():
        return
    try:
        seen = json.loads(SEEN_JSON_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(seen, list) or not seen:
        return

    now = _now()
    with _connect() as conn:
        for raw_url in seen:
            url = clean_url(str(raw_url))
            if not url:
                continue
            conn.execute(
                """
                INSERT INTO urls (url, status, first_seen, last_updated)
                VALUES (?, 'written', ?, ?)
                ON CONFLICT(url) DO NOTHING
                """,
                (url, now, now),
            )
        conn.commit()
    print(f"[STORE] Migrated {len(seen)} URLs from seen_urls.json")


def _row_to_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    tracks = item.pop("tracks_json", "[]") or "[]"
    try:
        item["tracks"] = json.loads(tracks)
    except json.JSONDecodeError:
        item["tracks"] = []
    item["snippet_only"] = bool(item.get("snippet_only"))
    return item


def upsert_discovered(items: list[dict]) -> int:
    """Insert new search hits. Returns count of newly inserted rows."""
    now = _now()
    inserted = 0
    with _connect() as conn:
        for item in items:
            url = clean_url(item.get("url", ""))
            if not url:
                continue
            tracks = item.get("tracks") or [item.get("track", "general")]
            track = "labs" if "labs" in tracks else tracks[0]
            cur = conn.execute(
                """
                INSERT INTO urls (
                    url, status, title, snippet, source_query, track,
                    tracks_json, first_seen, last_updated
                ) VALUES (?, 'discovered', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO NOTHING
                """,
                (
                    url,
                    item.get("title", ""),
                    item.get("snippet", ""),
                    item.get("source_query", ""),
                    track,
                    json.dumps(tracks),
                    now,
                    now,
                ),
            )
            inserted += cur.rowcount
        conn.commit()
    return inserted


def filter_new(items: list[dict]) -> list[dict]:
    """Return items whose URL is not already in the DB."""
    if not items:
        return []
    with _connect() as conn:
        existing = {
            row[0]
            for row in conn.execute("SELECT url FROM urls").fetchall()
        }
    out = []
    for item in items:
        url = clean_url(item.get("url", ""))
        if url and url not in existing:
            out.append(item)
    return out


def get_by_status(statuses: list[str]) -> list[dict]:
    if not statuses:
        return []
    placeholders = ",".join("?" * len(statuses))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM urls WHERE status IN ({placeholders}) ORDER BY first_seen",
            statuses,
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def get_reval_candidates() -> list[dict]:
    """URLs that failed eval or need retry."""
    return get_by_status(["discovered", "scrape_queued", "snippet_scored"])


def update_row(url: str, **fields) -> None:
    url = clean_url(url)
    if not url or not fields:
        return
    fields["last_updated"] = _now()
    if "tracks" in fields:
        fields["tracks_json"] = json.dumps(fields.pop("tracks"))
    if "snippet_only" in fields:
        fields["snippet_only"] = 1 if fields["snippet_only"] else 0
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [url]
    with _connect() as conn:
        conn.execute(f"UPDATE urls SET {cols} WHERE url = ?", vals)
        conn.commit()


def mark_status(url: str, status: str, **extra) -> None:
    if status not in STATUSES:
        raise ValueError(f"Invalid status: {status}")
    update_row(url, status=status, **extra)


def mark_many_status(urls: list[str], status: str, **extra) -> None:
    for url in urls:
        mark_status(url, status, **extra)


def item_from_row(row: dict) -> dict:
    """Normalize DB row to pipeline item dict."""
    tracks = row.get("tracks") or []
    return {
        "url": row["url"],
        "title": row.get("title", ""),
        "snippet": row.get("snippet", ""),
        "source_query": row.get("source_query", ""),
        "track": row.get("track", "general"),
        "tracks": tracks,
        "snippet_only": row.get("snippet_only", False),
        "snippet_score": row.get("snippet_score"),
        "full_score": row.get("full_score"),
        "scraped_title": row.get("scraped_title", ""),
        "scraped_body": row.get("scraped_body", ""),
        "scrape_error": row.get("scrape_error", ""),
        "name": row.get("name", ""),
        "type": row.get("opp_type", ""),
        "deadline": row.get("deadline", ""),
        "region": row.get("region", ""),
        "score": row.get("full_score") or row.get("snippet_score"),
    }


def count_by_status() -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM urls GROUP BY status"
        ).fetchall()
    return {row[0]: row[1] for row in rows}
