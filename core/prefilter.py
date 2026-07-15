"""
prefilter.py
Fast pre-filter that runs BEFORE scraping and LLM evaluation.
Kills obvious junk using URL and snippet heuristics only.
No API calls — pure Python. Runs in milliseconds.

Goal: reduce raw results so OpenRouter quota (~150/day) is spent on
plausible undergrad healthtech fits only.
"""

from pathlib import Path
from urllib.parse import urlparse

import yaml

from core.heuristic_parser import url_derived_text

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
    "/news/", "/blog/", "/blog/post/", "/press-release/", "/press_release/",
    "/article/", "/articles/", "/story/", "/stories/",
    "/jobs/", "/careers/", "/job-board/",
    "/p/",   # Instagram post pattern
    "/posts/",  # Facebook post pattern
    "/status/",  # Twitter/X
    "/watch",    # YouTube
    "/tag/", "/category/", "/author/",
    "/wp-content/",
]

# Citizenship / nationality kills — international student ineligible
CITIZENSHIP_SIGNALS = [
    "us citizen", "u.s. citizen", "u.s. citizens", "us citizens",
    "united states citizen", "united states citizens",
    "citizens only", "citizen only", "must be a citizen",
    "must be us citizen", "must be a us citizen",
    "us citizenship required", "u.s. citizenship required",
    "citizenship required", "only us citizens", "only u.s. citizens",
]

# Graduate / MD / PhD kills — no undergrad path
GRADUATE_ONLY_SIGNALS = [
    "md/phd", "m.d./ph.d", "m.d. / ph.d", "md phd",
    "phd only", "ph.d. only", "doctoral only", "ph.d only",
    "phd position", "phd opportunity", "phd program only",
    "postdoctoral", "postdoc",
    "master's required", "masters required", "master’s required",
    "mba required", "md required", "m.d. required",
    "graduate students only", "grad students only",
    "phd candidates only", "doctoral candidates only",
    "for phd students only", "for graduate students only",
    "m.d. candidates only",
]

# Incubators / accelerators — skipped for now
INCUBATOR_ACCEL_SIGNALS = [
    "incubator", "accelerator", "startup accelerator",
    "healthtech accelerator", "digital health accelerator",
]

EXCLUDED_TOPIC_SIGNALS = [
    "autism", "autistic",
    "genetic", "genetics", "genomic", "genomics",
    "pathology", "pathologist", "pathological",
    "homeopathy", "homeopathic", "homoeopathy", "homoeopathic",
    "radiology", "radiologist", "radiological",
]

WISCONSIN_EVENT_SIGNALS = [
    "networking", "meetup", "community event",
    "conference", "summit", "forum", "symposium",
    "register", "registration", "tickets", "rsvp",
]

# Snippet/title phrases that mean it's definitely not an opportunity
JUNK_TEXT_SIGNALS = [
    "we're hiring", "we are hiring", "job opening", "open position",
    "apply for this job", "job description", "full-time", "part-time",
    "salary range", "compensation:", "job requirements",
    "instagram post", "facebook post", "view on instagram",
    "watch video", "subscribe to", "follow us on",
    "press release", "for immediate release",
    "patient recruitment", "clinical trial enrollment", "enroll in our study",
] + CITIZENSHIP_SIGNALS + GRADUATE_ONLY_SIGNALS + INCUBATOR_ACCEL_SIGNALS + EXCLUDED_TOPIC_SIGNALS

# Junk text signals that apply to the labs track. Research/lab pages often
# legitimately use hiring language for RA / student roles, so we relax the
# job-posting kills and only hard-kill clearly irrelevant items.
JUNK_TEXT_SIGNALS_LABS = [
    "instagram post", "facebook post", "view on instagram",
    "watch video", "subscribe to", "follow us on",
    "press release", "for immediate release",
    "tenure-track faculty", "full professor", "assistant professor",
    "patient recruitment", "clinical trial enrollment", "enroll in our study",
] + CITIZENSHIP_SIGNALS + GRADUATE_ONLY_SIGNALS + INCUBATOR_ACCEL_SIGNALS + EXCLUDED_TOPIC_SIGNALS

