# OpTrack v2 — Opportunity Hunter

Automatically searches the web for fellowships, scholarships, research programs, and healthtech events relevant to clinical AI and digital health. Evaluates with OpenRouter and saves opportunities to Notion.

## How it works (snippet-first)

1. **Query builder** — builds search queries from `config/keywords.yaml` (general + labs tracks, OFY `site:` queries)
2. **Serper.dev** — Google Search API; returns title + URL + snippet per hit (1 credit per query)
3. **SQLite store** — `data/optrack.db` tracks every URL and status (replaces `seen_urls.json` queues)
4. **Prefilter** — drops obvious junk from snippets (no API cost)
5. **Snippet eval** — one compact TSV batch to OpenRouter; score ≥ 6 to proceed
6. **Fast path** — Madison/WI events and `opportunitiesforyouth.org` posts can write to Notion **without scrape**
7. **Scrape + enrich** — only for non-fast-path survivors that scored ≥ 6
8. **Notion writer** — saves accepted opportunities

**Runs via GitHub Actions** (daily auto / weekly Monday / manual dispatch).

---

## Setup

```bash
git clone https://github.com/yourname/optrack.git
cd optrack
pip install -r requirements.txt
cp .env.example .env   # local only; file is gitignored
```

| Key | Where |
|-----|--------|
| `SERPER_API_KEY` | [serper.dev](https://serper.dev) |
| `OPENROUTER_API_KEYS` | [openrouter.ai](https://openrouter.ai) (comma-separated) |
| `NOTION_TOKEN` | Notion integration |
| `NOTION_DB_ID` | Database ID from URL |

### Local commands

```bash
python main.py --daily --dry-run    # search + eval, no Notion
python main.py --daily              # full daily scan
python main.py --weekly             # deep scan
python main.py --reval              # retry queued URLs in SQLite
python main.py --min-score 6        # default minimum score
```

---

## Customising hunts

Edit `config/keywords.yaml`:

- `priority_combos` — daily high-value query pairs
- `site_queries` — aggregator searches (e.g. opportunitiesforyouth.org)
- `trusted_domains` — always pass prefilter
- Madison/Wisconsin networking and conference combos

---

## File structure

```
optrack/
├── main.py
├── config/keywords.yaml
├── core/
│   ├── store.py           # SQLite URL state
│   ├── snippet_paths.py   # Madison/WI + OFY fast path
│   ├── evaluator.py       # Snippet + enrich batch eval
│   ├── prefilter.py
│   ├── query_builder.py
│   └── notion_writer.py
├── scrapers/
│   ├── search_engine.py
│   └── page_scraper.py
├── data/optrack.db        # committed by CI bot
└── logs/run_log.json
```
