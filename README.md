# OpTrack v2 — Opportunity Hunter

Automatically searches for clinical AI / digital health opportunities for an
**international UW-Madison undergrad**, evaluates with OpenRouter, and writes
score ≥ **6** acceptances to Notion.

## Pipeline

```
Serper → SQLite upsert → deterministic prefilter → structured snippet eval
  → scrape survivors → structured full-page confirm → idempotent Notion write
```

Retry states (`eval_retry`, `scrape_retry`, `notion_retry`) are separate from
terminal rejects. Malformed/empty LLM responses never become rejects.

### Hard rejects (deterministic)
Africa-only, mental health/psychiatry, autism/genetics/proteomics/pathology/
homeopathy/radiology, US-citizen-only, graduate/MD/PhD/postdoc-only,
incubators/accelerators, listicles, job boards.

### Madison/WI exception
Healthtech networking / conferences / summits in Madison or Wisconsin may
accept at score ≥ 6 and can use the snippet-only fast path.

OFY (`opportunitiesforyouth.org`) is a **discovery source only** — full-page
confirmation is always required.

## Setup

```bash
git clone https://github.com/westernmonkey/optrack.git
cd optrack
pip install -r requirements.txt
cp .env.example .env
```

| Key | Where |
|-----|--------|
| `SERPER_API_KEY` | [serper.dev](https://serper.dev) — 1 credit per query |
| `OPENROUTER_API_KEYS` | [openrouter.ai](https://openrouter.ai) (comma-separated) |
| `NOTION_TOKEN` / `NOTION_DB_ID` | Notion integration + database |

## Commands

```bash
python main.py --daily --dry-run     # search + eval, no Notion / no written marks
python main.py --daily               # bounded high-value scan
python main.py --weekly              # deep scan
python main.py --reval               # stage-aware retry of queued failures
python main.py --reval --reval-stage notion --reval-limit 20
python main.py --min-score 6         # default acceptance floor
pytest -q                            # mocked regression tests
```

## State machine (`data/optrack.db`)

| Status | Meaning |
|--------|---------|
| `discovered` | New URL |
| `rejected_prefilter` | Deterministic junk |
| `eval_retry` | LLM/API parse failure — retry snippet eval |
| `rejected_snippet` | Valid LLM reject |
| `scrape_queued` / `scrape_retry` | Needs / failed scrape+enrich |
| `rejected_full` | Valid full-page reject |
| `notion_retry` | Notion write failed |
| `written` | Notion page created or already existed |

`--dry-run` never marks rows `written`.

## CI

GitHub Actions runs **daily** + **weekly** (Monday), with `concurrency` so two
jobs never edit `data/optrack.db` at once. Tests gate the hunt job.

## Layout

```
optrack/
├── main.py
├── config/keywords.yaml      # general / labs / wi_events tracks
├── core/
│   ├── filter_policy.py      # single source of truth for hard rules
│   ├── store.py              # SQLite state machine
│   ├── evaluator.py          # structured all-ID JSON eval
│   ├── notion_writer.py      # idempotent per-item writes
│   ├── prefilter.py
│   ├── snippet_paths.py
│   └── query_builder.py
├── scrapers/
├── tests/
├── data/optrack.db
└── logs/run_log.json         # concise structured summaries only
```
