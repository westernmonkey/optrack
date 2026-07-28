"""
Pytest regression suite for OpTrack stabilize refactor.
All external services are mocked — no Serper/OpenRouter/Notion credits.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure imports work from repo root
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Filter policy
# ---------------------------------------------------------------------------

class TestFilterPolicy:
    def test_accepts_international_undergrad_fellowship(self):
        from core.filter_policy import apply_acceptance_gates, prefilter_item
        item = {
            "url": "https://example.edu/clinical-ai-fellowship",
            "title": "Clinical AI Undergraduate Fellowship 2026",
            "snippet": "Open to international undergrads in digital health. Apply now.",
            "track": "general",
        }
        drop, _ = prefilter_item(item)
        assert not drop
        ok, reason = apply_acceptance_gates(item, 8)
        assert ok, reason

    def test_rejects_africa_only(self):
        from core.filter_policy import hard_reject, prefilter_item
        item = {
            "url": "https://africahealthcollaborative.org/ahif-2026",
            "title": "AHIF 2026 Young African Health Innovators",
            "snippet": "Unlocking Africa healthcare for African youth",
            "track": "general",
        }
        drop, reason = prefilter_item(item)
        assert drop
        assert "africa" in reason.lower() or "hard_reject" in reason or "africa_only" in reason

    def test_rejects_mental_health(self):
        from core.filter_policy import prefilter_item
        item = {
            "url": "https://example.org/youth-mental-health-fellowship",
            "title": "Youth Mental Health Fellowship",
            "snippet": "Behavioral health psychiatry fellowship for students",
            "track": "general",
        }
        drop, reason = prefilter_item(item)
        assert drop

    def test_rejects_listicle(self):
        from core.filter_policy import prefilter_item
        item = {
            "url": "https://scholarships.com/top-10",
            "title": "Top 10 Scholarships to Apply For",
            "snippet": "Scholarships by major — top 20 list",
            "track": "general",
        }
        drop, _ = prefilter_item(item)
        assert drop

    def test_rejects_job_board(self):
        from core.filter_policy import prefilter_item
        item = {
            "url": "https://indeed.com/jobs/clinical-ai",
            "title": "Clinical AI Engineer — We're Hiring",
            "snippet": "Full-time job opening salary range",
            "track": "general",
        }
        drop, _ = prefilter_item(item)
        assert drop

    def test_rejects_graduate_only(self):
        from core.filter_policy import prefilter_item
        item = {
            "url": "https://uni.edu/phd-only-clinical-ai",
            "title": "PhD Only Clinical AI Position",
            "snippet": "Postdoctoral fellowship for graduate students only",
            "track": "general",
        }
        drop, _ = prefilter_item(item)
        assert drop

    def test_rejects_citizenship(self):
        from core.filter_policy import prefilter_item
        item = {
            "url": "https://nih.gov/fellowship",
            "title": "NIH Clinical Informatics Fellowship",
            "snippet": "US citizens only. Citizenship required.",
            "track": "general",
        }
        drop, reason = prefilter_item(item)
        assert drop
        assert "citizen" in reason or "hard_reject" in reason

    def test_madison_event_exception(self):
        from core.filter_policy import apply_acceptance_gates, is_wisconsin_snippet_only
        item = {
            "url": "https://eventbrite.com/e/madison-healthtech-summit-2026",
            "title": "Madison Healthtech Summit 2026",
            "snippet": "Register for digital health conference in Madison Wisconsin",
            "source_query": "healthtech conference Madison",
            "track": "wi_events",
        }
        assert is_wisconsin_snippet_only(item)
        ok, reason = apply_acceptance_gates(item, 6)
        assert ok, reason

    def test_score_below_six_rejected(self):
        from core.filter_policy import apply_acceptance_gates
        item = {
            "url": "https://example.edu/fellowship",
            "title": "Clinical AI Fellowship",
            "snippet": "Digital health undergraduate fellowship apply",
            "track": "general",
        }
        ok, reason = apply_acceptance_gates(item, 5)
        assert not ok
        assert "score" in reason

    def test_scholarship_type_not_grant(self):
        from core.filter_policy import infer_type
        assert infer_type("Digital health scholarship undergraduate") == "Scholarship"
        assert infer_type("NIH research grant opportunity") == "Grant"

    def test_deadline_no_estimated_year(self):
        from core.filter_policy import infer_deadline
        assert infer_deadline("Applications open for 2026 cohort") is None
        assert infer_deadline("Deadline: March 15, 2026") == "March 15, 2026"

    def test_ofy_not_snippet_only(self):
        from core.filter_policy import is_ofy_snippet_only, is_snippet_only
        item = {
            "url": "https://opportunitiesforyouth.org/clinical-ai-fellowship",
            "title": "Clinical AI Fellowship — Apply Now",
            "snippet": "Digital health fellowship deadline 2026",
            "track": "general",
        }
        assert not is_ofy_snippet_only(item)
        assert not is_snippet_only(item)


# ---------------------------------------------------------------------------
# Evaluator contract
# ---------------------------------------------------------------------------

class TestEvaluatorContract:
    def test_valid_empty_accepted_is_zero_accept(self):
        from core.evaluator import parse_eval_response
        rows = parse_eval_response('{"accepted":[]}', [0, 1])
        assert len(rows) == 2
        assert all(r["decision"] == "reject" for r in rows)

    def test_malformed_prose_is_parse_error(self):
        from core.evaluator import EvalParseError, parse_eval_response
        with pytest.raises(EvalParseError):
            parse_eval_response(
                "Sure! Item 0 looks great, score 8. Item 1 is a reject.",
                [0, 1],
            )

    def test_partial_response_is_parse_error(self):
        from core.evaluator import EvalParseError, parse_eval_response
        with pytest.raises(EvalParseError) as ei:
            parse_eval_response(
                '{"results":[{"id":0,"decision":"accept","score":8,'
                '"reason":"good","eligibility_confidence":"high"}]}',
                [0, 1],
            )
        assert "missing_ids" in str(ei.value)

    def test_full_results_ok(self):
        from core.evaluator import parse_eval_response
        payload = {
            "results": [
                {"id": 0, "decision": "accept", "score": 8,
                 "reason": "fit", "eligibility_confidence": "high"},
                {"id": 1, "decision": "reject", "score": 2,
                 "reason": "citizen", "eligibility_confidence": "high"},
            ]
        }
        rows = parse_eval_response(json.dumps(payload), [0, 1])
        assert rows[0]["decision"] == "accept"
        assert rows[1]["decision"] == "reject"

    def test_duplicate_id_rejected(self):
        from core.evaluator import EvalParseError, parse_eval_response
        with pytest.raises(EvalParseError):
            parse_eval_response(
                '{"results":['
                '{"id":0,"decision":"accept","score":8,"reason":"a","eligibility_confidence":"high"},'
                '{"id":0,"decision":"reject","score":1,"reason":"b","eligibility_confidence":"low"}'
                "]}",
                [0],
            )

    def test_score_out_of_range(self):
        from core.evaluator import EvalParseError, parse_eval_response
        with pytest.raises(EvalParseError):
            parse_eval_response(
                '{"results":[{"id":0,"decision":"accept","score":99,'
                '"reason":"x","eligibility_confidence":"high"}]}',
                [0],
            )


# ---------------------------------------------------------------------------
# Store state machine
# ---------------------------------------------------------------------------

class TestStore:
    def test_upsert_and_filter_new(self, tmp_db):
        from core import store
        items = [
            {"url": "https://a.edu/x", "title": "A", "snippet": "s", "track": "general"},
            {"url": "https://b.edu/y", "title": "B", "snippet": "s", "track": "labs"},
        ]
        n = store.upsert_discovered(items, db_path=tmp_db)
        assert n == 2
        assert store.filter_new(items, db_path=tmp_db) == []
        assert store.filter_new(
            [{"url": "https://c.edu/z", "title": "C", "snippet": "", "track": "general"}],
            db_path=tmp_db,
        )

    def test_rediscovery_merges_tracks(self, tmp_db):
        from core import store
        store.upsert_discovered(
            [{"url": "https://a.edu/x", "title": "A", "snippet": "s",
              "tracks": ["general"], "track": "general"}],
            db_path=tmp_db,
        )
        store.upsert_discovered(
            [{"url": "https://a.edu/x", "title": "A2", "snippet": "longer snippet here",
              "tracks": ["labs"], "track": "labs"}],
            db_path=tmp_db,
        )
        rows = store.get_by_status(["discovered"], db_path=tmp_db)
        assert len(rows) == 1
        assert "labs" in rows[0]["tracks"]
        assert "general" in rows[0]["tracks"]
        assert rows[0]["track"] == "labs"

    def test_retry_vs_reject(self, tmp_db):
        from core import store
        store.upsert_discovered(
            [{"url": "https://a.edu/x", "title": "A", "snippet": "s", "track": "general"}],
            db_path=tmp_db,
        )
        store.mark_retry("https://a.edu/x", "eval_retry", "parse:non_json", db_path=tmp_db)
        store.mark_rejected(
            "https://b.edu/y", "rejected_snippet", "africa", db_path=tmp_db
        )
        reval = store.get_reval_candidates(db_path=tmp_db)
        urls = {r["url"] for r in reval}
        assert "https://a.edu/x" in urls
        assert "https://b.edu/y" not in urls

    def test_stage_aware_reval(self, tmp_db):
        from core import store
        store.mark_status("https://a.edu/1", "eval_retry", db_path=tmp_db)
        store.mark_status("https://a.edu/2", "scrape_retry", db_path=tmp_db)
        store.mark_status("https://a.edu/3", "notion_retry", db_path=tmp_db)
        assert len(store.get_by_stage("snippet", db_path=tmp_db)) == 1
        assert len(store.get_by_stage("scrape", db_path=tmp_db)) == 1
        assert len(store.get_by_stage("notion", db_path=tmp_db)) == 1

    def test_migration_not_written(self, tmp_path, monkeypatch):
        from core import store
        db = tmp_path / "mig.db"
        seen = tmp_path / "seen_urls.json"
        flag = tmp_path / ".seen_urls_migrated"
        seen.write_text(json.dumps(["https://old.edu/page"]))
        monkeypatch.setattr("core.store.DB_PATH", db)
        monkeypatch.setattr("core.store.SEEN_JSON_PATH", seen)
        monkeypatch.setattr("core.store.MIGRATION_FLAG", flag)
        store.init_db(db)
        rows = store.get_by_status(["discovered"], db_path=db)
        assert any(r["url"] == "https://old.edu/page" for r in rows)
        assert store.get_by_status(["written"], db_path=db) == []


# ---------------------------------------------------------------------------
# Notion writer
# ---------------------------------------------------------------------------

class TestNotion:
    def test_import_without_env(self, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.delenv("NOTION_DB_ID", raising=False)
        # Re-import should not crash
        import importlib
        import core.notion_writer as nw
        importlib.reload(nw)

    def test_batch_write_per_item_outcomes(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "tok")
        monkeypatch.setenv("NOTION_DB_ID", "db")
        import core.notion_writer as nw

        calls = {"n": 0}

        def fake_write(item):
            calls["n"] += 1
            if "fail" in item["url"]:
                return {
                    "ok": False, "url": item["url"], "page_id": None,
                    "error": "500", "existing": False,
                }
            return {
                "ok": True, "url": item["url"], "page_id": "p1",
                "error": "", "existing": False,
            }

        monkeypatch.setattr(nw, "write_one", fake_write)
        monkeypatch.setattr(nw.time, "sleep", lambda *_: None)
        results = nw.batch_write([
            {"url": "https://ok.edu", "name": "OK"},
            {"url": "https://fail.edu", "name": "Fail"},
        ])
        assert results[0]["ok"] is True
        assert results[1]["ok"] is False

    def test_existing_page_counts_as_success(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "tok")
        monkeypatch.setenv("NOTION_DB_ID", "db")
        import core.notion_writer as nw

        monkeypatch.setattr(nw, "find_existing_page", lambda *a, **k: "existing-id")
        result = nw.write_one({"url": "https://dup.edu", "name": "Dup"})
        assert result["ok"] and result["existing"]


# ---------------------------------------------------------------------------
# Dry-run / pipeline wiring
# ---------------------------------------------------------------------------

class TestDryRunAndPipeline:
    def test_dry_run_does_not_mark_written(self, tmp_db, monkeypatch):
        from core import store
        import main as main_mod

        monkeypatch.setattr(store, "DB_PATH", tmp_db)
        items = [{
            "url": "https://a.edu/fellowship",
            "name": "Clinical AI Fellowship",
            "score": 8,
            "type": "Fellowship",
            "track": "general",
            "tracks": ["general"],
            "deadline": "",
            "region": "Global",
            "snippet_score": 8,
            "full_score": 8,
        }]
        stats = main_mod._write_and_mark(items, dry_run=True)
        assert stats["dry_run"] == 1
        assert stats["written"] == 0
        assert store.get_by_status(["written"], db_path=tmp_db) == []

    def test_notion_failure_goes_to_retry(self, tmp_db, monkeypatch):
        from core import store
        import main as main_mod

        monkeypatch.setattr(store, "DB_PATH", tmp_db)

        def fake_batch(items):
            return [{
                "ok": False, "url": items[0]["url"], "page_id": None,
                "error": "500", "existing": False,
            }]

        monkeypatch.setattr(main_mod, "batch_write", fake_batch)
        items = [{
            "url": "https://a.edu/fellowship",
            "name": "Clinical AI Fellowship",
            "score": 8,
            "type": "Fellowship",
            "track": "general",
            "tracks": ["general"],
            "deadline": "",
            "region": "Global",
            "snippet_score": 8,
        }]
        stats = main_mod._write_and_mark(items, dry_run=False)
        assert stats["notion_retry"] == 1
        assert store.get_by_status(["notion_retry"], db_path=tmp_db)

    def test_enrich_empty_does_not_fallback(self, monkeypatch):
        """Full-page failure must queue retry, not accept snippet score."""
        from core.evaluator import enrich_batch

        monkeypatch.setattr(
            "core.evaluator.remaining_daily_budget", lambda: 10
        )
        monkeypatch.setattr(
            "core.evaluator._check_openrouter", lambda: None
        )
        monkeypatch.setattr(
            "core.evaluator._run_structured_chunk",
            lambda *a, **k: (None, "parse:non_json", "test-model"),
        )
        monkeypatch.setattr("core.evaluator.time.sleep", lambda *_: None)

        scraped = [{
            "url": "https://a.edu/x",
            "title": "Clinical AI Fellowship",
            "snippet": "digital health undergrad apply",
            "scraped_title": "Clinical AI Fellowship",
            "scraped_body": "International undergrads welcome. Apply by March.",
            "scrape_error": None,
            "snippet_score": 8,
            "track": "general",
            "tracks": ["general"],
        }]
        accepted, rejects, retryable = enrich_batch(scraped, min_score=6)
        assert accepted == []
        assert rejects == []
        assert len(retryable) == 1


# ---------------------------------------------------------------------------
# Serper fail-fast
# ---------------------------------------------------------------------------

class TestSerper:
    def test_fatal_credits(self, monkeypatch):
        from scrapers.search_engine import SerperFatalError, search

        monkeypatch.setenv("SERPER_API_KEY", "k")

        class FakeResp:
            status_code = 400
            text = "Not enough credits"

        monkeypatch.setattr(
            "scrapers.search_engine.requests.post",
            lambda *a, **k: FakeResp(),
        )
        with pytest.raises(SerperFatalError):
            search("test query")

    def test_all_failed_refused(self, monkeypatch):
        from scrapers.search_engine import SerperFatalError, batch_search

        monkeypatch.setenv("SERPER_API_KEY", "k")

        def boom(*a, **k):
            raise Exception("network")

        monkeypatch.setattr("scrapers.search_engine.search", boom)
        monkeypatch.setattr("scrapers.search_engine.time.sleep", lambda *_: None)
        with pytest.raises(SerperFatalError):
            batch_search(["q1", "q2"], delay=0)


# ---------------------------------------------------------------------------
# Query builder bounds
# ---------------------------------------------------------------------------

class TestQueryBuilder:
    def test_daily_bounded(self):
        from core.query_builder import build_queries
        qs = build_queries("daily")
        assert len(qs) < 120  # previously ~210
        tracks = {q["track"] for q in qs}
        assert "general" in tracks
        assert "labs" in tracks
        # wi_events may be present
        general = [q for q in qs if q["track"] == "general"]
        assert len(general) <= 40
