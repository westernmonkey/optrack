"""
evaluator.py — structured OpenRouter batch evaluation.

One JSON contract for snippet and full-page passes.
Valid empty/reject responses are terminal; malformed/partial are retryable.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

from core.filter_policy import (
    EVAL_SYSTEM_PROMPT,
    MIN_SCORE,
    apply_acceptance_gates,
    infer_deadline,
    infer_type,
)
from core.heuristic_parser import infer_region

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
USAGE_PATH = Path("data/openrouter_usage.json")

FREE_RPM = 20
FREE_RPD = 50
MIN_KEY_INTERVAL = 60.0 / FREE_RPM
DEFAULT_EVAL_DELAY = 3.1
SNIPPET_CHUNK_SIZE = 20
BATCH_CHUNK_SIZE = 12

DEFAULT_MODEL = "openrouter/free"
FREE_FALLBACK_MODELS = [
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
]


class QuotaExhausted(Exception):
    """All OpenRouter API keys are rate-limited or out of credits."""


class EvalParseError(Exception):
    """Response was not valid structured JSON covering all IDs."""


# ---------------------------------------------------------------------------
# Key / quota management
# ---------------------------------------------------------------------------

_key_index = 0
_exhausted_keys: set[str] = set()
_key_last_used: dict[str, float] = {}


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
            "(comma-separated) or OPENROUTER_API_KEY."
        )


def _key_id(key: str) -> str:
    return key[-8:] if len(key) >= 8 else key


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
    data = _load_usage()
    return int(data.get("keys", {}).get(_key_id(key), 0))


def _record_key_use(key: str) -> None:
    """Count usage only after a successful HTTP response."""
    data = _load_usage()
    kid = _key_id(key)
    data.setdefault("keys", {})
    data["keys"][kid] = int(data["keys"].get(kid, 0)) + 1
    data["date"] = str(date.today())
    _save_usage(data)
    _key_last_used[key] = time.time()


def remaining_daily_budget() -> int:
    keys = _api_keys()
    if not keys:
        return 0
    used = sum(_key_daily_count(k) for k in keys)
    return max(0, FREE_RPD * len(keys) - used)


def _wait_for_key(key: str) -> None:
    last = _key_last_used.get(key, 0)
    wait = MIN_KEY_INTERVAL - (time.time() - last)
    if wait > 0:
        time.sleep(wait)


def _is_upstream_rate_limit(response: requests.Response) -> bool:
    if response.status_code == 429:
        return True
    try:
        body = response.text.lower()
    except Exception:
        return False
    return "rate" in body and "limit" in body


def _next_key() -> str:
    global _key_index
    keys = _api_keys()
    if not keys:
        raise QuotaExhausted("No OpenRouter keys configured")
    available = [k for k in keys if k not in _exhausted_keys and _key_daily_count(k) < FREE_RPD]
    if not available:
        raise QuotaExhausted("All OpenRouter keys exhausted for today")
    for _ in range(len(available)):
        key = available[_key_index % len(available)]
        _key_index += 1
        if _key_daily_count(key) < FREE_RPD:
            return key
    raise QuotaExhausted("All OpenRouter keys exhausted for today")


def _mark_exhausted(key: str) -> None:
    _exhausted_keys.add(key)
    print(f"[OPENROUTER] Key …{_key_id(key)} marked exhausted")


def reset_exhausted_keys() -> None:
    _exhausted_keys.clear()


# ---------------------------------------------------------------------------
# HTTP / retry
# ---------------------------------------------------------------------------

def _chat_request(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str | None = None,
    key: str | None = None,
) -> tuple[str, str, str]:
    """
    Returns (content, model_used, key_used).
    Raises QuotaExhausted / requests errors.
    Usage is recorded only on HTTP 200.
    """
    _check_openrouter()
    models = [model] if model else _models()
    last_err: Exception | None = None

    for model_name in models:
        for _attempt in range(len(_api_keys()) + 1):
            try:
                key_used = key or _next_key()
            except QuotaExhausted:
                raise
            _wait_for_key(key_used)
            headers = {
                "Authorization": f"Bearer {key_used}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/westernmonkey/optrack",
                "X-Title": "OpTrack",
            }
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 4000,
            }
            try:
                r = requests.post(
                    OPENROUTER_URL, headers=headers, json=payload, timeout=90
                )
            except requests.RequestException as e:
                last_err = e
                print(f"[OPENROUTER] Request error ({model_name}): {e}")
                time.sleep(2)
                continue

            if r.status_code == 200:
                _record_key_use(key_used)
                data = r.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    or ""
                )
                return content, model_name, key_used

            if r.status_code in (401, 403):
                _mark_exhausted(key_used)
                last_err = RuntimeError(f"auth {r.status_code}")
                continue
            if _is_upstream_rate_limit(r):
                print(f"[OPENROUTER] 429 on {model_name} / …{_key_id(key_used)}")
                _mark_exhausted(key_used)
                last_err = RuntimeError("rate_limited")
                time.sleep(2)
                continue
            if r.status_code >= 500:
                last_err = RuntimeError(f"server {r.status_code}")
                time.sleep(3)
                continue

            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            print(f"[OPENROUTER] {last_err}")
            break  # try next model

    raise QuotaExhausted(f"OpenRouter failed: {last_err}")


def call_llm(system_prompt: str, user_prompt: str) -> str:
    content, _, _ = _chat_request(system_prompt, user_prompt)
    return content


def log_raw_response(label: str, text: str) -> None:
    Path("logs").mkdir(exist_ok=True)
    path = Path("logs/last_batch_response.txt")
    path.write_text(f"# {label}\n{text or ''}")


# ---------------------------------------------------------------------------
# Structured response contract
# ---------------------------------------------------------------------------

_RESULT_KEYS = {"id", "decision", "score", "reason", "eligibility_confidence"}


def _extract_json_object(text: str) -> dict | None:
    if not text or not text.strip():
        return None
    cleaned = text.strip()
    # Strip markdown fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # Direct parse
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            return {"results": obj}
    except json.JSONDecodeError:
        pass
    # Find outermost { ... }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(cleaned[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def parse_eval_response(
    text: str | None,
    expected_ids: list[int],
) -> list[dict]:
    """
    Validate structured batch response.
    Raises EvalParseError on malformed / partial / duplicate IDs.
    A valid response with all rejects (or accepted:[]) is OK.
    """
    if text is None or not str(text).strip():
        raise EvalParseError("empty_response")

    obj = _extract_json_object(str(text))
    if obj is None:
        raise EvalParseError("non_json_response")

    # Accept either {"results":[...]} or legacy {"accepted":[...],"rejected":[...]}
    rows: list[dict] = []
    if "results" in obj and isinstance(obj["results"], list):
        rows = obj["results"]
    elif "accepted" in obj or "rejected" in obj:
        for row in obj.get("accepted") or []:
            if isinstance(row, dict):
                row = dict(row)
                row.setdefault("decision", "accept")
                rows.append(row)
        for row in obj.get("rejected") or []:
            if isinstance(row, dict):
                row = dict(row)
                row.setdefault("decision", "reject")
                rows.append(row)
        # Valid zero-accept: {"accepted":[]} with no rejected covering nothing
        # is only OK if expected_ids is empty; otherwise treat as partial.
    else:
        raise EvalParseError("missing_results_key")

    if not isinstance(rows, list):
        raise EvalParseError("results_not_list")

    # Special case: explicit empty accepted with empty expected is fine;
    # {"accepted":[]} with expected IDs and no rejected → partial.
    if (
        not rows
        and expected_ids
        and "accepted" in obj
        and isinstance(obj.get("accepted"), list)
        and not obj.get("rejected")
    ):
        # Plan: parsed {"accepted":[]} is a valid zero-accept response
        # Meaning: model accepted none — treat all as reject? Or retry?
        # Plan says: "A parsed {"accepted":[]} is a valid zero-accept response"
        # So all expected IDs are implicit rejects.
        return [
            {
                "id": i,
                "decision": "reject",
                "score": 1,
                "reason": "zero_accept_response",
                "eligibility_confidence": "low",
            }
            for i in expected_ids
        ]

    seen: set[int] = set()
    normalized: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise EvalParseError("row_not_object")
        if "id" not in row:
            raise EvalParseError("row_missing_id")
        try:
            rid = int(row["id"])
        except (TypeError, ValueError) as e:
            raise EvalParseError("bad_id") from e
        if rid in seen:
            raise EvalParseError(f"duplicate_id:{rid}")
        seen.add(rid)

        decision = str(row.get("decision", "")).strip().lower()
        if decision not in ("accept", "reject"):
            # Infer from score if present
            try:
                sc = int(row.get("score", 0))
            except (TypeError, ValueError):
                sc = 0
            decision = "accept" if sc >= MIN_SCORE else "reject"

        try:
            score = int(row.get("score", 1 if decision == "reject" else MIN_SCORE))
        except (TypeError, ValueError) as e:
            raise EvalParseError(f"bad_score_id:{rid}") from e
        if score < 1 or score > 10:
            raise EvalParseError(f"score_out_of_range:{rid}")

        conf = str(row.get("eligibility_confidence", "medium")).lower()
        if conf not in ("high", "medium", "low"):
            conf = "medium"

        normalized.append({
            "id": rid,
            "decision": decision,
            "score": score,
            "reason": str(row.get("reason", ""))[:300],
            "eligibility_confidence": conf,
        })

    missing = [i for i in expected_ids if i not in seen]
    extra = [i for i in seen if i not in expected_ids]
    if missing:
        raise EvalParseError(f"missing_ids:{missing}")
    if extra:
        raise EvalParseError(f"unknown_ids:{extra}")

    return normalized


def build_notion_item(
    item: dict,
    score: int,
    title: str,
    body: str,
    track: str,
) -> dict:
    text = f"{title} {body}"
    opp_type = infer_type(text, track)
    deadline = infer_deadline(text)
    region = infer_region(text.lower())
    return {
        "url": item["url"],
        "name": (title or item.get("title") or "Unnamed")[:200],
        "type": opp_type,
        "deadline": deadline or "",
        "region": region,
        "score": score,
        "snippet_score": item.get("snippet_score", score),
        "full_score": score,
        "source_query": item.get("source_query", ""),
        "track": track,
        "tracks": item.get("tracks") or [track],
        "title": item.get("title", ""),
        "snippet": item.get("snippet", ""),
        "scraped_title": item.get("scraped_title", ""),
        "scraped_body": item.get("scraped_body", ""),
        "snippet_only": item.get("snippet_only", False),
    }


def _items_to_compact(candidates: list[dict], *, full: bool = False) -> str:
    lines = []
    for i, item in enumerate(candidates):
        title = item.get("scraped_title") or item.get("title") or ""
        if full:
            body = (item.get("scraped_body") or item.get("snippet") or "")[:1200]
        else:
            body = (item.get("snippet") or "")[:400]
        url = item.get("url", "")
        lines.append(
            json.dumps({"id": i, "title": title[:200], "url": url, "text": body})
        )
    return "[\n" + ",\n".join(lines) + "\n]"


def _run_structured_chunk(
    chunk: list[dict],
    *,
    full: bool = False,
) -> tuple[list[dict] | None, str, str]:
    """
    Returns (parsed_rows_or_None, error_code, model_used).
    None means retryable failure.
    """
    if remaining_daily_budget() <= 0:
        return None, "quota_exhausted", ""

    user_prompt = (
        "Evaluate these opportunities. Return JSON with a results array "
        "containing one object per id.\n\n"
        + _items_to_compact(chunk, full=full)
    )
    try:
        content, model_used, _ = _chat_request(EVAL_SYSTEM_PROMPT, user_prompt)
    except QuotaExhausted as e:
        return None, f"quota:{e}", ""
    except Exception as e:
        return None, f"request:{e}", ""

    label = "full" if full else "snippet"
    log_raw_response(label, content)

    expected = list(range(len(chunk)))
    try:
        rows = parse_eval_response(content, expected)
        return rows, "", model_used
    except EvalParseError as e:
        print(f"[EVAL] Parse failure ({label}): {e}")
        return None, f"parse:{e}", model_used


# ---------------------------------------------------------------------------
# Public batch APIs
# ---------------------------------------------------------------------------

def snippet_batch_eval(
    candidates: list[dict],
    min_score: int = MIN_SCORE,
    max_eval: int = 0,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Returns (snippet_only_accepts, scrape_queue, retryable_failures).

    Valid rejects are omitted from all three lists (caller marks rejected).
    Retryable failures go in the third list — never silently rejected.
    """
    if not candidates:
        return [], [], []

    _check_openrouter()
    work = candidates[:max_eval] if max_eval and max_eval > 0 else candidates
    leftover = candidates[len(work):] if max_eval and max_eval > 0 else []

    snippet_accepts: list[dict] = []
    scrape_queue: list[dict] = []
    retryable: list[dict] = list(leftover)
    # Track which URLs got a terminal decision
    decided: set[str] = set()

    for start in range(0, len(work), SNIPPET_CHUNK_SIZE):
        if remaining_daily_budget() <= 0:
            print("[EVAL] Daily OpenRouter budget exhausted — queuing remainder")
            retryable.extend(work[start:])
            break

        chunk = work[start:start + SNIPPET_CHUNK_SIZE]
        print(f"[EVAL] Snippet chunk {start // SNIPPET_CHUNK_SIZE + 1} "
              f"({len(chunk)} items, budget={remaining_daily_budget()})")
        rows, err, model_used = _run_structured_chunk(chunk, full=False)

        if rows is None:
            for item in chunk:
                item["last_error"] = err
                item["last_model"] = model_used
                retryable.append(item)
            time.sleep(DEFAULT_EVAL_DELAY)
            continue

        by_id = {r["id"]: r for r in rows}
        for i, item in enumerate(chunk):
            row = by_id[i]
            score = int(row["score"])
            decision = row["decision"]
            reason = row.get("reason", "")
            decided.add(item["url"])

            if decision == "reject" or score < min_score:
                item["rejection_reason"] = reason or f"score_{score}"
                item["_terminal_reject"] = True
                item["snippet_score"] = score
                continue

            accepted, gate_reason = apply_acceptance_gates(
                item, score, min_score, scraped=False
            )
            if not accepted:
                item["rejection_reason"] = gate_reason
                item["_terminal_reject"] = True
                item["snippet_score"] = score
                continue

            title = item.get("title") or "Unnamed"
            body = item.get("snippet") or ""
            track = (
                "labs" if "labs" in (item.get("tracks") or [])
                else item.get("track", "general")
            )
            built = build_notion_item(item, score, title, body, track)
            built["snippet_score"] = score
            built["snippet_only"] = bool(item.get("snippet_only"))
            built["eval_reason"] = reason
            built["last_model"] = model_used

            if built["snippet_only"]:
                snippet_accepts.append(built)
                print(f"  → SNIPPET: {built['name'][:55]} | {score}")
            else:
                # Carry fields needed for scrape
                queued = dict(item)
                queued.update({
                    "snippet_score": score,
                    "score": score,
                    "name": built["name"],
                    "type": built["type"],
                    "last_model": model_used,
                })
                scrape_queue.append(queued)
                print(f"  → SCRAPE: {built['name'][:55]} | {score}")

        time.sleep(DEFAULT_EVAL_DELAY)

    # Attach terminal rejects onto candidates for caller persistence
    for item in work:
        if item.get("_terminal_reject") and item["url"] not in {
            a["url"] for a in snippet_accepts
        } and item["url"] not in {s["url"] for s in scrape_queue}:
            # Already flagged; caller walks candidates for rejects
            pass

    return snippet_accepts, scrape_queue, retryable


