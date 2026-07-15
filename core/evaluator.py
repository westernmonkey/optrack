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
from core.prefilter import is_junk

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
USAGE_PATH = Path("data/openrouter_usage.json")

# Free tier with 0 credits purchased: 20 req/min, 50 req/day per key.
FREE_RPM = 20
FREE_RPD = 50
MIN_KEY_INTERVAL = 60.0 / FREE_RPM  # 3.0s between uses of the same key
DEFAULT_EVAL_DELAY = 3.1
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


def batch_evaluate(
    items: list[dict],
    min_score: int = 5,
    delay: float = DEFAULT_EVAL_DELAY,
    max_eval: int = 0,
) -> tuple[list[dict], list[dict]]:
    """
    Evaluate all selected items in one OpenRouter request.
    Returns (accepted, remaining_unevaluated).
    Items are compacted to keep the single request within model context limits.
    """
    _check_openrouter()
    model = _model()
    keys = _api_keys()
    budget = _remaining_daily_budget()
    print(
        f"[EVAL] OpenRouter batch — model: {model} — {len(keys)} key(s) — "
        f"request budget left: {budget}"
    )

    to_eval = items
    remaining: list[dict] = []
    cap = max_eval if max_eval and max_eval > 0 else len(items)
    if budget <= 0:
        print("[EVAL] Daily free-tier budget exhausted (50 req/day per key)")
        return [], items
    if len(items) > cap:
        to_eval = items[:cap]
        remaining = items[cap:]
        print(f"[EVAL] Capped at {cap}/{len(items)} (max-eval); "
              f"{len(remaining)} left for later")

    candidates = []
    eligible_ids = set()
    for idx, item in enumerate(to_eval):
        body = item.get("scraped_body") or item.get("snippet") or ""
        probe = dict(item)
        probe["title"] = item.get("scraped_title") or item.get("title", "")
        probe["snippet"] = body
        junk, reason = is_junk(probe)
        combined_probe = f"{probe['title']} {probe['snippet']}".lower()
        local_reject = next(
            (signal for signal in POSTSCRAPE_REJECT_SIGNALS if signal in combined_probe),
            None,
        )
        if local_reject:
            junk, reason = True, f"post-scrape reject signal: '{local_reject}'"
        if junk:
            print(f"  [POSTFILTER] {idx}: {reason}")
            continue
        eligible_ids.add(idx)
        candidates.append({
            "id": idx,
            "track": "labs" if "labs" in item.get("tracks", []) else item.get("track", "general"),
            "title": item.get("scraped_title") or item.get("title", ""),
            "url": item.get("url", ""),
            "content": body[:1200],
        })

    batch_prompt = f"""Evaluate every candidate below using the profile and hard-reject rules.
Return ONLY one JSON object with an "accepted" array.
Include ONLY accepted opportunities; omission means rejected.
Each accepted object must contain ONLY id and score.
The id must exactly match the input candidate id. Score must be 1-10.
Never accept incubators, accelerators, news/listicles, off-topic pages,
US-citizen-only opportunities, MD/PhD/master's/graduate-only opportunities,
or autism, genetics/genomics, pathology, homeopathy/homoeopathy, or radiology.
Do accept joinable Madison/Wisconsin healthtech networking, conferences, and summits.

CANDIDATES:
{json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))}"""
    batch_system_prompt = """You evaluate opportunities for an international
second-year UW-Madison undergraduate studying computer science and statistics,
focused on clinical AI, digital health, healthtech, medtech, clinical workflow,
health data, biomedical/clinical informatics, and hospital innovation.

PRIORITY: fellowships, scholarships, research lab join paths (RA/REU/internship),
summer research, and structured student programs with an apply path.
WISCONSIN EXCEPTION: accept all joinable healthtech, digital-health, clinical-AI,
health-data, hospital-innovation, and medtech networking events, meetups,
conferences, summits, and forums in Madison or Wisconsin.
Outside Madison/Wisconsin, deprioritize networking and generic conferences.

Hard reject US-citizen-only opportunities; MD, PhD, master's, postdoc, faculty,
or graduate-only opportunities with no undergraduate path; incubators;
accelerators; jobs; news, recaps, blogs, and directories; and unrelated topics.
Also hard reject autism, genetics/genomics, pathology, homeopathy/homoeopathy,
and radiology opportunities regardless of location.
Accept only specific, currently joinable apply/join opportunities.

Return exactly one valid JSON object of this shape:
{"accepted":[{"id":0,"score":8}]}
Omit every rejected candidate. Do not include markdown or commentary.
Score 7–10 for fellowships/labs/programs. Madison/Wisconsin events may score
4–8 based on relevance; omit networking elsewhere; plain conference elsewhere≤5."""

    key = _next_key()
    try:
        response = _chat_request(
            key,
            model,
            batch_system_prompt,
            batch_prompt,
            max_tokens=6000,
        )
        _record_key_use(key)
        if response.status_code in (429, 402):
            print(f"[EVAL] Batch request unavailable: HTTP {response.status_code}")
            return [], to_eval + remaining
        response.raise_for_status()
        content = (
            (response.json().get("choices") or [{}])[0]
            .get("message", {})
            .get("content")
        )
        Path("logs/last_batch_response.txt").write_text(str(content or ""))
        parsed = parse_response(content)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("accepted"), list):
            salvaged = salvage_accepted(content)
            if salvaged:
                print(f"[EVAL] Truncated JSON — salvaged {len(salvaged)} accepted objects")
                parsed = {"accepted": salvaged}
            else:
                print("[EVAL] Batch response was not valid accepted-array JSON")
                return [], to_eval + remaining
    except Exception as e:
        print(f"[EVAL] Batch request failed: {e}")
        return [], to_eval + remaining

    if not isinstance(parsed, dict) or not isinstance(parsed.get("accepted"), list):
        print("[EVAL] Batch response was not valid accepted-array JSON")
        return [], to_eval + remaining

    accepted = []
    for result in parsed["accepted"]:
        if not isinstance(result, dict):
            continue
        try:
            idx = int(result.get("id"))
            score = int(result.get("score", 0) or 0)
        except (TypeError, ValueError):
            continue
        if idx not in eligible_ids or score < min_score:
            continue
        item = to_eval[idx]
        body = item.get("scraped_body") or item.get("snippet") or ""
        title = (
            result.get("name")
            or item.get("scraped_title")
            or item.get("title")
            or "Unnamed"
        )
        track = (
            "labs" if "labs" in item.get("tracks", [])
            else item.get("track", "general")
        )
        combined = (
            f"{title} {body} {url_derived_text(item.get('url', ''))}"
        ).lower()
        opp_type = infer_type(combined, track)
        if opp_type.lower() in ("accelerator", "incubator"):
            continue
        is_wisconsin_event = (
            ("madison" in combined or "wisconsin" in combined)
            and _is_low_priority_type(opp_type)
        )
        # Madison/Wisconsin events are explicitly included; deprioritize elsewhere.
        if _is_low_priority_type(opp_type):
            if is_wisconsin_event:
                pass
            elif "network" in opp_type.lower() or "meetup" in opp_type.lower():
                print(f"  → SKIP (networking): {str(title)[:50]}")
                continue
            elif score < 8:
                print(f"  → SKIP (low-priority {opp_type}, score={score}): {str(title)[:50]}")
                continue
        if not is_wisconsin_event and not _is_high_priority_type(opp_type) and score < 7:
            print(f"  → SKIP (not fellowship/lab/program): {str(title)[:50]}")
            continue
        item["name"] = str(title)[:200]
        item["type"] = opp_type
        item["deadline"] = infer_deadline(combined)
        item["region"] = infer_region(combined)
        item["eligibility"] = "Verify undergraduate and international eligibility"
        item["description"] = body[:200]
        item["ai_summary"] = (
            f"Batch-evaluated fit for international clinical AI undergrad"
        )
        item["score"] = score
        accepted.append(item)
        print(f"  → ACCEPTED: {item['name'][:60]} | score={score} | {opp_type}")

    print(f"[EVAL] {len(accepted)}/{len(to_eval)} accepted from 1 API request "
          f"(score ≥ {min_score})")
    if remaining:
        print(f"[EVAL] {len(remaining)} items remaining unevaluated")
    return accepted, remaining
