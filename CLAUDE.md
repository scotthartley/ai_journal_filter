# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`ai_journal_filter` is a cron-job script that fetches scientific journal RSS feeds, filters articles for relevance using an LLM (Claude or Gemini), deduplicates via SQLite, and publishes a curated RSS feed to a local file for static web serving.

## Installation & Setup

```bash
# Install with one or both providers
pip install -e ".[anthropic]"
pip install -e ".[gemini]"
pip install -e ".[anthropic,gemini]"

# Set API key
export ANTHROPIC_API_KEY=sk-...   # for Claude
export GEMINI_API_KEY=...         # for Gemini
```

## Running

```bash
# Run with a config file
ai-journal-filter --config config.yaml

# Mark all current articles as seen without filtering (useful on first run)
ai-journal-filter --config config.yaml --skip-backlog
```

## No Tests or Linting Config

There are no automated tests or linting configuration. The project has no `pytest`, `flake8`, `black`, or `mypy` setup.

## Architecture

All logic lives in a single file: `src/ai_journal_filter/cli.py` (~900 lines).

### Data Flow

1. **Fetch** — `fetch_all_feeds()` retrieves article metadata from configured RSS/Atom URLs via `feedparser`
2. **Deduplicate** — `is_seen()` checks SQLite; new articles are marked seen via `mark_seen()` before LLM calls
3. **Filter** — `filter_new_articles()` batches articles and calls `filter_batch()`, which dispatches to `_filter_batch_anthropic()` or `_filter_batch_gemini()`
4. **Persist** — Matched articles saved to `matched_articles` table; `mark_llm_processed()` updates the `seen_articles` table
5. **Output** — `generate_output_feed()` regenerates the full RSS from the database on every run; `generate_output_text()` optionally writes a plain text summary

### Database Schema (SQLite)

Two tables:
- `seen_articles` — all fetched articles (deduplication, tracks whether LLM-processed)
- `matched_articles` — articles that passed LLM filtering, with rationale and metadata

Schema migration is handled dynamically (columns added via `ALTER TABLE` if missing).

### Key Design Decisions

- **Mark-before-filter**: Articles are marked seen *before* LLM API calls to avoid reprocessing if the process crashes mid-batch
- **Stateless regeneration**: The output RSS is fully regenerated from the database on each run
- **Provider abstraction**: Anthropic and Gemini are conditionally imported; `filter_batch()` dispatches based on the configured provider
- **Retry logic**: Both provider wrappers handle rate limits (429) and overload errors (503/529) with exponential-style retries
- **Path resolution**: All paths in the config (database, output files, log file) are resolved relative to the config file's directory

### Configuration (`config.yaml`)

Based on `config_sample.yaml`. Key sections:
- `feeds`: list of RSS/Atom URLs
- `research_interests`: free-text description passed to the LLM
- `prompt_template`: optional override (must include `{research_interests}` and `{articles_json}` placeholders)
- `provider`: `"anthropic"` or `"gemini"`
- `anthropic` / `gemini`: model, batch size, token limit, optional RPM cap
- `output`: RSS file path, feed metadata, `max_articles`, `max_age_days`
- `database`: SQLite path, optional `prune_age_days`
- `logging`: level, optional file path

`config.yaml` and output files are gitignored by default.
