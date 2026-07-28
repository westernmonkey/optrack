"""
store.py — SQLite URL state machine for OpTrack.

Tracks every discovered URL through deterministic and LLM stages.
Retry states are distinct from terminal rejections.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.deduper import clean_url

DB_PATH = Path("data/optrack.db")
SEEN_JSON_PATH = Path("data/seen_urls.json")
MIGRATION_FLAG = Path("data/.seen_urls_migrated")

STATUSES = frozenset({
    "discovered",
    "rejected_prefilter",
    "snippet_scored",
    "rejected_snippet",
    "eval_retry",
    "scrape_queued",
    "scrape_retry",
    "scraped",
    "rejected_full",
    "notion_retry",
    "written",
})

TERMINAL_STATUSES = frozenset({
    "rejected_prefilter",
    "rejected_snippet",
    "rejected_full",
    "written",
})

RETRY_STATUSES = frozenset({
    "discovered",
    "eval_retry",
    "scrape_queued",
    "scrape_retry",
    "snippet_scored",
    "notion_retry",
})

# Allowed columns for dynamic UPDATE
_UPDATABLE = frozenset({
    "status", "title", "snippet", "source_query", "track", "tracks_json",
    "snippet_score", "full_score", "opp_type", "deadline", "region", "name",
    "scraped_title", "scraped_body", "scrape_error", "snippet_only",
    "rejection_reason", "attempt_count", "last_error", "last_model",
    "notion_page_id", "last_updated",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
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
                rejection_reason TEXT DEFAULT '',
                attempt_count INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                last_model TEXT DEFAULT '',
                notion_page_id TEXT DEFAULT '',
                first_seen TEXT NOT NULL,
                last_updated TEXT NOT NULL
            )
        """)
        # Migrate older schemas: add missing columns
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(urls)").fetchall()
        }
        for col, decl in [
            ("rejection_reason", "TEXT DEFAULT ''"),
            ("attempt_count", "INTEGER DEFAULT 0"),
            ("last_error", "TEXT DEFAULT ''"),
            ("last_model", "TEXT DEFAULT ''"),
            ("notion_page_id", "TEXT DEFAULT ''"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE urls ADD COLUMN {col} {decl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(status)"
        )
        conn.commit()
    migrate_seen_json(db_path)


def migrate_seen_json(db_path: Path | None = None) -> None:
    """
    One-time import of legacy seen_urls.json.
    Historical URLs are marked discovered (not written) — we cannot infer
    successful Notion writes from a bare URL list.
    """
    if MIGRATION_FLAG.exists():
        return
    if not SEEN_JSON_PATH.exists():
        MIGRATION_FLAG.parent.mkdir(parents=True, exist_ok=True)
        MIGRATION_FLAG.write_text(_now())
        return

    try:
        seen = json.loads(SEEN_JSON_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(seen, list):
        return

    now = _now()
    inserted = 0
    with _connect(db_path) as conn:
        for raw_url in seen:
            url = clean_url(str(raw_url))
            if not url:
                continue
            cur = conn.execute(
                """
                INSERT INTO urls (url, status, first_seen, last_updated,
                                  rejection_reason)
                VALUES (?, 'discovered', ?, ?, 'migrated_from_seen_urls')
                ON CONFLICT(url) DO NOTHING
                """,
                (url, now, now),
            )
            inserted += cur.rowcount
        conn.commit()

    MIGRATION_FLAG.parent.mkdir(parents=True, exist_ok=True)
    MIGRATION_FLAG.write_text(_now())
    if inserted:
        print(f"[STORE] Migrated {inserted} URLs from seen_urls.json as discovered")


def _row_to_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    tracks = item.pop("tracks_json", "[]") or "[]"
    try:
        item["tracks"] = json.loads(tracks)
    except json.JSONDecodeError:
        item["tracks"] = []
    item["snippet_only"] = bool(item.get("snippet_only"))
    return item


def upsert_discovered(items: list[dict], db_path: Path | None = None) -> int:
    """
    Insert new hits; on rediscovery, refresh metadata/tracks without
    resetting terminal statuses.
    Returns count of newly inserted rows.
    """
    now = _now()
    inserted = 0
    with _connect(db_path) as conn:
        for item in items:
            url = clean_url(item.get("url", ""))
            if not url:
                continue
            tracks = item.get("tracks") or [item.get("track", "general")]
            track = "labs" if "labs" in tracks else tracks[0]
            title = item.get("title", "") or ""
            snippet = item.get("snippet", "") or ""
            source_query = item.get("source_query", "") or ""

            existing = conn.execute(
                "SELECT status, tracks_json, title, snippet, source_query FROM urls WHERE url = ?",
                (url,),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO urls (
                        url, status, title, snippet, source_query, track,
                        tracks_json, first_seen, last_updated
                    ) VALUES (?, 'discovered', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (url, title, snippet, source_query, track,
                     json.dumps(tracks), now, now),
                )
                inserted += 1
                continue

            # Merge tracks / refresh metadata; keep status
            try:
                old_tracks = json.loads(existing["tracks_json"] or "[]")
            except json.JSONDecodeError:
                old_tracks = []
            merged = list(dict.fromkeys(old_tracks + list(tracks)))
            new_track = "labs" if "labs" in merged else (merged[0] if merged else track)
            conn.execute(
                """
                UPDATE urls SET
                    title = CASE WHEN ? != '' THEN ? ELSE title END,
                    snippet = CASE WHEN ? != '' THEN ? ELSE snippet END,
                    source_query = CASE WHEN ? != '' THEN ? ELSE source_query END,
                    track = ?,
                    tracks_json = ?,
                    last_updated = ?
                WHERE url = ?
                """,
                (
                    title, title, snippet, snippet, source_query, source_query,
                    new_track, json.dumps(merged), now, url,
                ),
            )
        conn.commit()
    return inserted


def filter_new(items: list[dict], db_path: Path | None = None) -> list[dict]:
    """Return items whose URL is not already in the DB (indexed lookup)."""
    if not items:
        return []
    urls = [clean_url(i.get("url", "")) for i in items]
    urls = [u for u in urls if u]
    if not urls:
        return []

    existing: set[str] = set()
    with _connect(db_path) as conn:
        # Chunk to stay under SQLite variable limits
        chunk = 500
        for i in range(0, len(urls), chunk):
            part = urls[i:i + chunk]
            placeholders = ",".join("?" * len(part))
            rows = conn.execute(
                f"SELECT url FROM urls WHERE url IN ({placeholders})",
                part,
            ).fetchall()
            existing.update(r[0] for r in rows)

    out = []
    for item in items:
        url = clean_url(item.get("url", ""))
        if url and url not in existing:
            out.append(item)
    return out


def url_exists(url: str, db_path: Path | None = None) -> bool:
    url = clean_url(url)
    if not url:
        return False
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM urls WHERE url = ? LIMIT 1", (url,)
        ).fetchone()
    return row is not None


def get_by_status(
    statuses: list[str],
    limit: int = 0,
    db_path: Path | None = None,
) -> list[dict]:
    if not statuses:
        return []
    placeholders = ",".join("?" * len(statuses))
    sql = (
        f"SELECT * FROM urls WHERE status IN ({placeholders}) "
        f"ORDER BY first_seen"
    )
    params: list = list(statuses)
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_item(r) for r in rows]


def get_reval_candidates(limit: int = 0, db_path: Path | None = None) -> list[dict]:
    """Retryable failures / unfinished work — not terminal rejects."""
    return get_by_status(list(RETRY_STATUSES), limit=limit, db_path=db_path)


def get_by_stage(
    stage: str,
    limit: int = 0,
    db_path: Path | None = None,
) -> list[dict]:
    """Stage-aware reval targeting."""
    mapping = {
        "snippet": ["discovered", "eval_retry"],
        "scrape": ["scrape_queued", "scrape_retry", "snippet_scored"],
        "notion": ["notion_retry"],
        "all": list(RETRY_STATUSES),
    }
    statuses = mapping.get(stage, list(RETRY_STATUSES))
    return get_by_status(statuses, limit=limit, db_path=db_path)


def update_row(url: str, db_path: Path | None = None, **fields) -> None:
    url = clean_url(url)
    if not url or not fields:
        return
    fields["last_updated"] = _now()
    if "tracks" in fields:
        fields["tracks_json"] = json.dumps(fields.pop("tracks"))
    if "snippet_only" in fields:
        fields["snippet_only"] = 1 if fields["snippet_only"] else 0

    safe = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not safe:
        return
    cols = ", ".join(f"{k} = ?" for k in safe)
    vals = list(safe.values()) + [url]
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE urls SET {cols} WHERE url = ?", vals)
        conn.commit()


def mark_status(
    url: str,
    status: str,
    db_path: Path | None = None,
    *,
    bump_attempt: bool = False,
    **extra,
) -> None:
    if status not in STATUSES:
        raise ValueError(f"Invalid status: {status}")
    url = clean_url(url)
    if not url:
        return
    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO urls (url, status, first_seen, last_updated)
            VALUES (?, 'discovered', ?, ?)
            ON CONFLICT(url) DO NOTHING
            """,
            (url, now, now),
        )
        if bump_attempt:
            conn.execute(
                "UPDATE urls SET attempt_count = attempt_count + 1 WHERE url = ?",
                (url,),
            )
        conn.commit()
    update_row(url, db_path=db_path, status=status, **extra)


def mark_retry(
    url: str,
    status: str,
    error: str,
    *,
    model: str = "",
    db_path: Path | None = None,
) -> None:
    if status not in ("eval_retry", "scrape_retry", "notion_retry", "discovered"):
        raise ValueError(f"Not a retry status: {status}")
    mark_status(
        url,
        status,
        db_path=db_path,
        bump_attempt=True,
        last_error=(error or "")[:500],
        last_model=model or "",
    )


def mark_rejected(
    url: str,
    status: str,
    reason: str,
    db_path: Path | None = None,
    **extra,
) -> None:
    if status not in ("rejected_prefilter", "rejected_snippet", "rejected_full"):
        raise ValueError(f"Not a rejection status: {status}")
    mark_status(
        url,
        status,
        db_path=db_path,
        rejection_reason=(reason or "")[:500],
        **extra,
    )


def mark_many_status(
    urls: list[str],
    status: str,
    db_path: Path | None = None,
    **extra,
) -> None:
    for url in urls:
        mark_status(url, status, db_path=db_path, **extra)


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
        "rejection_reason": row.get("rejection_reason", ""),
        "attempt_count": row.get("attempt_count", 0),
        "last_error": row.get("last_error", ""),
        "last_model": row.get("last_model", ""),
        "notion_page_id": row.get("notion_page_id", ""),
        "status": row.get("status", ""),
    }


def count_by_status(db_path: Path | None = None) -> dict[str, int]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM urls GROUP BY status"
        ).fetchall()
    return {row[0]: row[1] for row in rows}
