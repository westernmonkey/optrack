"""
evaluator.py
Sends each scraped page to OpenRouter for evaluation.
Supports multiple API keys with round-robin rotation (~150 req/day with 3 free keys).
"""

import json
import os
import re
import time
from datetime import date
from pathlib import Path

import requests

from core.heuristic_parser import infer_deadline, infer_region, infer_type, url_derived_text

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
USAGE_PATH = Path("data/openrouter_usage.json")

# Free tier with 0 credits purchased: 20 req/min, 50 req/day per key.
FREE_RPM = 20
FREE_RPD = 50
MIN_KEY_INTERVAL = 60.0 / FREE_RPM  # 3.0s between uses of the same key
DEFAULT_EVAL_DELAY = 3.1
SNIPPET_CHUNK_SIZE = 40
BATCH_CHUNK_SIZE = 15
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


def _is_low_priority_type(opp_type: str) -> bool:
    t = (opp_type or "").strip().lower()
    return t in LOW_PRIORITY_TYPES or "network" in t


def _is_high_priority_type(opp_type: str) -> bool:
    t = (opp_type or "").strip().lower()
    return t in HIGH_PRIORITY_TYPES or "fellow" in t or "intern" in t or "research" in t


POSTSCRAPE_REJECT_SIGNALS = [
    "jobs and internships",
    "per diem job",
    "job at ",
    "career opportunities",
]


# Models verified working on free tier (upstream limits rotate — openrouter/free auto-picks).
DEFAULT_MODEL = "openrouter/free"
FREE_FALLBACK_MODELS = [
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
]


class QuotaExhausted(Exception):
    """All OpenRouter API keys are rate-limited or out of credits."""


def _api_keys() -> list[str]:
    raw = os.environ.get("OPENROUTER_API_KEYS", "").strip()
    if raw:
        return [k.strip() for k in raw.split(",") if k.strip()]
    keys = []
    for i in range(1, 10):
        k = os.environ.get(f"OPENROUTER_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    single = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if single and single not in keys:
        keys.insert(0, single)
    return keys


def _model() -> str:
    return os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _models() -> list[str]:
    """Primary model plus free fallbacks for rate-limit rotation."""
    primary = _model()
    raw = os.environ.get("OPENROUTER_FALLBACK_MODELS", "").strip()
    if raw:
        fallbacks = [m.strip() for m in raw.split(",") if m.strip()]
    else:
        fallbacks = list(FREE_FALLBACK_MODELS)
    models = [primary]
    for m in fallbacks:
        if m not in models:
            models.append(m)
    return models


def _check_openrouter() -> None:
    if not _api_keys():
        raise EnvironmentError(
            "No OpenRouter API keys found. Set OPENROUTER_API_KEYS "
            "(comma-separated) or OPENROUTER_API_KEY_1, OPENROUTER_API_KEY_2, ..."
        )


# Round-robin index across process lifetime
_key_index = 0
_exhausted_keys: set[str] = set()
_key_last_used: dict[str, float] = {}


def _key_id(key: str) -> str:
    return key[-8:]


def _load_usage() -> dict:
    try:
        data = json.loads(USAGE_PATH.read_text())
        if data.get("date") != str(date.today()):
            return {"date": str(date.today()), "keys": {}}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"date": str(date.today()), "keys": {}}


def _save_usage(data: dict) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(data, indent=2))


def _key_daily_count(key: str) -> int:
    kid = _key_id(key)
    return int(_load_usage().get("keys", {}).get(kid, 0))


def _record_key_use(key: str) -> None:
    global _key_last_used
    _key_last_used[key] = time.time()
    data = _load_usage()
    if data.get("date") != str(date.today()):
        data = {"date": str(date.today()), "keys": {}}
    kid = _key_id(key)
    data["keys"][kid] = int(data["keys"].get(kid, 0)) + 1
    _save_usage(data)


