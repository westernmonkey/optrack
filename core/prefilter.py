"""
prefilter.py
Fast pre-filter that runs BEFORE scraping and Claude evaluation.
Kills obvious junk using URL and snippet heuristics only.
No API calls — pure Python. Runs in milliseconds.

Goal: reduce 200 raw results down to ~30-50 worth evaluating.
"""

from urllib.parse import urlparse

# Domains that will never contain registerable opportunities
JUNK_DOMAINS = {
    # Social media
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "twitter.com", "x.com", "www.twitter.com",
    "linkedin.com", "www.linkedin.com",
    "youtube.com", "www.youtube.com", "youtu.be",
    "tiktok.com", "www.tiktok.com",
    "pinterest.com", "reddit.com", "www.reddit.com",
    # News aggregators / press
    "news.google.com", "apple.news",
    "prnewswire.com", "businesswire.com", "globenewswire.com",
    "accesswire.com", "einpresswire.com",
    # Job boards
    "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "careerjet.com", "simplyhired.com",
    "jobs.lever.co", "boards.greenhouse.io", "workday.com",
    # Generic wikis / encyclopedias
    "wikipedia.org", "wikihow.com",
    # App stores
    "apps.apple.com", "play.google.com",
}

# URL path fragments that almost always mean it's a news article or job post
JUNK_PATH_FRAGMENTS = [
    "/news/", "/blog/post/", "/press-release/", "/press_release/",
    "/article/", "/articles/", "/story/", "/stories/",
    "/jobs/", "/careers/", "/job-board/",
    "/p/",   # Instagram post pattern
    "/posts/",  # Facebook post pattern
    "/status/",  # Twitter/X
    "/watch",    # YouTube
    "/tag/", "/category/", "/author/",
    "/wp-content/",
]

# Snippet/title phrases that mean it's definitely not an opportunity
JUNK_TEXT_SIGNALS = [
    "we're hiring", "we are hiring", "job opening", "open position",
    "apply for this job", "job description", "full-time", "part-time",
    "salary range", "compensation:", "job requirements",
    "instagram post", "facebook post", "view on instagram",
    "watch video", "subscribe to", "follow us on",
    "press release", "for immediate release",
    "phd position", "phd opportunity", "postdoctoral", "postdoc",
    "patient recruitment", "clinical trial enrollment", "enroll in our study",
]

# Title/snippet must contain at least one of these to pass
# (unless URL is from a known-good domain)
OPPORTUNITY_SIGNALS = [
    "apply", "application", "applications open", "apply now",
    "register", "registration", "enroll", "enrollment",
    "deadline", "cohort", "fellowship", "accelerator",
    "grant", "scholarship", "open call", "nominations",
    "program", "incubator", "hackathon", "competition",
    "leadership program", "innovation program", "pitch",
]

# Domains that are almost always legit opportunity sources — skip signal check
TRUSTED_DOMAINS = {
    "opportunitydesk.org", "devpost.com", "f6s.com",
    "ashoka.org", "atlanticfellows.org", "rhodeshouse.ox.ac.uk",
    "matter.health", "startuphealth.com", "rockhealth.com",
    "ycombinator.com", "plugandplaytechcenter.com",
    "dubaifuture.ae", "dha.gov.ae", "in5.ae",
    "wamda.com", "magnitt.com", "1871.com",
}


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def is_junk(item: dict) -> tuple[bool, str]:
    """
    Returns (True, reason) if item should be dropped, (False, '') if it passes.
    """
    url = item.get("url", "")
    title = (item.get("title") or "").lower()
    snippet = (item.get("snippet") or "").lower()
    combined_text = title + " " + snippet

    domain = get_domain(url)
    path = urlparse(url).path.lower() if url else ""

    # 1. Hard kill — junk domain
    if domain in JUNK_DOMAINS:
        return True, f"junk domain: {domain}"

    # 2. Hard kill — junk path pattern
    for frag in JUNK_PATH_FRAGMENTS:
        if frag in path:
            return True, f"junk URL path: {frag}"

    # 3. Hard kill — junk text signal in title/snippet
    for signal in JUNK_TEXT_SIGNALS:
        if signal in combined_text:
            return True, f"junk text signal: '{signal}'"

    # 4. Trusted domain — always pass
    if domain in TRUSTED_DOMAINS:
        return False, ""

    # 5. Must contain at least one opportunity signal
    if not any(sig in combined_text for sig in OPPORTUNITY_SIGNALS):
        return True, "no opportunity signal in title/snippet"

    return False, ""


def prefilter(items: list[dict]) -> list[dict]:
    """
    Drop obvious junk. Returns only items worth scraping + evaluating.
    """
    passed = []
    dropped = 0

    for item in items:
        junk, reason = is_junk(item)
        if junk:
            dropped += 1
            # Uncomment for debug:
            # print(f"  [PREFILTER DROP] {item.get('url','')[:60]} — {reason}")
        else:
            passed.append(item)

    total = len(items)
    print(f"[PREFILTER] {len(passed)}/{total} passed ({dropped} dropped as obvious junk)")
    return passed