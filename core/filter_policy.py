"""
filter_policy.py — single source of truth for hard rejects, topics, and gates.

Deterministic rules are law; the LLM ranks but cannot override hard rejects.
"""

from __future__ import annotations

from urllib.parse import urlparse

from core.heuristic_parser import url_derived_text

MIN_SCORE = 6

# Domains that never contain joinable opportunities
JUNK_DOMAINS = {
    "instagram.com", "facebook.com", "m.facebook.com",
    "twitter.com", "x.com", "youtube.com", "youtu.be",
    "tiktok.com", "pinterest.com", "reddit.com", "linkedin.com",
    "news.google.com", "apple.news",
    "prnewswire.com", "businesswire.com", "globenewswire.com",
    "accesswire.com", "einpresswire.com", "gulfnews.com",
    "ecals.cals.wisc.edu",
    "indeed.com", "ae.indeed.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "careerjet.com", "simplyhired.com",
    "jobs.lever.co", "boards.greenhouse.io", "workday.com",
    "amazon.jobs", "careers.microsoft.com", "jobs.siemens.com",
    "scholarships.com", "bold.org", "scholarshiptigers.com",
    "scholarshiplibrary.com", "opportunitiescircle.com",
    "scholarshipregion.com", "scholarshipsads.com",
    "wikipedia.org", "wikihow.com",
    "apps.apple.com", "play.google.com",
}

AFRICA_ONLY_DOMAINS = {
    "africahealthcollaborative.org",
    "torgceri.org",
    "mhinnovation.net",
}

JUNK_PATH_FRAGMENTS = [
    "/news/", "/blog/", "/blog/post/", "/press-release/", "/press_release/",
    "/article/", "/articles/", "/story/", "/stories/",
    "/jobs/", "/careers/", "/job-board/",
    "/p/", "/posts/", "/status/", "/watch",
    "/tag/", "/category/", "/author/", "/wp-content/",
    "/scholarships-by-", "/by-major/", "/financial-aid/college-scholarships/",
]

CITIZENSHIP_SIGNALS = [
    "us citizen", "u.s. citizen", "u.s. citizens", "us citizens",
    "united states citizen", "united states citizens",
    "citizens only", "citizen only", "must be a citizen",
    "must be us citizen", "must be a us citizen",
    "us citizenship required", "u.s. citizenship required",
    "citizenship required", "only us citizens", "only u.s. citizens",
]

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

INCUBATOR_ACCEL_SIGNALS = [
    "incubator", "accelerator", "startup accelerator",
    "healthtech accelerator", "digital health accelerator",
]

# Strong topic kills — avoid bare words that appear in nav/footers ("nursing", "genetic").
EXCLUDED_TOPIC_SIGNALS = [
    "autism", "autistic",
    "genetics fellowship", "genomic medicine", "genomics fellowship",
    "proteomics", "proteomic",
    "pathology fellowship", "pathologist",
    "homeopathy", "homeopathic", "homoeopathy", "homoeopathic",
    "radiology fellowship", "radiologist", "department of radiology",
    "mental health", "youth mental health", "behavioral health",
    "psychiatry", "psychiatric", "psychologist", "psychology fellowship",
    "wellbeing fellowship", "well-being fellowship", "mhinnovation",
    "nursing fellowship", "nursing scholarship", "nursing students",
    "school of nursing", "notes on nursing", "bsn ", "rn fellowship",
]

# Broader topic tokens — title/snippet only (pre-scrape); too noisy on full pages.
EXCLUDED_TOPIC_TITLE_SIGNALS = [
    "autism", "genetic", "genetics", "genomic", "genomics",
    "proteomic", "proteomics", "pathology", "homeopathy", "radiology",
    "mental health", "behavioral health", "psychiatry", "psychiatric",
    "nursing",
]

AFRICA_ONLY_SIGNALS = [
    "africa healthcare", "africa health", "african healthcare",
    "african health", "young african", "african professionals",
    "african youth", "for african", "africans only", "africa-only",
    "sub-saharan", "west africa", "east africa", "southern africa",
    "unlocking africa",
    "africa health collaborative", "ahif 2026", "ahif ",
    "african innovators", "african startups", "pan-african",
]