def _remaining_daily_budget() -> int:
    return sum(max(0, FREE_RPD - _key_daily_count(k)) for k in _api_keys())


def _wait_for_key(key: str) -> None:
    last = _key_last_used.get(key, 0.0)
    wait = MIN_KEY_INTERVAL - (time.time() - last)
    if wait > 0:
        time.sleep(wait)


def _is_upstream_rate_limit(response: requests.Response) -> bool:
    if response.status_code != 429:
        return False
    text = response.text.lower()
    return "upstream" in text or "provider returned error" in text


def _next_key() -> str:
    global _key_index
    keys = _api_keys()
    available = [
        k for k in keys
        if k not in _exhausted_keys and _key_daily_count(k) < FREE_RPD
    ]
    if not available:
        raise QuotaExhausted(
            "All OpenRouter API keys exhausted (daily 50/key limit or 429)"
        )
    key = available[_key_index % len(available)]
    _key_index += 1
    _wait_for_key(key)
    return key


def _mark_exhausted(key: str) -> None:
    _exhausted_keys.add(key)
    print(f"  [EVAL] Key ...{key[-6:]} exhausted — rotating")


def _reset_exhausted_keys() -> None:
    global _exhausted_keys
    _exhausted_keys = set()


def _chat_request(
    key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 600,
) -> requests.Response:
    return requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/optrack",
            "X-Title": "OpTrack",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )


def call_llm(system_prompt: str, user_prompt: str) -> str:
    keys = _api_keys()
    last_error: Exception | None = None

    for model in _models():
        attempts = max(len(keys), 1)
        for _ in range(attempts):
            key = _next_key()
            try:
                response = _chat_request(key, model, system_prompt, user_prompt)
                if response.status_code == 429 and _is_upstream_rate_limit(response):
                    print(f"  [EVAL] Model {model} upstream-limited — trying fallback")
                    last_error = requests.HTTPError("429 upstream", response=response)
                    break  # next model, keep keys
                if response.status_code in (429, 402):
                    _mark_exhausted(key)
                    last_error = requests.HTTPError(
                        f"{response.status_code}", response=response
                    )
                    continue
                response.raise_for_status()
                data = response.json()
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
                if not content:
                    print(f"  [EVAL] Empty content from {model} — retrying")
                    last_error = ValueError("empty LLM content")
                    continue
                _record_key_use(key)
                return content
            except QuotaExhausted:
                break
            except requests.HTTPError as e:
                last_error = e
                resp = e.response
                if resp is not None and resp.status_code == 429 and _is_upstream_rate_limit(resp):
                    print(f"  [EVAL] Model {model} upstream-limited — trying fallback")
                    break
                status = resp.status_code if resp is not None else None
                if status in (429, 402):
                    _mark_exhausted(key)
                    continue
                raise
            except Exception as e:
                last_error = e
                raise

        if len(_models()) > 1:
            _reset_exhausted_keys()

    raise QuotaExhausted(
        f"All OpenRouter keys/models exhausted. Last error: {last_error}"
    ) from last_error


