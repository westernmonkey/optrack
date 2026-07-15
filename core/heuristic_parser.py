"""
heuristic_parser.py
Extract Notion fields from URL + scraped pages using rules only — no LLM.
"""

import re
from urllib.parse import unquote, urlparse

REGION_KEYWORDS = {
    "Dubai": ["dubai", "uae", "abu dhabi"],
    "UAE": ["uae", "united arab emirates"],
    "Chicago": ["chicago", "midwest", "illinois"],
    "Midwest": ["midwest", "wisconsin", "milwaukee", "madison", "minneapolis"],
    "San Francisco": ["san francisco", "bay area", "sf "],
    "Global": ["global", "virtual", "online", "worldwide", "international"],
}

TYPE_RULES = [
    (["fellowship", "fellow program"], "Fellowship"),
    (["grant", "scholarship"], "Grant"),
    (["hackathon"], "Hackathon"),
    (["demo day", "demo-day", "demoday"], "Demo Day"),
    (["networking", "meetup", "mixer", "happy hour"], "Networking"),
    (["conference", "summit", "symposium", "forum"], "Conference"),
    (["competition", "challenge", "pitch"], "Competition"),
    (["intern", "internship", "reu", "summer research"], "Research Internship"),
    (["research assistant", "undergraduate research", "student researcher"], "Research Assistant"),
    (["open call", "applications open", "apply now"], "Open Call"),
]

# Drop incubators/accelerators in the non-LLM import path
HEURISTIC_DROP_SIGNALS = [
    "incubator", "accelerator",
    "us citizen", "u.s. citizen", "citizens only", "citizenship required",
    "phd only", "postdoctoral", "postdoc", "graduate students only",
    "master's required", "masters required", "md required",
    "autism", "autistic",
    "genetic", "genetics", "genomic", "genomics",
    "pathology", "pathologist",
    "homeopathy", "homeopathic", "homoeopathy", "homoeopathic",
    "radiology", "radiologist",
]

LAB_SIGNALS = [
    "research assistant", "undergraduate research", "lab", "intern",
    "internship", "reu", "fellowship", "trainee", "student researcher",
    "join our lab", "research group", "biomedical informatics",
    "health informatics", "clinical ai",
]

DEADLINE_PATTERNS = [
    r"deadline[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"deadline[:\s]+(\d{4}-\d{2}-\d{2})",
    r"apply by[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"(\d{4}-\d{2}-\d{2})",
]

# Obvious junk from URL alone — job boards, single listings, social
URL_JUNK_DOMAINS = {
    "myworkdayjobs.com", "workable.com", "greenhouse.io", "lever.co",
    "smartrecruiters.com", "taleo.net", "icims.com",
}
URL_JUNK_FRAGMENTS = [
    "/job/", "/jobs/", "/careers/", "/career/", "/hiring/",
    "/press-release", "/news/", "/blog/", "/article/",
    "/watch", "/video/", "/tag/", "/author/",
]


def url_derived_text(url: str) -> str:
    """Flatten URL domain + path into searchable text."""
    if not url:
        return ""
    parsed = urlparse(url)
    domain = parsed.netloc.lower().lstrip("www.")
    path = unquote(parsed.path).lower()
    slug = path.rstrip("/").split("/")[-1] if path else domain
    flat = f"{domain} {path} {slug}".replace("-", " ").replace("_", " ").replace("/", " ")
    return re.sub(r"\s+", " ", flat).strip()


GENERIC_SLUGS = {
    "about", "contact", "events", "event", "schedule", "apply", "index",
    "home", "news", "blog", "page", "pages", "membership", "member",
    "speakers", "agenda", "program", "programs", "overview", "info",
}


