"""
evaluator.py
Sends each scraped page to a local Ollama model for evaluation.
"""

import json
import os
import time

import requests

DEFAULT_OLLAMA_BASE = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2"


def _ollama_base() -> str:
    return os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE).rstrip("/")


def _ollama_model() -> str:
    explicit = os.environ.get("OLLAMA_MODEL", "").strip()
    if explicit:
        return explicit
    # Auto-pick first installed model if OLLAMA_MODEL unset
    try:
        r = requests.get(f"{_ollama_base()}/api/tags", timeout=5)
        r.raise_for_status()
        models = r.json().get("models", [])
        if models:
            return models[0]["name"]
    except Exception:
        pass
    return DEFAULT_OLLAMA_MODEL


def _check_ollama() -> None:
    try:
        r = requests.get(f"{_ollama_base()}/api/tags", timeout=5)
        r.raise_for_status()
    except Exception as e:
        raise EnvironmentError(
            f"Ollama not reachable at {_ollama_base()}. "
            f"Start it with: ollama serve — then pull a model: ollama pull llama3.2\n"
            f"({e})"
        ) from e


def call_llm(system_prompt: str, user_prompt: str) -> str:
    model = _ollama_model()
    response = requests.post(
        f"{_ollama_base()}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_predict": 500},
        },
        timeout=300,
    )
    response.raise_for_status()
    data = response.json()
    return data["message"]["content"]


SYSTEM_PROMPT = """You are OpTrack's opportunity evaluator for a researcher in clinical AI and digital health (Dubai + Chicago).

TASK: Decide if this webpage is a REAL opportunity someone can register for, apply to, attend (with registration), or join — not just read about.

ACCEPT (be inclusive — when in doubt, accept with a lower score):
- Fellowships, grants, scholarships, accelerators, incubators, open calls
- Conferences, summits, forums, symposia WITH registration/tickets/apply/RSVP/call for abstracts/posters
- Demo days, pitch events, hackathons, innovation challenges, startup competitions
- Networking events, meetups, mixers, happy hours for healthtech/digital health/clinical AI
- Leadership programs, emerging innovator programs, student/early-career programs in health
- Event pages on Eventbrite, Meetup, Luma, university/hospital sites if participation is possible
- Pages that say "register", "apply", "submit", "nominate", "join", "tickets", "save your spot", "applications open", "cohort", "deadline"

REJECT ONLY when clearly NOT joinable:
- Pure news/press/blog recap with no application or registration
- Permanent job postings (full-time staff hire, not a program)
- Generic directory/listicle with no specific program
- Completely unrelated topic (crypto, unrelated industry expo with no health angle)

TOPICS OF INTEREST: clinical AI, digital health, healthtech, medtech, hospital innovation, health data, CDS, clinical workflow, ambient/documentation AI
REGIONS OF INTEREST: Dubai, UAE, Chicago, Midwest, US, Global, Virtual

Respond ONLY with valid JSON. No markdown fences, no extra text.

If ACCEPT:
{
  "is_opportunity": true,
  "name": "Specific program or event name",
  "type": "Fellowship|Grant|Scholarship|Conference|Summit|Networking|Hackathon|Accelerator|Incubator|Demo Day|Competition|Leadership Program|Other",
  "deadline": "YYYY-MM-DD or null",
  "region": "Dubai|UAE|Chicago|Midwest|Global|Virtual|Other",
  "eligibility": "Who can participate (max 150 chars)",
  "description": "What it is and why it matters (max 200 chars)",
  "ai_summary": "Fit for clinical AI / digital health researcher in Dubai-Chicago (max 200 chars)",
  "score": <1-10, 10=perfect match>
}

If REJECT:
{
  "is_opportunity": false,
  "reason": "One sentence"
}"""


LABS_SYSTEM_PROMPT = """You are OpTrack's labs-track evaluator for an undergraduate targeting US clinical AI / digital health / health informatics research labs (workflow, CDS, RCM, intake, documentation AI).

TASK: Decide if this page offers a way for a STUDENT or early-career person to join a lab, research group, or structured research program — even if the title uses generic wording.

ACCEPT (be inclusive — labs rarely say "research assistant" explicitly):
- Undergraduate research, URAP, REU, summer research, research internship
- Generic intern programs at labs/centers: "CERES interns", "summer interns", "student interns", "research interns", "lab interns"
- Fellowships/traineeships aimed at students or recent grads (not faculty)
- "Join our lab", "work with us", "research opportunities", "openings", "positions for students"
- Lab/center pages at .edu hospitals that list student programs, trainee slots, or application instructions
- NIH/NLM summer programs, DBMI summer research, structured lab rotations with apply link
- Pages where a named lab (clinical AI, health informatics, biomedical data science) + student/intern/trainee language co-occur

REJECT ONLY when clearly NOT a student join path:
- Tenure-track or faculty hiring only
- PhD/postdoc-only ads with no undergrad path
- Pure research description / publication list / PI bio with zero way to apply
- Unrelated corporate full-time jobs

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
  "ai_summary": "Why good for clinical AI undergrad path (max 200 chars)",
  "score": <1-10, 10=clinical AI lab explicitly open to undergrads>
}

If REJECT:
{
  "is_opportunity": false,
  "reason": "One sentence"
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

Extract whether this is a joinable opportunity. Look for implicit signals — e.g. "interns", "fellows", "cohort", "register", "apply", "lab", "research group", "student program" — not only exact phrases like "research assistant"."""


def parse_response(text: str) -> dict | None:
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

            item["name"]        = parsed.get("name") or item.get("title", "Unnamed")
            item["type"]        = parsed.get("type", "Other")
            item["deadline"]    = parsed.get("deadline")
            item["region"]      = parsed.get("region", "Unknown")
            item["eligibility"] = parsed.get("eligibility", "")
            item["description"] = parsed.get("description", "")
            item["ai_summary"]  = parsed.get("ai_summary", "")
            item["score"]       = int(parsed.get("score", 5))
            return item

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            body = ""
            try:
                body = e.response.text[:200] if e.response is not None else ""
            except Exception:
                pass
            if status == 429:
                print("  [EVAL] Rate limited — waiting 30s")
                time.sleep(30)
                continue
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


def batch_evaluate(items: list[dict], min_score: int = 5, delay: float = 0.5) -> list[dict]:
    accepted = []
    total = len(items)
    _check_ollama()
    model = _ollama_model()
    print(f"[EVAL] Ollama @ {_ollama_base()} — model: {model}")

    for i, item in enumerate(items, 1):
        print(f"[EVAL {i}/{total}] {item.get('url', '')[:70]}")
        result = evaluate(item)
        if result and result.get("score", 0) >= min_score:
            accepted.append(result)
            print(f"  → ACCEPTED: {result['name'][:60]} | score={result['score']}")
        time.sleep(delay)

    print(f"[EVAL] {len(accepted)}/{total} items accepted (score ≥ {min_score})")
    return accepted