SYSTEM_PROMPT = """You are OpTrack's opportunity evaluator for an INTERNATIONAL undergraduate (2nd year) studying Computer Science & Statistics at UW-Madison, focused on clinical AI / digital health / healthtech. Not a US citizen. Not pursuing MD, PhD, or master's right now.

TASK: Decide if this webpage is a REAL opportunity this student can APPLY TO or JOIN — prefer structured pathways over one-off events.

PRIORITY ACCEPT (score 7–10):
- Fellowships, scholarships, grants, traineeships open to undergrads / bachelor's / international students
- Research programs, REUs, summer research, lab join paths, research assistant / intern roles for students
- Structured student / early-career programs with apply → cohort → project (not just a meetup)

SECONDARY ACCEPT (score at most 5–6, only if clearly student/research relevant):
- Research-oriented conferences with a student track or open call for abstracts/posters
- Innovation challenges / competitions with a real application (not spectator tickets)
- EXCEPTION: accept all joinable healthtech/digital-health/clinical-AI networking,
  conferences, summits, forums, and meetups in Madison or Wisconsin

HARD REJECT (is_opportunity false):
- Networking and generic conferences outside Madison/Wisconsin unless they have a
  student/research path
- Requires US citizenship or citizens-only eligibility
- MD, PhD, master's, postdoc, or graduate-only with no undergrad/bachelor's path
- Incubators or accelerators
- Pure news/press/blog, directories/listicles, permanent full-time jobs
- Off-topic (not healthtech / clinical AI / digital health / medtech / health informatics)
- Any autism, genetics/genomics, pathology, homeopathy/homoeopathy, or radiology opportunity

Be EXCLUSIVE on eligibility mismatches. Prefer fewer high-fit fellowships/labs over many events.

TOPICS: clinical AI, digital health, healthtech, medtech, hospital innovation, health data, CDS, clinical workflow, ambient/documentation AI, biomedical/clinical informatics
REGIONS: Dubai, UAE, Chicago, Midwest, Wisconsin, US, Global, Virtual

Respond ONLY with valid JSON. No markdown fences, no extra text.

If ACCEPT:
{
  "is_opportunity": true,
  "name": "Specific program or fellowship name",
  "type": "Fellowship|Grant|Scholarship|Research Internship|Research Assistant|Summer Program|Student Program|Competition|Leadership Program|Networking|Conference|Summit|Other",
  "deadline": "YYYY-MM-DD or null",
  "region": "Dubai|UAE|Chicago|Midwest|Global|Virtual|Other",
  "eligibility": "Who can participate (max 150 chars)",
  "description": "What it is and why it matters (max 200 chars)",
  "ai_summary": "Fit for international UW-Madison undergrad seeking fellowship/lab/program (max 200 chars)",
  "score": <1-10; 10=perfect fellowship/lab/program match; networking=reject; plain conference≤5>
}

If REJECT:
{
  "is_opportunity": false,
  "reason": "One sentence (cite networking/conference-only, citizenship, degree, incubator, news, or off-topic when relevant)"
}"""


LABS_SYSTEM_PROMPT = """You are OpTrack's labs-track evaluator for an INTERNATIONAL undergraduate (2nd year, CS & Statistics, UW-Madison) targeting US clinical AI / digital health / health informatics research labs (workflow, CDS, RCM, intake, documentation AI). Not a US citizen. Not MD/PhD/master's track.

TASK: Decide if this page offers a way for an UNDERGRAD / bachelor's student to join a lab, research group, or structured research program / fellowship.

ACCEPT (high priority):
- Undergraduate research, URAP, REU, summer research, research internship
- Intern programs at labs/centers open to undergrads
- Fellowships/traineeships aimed at undergrads or bachelor's students
- "Join our lab", "work with us", "research opportunities", openings for students
- Lab/center pages listing student programs or application instructions
- NIH/NLM summer programs, DBMI summer research, lab rotations with apply link open to undergrads

HARD REJECT:
- Networking events, conferences, meetups (wrong track)
- Requires US citizenship
- PhD/postdoc/MD/master's-only with no undergrad path
- Faculty or tenure-track hiring only
- Pure research bio / publication list with zero apply path
- Unrelated corporate full-time jobs
- Incubators or accelerators
- Autism, genetics/genomics, pathology, homeopathy/homoeopathy, or radiology

Be EXCLUSIVE: if graduate-only or citizen-only, REJECT.

TOPICS: clinical AI, health informatics, digital health, CDS, clinical workflow, ambient AI, EHR/RCM automation
LOCATIONS: US academic medical centers preferred; remote/summer OK

Respond ONLY with valid JSON. No markdown fences, no extra text.

If ACCEPT:
{
  "is_opportunity": true,
  "name": "Lab, program, or position name",
  "type": "Research Internship|Research Assistant|Student Research|Summer Program|Research Fellowship|Lab Program|Other",
  "deadline": "YYYY-MM-DD or null",
  "region": "City or institution",
  "eligibility": "Undergrad/student eligibility (max 150 chars)",
  "description": "Lab focus and what you'd do (max 200 chars)",
  "ai_summary": "Why good for international clinical AI undergrad lab path (max 200 chars)",
  "score": <1-10, 10=clinical AI lab explicitly open to undergrads including international>
}

If REJECT:
{
  "is_opportunity": false,
  "reason": "One sentence (cite citizenship, degree, incubator/accelerator, networking, or no undergrad path when relevant)"
}"""


