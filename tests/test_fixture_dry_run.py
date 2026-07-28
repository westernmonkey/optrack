"""
Fixture dry-run: fixed corpus through deterministic filters.
Asserts expected accept/reject IDs and zero DB mutation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = [
    {
        "id": "good_fellowship",
        "expect": "pass_prefilter",
        "item": {
            "url": "https://example.edu/clinical-ai-undergrad-fellowship",
            "title": "Clinical AI Undergraduate Fellowship",
            "snippet": "Digital health fellowship for international undergrads. Apply now. Deadline March 1, 2026.",
            "track": "general",
            "source_query": "clinical AI fellowship undergraduate",
        },
        "score": 8,
        "expect_accept": True,
    },
    {
        "id": "africa_only",
        "expect": "drop",
        "item": {
            "url": "https://torgceri.org/program",
            "title": "Young African Health Innovators",
            "snippet": "Pan-African healthtech for African professionals",
            "track": "general",
        },
        "score": 9,
        "expect_accept": False,
    },
    {
        "id": "mental_health",
        "expect": "drop",
        "item": {
            "url": "https://mhinnovation.net/fellowship",
            "title": "Youth Mental Health Fellowship",
            "snippet": "Behavioral health psychiatry program",
            "track": "general",
        },
        "score": 9,
        "expect_accept": False,
    },
    {
        "id": "wi_event",
        "expect": "pass_prefilter",
        "item": {
            "url": "https://events.wisc.edu/healthtech-summit-2026",
            "title": "Madison Healthtech Summit 2026",
            "snippet": "Register for digital health conference in Madison Wisconsin",
            "track": "wi_events",
            "source_query": "healthtech conference Madison",
        },
        "score": 6,
        "expect_accept": True,
    },
    {
        "id": "citizen_only",
        "expect": "drop",
        "item": {
            "url": "https://nih.gov/fellowship-us",
            "title": "NIH Digital Health Fellowship",
            "snippet": "US citizens only. Citizenship required.",
            "track": "general",
        },
        "score": 9,
        "expect_accept": False,
    },
]


def test_fixture_corpus_dry_run(tmp_db, monkeypatch):
    from core import store
    from core.filter_policy import apply_acceptance_gates, prefilter_item

    monkeypatch.setattr(store, "DB_PATH", tmp_db)
    before = store.count_by_status(db_path=tmp_db)

    accepted_ids = []
    rejected_ids = []
    for fix in FIXTURES:
        item = fix["item"]
        drop, reason = prefilter_item(item)
        if fix["expect"] == "drop":
            assert drop, f"{fix['id']} should drop, got pass"
            rejected_ids.append(fix["id"])
            continue
        assert not drop, f"{fix['id']} should pass prefilter: {reason}"
        ok, gate = apply_acceptance_gates(item, fix["score"])
        if fix["expect_accept"]:
            assert ok, f"{fix['id']} should accept: {gate}"
            accepted_ids.append(fix["id"])
        else:
            assert not ok
            rejected_ids.append(fix["id"])

    assert "good_fellowship" in accepted_ids
    assert "wi_event" in accepted_ids
    assert "africa_only" in rejected_ids
    assert "mental_health" in rejected_ids
    assert "citizen_only" in rejected_ids

    # Zero state mutation from dry filter pass
    after = store.count_by_status(db_path=tmp_db)
    assert before == after
