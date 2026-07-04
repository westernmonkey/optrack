import os
import time
import requests
from datetime import date

TOKEN = os.environ["NOTION_TOKEN"]
DB_ID = os.environ["NOTION_DB_ID"]
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}


def batch_write(items):
    success = 0
    for i, item in enumerate(items, 1):
        name = item.get("name", "Unnamed")[:50]
        print(f"[NOTION {i}/{len(items)}] Writing: {name}")
        payload = {
            "parent": {"database_id": DB_ID},
            "properties": {
                "Title":    {"title": [{"text": {"content": str(item.get("name", ""))[:200]}}]},
                "URL":      {"url": item.get("url") or None},
                "Category": {"multi_select": [{"name": str(item.get("type", "Other"))[:100]}]},
                "Region":   {"select": {"name": str(item.get("region", "Global"))[:100]}},
                "Deadline": {"rich_text": [{"text": {"content": str(item.get("deadline") or "TBD")[:200]}}]},
                "Source":   {"rich_text": [{"text": {"content": str(item.get("source_query", "optrack"))[:200]}}]},
                "Score":    {"number": int(item.get("score", 5))},
                "Found On": {"date": {"start": str(date.today())}},
                "Status":   {"select": {"name": "New"}}
            }
        }
        r = requests.post("https://api.notion.com/v1/pages", headers=HEADERS, json=payload)
        if r.status_code == 429:
            time.sleep(10)
            requests.post("https://api.notion.com/v1/pages", headers=HEADERS, json=payload)
        elif r.status_code not in (200, 201):
            print(f"  [NOTION ERR] {r.status_code}: {r.text[:200]}")
        else:
            success += 1
        time.sleep(0.4)
    print(f"[NOTION] {success}/{len(items)} written successfully.")
    return success