def _system_prompt_for(item: dict) -> str:
    tracks = item.get("tracks")
    track = ("labs" if tracks and "labs" in tracks else
             (tracks[0] if tracks else item.get("track", "general")))
    return LABS_SYSTEM_PROMPT if track == "labs" else SYSTEM_PROMPT


def build_user_prompt(item: dict) -> str:
    title = item.get("scraped_title") or item.get("title", "")
    body = item.get("scraped_body") or item.get("snippet", "")
    url = item.get("url", "")
    query = item.get("source_query", "")
    track = item.get("track", "general")
    return f"""Evaluate this search result for track: {track}

URL: {url}
Google query that found it: {query}
Page title: {title}

Page content (may be truncated):
{body[:3500]}

Hard-reject if: US citizenship required; MD/PhD/master's-only with no undergrad path; incubator/accelerator; news-only; not healthtech. Otherwise extract whether this is a joinable opportunity for an international undergrad."""


def parse_response(text: str | None) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None


def salvage_accepted(text: str | None) -> list[dict]:
    """Recover complete accepted objects from truncated batch JSON."""
    if not text:
        return []
    # Prefer compact {"id":N,"score":N} objects
    compact = re.findall(
        r'\{\s*"id"\s*:\s*(\d+)\s*,\s*"score"\s*:\s*(\d+)\s*\}',
        text,
    )
    if compact:
        return [{"id": int(i), "score": int(s)} for i, s in compact]
    # Fuller objects from richer batch schemas
    full = re.findall(
        r'\{\s*"id"\s*:\s*(\d+)\s*,\s*"name"\s*:\s*"((?:\\.|[^"\\])*)"'
        r'.*?"score"\s*:\s*(\d+)\s*\}',
        text,
        flags=re.DOTALL,
    )
    return [{"id": int(i), "name": n, "score": int(s)} for i, n, s in full]


def evaluate(item: dict, retries: int = 1) -> dict | None:
    if not item.get("scraped_body") and not item.get("snippet"):
        return None

    prompt = build_user_prompt(item)
    system_prompt = _system_prompt_for(item)

    for attempt in range(retries + 1):
        try:
            text = call_llm(system_prompt, prompt)
            parsed = parse_response(text)

            if parsed is None:
                print(f"  [EVAL] Parse failed for {item['url'][:60]}")
                return None

            if not parsed.get("is_opportunity"):
                print(f"  [EVAL] Rejected: {parsed.get('reason', 'not an opportunity')}")
                return None

            score = int(parsed.get("score", 0) or 0)
            if score <= 0:
                print(f"  [EVAL] Rejected: score={score} (hard zero)")
                return None

            opp_type = (parsed.get("type") or "Other").strip()
            if opp_type.lower() in ("accelerator", "incubator"):
                print(f"  [EVAL] Rejected: incubator/accelerator type")
                return None

            item["name"] = parsed.get("name") or item.get("title", "Unnamed")
            item["type"] = opp_type
            item["deadline"] = parsed.get("deadline")
            item["region"] = parsed.get("region", "Unknown")
            item["eligibility"] = parsed.get("eligibility", "")
            item["description"] = parsed.get("description", "")
            item["ai_summary"] = parsed.get("ai_summary", "")
            item["score"] = score
            return item

        except QuotaExhausted:
            raise
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            body = ""
            try:
                body = e.response.text[:200] if e.response is not None else ""
            except Exception:
                pass
            print(f"  [EVAL] HTTP {status}: {body}")
            if attempt < retries:
                time.sleep(5)
                continue
            return None
        except Exception as e:
            print(f"  [EVAL] Error: {e}")
            if attempt < retries:
                time.sleep(5)
                continue
            return None

    return None