def org_from_domain(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().lstrip("www.")
    parts = host.split(".")
    if len(parts) >= 2 and parts[-2] in ("edu", "org", "gov"):
        name = parts[-3] if len(parts) >= 3 else parts[0]
    else:
        name = parts[0]
    name = name.replace("-", " ").replace("_", " ")
    if name.isdigit():
        return host.split(".")[0].upper()
    return name.upper() if len(name) <= 4 else name.title()


def _slug_to_title(slug: str) -> str:
    slug = re.sub(r"\.(html|php|aspx|htm)$", "", slug, flags=re.I)
    slug = slug.replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", slug).strip().title()


def title_from_url(url: str) -> str:
    parsed = urlparse(url)
    org = org_from_domain(url)
    segments = [s for s in unquote(parsed.path).strip("/").split("/") if s]
    segments = [re.sub(r"\.(html|php|aspx|htm)$", "", s, flags=re.I) for s in segments]

    # Pick the most descriptive path segment (skip generic slugs)
    title_seg = ""
    for seg in reversed(segments):
        key = seg.lower().replace("-", "").replace("_", "")
        if key not in GENERIC_SLUGS and not key.isdigit() and len(key) > 2:
            title_seg = _slug_to_title(seg)
            break

    if not title_seg and segments:
        title_seg = _slug_to_title(segments[-1])
    if not title_seg:
        return org[:200]

    if title_seg.lower() in GENERIC_SLUGS or title_seg.isdigit():
        return org[:200]

    # Don't repeat org if already in title
    if org.lower() in title_seg.lower():
        return title_seg[:200]
    return f"{title_seg} — {org}"[:200]


def is_url_junk(url: str) -> bool:
    lower = url.lower()
    domain = urlparse(url).netloc.lower()
    if any(j in domain for j in URL_JUNK_DOMAINS):
        return True
    if domain.startswith("careers.") or ".wd5.myworkdayjobs." in domain:
        return True
    return any(frag in lower for frag in URL_JUNK_FRAGMENTS)


def enrich_from_url(item: dict) -> dict:
    """Fill title/snippet from URL so prefilter + parser work without Serper metadata."""
    url = item.get("url", "")
    if not item.get("title"):
        item["title"] = title_from_url(url)
    if not item.get("snippet"):
        item["snippet"] = url_derived_text(url)
    return item


def _combined_text(item: dict) -> str:
    url = item.get("url") or ""
    title = item.get("scraped_title") or item.get("title") or ""
    body = item.get("scraped_body") or item.get("snippet") or ""
    return (title + " " + body + " " + url_derived_text(url)).lower()


def infer_track(item: dict) -> str:
    text = _combined_text(item)
    url = (item.get("url") or "").lower()
    if any(sig in text or sig in url for sig in LAB_SIGNALS):
        if any(w in text for w in ["conference", "summit", "networking", "meetup", "eventbrite"]):
            return "general"
        return "labs"
    if ".edu" in url and any(w in text for w in ["intern", "research", "lab", "fellow"]):
        return "labs"
    return item.get("track") or "general"


def infer_region(text: str) -> str:
    for region, keywords in REGION_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return region
    return "Global"


def infer_type(text: str, track: str) -> str:
    for keywords, label in TYPE_RULES:
        if any(kw in text for kw in keywords):
            return label
    if track == "labs":
        return "Lab Program"
    return "Other"


def infer_deadline(text: str) -> str | None:
    for pattern in DEADLINE_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).strip()[:200]
    # Guess year from URL/text when no explicit deadline
    year_m = re.search(r"\b(202[5-9])\b", text)
    if year_m:
        return f"{year_m.group(1)} (estimated)"
    return None


def infer_score(text: str, track: str) -> int:
    score = 5
    health = ["health", "clinical", "digital health", "healthtech", "medtech", "hospital", "biomedical"]
    if any(w in text for w in health):
        score += 2
    if track == "labs" and any(w in text for w in ["undergrad", "student", "intern", "apply"]):
        score += 2
    if any(w in text for w in ["apply", "register", "deadline", "applications open"]):
        score += 1
    return min(score, 10)


def parse_item(item: dict) -> dict | None:
    """Return enriched item or None if unusable."""
    url = item.get("url", "")
    if is_url_junk(url):
        return None

    enrich_from_url(item)
    title = (item.get("scraped_title") or item.get("title") or title_from_url(url)).strip()
    body = (item.get("scraped_body") or item.get("snippet") or url_derived_text(url)).strip()
    if not title and not body:
        return None

    # Skip useless numeric-only or generic-only titles
    bare = title.split("—")[0].strip().lower()
    if bare.isdigit() or bare in GENERIC_SLUGS:
        return None

    text = _combined_text(item)
    if any(sig in text for sig in HEURISTIC_DROP_SIGNALS):
        return None

    track = infer_track(item)
    region = infer_region(text)
    opp_type = infer_type(text, track)
    deadline = infer_deadline(text)
    score = infer_score(text, track)

    desc = body[:200] if body else title[:200]
    domain = urlparse(url).netloc.lower().lstrip("www.")

    item["name"] = title[:200]
    item["type"] = opp_type
    item["region"] = region
    item["deadline"] = deadline or ""
    item["description"] = desc
    item["eligibility"] = ""
    item["ai_summary"] = f"URL import ({track})"
    item["score"] = score
    item["track"] = track
    item["source_query"] = f"{track} · {domain}"
    return item
