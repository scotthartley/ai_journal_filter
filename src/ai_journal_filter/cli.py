"""
Fetches scientific journal RSS feeds, filters articles against research
interests using a configurable LLM API, deduplicates via SQLite, and publishes a
filtered RSS feed to a configurable path for static web serving.

Run as a cron job:
  0 */6 * * * /path/to/venv/bin/ai-journal-filter --config /path/to/config.yaml
"""

import argparse
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler

try:
    import anthropic
except ImportError:
    anthropic = None
try:
    from google import genai
    from google.genai import types as _genai_types
    from google.genai import errors as _genai_errors
except ImportError:
    genai = None
    _genai_types = None
    _genai_errors = None
import feedparser
import yaml
from feedgen.feed import FeedGenerator


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PRUNE_AGE_DAYS = 90
DEFAULT_MAX_ARTICLES = 100
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_TOKENS = 2048
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-6"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_FEED_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Config & Setup
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> dict:
    """Load YAML config; raise on missing or malformed file."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise ValueError(f"Config file is not a valid YAML mapping: {config_path}")
    return config


def setup_logging(logging_config: dict, config_dir: str) -> None:
    """Configure root logger with StreamHandler and optional RotatingFileHandler."""
    level_name = logging_config.get("level", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handlers = [logging.StreamHandler(sys.stdout)]

    log_file = logging_config.get("file")
    if log_file:
        if not os.path.isabs(log_file):
            log_file = os.path.join(config_dir, log_file)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=3
        )
        file_handler.setLevel(level)
        handlers.append(file_handler)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    """Create tables/indexes if they don't exist, return the connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS seen_articles (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url        TEXT NOT NULL UNIQUE,
            feed_name  TEXT NOT NULL,
            title      TEXT,
            fetched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS matched_articles (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url        TEXT NOT NULL UNIQUE,
            feed_name  TEXT NOT NULL,
            title      TEXT NOT NULL,
            authors    TEXT,
            published  TEXT,
            summary    TEXT,
            rationale  TEXT NOT NULL,
            matched_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_seen_articles_url
            ON seen_articles(url);

        CREATE INDEX IF NOT EXISTS idx_matched_articles_matched_at
            ON matched_articles(matched_at DESC);
    """)
    conn.commit()
    # Schema migration: add llm_processed column if not already present.
    # DEFAULT 1 means all existing rows are treated as already processed.
    try:
        conn.execute(
            "ALTER TABLE seen_articles ADD COLUMN llm_processed INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE matched_articles ADD COLUMN image_url TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
    return conn


def is_seen(conn: sqlite3.Connection, url: str) -> bool:
    """Return True if url is in seen_articles and was successfully LLM-processed."""
    row = conn.execute(
        "SELECT 1 FROM seen_articles WHERE url = ? AND llm_processed = 1", (url,)
    ).fetchone()
    return row is not None


def mark_seen(
    conn: sqlite3.Connection, url: str, feed_name: str, title: str | None
) -> None:
    """Insert url into seen_articles with llm_processed=0 (INSERT OR IGNORE for idempotency)."""
    fetched_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO seen_articles (url, feed_name, title, fetched_at, llm_processed) "
        "VALUES (?, ?, ?, ?, 0)",
        (url, feed_name, title, fetched_at),
    )
    conn.commit()


def mark_llm_processed(conn: sqlite3.Connection, urls: list[str]) -> None:
    """Set llm_processed=1 for the given URLs after a successful LLM batch."""
    if not urls:
        return
    placeholders = ",".join("?" * len(urls))
    conn.execute(
        f"UPDATE seen_articles SET llm_processed = 1 WHERE url IN ({placeholders})",
        urls,
    )
    conn.commit()


def save_matched_article(
    conn: sqlite3.Connection,
    url: str,
    feed_name: str,
    title: str,
    authors: str | None,
    published: str | None,
    summary: str | None,
    rationale: str,
    image_url: str | None = None,
) -> None:
    """Insert a matched article (INSERT OR IGNORE for idempotency)."""
    matched_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO matched_articles "
        "(url, feed_name, title, authors, published, summary, rationale, image_url, matched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (url, feed_name, title, authors, published, summary, rationale, image_url, matched_at),
    )
    conn.commit()


def get_matched_articles(
    conn: sqlite3.Connection, max_articles: int, max_age_days: int
) -> list[sqlite3.Row]:
    """Return matched articles ordered by matched_at DESC, filtered by age."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
    ).isoformat()
    rows = conn.execute(
        "SELECT * FROM matched_articles "
        "WHERE matched_at >= ? "
        "ORDER BY matched_at DESC "
        "LIMIT ?",
        (cutoff, max_articles),
    ).fetchall()
    return rows


