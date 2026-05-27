"""
page_scraper.py
Fetches the full text of a URL for Claude to evaluate.
Uses requests + BeautifulSoup — no headless browser needed.
Handles 403s, timeouts, and bot-protection gracefully.
"""

import time
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Tags that are pure noise — remove before extracting text
NOISE_TAGS = ["script", "style", "nav", "footer", "header", "aside",
              "noscript", "iframe", "svg", "form", "button"]

MIN_BODY_LENGTH = 80   # chars — below this the page is useless
MAX_BODY_LENGTH = 4000 # chars — cap what we send to Claude


def scrape(url: str, retries: int = 1) -> dict:
    """
    Fetch and clean a page.
    Returns {title, body, error}.
    error is None on success, a string on failure.
    """
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
            if r.status_code == 403:
                # Fall back to snippet only — caller handles this
                return {"title": "", "body": "", "error": "403"}
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(NOISE_TAGS):
                tag.decompose()

            title = ""
            if soup.title and soup.title.string:
                title = soup.title.string.strip()[:300]

            body = " ".join(soup.get_text(separator=" ").split())[:MAX_BODY_LENGTH]

            if len(body) < MIN_BODY_LENGTH:
                return {"title": title, "body": "", "error": "empty_page"}

            return {"title": title, "body": body, "error": None}

        except requests.Timeout:
            if attempt < retries:
                time.sleep(2)
                continue
            return {"title": "", "body": "", "error": "timeout"}
        except Exception as e:
            return {"title": "", "body": "", "error": str(e)[:100]}

    return {"title": "", "body": "", "error": "max_retries"}


def batch_scrape(items: list[dict], delay: float = 1.0) -> list[dict]:
    """
    Scrape each item's URL and attach title/body to it.
    Items with errors get error field set; caller decides whether to keep them.
    """
    total = len(items)
    for i, item in enumerate(items, 1):
        url = item.get("url", "")
        print(f"[SCRAPE {i}/{total}] {url[:80]}")
        result = scrape(url)
        item["scraped_title"] = result["title"]
        item["scraped_body"] = result["body"]
        item["scrape_error"] = result["error"]
        time.sleep(delay)
    return items
