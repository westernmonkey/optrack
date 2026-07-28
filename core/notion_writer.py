"""
notion_writer.py — idempotent Notion writes with per-item outcomes.
"""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Any

import requests


def _credentials() -> tuple[str, str]:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_DB_ID", "").strip()
    if not token or not db_id:
        raise EnvironmentError(
            "NOTION_TOKEN and NOTION_DB_ID must be set to write to Notion."
        )
    return token, db_id


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def find_existing_page(url: str, *, token: str | None = None, db_id: str | None = None) -> str | None:
    """Return Notion page id if a row with this URL already exists."""
    if not url:
        return None
    if token is None or db_id is None:
        token, db_id = _credentials()
    payload = {
        "filter": {
            "property": "URL",
            "url": {"equals": url},
        },
        "page_size": 1,
    }
    try:
        r = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=_headers(token),
            json=payload,
            timeout=30,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    results = r.json().get("results") or []
    if not results:
        return None
    return results[0].get("id")


def _build_payload(item: dict, db_id: str) -> dict[str, Any]:
    return {
        "parent": {"database_id": db_id},
        "properties": {
            "Title": {
                "title": [
                    {"text": {"content": str(item.get("name", ""))[:200]}}
                ]
            },
            "URL": {"url": item.get("url") or None},
            "Category": {
                "multi_select": [{"name": str(item.get("type", "Other"))[:100]}]
            },
            "Region": {
                "select": {"name": str(item.get("region", "Global"))[:100]}
            },
            "Deadline": {
                "rich_text": [
                    {"text": {"content": str(item.get("deadline") or "TBD")[:200]}}
                ]
            },
            "Source": {
                "rich_text": [
                    {
                        "text": {
                            "content": str(item.get("source_query", "optrack"))[:200]
                        }
                    }
                ]
            },
            "Score": {"number": int(item.get("score", 5))},
            "Found On": {"date": {"start": str(date.today())}},
            "Status": {"select": {"name": "New"}},
        },
    }


def write_one(item: dict) -> dict:
    """
    Write a single opportunity.
    Returns {"ok": bool, "url": str, "page_id": str|None, "error": str, "existing": bool}
    """
    url = item.get("url", "")
    name = item.get("name", "Unnamed")[:50]
    try:
        token, db_id = _credentials()
    except EnvironmentError as e:
        return {
            "ok": False,
            "url": url,
            "page_id": None,
            "error": str(e),
            "existing": False,
        }

    existing_id = find_existing_page(url, token=token, db_id=db_id)
    if existing_id:
        print(f"[NOTION] Already exists: {name}")
        return {
            "ok": True,
            "url": url,
            "page_id": existing_id,
            "error": "",
            "existing": True,
        }

    payload = _build_payload(item, db_id)
    headers = _headers(token)

    def _post() -> requests.Response:
        return requests.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json=payload,
            timeout=30,
        )

    try:
        r = _post()
    except requests.Timeout:
        return {
            "ok": False, "url": url, "page_id": None,
            "error": "timeout", "existing": False,
        }
    except requests.RequestException as e:
        return {
            "ok": False, "url": url, "page_id": None,
            "error": f"request:{e}", "existing": False,
        }

    if r.status_code == 429:
        time.sleep(10)
        try:
            r = _post()
        except requests.RequestException as e:
            return {
                "ok": False, "url": url, "page_id": None,
                "error": f"retry_failed:{e}", "existing": False,
            }
        if r.status_code not in (200, 201):
            return {
                "ok": False, "url": url, "page_id": None,
                "error": f"429_retry:{r.status_code}", "existing": False,
            }

    if r.status_code not in (200, 201):
        err = f"{r.status_code}:{r.text[:200]}"
        print(f"  [NOTION ERR] {err}")
        return {
            "ok": False, "url": url, "page_id": None,
            "error": err, "existing": False,
        }

    page_id = r.json().get("id")
    print(f"[NOTION] Wrote: {name}")
    return {
        "ok": True,
        "url": url,
        "page_id": page_id,
        "error": "",
        "existing": False,
    }


def batch_write(items: list[dict]) -> list[dict]:
    """
    Write all items. Returns list of per-item result dicts.
    Does not raise on partial failure.
    """
    results = []
    for i, item in enumerate(items, 1):
        name = item.get("name", "Unnamed")[:50]
        print(f"[NOTION {i}/{len(items)}] {name}")
        result = write_one(item)
        results.append(result)
        time.sleep(0.4)
    ok = sum(1 for r in results if r["ok"])
    print(f"[NOTION] {ok}/{len(items)} succeeded.")
    return results