def collect_snippet_rejects(candidates: list[dict]) -> list[dict]:
    """Items marked terminal reject during snippet_batch_eval."""
    return [c for c in candidates if c.get("_terminal_reject")]


def enrich_batch(
    scraped: list[dict],
    min_score: int = MIN_SCORE,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Full-page confirmation.
    Returns (accepted, terminal_rejects, retryable_failures).
    Never falls back to accepting snippet scores on empty enrich.
    """
    if not scraped:
        return [], [], []

    _check_openrouter()
    accepted: list[dict] = []
    rejects: list[dict] = []
    retryable: list[dict] = []

    # Separate hard scrape failures
    clean: list[dict] = []
    for item in scraped:
        err = item.get("scrape_error")
        if err:
            # Classification happens in page_scraper; retryable vs terminal
            if item.get("scrape_retryable", True):
                item["last_error"] = f"scrape:{err}"
                retryable.append(item)
            else:
                item["rejection_reason"] = f"scrape:{err}"
                rejects.append(item)
        else:
            clean.append(item)

    for start in range(0, len(clean), BATCH_CHUNK_SIZE):
        if remaining_daily_budget() <= 0:
            print("[EVAL] Budget exhausted during enrich — queuing remainder")
            retryable.extend(clean[start:])
            break

        chunk = clean[start:start + BATCH_CHUNK_SIZE]
        print(f"[EVAL] Enrich chunk {start // BATCH_CHUNK_SIZE + 1} "
              f"({len(chunk)} items)")
        rows, err, model_used = _run_structured_chunk(chunk, full=True)

        if rows is None:
            for item in chunk:
                item["last_error"] = err
                item["last_model"] = model_used
                retryable.append(item)
            time.sleep(DEFAULT_EVAL_DELAY)
            continue

        by_id = {r["id"]: r for r in rows}
        for i, item in enumerate(chunk):
            row = by_id[i]
            score = int(row["score"])
            decision = row["decision"]
            reason = row.get("reason", "")
            title = item.get("scraped_title") or item.get("title") or "Unnamed"
            body = item.get("scraped_body") or item.get("snippet") or ""
            track = (
                "labs" if "labs" in (item.get("tracks") or [])
                else item.get("track", "general")
            )

            if decision == "reject" or score < min_score:
                item["rejection_reason"] = reason or f"score_{score}"
                item["full_score"] = score
                rejects.append(item)
                continue

            ok, gate_reason = apply_acceptance_gates(
                item, score, min_score, scraped=True
            )
            if not ok:
                item["rejection_reason"] = gate_reason
                item["full_score"] = score
                rejects.append(item)
                continue

            built = build_notion_item(item, score, title, body, track)
            built["full_score"] = score
            built["snippet_score"] = item.get("snippet_score", score)
            built["last_model"] = model_used
            built["eval_reason"] = reason
            accepted.append(built)
            print(f"  → FULL: {built['name'][:55]} | {score}")

        time.sleep(DEFAULT_EVAL_DELAY)

    return accepted, rejects, retryable


# Back-compat alias used by older scripts
def apply_acceptance_gates_compat(item, score, min_score, title, body, track):
    ok, _ = apply_acceptance_gates(item, score, min_score, scraped=bool(body))
    return ok