def parse_tsv_scores(text: str | None) -> list[dict]:
    """Parse id,score,decision lines from snippet batch response."""
    if not text:
        return []
    results = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.lower().startswith("id,"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            idx = int(parts[0])
            score = int(parts[1])
        except ValueError:
            continue
        decision = parts[2].lower() if len(parts) > 2 else "accept"
        if decision in ("accept", "accepted", "yes"):
            results.append({"id": idx, "score": score})
    return results


def apply_acceptance_gates(
    item: dict,
    score: int,
    min_score: int,
    title: str,
    body: str,
    track: str,
) -> bool:
    """Single score floor (min_score). No hidden 7/8 type gates."""
    if score < min_score:
        return False
    combined = f"{title} {body} {url_derived_text(item.get('url', ''))}".lower()
    opp_type = infer_type(combined, track)
    if opp_type.lower() in ("accelerator", "incubator"):
        return False
    return True


def build_notion_item(
    item: dict,
    score: int,
    title: str,
    body: str,
    track: str,
) -> dict:
    combined = f"{title} {body} {url_derived_text(item.get('url', ''))}".lower()
    opp_type = infer_type(combined, track)
    out = dict(item)
    out["name"] = str(title)[:200]
    out["type"] = opp_type
    out["deadline"] = infer_deadline(combined)
    out["region"] = infer_region(combined)
    out["eligibility"] = "Verify undergraduate and international eligibility"
    out["description"] = body[:200]
    out["ai_summary"] = "Snippet-evaluated fit for international clinical AI undergrad"
    out["score"] = score
    out["track"] = track
    return out


SNIPPET_SYSTEM_PROMPT = """You evaluate search-result snippets for an international
second-year UW-Madison undergraduate (CS & Statistics) focused on clinical AI,
digital health, healthtech, medtech, and hospital innovation.

PRIORITY: fellowships, scholarships, research lab paths (RA/REU/internship),
summer research, structured student programs with an apply path.
WISCONSIN EXCEPTION: accept joinable healthtech/digital-health/clinical-AI
networking, conferences, summits, forums in Madison or Wisconsin (score 6+).
OpportunitiesForYouth.org health posts: accept if clearly apply-able (score 6+).

Hard reject: US-citizen-only; MD/PhD/master's/postdoc/faculty-only; incubators;
accelerators; full-time jobs; news/listicles; autism/genetics/pathology/
homeopathy/radiology topics.

Return ONLY lines of: id,score,decision
- id: matches input row id
- score: 1-10 (accept only if 6+)
- decision: accept or reject
No markdown, no JSON, no commentary."""


def _items_to_tsv(candidates: list[dict]) -> str:
    lines = ["id|title|snippet|url|track"]
    for c in candidates:
        title = (c.get("title") or "").replace("|", "/")[:200]
        snippet = (c.get("snippet") or "").replace("|", "/")[:300]
        url = (c.get("url") or "").replace("|", "/")[:200]
        track = c.get("track", "general")
        lines.append(f"{c['id']}|{title}|{snippet}|{url}|{track}")
    return "\n".join(lines)


def _run_snippet_chunk(model: str, chunk: list[dict]) -> list[dict] | None:
    tsv = _items_to_tsv(chunk)
    user_prompt = f"""Score each row. Output only id,score,decision lines for accepts (score>=6).

ROWS:
{tsv}"""
    key = _next_key()
    response = _chat_request(
        key, model, SNIPPET_SYSTEM_PROMPT, user_prompt, max_tokens=1500,
    )
    _record_key_use(key)
    if response.status_code in (429, 402):
        print(f"[EVAL] Snippet chunk unavailable: HTTP {response.status_code}")
        return None
    response.raise_for_status()
    content = (
        (response.json().get("choices") or [{}])[0]
        .get("message", {})
        .get("content")
    )
    Path("logs/last_snippet_response.txt").write_text(str(content or ""))
    parsed = parse_tsv_scores(content)
    if parsed:
        return parsed
    salvaged = salvage_accepted(content)
    if salvaged:
        print(f"[EVAL] Salvaged {len(salvaged)} from snippet response")
        return salvaged
    return None


def snippet_batch_eval(
    items: list[dict],
    min_score: int = 6,
    max_eval: int = 0,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Evaluate snippets only (no scrape required).
    Returns (snippet_only_accepts, scrape_queued_accepts, failed_items).
    """
    _check_openrouter()
    model = _model()
    budget = _remaining_daily_budget()
    print(
        f"[EVAL] Snippet batch — model: {model} — "
        f"request budget left: {budget}"
    )

    to_eval = items
    remaining: list[dict] = []
    cap = max_eval if max_eval and max_eval > 0 else len(items)
    if budget <= 0:
        print("[EVAL] Daily free-tier budget exhausted")
        return [], [], items
    if len(items) > cap:
        to_eval = items[:cap]
        remaining = items[cap:]

    candidates = []
    for idx, item in enumerate(to_eval):
        candidates.append({
            "id": idx,
            "track": "labs" if "labs" in (item.get("tracks") or [])
            else item.get("track", "general"),
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "url": item.get("url", ""),
        })

    if not candidates:
        return [], [], remaining

    chunks = [
        candidates[i:i + SNIPPET_CHUNK_SIZE]
        for i in range(0, len(candidates), SNIPPET_CHUNK_SIZE)
    ]
    print(f"[EVAL] {len(candidates)} snippets in {len(chunks)} chunk(s) "
          f"(≤{SNIPPET_CHUNK_SIZE} each)")

    batch_results: list[dict] = []
    failed_ids: set[int] = set()
    for i, chunk in enumerate(chunks, 1):
        try:
            print(f"[EVAL] Snippet chunk {i}/{len(chunks)} — {len(chunk)} rows")
            result = _run_snippet_chunk(model, chunk)
            if result is None:
                failed_ids.update(c["id"] for c in chunk)
                continue
            batch_results.extend(result)
        except Exception as e:
            print(f"[EVAL] Snippet chunk {i} failed: {e}")
            failed_ids.update(c["id"] for c in chunk)

    failed_items = [to_eval[i] for i in sorted(failed_ids)] + remaining
    snippet_accepts: list[dict] = []
    scrape_queue: list[dict] = []

    for result in batch_results:
        try:
            idx = int(result["id"])
            score = int(result.get("score", 0) or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if idx < 0 or idx >= len(to_eval):
            continue
        item = to_eval[idx]
        title = item.get("title", "")
        body = item.get("snippet", "")
        track = (
            "labs" if "labs" in (item.get("tracks") or [])
            else item.get("track", "general")
        )
        if not apply_acceptance_gates(item, score, min_score, title, body, track):
            continue
        built = build_notion_item(item, score, title, body, track)
        built["snippet_score"] = score
        if item.get("snippet_only"):
            snippet_accepts.append(built)
            print(f"  → SNIPPET-ONLY: {built['name'][:55]} | score={score}")
        else:
            scrape_queue.append(built)
            print(f"  → SCRAPE-QUEUE: {built['name'][:55]} | score={score}")

    print(
        f"[EVAL] Snippet pass: {len(snippet_accepts)} fast-path, "
        f"{len(scrape_queue)} need scrape, {len(failed_items)} failed/capped"
    )
    return snippet_accepts, scrape_queue, failed_items


ENRICH_SYSTEM_PROMPT = """Confirm scraped opportunities for an international UW-Madison
undergrad (clinical AI / digital health focus). Hard reject citizen-only, grad-only,
incubators, jobs, off-topic.

Return ONLY JSON: {"accepted":[{"id":0,"score":8}]}
Include only still-valid opportunities. id must match input."""


def enrich_batch(
    items: list[dict],
    min_score: int = 6,
) -> tuple[list[dict], list[dict]]:
    """Re-score scraped pages after full body fetch."""
    if not items:
        return [], []

    _check_openrouter()
    model = _model()
    all_accepted: list[dict] = []
    accepted_indices: set[int] = set()

    for chunk_start in range(0, len(items), BATCH_CHUNK_SIZE):
        chunk_items = items[chunk_start:chunk_start + BATCH_CHUNK_SIZE]
        candidates = []
        local_to_global: list[int] = []

        for local_id, item in enumerate(chunk_items):
            global_id = chunk_start + local_id
            body = item.get("scraped_body") or item.get("snippet", "")
            combined = f"{item.get('title','')} {body}".lower()
            reject = next(
                (s for s in POSTSCRAPE_REJECT_SIGNALS if s in combined), None
            )
            if reject:
                print(f"  [POSTSCRAPE] skip: {reject}")
                continue
            local_to_global.append(global_id)
            candidates.append({
                "id": len(candidates),
                "title": item.get("scraped_title") or item.get("title", ""),
                "url": item.get("url", ""),
                "content": body[:1500],
            })

        if not candidates:
            continue

        prompt = f"""Confirm these scraped pages. Return JSON accepted array id+score only.

CANDIDATES:
{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}"""

        try:
            key = _next_key()
            response = _chat_request(
                key, model, ENRICH_SYSTEM_PROMPT, prompt, max_tokens=2000,
            )
            _record_key_use(key)
            if response.status_code in (429, 402):
                continue
            response.raise_for_status()
            content = (
                (response.json().get("choices") or [{}])[0]
                .get("message", {})
                .get("content")
            )
            parsed = parse_response(content)
            if not isinstance(parsed, dict):
                salvaged = salvage_accepted(content)
                parsed = {"accepted": salvaged} if salvaged else None
            if not parsed:
                continue
            accepted_raw = parsed.get("accepted", [])
        except Exception as e:
            print(f"[EVAL] Enrich chunk failed: {e}")
            continue

        for result in accepted_raw:
            try:
                local_idx = int(result["id"])
                score = int(result.get("score", 0) or 0)
                if local_idx < 0 or local_idx >= len(local_to_global):
                    continue
                global_idx = local_to_global[local_idx]
            except (TypeError, ValueError, KeyError):
                continue
            item = items[global_idx]
            title = item.get("scraped_title") or item.get("title", "")
            body = item.get("scraped_body") or item.get("snippet", "")
            track = (
                "labs" if "labs" in (item.get("tracks") or [])
                else item.get("track", "general")
            )
            if not apply_acceptance_gates(item, score, min_score, title, body, track):
                continue
            built = build_notion_item(item, score, title, body, track)
            built["full_score"] = score
            all_accepted.append(built)
            accepted_indices.add(global_idx)
            print(f"  → ENRICHED: {built['name'][:55]} | score={score}")

    failed = [items[i] for i in range(len(items)) if i not in accepted_indices]
    return all_accepted, failed


def batch_evaluate(
    items: list[dict],
    min_score: int = 6,
    delay: float = DEFAULT_EVAL_DELAY,
    max_eval: int = 0,
) -> tuple[list[dict], list[dict]]:
    """Legacy wrapper: snippet eval + scrape queue metadata."""
    snippet_accepts, scrape_queue, failed = snippet_batch_eval(
        items, min_score=min_score, max_eval=max_eval,
    )
    for item in scrape_queue:
        item["_needs_scrape"] = True
    accepted = snippet_accepts + scrape_queue
    return accepted, failed

