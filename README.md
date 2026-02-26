# claude_journal_filter

A cron-job script that fetches scientific journal RSS feeds, filters articles against your research interests using an LLM, and publishes a curated RSS feed you can subscribe to in any feed reader.

## Features

- Fetches any number of RSS/Atom feeds
- Filters articles in batches using **Anthropic Claude** or **Google Gemini**
- Deduplicates via SQLite so articles are only evaluated once
- Publishes a filtered RSS feed to a local file (suitable for static web serving)
- Configurable rate limiting to stay within API quotas
- Designed to run unattended as a cron job

## Requirements

- Python 3.12+
- An [Anthropic API key](https://console.anthropic.com/) **or** a [Google Gemini API key](https://aistudio.google.com/apikey)

## Installation

```bash
git clone https://github.com/your-username/claude_journal_filter.git
cd claude_journal_filter

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

## Configuration

Copy the sample config and edit it:

```bash
cp config_sample.yaml config.yaml
```

Key sections:

| Section | Description |
|---|---|
| `feeds` | List of RSS/Atom feed URLs to fetch |
| `research_interests` | Free-text description of your interests, used as the LLM prompt |
| `provider` | `"anthropic"` or `"gemini"` |
| `anthropic` / `gemini` | Model, batch size, token limit, and optional rate cap |
| `output` | Where to write the filtered RSS feed and how many articles to keep |
| `database` | Path to the SQLite database file |
| `logging` | Log level and optional log file |

### Provider options

**Anthropic (Claude)**

```yaml
provider: "anthropic"

anthropic:
  model: "claude-opus-4-6"
  batch_size: 20
  max_tokens: 2048
```

Set the `ANTHROPIC_API_KEY` environment variable before running.

**Google Gemini**

```yaml
provider: "gemini"

gemini:
  model: "gemini-2.5-flash"
  batch_size: 20
  max_tokens: 4096
  rpm_limit: 10  # free tier is 10 requests per minute
```

Set the `GEMINI_API_KEY` environment variable before running.

### Rate limiting

The optional `rpm_limit` key (in either provider block) caps requests per minute. The script enforces a minimum interval between batch API calls and will sleep as needed. Set to `0` or omit to disable.

## Usage

```bash
export ANTHROPIC_API_KEY=sk-...   # or GEMINI_API_KEY
source .venv/bin/activate
python claude_journal_filter.py --config config.yaml
```

The script will:

1. Fetch all configured feeds
2. Skip articles already in the database
3. Send new articles to the LLM in batches for relevance filtering
4. Save matched articles to SQLite
5. Write a fresh RSS feed to the configured output path

### Running as a cron job

```cron
0 */6 * * * /path/to/.venv/bin/python /path/to/claude_journal_filter.py --config /path/to/config.yaml
```

All paths in `config.yaml` are resolved relative to the config file itself, so the script behaves consistently regardless of the working directory it is launched from.

### Serving the output feed

The output RSS file is a plain XML file. To make it accessible over the web, point a static web server (nginx, Caddy, Apache, etc.) at the directory containing it, or copy it to any static hosting provider.

## Database

The SQLite database contains two tables:

- `seen_articles` — every article URL fetched, to prevent re-evaluation
- `matched_articles` — articles approved by the LLM, used to build the output feed

The database persists across runs. Delete it to re-evaluate all articles from scratch.

## Acknowledgments

This project was written by [Claude Code](https://claude.ai/code), Anthropic's AI coding assistant.

## License

MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