# Job/listicle language — safe on title/snippet; noisy on full scraped HTML.
LISTICLE_NEWS_SIGNALS = [
    "we're hiring", "we are hiring", "job opening", "open position",
    "apply for this job", "job description",
    "salary range", "compensation:", "job requirements",
    "instagram post", "facebook post", "view on instagram",
    "watch video", "subscribe to", "follow us on",
    "press release", "for immediate release",
    "patient recruitment", "clinical trial enrollment", "enroll in our study",
    "top 10", "top 20", "top 60", "top 90", "scholarships to apply for",
    "scholarships by major", "library highlights", "attracts thousands",
    "only 2% made it", "jobs in dubai", "intern jobs",
]

# Only match these on pre-scrape title/snippet — footers say "full-time" constantly.
LISTICLE_TITLE_ONLY_SIGNALS = [
    "full-time", "part-time",
]

HEALTHTECH_SIGNALS = [
    "healthtech", "health tech", "digital health", "clinical ai",
    "clinical informatics", "medtech", "med tech", "hospital innovation",
    "health data", "health informatics", "biomedical informatics",
    "health innovation", "healthcare innovation", "biohealth",
    "clinical workflow", "cds", "ehr",
]

OPPORTUNITY_SIGNALS = [
    "fellowship", "scholarship", "grant", "open call", "nominations",
    "undergraduate research", "research assistant", "research internship",
    "summer research", "reu", "traineeship", "student program",
    "apply", "application", "applications open", "apply now",
    "deadline", "cohort", "accepting applications", "now open",
    "call for", "program", "internship", "hackathon", "competition",
    "abstract submission", "call for papers", "student track",
]

LABS_OPPORTUNITY_SIGNALS = [
    "research assistant", "undergraduate research", "student researcher",
    "research internship", "summer research", "research opportunit",
    "join the lab", "join our lab", "research fellowship", "fellowship",
    "internship", "apply", "application", "positions", "opening",
    "program", "research program", "lab", "research experience",
    "reu", "traineeship", "scholar",
]

WISCONSIN_EVENT_SIGNALS = [
    "networking", "meetup", "community event",
    "conference", "summit", "forum", "symposium",
    "register", "registration", "tickets", "rsvp",
]

LOW_PRIORITY_TYPES = {
    "networking", "meetup", "mixer", "happy hour", "demo day",
    "summit", "conference", "forum", "symposium",
}

HIGH_PRIORITY_TYPES = {
    "fellowship", "grant", "scholarship", "research internship",
    "research assistant", "student research", "summer program",
    "research fellowship", "lab program", "student program",
    "open call", "leadership program",
}

OFY_DOMAIN = "opportunitiesforyouth.org"
OFY_TITLE_SIGNALS = [
    "apply", "fellowship", "scholarship", "internship", "grant",
    "fully funded", "call for", "applications open", "deadline",
]

# Eligibility / geography — safe to match in page body (not footer noise).
ELIGIBILITY_SIGNALS = (
    CITIZENSHIP_SIGNALS
    + GRADUATE_ONLY_SIGNALS
    + INCUBATOR_ACCEL_SIGNALS
    + AFRICA_ONLY_SIGNALS
    + EXCLUDED_TOPIC_SIGNALS
)

HARD_REJECT_SIGNALS = (
    ELIGIBILITY_SIGNALS
    + LISTICLE_NEWS_SIGNALS
    + LISTICLE_TITLE_ONLY_SIGNALS
    + EXCLUDED_TOPIC_TITLE_SIGNALS
)

# Labs track relaxes job-posting language but keeps eligibility/topic kills
HARD_REJECT_SIGNALS_LABS = (
    ELIGIBILITY_SIGNALS
    + [
        "instagram post", "facebook post", "view on instagram",
        "watch video", "subscribe to", "follow us on",
        "press release", "for immediate release",
        "tenure-track faculty", "full professor", "assistant professor",
        "patient recruitment", "clinical trial enrollment", "enroll in our study",
    ]
)