def prune_seen_articles(conn: sqlite3.Connection, prune_age_days: int) -> None:
    """Delete seen_articles entries older than prune_age_days days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=prune_age_days)).isoformat()
    cursor = conn.execute("DELETE FROM seen_articles WHERE fetched_at < ?", (cutoff,))
    conn.commit()
    logging.getLogger(__name__).info(
        "Pruned %d old entries from seen_articles.", cursor.rowcount
    )


# ---------------------------------------------------------------------------
# Feed Fetching
# ---------------------------------------------------------------------------

def _strip_html(raw: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", raw)


def _extract_image_url(entry: dict) -> str | None:
    """Extract the first image URL from feedparser media/enclosure fields or HTML content."""
    for media in entry.get("media_content", []):
        url = media.get("url", "")
        if url:
            return url
    for thumb in entry.get("media_thumbnail", []):
        url = thumb.get("url", "")
        if url:
            return url
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image/") and enc.get("url"):
            return enc["url"]
    # Fallback: look for <img> in entry HTML content or summary
    for html in [
        next((c.get("value", "") for c in entry.get("content", [])), ""),
        entry.get("summary", ""),
    ]:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def fetch_feed(feed_config: dict) -> list[dict]:
    """
    Parse a single RSS/Atom feed with feedparser.
    Strips HTML from summary, truncates to 1000 chars.
    Logs a warning and returns [] on error.
    """
    name = feed_config.get("name", "unknown")
    url = feed_config.get("url", "")
    logger = logging.getLogger(__name__)

    try:
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(DEFAULT_FEED_TIMEOUT)
        try:
            parsed = feedparser.parse(url)
        finally:
            socket.setdefaulttimeout(old_timeout)
    except Exception as exc:
        logger.warning("Failed to fetch feed '%s' (%s): %s", name, url, exc)
        return []

    if parsed.get("bozo") and not parsed.get("entries"):
        logger.warning(
            "Feed '%s' (%s) returned a malformed/empty response: %s",
            name,
            url,
            parsed.get("bozo_exception", "unknown error"),
        )
        return []

    articles = []
    for entry in parsed.entries:
        link = entry.get("link") or entry.get("id") or ""
        if not link:
            continue

        title = entry.get("title", "").strip()

        # Prefer content over summary for abstract
        raw_summary = ""
        if entry.get("content"):
            raw_summary = entry.content[0].get("value", "")
        elif entry.get("summary"):
            raw_summary = entry.get("summary", "")

        summary = _strip_html(raw_summary)

        # Authors
        authors_list = []
        for a in entry.get("authors", []):
            name_part = a.get("name", "").strip()
            if name_part:
                authors_list.append(name_part)
        authors = ", ".join(authors_list) if authors_list else None

        # Published date (keep as string)
        published = None
        if entry.get("published"):
            published = entry.published
        elif entry.get("updated"):
            published = entry.updated

        articles.append(
            {
                "url": link,
                "feed_name": name,
                "title": title,
                "summary": summary,
                "authors": authors,
                "published": published,
                "image_url": _extract_image_url(entry),
            }
        )

    logger.info("Fetched %d articles from feed '%s'", len(articles), name)
    return articles


def fetch_all_feeds(feeds_config: list[dict]) -> list[dict]:
    """Fetch all feeds and return a flat list of article dicts."""
    all_articles = []
    for feed_config in feeds_config:
        all_articles.extend(fetch_feed(feed_config))
    return all_articles


# ---------------------------------------------------------------------------
# LLM Filtering
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are a research article relevance filter. Your task is to review a list of scientific article titles and abstracts, and identify which ones are relevant to the researcher's stated interests.

RESEARCHER'S INTERESTS:
{research_interests}

ARTICLES TO EVALUATE:
The following is a JSON array of articles, each with an "index" (0-based), "title", and "abstract".

{articles_json}

INSTRUCTIONS:
- Evaluate each article against the researcher's interests.
- Be conservative: only include an article if you are confident it is relevant. When the abstract is absent, very short, or appears to be metadata rather than a real abstract, apply a higher threshold — do not include the article based on vague or superficial title-keyword overlap alone.
- Return ONLY a JSON array containing objects for articles that ARE relevant.
- Each object must have exactly two fields:
    "index": the integer index of the article from the input array
    "rationale": a concise 1-3 sentence explanation of why this article is relevant
- If NO articles are relevant, return an empty JSON array: []
- Do NOT include any text before or after the JSON array.
- Do NOT wrap the JSON in markdown code fences.

Example of a valid response:
[{{"index": 0, "rationale": "..."}}]\
"""


