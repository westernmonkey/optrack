"""
evaluator.py
Sends each scraped page to Claude (claude-haiku-3-5) for evaluation.
Claude decides:
  - Is this a real program someone can apply/register for?
  - What are the key details (deadline, eligibility, type, region)?
  - Score 1-10 for relevance to the user's interests.

Cost: ~$0.02 per daily run, ~$0.06 per weekly run.
"""

import json
import os
import time

import anthropic

CLIENT = None


def get_client() -> anthropic.Anthropic:
    global CLIENT
    if CLIENT is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set. Check your .env file.")
        CLIENT = anthropic.Anthropic(api_key=api_key)
    return CLIENT


SYSTEM_PROMPT = """You are an opportunity evaluator for a researcher in clinical AI and digital health based in Dubai and Chicago.

Your job: read a webpage and decide if it describes a real program that a person can apply or register for — such as a fellowship, accelerator, grant, scholarship, leadership program, incubator, hackathon, pitch competition, or similar. 

The user's interests:
- Topics: clinical AI, digital health, healthtech, medtech, hospital innovation, health data, regulatory science
- Regions: Dubai, UAE, Chicago, Midwest US, MENA, Global

Respond ONLY with a JSON object. No preamble, no markdown fences.

If it IS a real registerable opportunity:
{
  "is_opportunity": true,
  "name": "Program name",
  "type": "Fellowship|Accelerator|Grant|Scholarship|Hackathon|Leadership Program|Incubator|Competition|Other",
  "deadline": "YYYY-MM-DD or null if not found",
  "region": "Dubai|UAE|Chicago|Midwest|MENA|Global|Other",
  "eligibility": "Short description of who can apply (max 150 chars)",
  "description": "What the program is (max 200 chars)",
  "ai_summary": "Why this is or isn't interesting for a clinical AI researcher in Dubai/Chicago (max 200 chars)",
  "score": <integer 1-10, where 10 = perfect match for clinical AI + Dubai/Chicago>
}

If it is NOT a real registerable opportunity (news article, job posting, general info page, conference coverage, etc.):
{
  "is_opportunity": false,
  "reason": "Brief reason"
}"""


def build_user_prompt(item: dict) -> str:
    title = item.get("scraped_title") or item.get("title", "")
    body = item.get("scraped_body") or item.get("snippet", "")
    url = item.get("url", "")
    query = item.get("source_query", "")
    return f"""URL: {url}
Search query that found this: {query}
Page title: {title}
Page content:
{body[:3500]}"""


def parse_response(text: str) -> dict | None:
    """Parse Claude's JSON response. Returns None on failure."""
    text = text.strip()
    # Strip markdown fences if Claude ignored instructions
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def evaluate(item: dict, retries: int = 1) -> dict | None:
    """
    Evaluate a single item. Returns enriched item dict or None if rejected/failed.
    """
    # Skip if scraper totally failed and we have no body or snippet
    if not item.get("scraped_body") and not item.get("snippet"):
        return None

    client = get_client()
    prompt = build_user_prompt(item)

    for attempt in range(retries + 1):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            parsed = parse_response(text)

            if parsed is None:
                print(f"  [EVAL] Parse failed for {item['url'][:60]}")
                return None

            if not parsed.get("is_opportunity"):
                print(f"  [EVAL] Rejected: {parsed.get('reason', 'not an opportunity')}")
                return None

            # Merge evaluation results into item
            item["name"]        = parsed.get("name") or item.get("title", "Unnamed")
            item["type"]        = parsed.get("type", "Other")
            item["deadline"]    = parsed.get("deadline")
            item["region"]      = parsed.get("region", "Unknown")
            item["eligibility"] = parsed.get("eligibility", "")
            item["description"] = parsed.get("description", "")
            item["ai_summary"]  = parsed.get("ai_summary", "")
            item["score"]       = int(parsed.get("score", 5))
            return item

        except anthropic.RateLimitError:
            print("  [EVAL] Rate limit hit — waiting 30s")
            time.sleep(30)
            continue
        except Exception as e:
            print(f"  [EVAL] Error: {e}")
            if attempt < retries:
                time.sleep(5)
                continue
            return None

    return None


def batch_evaluate(items: list[dict], min_score: int = 5, delay: float = 0.3) -> list[dict]:
    """
    Evaluate all items with Claude. Filter by min_score.
    Returns only accepted, high-quality opportunities.
    """
    accepted = []
    total = len(items)

    for i, item in enumerate(items, 1):
        print(f"[EVAL {i}/{total}] {item.get('url', '')[:70]}")
        result = evaluate(item)
        if result and result.get("score", 0) >= min_score:
            accepted.append(result)
            print(f"  → ACCEPTED: {result['name'][:60]} | score={result['score']}")
        time.sleep(delay)

    print(f"[EVAL] {len(accepted)}/{total} items accepted (score ≥ {min_score})")
    return accepted