# Post-scrape: listicle/job noise only on title + lead (not whole page).
# Topic exclusions use strong phrases in ELIGIBILITY_SIGNALS — not bare "nursing".
SCRAPE_LEAD_ONLY_SIGNALS = (
    LISTICLE_NEWS_SIGNALS
    + LISTICLE_TITLE_ONLY_SIGNALS
)

TYPE_RULES = [
    (["fellowship", "fellow program"], "Fellowship"),
    (["scholarship"], "Scholarship"),
    (["grant"], "Grant"),
    (["hackathon"], "Hackathon"),
    (["demo day", "demo-day", "demoday"], "Demo Day"),
    (["networking", "meetup", "mixer", "happy hour"], "Networking"),
    (["conference", "summit", "symposium", "forum"], "Conference"),
    (["competition", "challenge", "pitch"], "Competition"),
    (["intern", "internship", "reu", "summer research"], "Research Internship"),
    (["research assistant", "undergraduate research", "student researcher"], "Research Assistant"),
    (["open call", "applications open", "apply now"], "Open Call"),
]


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def _item_track(item: dict) -> str:
    tracks = item.get("tracks")
    if tracks:
        if "labs" in tracks:
            return "labs"
        # wi_events uses general hard-reject policy
        for t in tracks:
            if t != "wi_events":
                return t
        return "general"
    track = item.get("track", "general")
    return "general" if track == "wi_events" else track


def _probe_text(item: dict, use_body: bool = False) -> str:
    title = (item.get("scraped_title") or item.get("title") or "").lower()
    if use_body:
        body = (item.get("scraped_body") or item.get("snippet") or "").lower()
    else:
        body = (item.get("snippet") or "").lower()
    url = item.get("url") or ""
    return f"{title} {body} {url_derived_text(url)}"


def has_healthtech(text: str) -> bool:
    return any(sig in text for sig in HEALTHTECH_SIGNALS)


def is_wisconsin_context(text: str, source_query: str = "") -> bool:
    q = (source_query or "").lower()
    return (
        "madison" in text or "wisconsin" in text or "milwaukee" in text
        or "madison" in q or "wisconsin" in q or "milwaukee" in q
    )


def is_low_priority_type(opp_type: str) -> bool:
    t = (opp_type or "").strip().lower()
    return t in LOW_PRIORITY_TYPES or "network" in t


def is_high_priority_type(opp_type: str) -> bool:
    t = (opp_type or "").strip().lower()
    return t in HIGH_PRIORITY_TYPES or "fellow" in t or "intern" in t or "research" in t


def infer_type(text: str, track: str = "general") -> str:
    lower = text.lower()
    for keywords, label in TYPE_RULES:
        if any(kw in lower for kw in keywords):
            return label
    if track == "labs":
        return "Lab Program"
    return "Other"


def infer_deadline(text: str) -> str | None:
    import re
    patterns = [
        r"deadline[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"deadline[:\s]+(\d{4}-\d{2}-\d{2})",
        r"apply by[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"(\d{4}-\d{2}-\d{2})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).strip()[:200]
    return None


def _load_yaml_junk_extras() -> list[str]:
    """Merge executable scoring.junk_signals from keywords.yaml into policy."""
    from pathlib import Path
    import yaml
    path = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"
    try:
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    except OSError:
        return []
    scoring = config.get("scoring") or {}
    extras = []
    for sig in scoring.get("junk_signals") or []:
        s = str(sig).strip().lower()
        if s and s not in extras:
            extras.append(s)
    return extras


_YAML_JUNK_EXTRAS = _load_yaml_junk_extras()


def _lead_text(item: dict, *, body_chars: int = 800) -> str:
    """Title + snippet + lead body — avoids footer/nav false positives."""
    title = (item.get("scraped_title") or item.get("title") or "").lower()
    snippet = (item.get("snippet") or "").lower()
    body = (item.get("scraped_body") or "")[:body_chars].lower()
    url = item.get("url") or ""
    return f"{title} {snippet} {body} {url_derived_text(url)}"