# Labs-track signals — research / student opportunity language.
LABS_OPPORTUNITY_SIGNALS = [
    "research assistant", "undergraduate research", "student researcher",
    "research internship", "summer research", "research opportunit",
    "join the lab", "join our lab", "research fellowship", "fellowship",
    "internship", "apply", "application", "positions", "opening",
    "program", "research program", "lab", "research experience",
    "reu", "traineeship", "scholar",
]

# Title/snippet must contain at least one of these to pass
# (unless URL is from a known-good domain). Fellowship/lab language first.
OPPORTUNITY_SIGNALS = [
    "fellowship", "scholarship", "grant", "open call", "nominations",
    "undergraduate research", "research assistant", "research internship",
    "summer research", "reu", "traineeship", "student program",
    "apply", "application", "applications open", "apply now",
    "deadline", "cohort", "accepting applications", "now open",
    "call for", "program", "internship", "hackathon", "competition",
    # Conferences kept weak — prefer research CFP language elsewhere
    "abstract submission", "call for papers", "student track",
]


def _load_domains(key: str) -> set[str]:
    config_path = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return {domain.lower().lstrip("www.") for domain in config.get(key, [])}


def load_trusted_domains() -> set[str]:
    return _load_domains("trusted_domains")


# Domains that are almost always legit opportunity sources — skip signal check
TRUSTED_DOMAINS = load_trusted_domains()
LABS_TRUSTED_DOMAINS = _load_domains("labs_trusted_domains")


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def _item_track(item: dict) -> str:
    """Prefer labs if the item was surfaced by the labs track at all."""
    tracks = item.get("tracks")
    if tracks:
        return "labs" if "labs" in tracks else tracks[0]
    return item.get("track", "general")


def is_junk(item: dict) -> tuple[bool, str]:
    """
    Returns (True, reason) if item should be dropped, (False, '') if it passes.
    Applies track-specific rules: the labs track allows research/RA language
    and trusts academic (.edu) domains that the general track would reject.
    """
    url = item.get("url", "")
    title = (item.get("title") or "").lower()
    snippet = (item.get("snippet") or "").lower()
    combined_text = title + " " + snippet + " " + url_derived_text(url)

    domain = get_domain(url)
    path = urlparse(url).path.lower() if url else ""
    track = _item_track(item)

    # 1. Hard kill — junk domain (social / news / job boards)
    if domain in JUNK_DOMAINS:
        return True, f"junk domain: {domain}"

    # 2. Hard kill — junk path pattern
    #    Labs pages often live under /jobs/ or /careers/ for RA roles, so
    #    skip those particular fragments for the labs track.
    labs_ok_fragments = {"/jobs/", "/careers/", "/job-board/"}
    for frag in JUNK_PATH_FRAGMENTS:
        if track == "labs" and frag in labs_ok_fragments:
            continue
        if frag in path:
            return True, f"junk URL path: {frag}"

    # 3. Hard kill — junk text signal (track-specific list)
    #    Citizenship / graduate-only / incubator kills run BEFORE trusted-domain
    #    bypass so ineligible pages never burn eval quota.
    junk_signals = JUNK_TEXT_SIGNALS_LABS if track == "labs" else JUNK_TEXT_SIGNALS
    for signal in junk_signals:
        if signal in combined_text:
            return True, f"junk text signal: '{signal}'"

    # 4. Trusted domain — always pass (after hard eligibility kills)
    if domain in TRUSTED_DOMAINS:
        return False, ""

    if track == "labs":
        # Trust academic domains and known lab hosts outright.
        if domain in LABS_TRUSTED_DOMAINS or domain.endswith(".edu"):
            return False, ""
        signals = LABS_OPPORTUNITY_SIGNALS
    else:
        signals = OPPORTUNITY_SIGNALS

    # Madison/Wisconsin event coverage is intentionally broad.
    is_wisconsin = "madison" in combined_text or "wisconsin" in combined_text
    if is_wisconsin and any(sig in combined_text for sig in WISCONSIN_EVENT_SIGNALS):
        return False, ""

    # 5. Must contain at least one opportunity signal
    if not any(sig in combined_text for sig in signals):
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