def build_prompt(research_interests: str, batch: list[dict], template: str = _PROMPT_TEMPLATE) -> str:
    """Build the LLM prompt for a batch of articles."""
    articles_json = json.dumps(
        [
            {"index": i, "title": a["title"], "abstract": a["summary"][:1000]}
            for i, a in enumerate(batch)
        ],
        ensure_ascii=False,
        indent=2,
    )
    return template.format(
        research_interests=research_interests.strip(),
        articles_json=articles_json,
    )


def call_claude(client, model: str, prompt: str, max_tokens: int) -> str:
    """Call the Claude API and return the raw text response."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def call_gemini(client, model: str, prompt: str, max_tokens: int) -> str:
    """Call the Gemini API and return the raw text response."""
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=_genai_types.GenerateContentConfig(max_output_tokens=max_tokens),
    )
    return response.text


def parse_llm_response(response_text: str) -> list[dict]:
    """
    Parse the LLM's JSON array response.
    First tries json.loads; falls back to regex extraction.
    Raises ValueError if neither succeeds.
    """
    text = response_text.strip()

    # Direct parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Regex fallback: find the outermost [...] block
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"Could not parse a JSON array from LLM response: {text[:200]!r}"
    )


def _filter_batch_anthropic(
    client, prompt: str, model: str, max_tokens: int, max_retries: int, logger
) -> list[dict] | None:
    """Call Claude with retries for rate-limit and overload errors.
    Returns a list of matched dicts on success, or None on failure (batch will be retried)."""
    for attempt in range(max_retries + 1):
        try:
            return parse_llm_response(call_claude(client, model, prompt, max_tokens))
        except anthropic.RateLimitError:
            wait = 60
            logger.warning(
                "Anthropic rate limit (attempt %d/%d). Waiting %ds...",
                attempt + 1, max_retries + 1, wait,
            )
            if attempt < max_retries:
                time.sleep(wait)
            else:
                logger.warning("Exhausted retries on rate limit; batch will be retried next run.")
                return None
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529:
                wait = 30
                logger.warning(
                    "Anthropic overloaded (attempt %d/%d). Waiting %ds...",
                    attempt + 1, max_retries + 1, wait,
                )
                if attempt < max_retries:
                    time.sleep(wait)
                else:
                    logger.warning("Exhausted retries on overload; batch will be retried next run.")
                    return None
            else:
                logger.error("Anthropic API error (status %d): %s", exc.status_code, exc)
                return None
        except ValueError as exc:
            logger.warning("Could not parse LLM response for batch: %s", exc)
            return None
    return None


def _filter_batch_gemini(
    client, prompt: str, model: str, max_tokens: int, max_retries: int, logger
) -> list[dict] | None:
    """Call Gemini with retries for rate-limit and overload errors.
    Returns a list of matched dicts on success, or None on failure (batch will be retried)."""
    for attempt in range(max_retries + 1):
        try:
            return parse_llm_response(call_gemini(client, model, prompt, max_tokens))
        except _genai_errors.ClientError as exc:
            if exc.code == 429:
                wait = 60
                logger.warning(
                    "Gemini rate limit (attempt %d/%d). Waiting %ds...",
                    attempt + 1, max_retries + 1, wait,
                )
                if attempt < max_retries:
                    time.sleep(wait)
                else:
                    logger.warning("Exhausted retries on rate limit; batch will be retried next run.")
                    return None
            else:
                logger.error("Gemini API error (code %d): %s", exc.code, exc)
                return None
        except _genai_errors.ServerError as exc:
            if exc.code == 503:
                wait = 30
                logger.warning(
                    "Gemini overloaded (attempt %d/%d). Waiting %ds...",
                    attempt + 1, max_retries + 1, wait,
                )
                if attempt < max_retries:
                    time.sleep(wait)
                else:
                    logger.warning("Exhausted retries on overload; batch will be retried next run.")
                    return None
            else:
                logger.error("Gemini API error (code %d): %s", exc.code, exc)
                return None
        except ValueError as exc:
            logger.warning("Could not parse LLM response for batch: %s", exc)
            return None
    return None


def filter_batch(
    client,
    batch: list[dict],
    research_interests: str,
    model: str,
    max_tokens: int,
    provider: str,
    max_retries: int = 2,
    template: str = _PROMPT_TEMPLATE,
) -> list[dict] | None:
    """
    Filter a single batch of articles with the configured LLM provider.
    Dispatches to the provider-specific retry helper.
    Returns list of dicts with keys: index, rationale on success, or None on failure.
    """
    logger = logging.getLogger(__name__)
    prompt = build_prompt(research_interests, batch, template)
    if provider == "gemini":
        return _filter_batch_gemini(client, prompt, model, max_tokens, max_retries, logger)
    else:
        return _filter_batch_anthropic(client, prompt, model, max_tokens, max_retries, logger)


def filter_new_articles(
    client,
    articles: list[dict],
    conn: sqlite3.Connection,
    config: dict,
    provider: str = "anthropic",
) -> list[dict]:
    """
    Full pipeline:
    1. Filter out already-seen articles.
    2. Mark all new articles as seen (mark-before-filter pattern).
    3. Batch and call the configured LLM provider.
    4. Save matched articles to DB.
    Returns list of matched article dicts (with rationale).
    """
    logger = logging.getLogger(__name__)
    research_interests = config["research_interests"]
    prompt_template = config.get("prompt_template", _PROMPT_TEMPLATE)
    llm_cfg = config.get(provider, {})
    default_model = DEFAULT_GEMINI_MODEL if provider == "gemini" else DEFAULT_ANTHROPIC_MODEL
    model = llm_cfg.get("model", default_model)
    batch_size = int(llm_cfg.get("batch_size", DEFAULT_BATCH_SIZE))
    max_tokens = int(llm_cfg.get("max_tokens", DEFAULT_MAX_TOKENS))
    rpm_limit = int(llm_cfg.get("rpm_limit", 0))  # 0 = no limit
    min_interval = 60.0 / rpm_limit if rpm_limit > 0 else 0.0

    # Deduplicate against DB
    new_articles = [a for a in articles if not is_seen(conn, a["url"])]
    logger.info(
        "%d new articles (out of %d total) to evaluate.",
        len(new_articles),
        len(articles),
    )

    if not new_articles:
        return []

    # Mark-before-filter: persist all new articles before any API calls
    for article in new_articles:
        mark_seen(conn, article["url"], article["feed_name"], article["title"])

    # Process in batches
    matched = []
    last_call_time: float | None = None
    for batch_start in range(0, len(new_articles), batch_size):
        if min_interval > 0 and last_call_time is not None:
            elapsed = time.monotonic() - last_call_time
            wait = min_interval - elapsed
            if wait > 0:
                logger.debug("Rate limiting: waiting %.1fs before next batch.", wait)
                time.sleep(wait)

        batch = new_articles[batch_start : batch_start + batch_size]
        logger.info(
            "Sending batch %d-%d to %s...",
            batch_start,
            batch_start + len(batch) - 1,
            provider,
        )
        last_call_time = time.monotonic()
        results = filter_batch(
            client, batch, research_interests, model, max_tokens, provider,
            template=prompt_template,
        )

        if results is None:
            logger.warning(
                "Batch %d-%d failed; articles will be retried next run.",
                batch_start,
                batch_start + len(batch) - 1,
            )
            continue

        mark_llm_processed(conn, [a["url"] for a in batch])
        for result in results:
            idx = result.get("index")
            rationale = result.get("rationale", "")
            if idx is None or not isinstance(idx, int) or idx >= len(batch):
                logger.warning("Invalid index %r in LLM response; skipping.", idx)
                continue

            article = batch[idx]
            save_matched_article(
                conn,
                url=article["url"],
                feed_name=article["feed_name"],
                title=article["title"],
                authors=article.get("authors"),
                published=article.get("published"),
                summary=article.get("summary"),
                rationale=rationale,
                image_url=article.get("image_url"),
            )
            matched.append({**article, "rationale": rationale})
            logger.info("Matched: %s", article["title"][:80])

    logger.info("%s matched %d new articles.", provider.capitalize(), len(matched))
    return matched


# ---------------------------------------------------------------------------
# Output Feed Generation
# ---------------------------------------------------------------------------

def _parse_date_to_utc(date_str: str | None) -> datetime:
    """
    Try to parse an RFC 2822 or ISO-8601 date string to an aware datetime.
    Falls back to datetime.now(UTC) on failure.
    """
    if not date_str:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    # Try ISO-8601
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def generate_output_feed(conn: sqlite3.Connection, output_config: dict, config_dir: str) -> None:
    """
    Rebuild the full RSS feed from DB history (filtered by age/count).
    Embeds rationale + summary in the entry description.
    """
    logger = logging.getLogger(__name__)

    max_articles = int(output_config.get("max_articles", DEFAULT_MAX_ARTICLES))
    max_age_days = int(output_config.get("max_age_days", DEFAULT_MAX_AGE_DAYS))
    rss_path = output_config.get("rss_path", "filtered_feed.xml")
    if not os.path.isabs(rss_path):
        rss_path = os.path.join(config_dir, rss_path)

    rows = get_matched_articles(conn, max_articles, max_age_days)
    logger.info("Generating output feed with %d articles → %s", len(rows), rss_path)

    fg = FeedGenerator()
    fg.id(output_config.get("feed_link", "https://example.com/filtered_feed.xml"))
    fg.title(output_config.get("feed_title", "Filtered Research Feed"))
    fg.link(href=output_config.get("feed_link", "https://example.com/filtered_feed.xml"))
    fg.description(
        output_config.get("feed_description", "Articles curated by AI")
    )
    fg.language("en")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for row in rows:
        fe = fg.add_entry()
        fe.id(row["url"])
        fe.title(row["title"])
        fe.link(href=row["url"])

        # Build HTML description
        desc_parts = []
        if row["image_url"]:
            desc_parts.append(f'<p><img src="{row["image_url"]}" alt="TOC graphic" /></p>')
        if row["feed_name"]:
            desc_parts.append(f"<p><strong>Source:</strong> {row['feed_name']}</p>")
        if row["authors"]:
            desc_parts.append(f"<p><strong>Authors:</strong> {row['authors']}</p>")
        if row["rationale"]:
            desc_parts.append(
                f"<p><strong>Why it matches:</strong> {row['rationale']}</p>"
            )
        if row["summary"]:
            desc_parts.append(f"<p><strong>Abstract:</strong> {row['summary']}</p>")
        fe.description("".join(desc_parts))

        pub_dt = _parse_date_to_utc(row["published"])
        fe.pubDate(pub_dt)

    fg.rss_file(rss_path, pretty=True)
    logger.info("Feed written to %s", rss_path)


def generate_output_text(conn: sqlite3.Connection, output_config: dict, config_dir: str) -> None:
    """Write matched articles as a formatted plain text file."""
    import textwrap
    logger = logging.getLogger(__name__)

    max_articles = int(output_config.get("max_articles", DEFAULT_MAX_ARTICLES))
    max_age_days = int(output_config.get("max_age_days", DEFAULT_MAX_AGE_DAYS))
    text_path = output_config.get("text_path")
    if not os.path.isabs(text_path):
        text_path = os.path.join(config_dir, text_path)

    rows = get_matched_articles(conn, max_articles, max_age_days)
    logger.info("Generating text output with %d articles → %s", len(rows), text_path)

    lines = []
    title = output_config.get("feed_title", "Filtered Research Feed")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(title)
    lines.append(f"Generated: {generated}")
    lines.append("")

    for i, row in enumerate(rows, start=1):
        pub_dt = _parse_date_to_utc(row["published"])
        pub_str = pub_dt.strftime("%Y-%m-%d") if row["published"] else ""

        lines.append(f"{i}. {row['title']}")
        if row["feed_name"]:
            lines.append(f"   Source:  {row['feed_name']}")
        if row["authors"]:
            authors = " ".join(row["authors"].split())
            lines.append(f"   Authors: {authors}")
        if pub_str:
            lines.append(f"   Date:    {pub_str}")
        if row["rationale"]:
            lines.append(f"   Match:   {row['rationale']}")
        if row["summary"]:
            wrapped = textwrap.fill(row["summary"], width=80,
                                    initial_indent="   Abstract: ",
                                    subsequent_indent="            ")
            lines.append(wrapped)
        lines.append(f"   Link:    {row['url']}")
        lines.append("")

    with open(text_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info("Text output written to %s", text_path)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Filter scientific journal RSS feeds using a configurable LLM API."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--skip-backlog",
        action="store_true",
        help=(
            "Mark all current feed articles as seen without filtering them. "
            "Useful on first run to avoid processing a large backlog."
        ),
    )
    args = parser.parse_args()

    # Resolve config path and directory for relative paths
    config_path = os.path.abspath(args.config)
    config_dir = os.path.dirname(config_path)

    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.get("logging", {}), config_dir)
    logger = logging.getLogger(__name__)
    logger.info("Starting ai_journal_filter (config: %s)", config_path)

    provider = config.get("provider", "anthropic")
    logger.info("Using LLM provider: %s", provider)

    if provider == "gemini":
        if genai is None:
            logger.error(
                "google-generativeai package not installed. "
                "Run: pip install google-generativeai"
            )
            sys.exit(1)
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.error("GEMINI_API_KEY environment variable is not set. Exiting.")
            sys.exit(1)
    else:
        if anthropic is None:
            logger.error(
                "anthropic package not installed. "
                "Run: pip install anthropic"
            )
            sys.exit(1)
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.error(
                "ANTHROPIC_API_KEY environment variable is not set. Exiting."
            )
            sys.exit(1)

    try:
        # Resolve DB path
        db_path = config.get("database", {}).get("path", "journal_filter.db")
        if not os.path.isabs(db_path):
            db_path = os.path.join(config_dir, db_path)

        conn = init_db(db_path)
        logger.info("Database initialized: %s", db_path)

        prune_age_days = int(config.get("database", {}).get("prune_age_days", DEFAULT_PRUNE_AGE_DAYS))
        if prune_age_days > 0:
            prune_seen_articles(conn, prune_age_days)

        if provider == "gemini":
            client = genai.Client(api_key=api_key)
        else:
            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

        # Fetch
        feeds_config = config.get("feeds", [])
        articles = fetch_all_feeds(feeds_config)
        logger.info("Total articles fetched across all feeds: %d", len(articles))

        if args.skip_backlog:
            # Mark all unseen articles as seen without filtering
            new_articles = [a for a in articles if not is_seen(conn, a["url"])]
            for article in new_articles:
                mark_seen(conn, article["url"], article["feed_name"], article["title"])
            if new_articles:
                mark_llm_processed(conn, [a["url"] for a in new_articles])
            logger.info(
                "--skip-backlog: marked %d articles as seen, skipping LLM filtering.",
                len(new_articles),
            )
        else:
            # Filter (dedup → mark seen → batch → LLM → save matches)
            filter_new_articles(client, articles, conn, config, provider)

            # Generate output(s) from full DB history
            output_config = config.get("output", {})
            if output_config.get("rss_path"):
                generate_output_feed(conn, output_config, config_dir)
            if output_config.get("text_path"):
                generate_output_text(conn, output_config, config_dir)

        conn.close()
        logger.info("Done.")

    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
