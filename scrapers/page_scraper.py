"""
page_scraper.py — fetch page text with bounded retries and error class.
"""

from __future__ import annotations

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

NOISE_TAGS = [
    "script", "style", "nav", "footer", "header", "aside",
    "noscript", "iframe", "svg", "form", "button",
]

MIN_BODY_LENGTH = 80
MAX_BODY_LENGTH = 4000

# Terminal (do not retry): soft 403/404/410, empty page
TERMINAL_ERRORS = frozenset({
    "403", "404", "410", "empty_page", "not_html",
})


def classify_scrape_error(error: str | None) -> bool:
    """Return True if retryable."""
    if not error:
        return False
    code = str(error).split(":")[0].strip().lower()
    if code in TERMINAL_ERRORS:
        return False
    if code.isdigit() and int(code) in (403, 404, 410, 451):
        return False
    return True


def scrape(url: str, retries: int = 2) -> dict:
    """
    Fetch and clean a page.
    Returns {title, body, error, retryable}.
    """
    last_error = "max_retries"
    for attempt in range(retries + 1):
        try:
            r = requests.get(
                url, headers=HEADERS, timeout=12, allow_redirects=True
            )
            if r.status_code in (403, 404, 410, 451):
                return {
                    "title": "",
                    "body": "",
                    "error": str(r.status_code),
                    "retryable": False,
                }
            if r.status_code >= 500:
                last_error = str(r.status_code)
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                return {
                    "title": "", "body": "",
                    "error": last_error, "retryable": True,
                }
            r.raise_for_status()

            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype and "text" not in ctype:
                return {
                    "title": "", "body": "",
                    "error": "not_html", "retryable": False,
                }

            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(NOISE_TAGS):
                tag.decompose()

            title = ""
            if soup.title and soup.title.string:
                title = soup.title.string.strip()[:300]

            body = " ".join(soup.get_text(separator=" ").split())[:MAX_BODY_LENGTH]
            if len(body) < MIN_BODY_LENGTH:
                return {
                    "title": title, "body": "",
                    "error": "empty_page", "retryable": False,
                }
            return {"title": title, "body": body, "error": None, "retryable": False}

        except requests.Timeout:
            last_error = "timeout"
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return {
                "title": "", "body": "",
                "error": "timeout", "retryable": True,
            }
        except requests.RequestException as e:
            last_error = str(e)[:100]
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return {
                "title": "", "body": "",
                "error": last_error,
                "retryable": classify_scrape_error(last_error),
            }
        except Exception as e:
            return {
                "title": "", "body": "",
                "error": str(e)[:100],
                "retryable": False,
            }

    return {
        "title": "", "body": "",
        "error": last_error, "retryable": classify_scrape_error(last_error),
    }


def batch_scrape(items: list[dict], delay: float = 1.0) -> list[dict]:
    total = len(items)
    for i, item in enumerate(items, 1):
        url = item.get("url", "")
        print(f"[SCRAPE {i}/{total}] {url[:80]}")
        result = scrape(url)
        item["scraped_title"] = result["title"]
        item["scraped_body"] = result["body"]
        item["scrape_error"] = result["error"]
        item["scrape_retryable"] = result.get("retryable", False)
        time.sleep(delay)
    return items
