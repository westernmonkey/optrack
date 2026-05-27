# OpTrack v2 — Opportunity Hunter

Automatically searches the internet for fellowships, accelerators, grants, scholarships, and leadership programs relevant to clinical AI and digital health. Evaluates each one with Claude AI and saves real opportunities to Notion.

## How it works

1. **Query builder** — combines opportunity types × regions from `config/keywords.yaml` into ~200 Google search queries
2. **Serper.dev** — fires queries at Google Search API (free tier: 2,500/month)
3. **Dedup** — skips any URL already processed in a previous run
4. **Page scraper** — fetches the full text of each new URL
5. **Claude evaluator** — decides if it's a real registerable program, extracts deadline/eligibility/description, scores relevance 1–10
6. **Notion writer** — saves accepted opportunities to your database
7. **State commit** — commits `seen_urls.json` back to the repo so nothing is processed twice

**Runs automatically via GitHub Actions:**
- Daily at 5am UTC — light scan (priority queries only, ~30 queries)
- Weekly Monday 4am UTC — deep scan (all ~200 queries)
- Manual trigger available any time from GitHub Actions tab

**Estimated monthly cost:**
- Serper.dev: free (uses ~1,600 of 2,500 free queries)
- Claude API: ~$0.80/month (Haiku model)
- GitHub Actions: free (uses ~260 of 2,000 free minutes)

---

## Setup

### 1. Fork / clone this repo

```bash
git clone https://github.com/yourname/optrack.git
cd optrack
```

### 2. Install dependencies (for local testing)

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
cp .env.example .env
# Fill in your credentials in .env
```

You need four credentials:

| Key | Where to get it |
|-----|----------------|
| `SERPER_API_KEY` | [serper.dev](https://serper.dev) — sign up, free tier |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `NOTION_TOKEN` | [notion.so/my-integrations](https://www.notion.so/my-integrations) |
| `NOTION_DB_ID` | Your Notion database URL (32-char ID before `?v=`) |

### 4. Set up your Notion database

Create a new Notion database with these exact property names and types:

| Property | Type |
|----------|------|
| Name | Title |
| URL | URL |
| Type | Select |
| Region | Select |
| Score | Number |
| Status | Select |
| Deadline | Date |
| Found On | Date |
| Description | Rich Text |
| Eligibility | Rich Text |
| AI Summary | Rich Text |
| Source Query | Rich Text |

Then share the database with your integration (click Share → Invite → your integration name).

### 5. Add GitHub Actions secrets

Go to your repo → Settings → Secrets and variables → Actions → New repository secret:

- `SERPER_API_KEY`
- `ANTHROPIC_API_KEY`
- `NOTION_TOKEN`
- `NOTION_DB_ID`

### 6. Test locally

```bash
# Dry run — searches and evaluates but doesn't write to Notion
python main.py --daily --dry-run

# Full run
python main.py --daily

# Deep scan
python main.py --weekly
```

---

## Customising what it hunts

Edit `config/keywords.yaml`:

- **`opportunity_types`** — what kinds of programs to search for
- **`regions`** — where to look
- **`priority_combos`** — high-value pairs that run every day
- **`scoring`** — hints Claude uses when evaluating relevance

---

## Adjusting quality threshold

The default minimum Claude score is **5/10**. To raise the bar:

```bash
python main.py --daily --min-score 7
```

Or edit the GitHub Actions workflow to pass `--min-score 7`.

---

## File structure

```
optrack/
├── main.py                    # Entry point
├── requirements.txt
├── .env.example               # Copy to .env with your credentials
├── .gitignore
├── config/
│   └── keywords.yaml          # Edit this to change what you hunt for
├── core/
│   ├── query_builder.py       # Builds search queries from keywords.yaml
│   ├── deduper.py             # Tracks seen URLs across runs
│   ├── evaluator.py           # Claude AI evaluation logic
│   └── notion_writer.py       # Writes to Notion
├── scrapers/
│   ├── search_engine.py       # Serper.dev API wrapper
│   └── page_scraper.py        # Fetches + cleans page content
├── data/
│   └── seen_urls.json         # Auto-updated, committed by bot
├── logs/
│   └── run_log.json           # Run history (last 50 runs)
└── .github/
    └── workflows/
        └── daily.yml          # GitHub Actions schedule
```