def hard_reject(item: dict, *, scraped: bool = False) -> tuple[bool, str]:
    """
    Deterministic hard reject. Returns (True, reason_code) if rejected.

    Pre-scrape: title/snippet/URL only.
    Post-scrape: eligibility on lead+body; noisy listicle/topic tokens only on
    title + lead text so footers mentioning nursing/full-time do not kill fits.
    """
    url = item.get("url", "")
    domain = get_domain(url)
    path = urlparse(url).path.lower() if url else ""
    track = _item_track(item)

    if domain in JUNK_DOMAINS or any(domain.endswith(f".{d}") for d in JUNK_DOMAINS):
        return True, f"junk_domain:{domain}"
    if domain in AFRICA_ONLY_DOMAINS or any(
        domain.endswith(f".{d}") for d in AFRICA_ONLY_DOMAINS
    ):
        return True, f"africa_only_domain:{domain}"

    labs_ok = {"/jobs/", "/careers/", "/job-board/"}
    for frag in JUNK_PATH_FRAGMENTS:
        if track == "labs" and frag in labs_ok:
            continue
        if scraped and frag in ("/news/", "/blog/", "/article/", "/articles/"):
            continue
        if frag in path:
            return True, f"junk_path:{frag}"

    if not scraped:
        text = _probe_text(item, use_body=False)
        signals = HARD_REJECT_SIGNALS_LABS if track == "labs" else HARD_REJECT_SIGNALS
        for signal in signals:
            if signal in text:
                return True, f"hard_reject:{signal}"
        if track != "labs":
            for signal in _YAML_JUNK_EXTRAS:
                if signal in text:
                    return True, f"yaml_junk:{signal}"
        return False, ""

    # --- scraped ---
    lead = _lead_text(item, body_chars=900)
    body_probe = _probe_text(item, use_body=True)

    for signal in ELIGIBILITY_SIGNALS:
        # Prefer lead for topic phrases; citizenship/grad anywhere in body is ok
        haystack = body_probe if signal in (
            CITIZENSHIP_SIGNALS + GRADUATE_ONLY_SIGNALS + INCUBATOR_ACCEL_SIGNALS
        ) else lead
        # Africa + strong excluded topics: lead + first 2k of body
        if signal in AFRICA_ONLY_SIGNALS or signal in EXCLUDED_TOPIC_SIGNALS:
            haystack = _lead_text(item, body_chars=2000)
        if signal in haystack:
            return True, f"hard_reject:{signal}"

    noisy = SCRAPE_LEAD_ONLY_SIGNALS
    if track == "labs":
        noisy = [
            "instagram post", "facebook post", "view on instagram",
            "watch video", "subscribe to", "follow us on",
            "press release", "for immediate release",
            "tenure-track faculty", "full professor", "assistant professor",
            "patient recruitment", "clinical trial enrollment", "enroll in our study",
        ]
    for signal in noisy:
        if signal in lead:
            return True, f"hard_reject:{signal}"

    if track != "labs":
        for signal in _YAML_JUNK_EXTRAS:
            if signal in lead:
                return True, f"yaml_junk:{signal}"

    return False, ""


def needs_opportunity_signal(item: dict) -> bool:
    """Trusted/.edu may skip weak opportunity-signal check only after hard rejects."""
    domain = get_domain(item.get("url", ""))
    track = _item_track(item)
    from pathlib import Path
    import yaml
    config_path = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except OSError:
        config = {}
    trusted = {d.lower().lstrip("www.") for d in config.get("trusted_domains", [])}
    labs_trusted = {d.lower().lstrip("www.") for d in config.get("labs_trusted_domains", [])}
    if domain in trusted:
        return False
    if track == "labs" and (domain in labs_trusted or domain.endswith(".edu")):
        return False
    return True


def prefilter_item(item: dict) -> tuple[bool, str]:
    """
    Pre-scrape gate. Returns (True, reason) if should DROP.
    """
    rejected, reason = hard_reject(item, scraped=False)
    if rejected:
        return True, reason

    text = _probe_text(item, use_body=False)
    track = _item_track(item)
    source_query = item.get("source_query") or ""

    # WI event hits from WI event queries: require healthtech
    if is_wisconsin_context(text, source_query) and any(
        s in text for s in WISCONSIN_EVENT_SIGNALS
    ):
        if has_healthtech(text) or has_healthtech(source_query.lower()):
            return False, ""
        # Off-topic WI event — drop unless opportunity signals present
        if not any(sig in text for sig in OPPORTUNITY_SIGNALS):
            return True, "wi_event_off_topic"

    if track == "general" and not has_healthtech(text):
        # Allow if strong opportunity language + health-adjacent word
        if "health" not in text and "clinical" not in text and "medical" not in text:
            if needs_opportunity_signal(item):
                # still allow pure REU/fellowship without health if labs-ish
                if not any(s in text for s in ("fellowship", "reu", "scholarship", "internship")):
                    return True, "no_healthtech_signal"

    if needs_opportunity_signal(item):
        signals = LABS_OPPORTUNITY_SIGNALS if track == "labs" else OPPORTUNITY_SIGNALS
        if not any(sig in text for sig in signals):
            return True, "no_opportunity_signal"

    return False, ""


def is_wisconsin_snippet_only(item: dict) -> bool:
    text = _probe_text(item, use_body=False)
    query = (item.get("source_query") or "").lower()
    if any(b in text for b in EXCLUDED_TOPIC_SIGNALS + AFRICA_ONLY_SIGNALS):
        return False
    if not is_wisconsin_context(text, query):
        return False
    if not any(sig in text for sig in WISCONSIN_EVENT_SIGNALS):
        return False
    if not (has_healthtech(text) or has_healthtech(query)):
        return False
    # Prefer event-like queries
    if not any(sig in query for sig in ("networking", "meetup", "conference", "summit", "forum")):
        if not any(sig in text for sig in ("2025", "2026", "2027", "register", "registration")):
            return False
    return True


def is_ofy_snippet_only(item: dict) -> bool:
    """OFY is discovery-only for tagging; still requires full-page confirmation."""
    return False  # plan: all OFY need full-page confirmation


def is_snippet_only(item: dict) -> bool:
    return is_wisconsin_snippet_only(item)


def apply_acceptance_gates(
    item: dict,
    score: int,
    min_score: int = MIN_SCORE,
    *,
    scraped: bool = False,
) -> tuple[bool, str]:
    """
    Final deterministic gate after LLM scoring.
    Returns (accepted, reason).
    """
    if score < min_score:
        return False, f"score_below_{min_score}"

    rejected, reason = hard_reject(item, scraped=scraped)
    if rejected:
        return False, reason

    title = item.get("scraped_title") or item.get("title") or ""
    body = item.get("scraped_body") or item.get("snippet") or ""
    text = _probe_text(item, use_body=scraped)
    track = _item_track(item)
    opp_type = infer_type(f"{title} {body}", track)

    if opp_type.lower() in ("accelerator", "incubator"):
        return False, "incubator_accelerator"

    if is_low_priority_type(opp_type):
        if not (is_wisconsin_context(text, item.get("source_query", "")) and has_healthtech(text)):
            return False, f"low_priority:{opp_type}"

    return True, "accepted"


EVAL_SYSTEM_PROMPT = """You evaluate opportunities for an international second-year
UW-Madison undergraduate (CS & Statistics) focused on clinical AI, digital health,
healthtech, medtech, biomedical/clinical informatics, and hospital innovation.

Return ONLY valid JSON of this exact shape covering EVERY input id:
{"results":[{"id":0,"decision":"accept","score":8,"reason":"brief","eligibility_confidence":"high"}]}

decision must be "accept" or "reject".
score is 1-10. Accept only if score >= 6 AND joinable for undergrads including international.
eligibility_confidence is "high", "medium", or "low".

ACCEPT: fellowships, scholarships (single program, not listicles), REU/RA/internships,
summer research, structured student programs, open calls, competitions with apply paths.
Madison/Wisconsin healthtech networking/conferences/summits may accept at score 6+.

HARD REJECT (decision=reject): Africa-only/Africa-targeted; mental health/psychiatry;
autism/genetics/proteomics/pathology/homeopathy/radiology; US-citizen-only;
MD/PhD/master's/postdoc/faculty-only; incubators/accelerators; job boards/full-time jobs;
listicles/news/blogs; high-school/teen programs.

Be decisive. Include every id exactly once. No markdown."""
