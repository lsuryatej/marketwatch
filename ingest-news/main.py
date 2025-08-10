#!/usr/bin/env python3
"""
Ingest news articles from external sources, run sentiment analysis and persist results.

This script is designed to run as a long‑lived service inside a container.  It periodically
fetches the latest news from NewsAPI (if an API key is provided) or, when running in offline
mode, generates synthetic articles for demonstration.  Each article is tokenised and passed
through the ProsusAI/finbert model to obtain probabilities for positive, neutral and negative
sentiment.  The net sentiment score (P(pos) - P(neg)) is recorded per company in the
`news_sentiment` table.

The raw article data is stored in `news_raw` for future reference.  When a ticker is encountered
for the first time it is inserted into the `company` table.
"""

import os
import time
import logging
import datetime as dt
import random
import re
from typing import List, Dict, Any, Tuple

import psycopg2
import psycopg2.extras

# We import transformers lazily because downloading models can be expensive.  If a NEWS_API_KEY
# isn't provided we skip the real model and generate dummy sentiment instead.
try:
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    import torch
    import torch.nn.functional as F
except Exception:
    AutoTokenizer = None  # type: ignore
    AutoModelForSequenceClassification = None  # type: ignore
    torch = None  # type: ignore


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest-news")


DATABASE_URL = os.getenv("DATABASE_URL") or "postgresql://postgres:postgres@database:5432/market"
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
GDELT_BASE_URL = os.getenv("GDELT_BASE_URL")
POLLING_INTERVAL = int(os.getenv("POLLING_INTERVAL_SECONDS", "300"))
USE_DUMMY_NEWS = os.getenv("USE_DUMMY_NEWS", "false").lower() in ("1", "true", "yes")


def connect_db():
    return psycopg2.connect(DATABASE_URL)


def _parse_iso8601(value: str) -> dt.datetime:
    """Parse an ISO8601 timestamp string, supporting a trailing 'Z'."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class CompanyCache:
    """A simple in‑memory cache for ticker → company_id lookups."""

    def __init__(self):
        self._cache: Dict[str, int] = {}

    def get_company_id(self, conn: psycopg2.extensions.connection, ticker: str) -> int:
        ticker = ticker.upper()
        if ticker in self._cache:
            return self._cache[ticker]
        cur = conn.cursor()
        # Try to insert the company; if it already exists, this no‑ops.
        cur.execute(
            "INSERT INTO company (ticker) VALUES (%s) ON CONFLICT (ticker) DO NOTHING RETURNING id",
            (ticker,),
        )
        inserted = cur.fetchone()
        if inserted:
            company_id = inserted[0]
        else:
            # Fetch existing id
            cur.execute("SELECT id FROM company WHERE ticker = %s", (ticker,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Failed to find or insert company for ticker {ticker}")
            company_id = row[0]
        conn.commit()
        self._cache[ticker] = company_id
        return company_id


def extract_tickers(text: str) -> List[str]:
    """
    Extract potential tickers from a piece of text.  This heuristic looks for terms like $AAPL
    or sequences of 2–5 uppercase letters (common stock symbols) surrounded by word boundaries.
    """
    tickers = set()
    # Dollar sign prefixed tickers
    for match in re.finditer(r"\$(?P<tkr>[A-Z]{1,5})\b", text):
        tickers.add(match.group("tkr"))
    # Capitalised words
    for match in re.finditer(r"\b([A-Z]{2,5})\b", text):
        tickers.add(match.group(1))
    return list(tickers)


def fetch_dummy_articles() -> List[Dict[str, Any]]:
    """Generate synthetic news articles with random sentiment for demonstration purposes."""
    now = dt.datetime.utcnow()
    tickers = ["AAPL", "GOOG", "MSFT", "AMZN", "TSLA", "NFLX"]
    articles = []
    for _ in range(5):
        tkr = random.choice(tickers)
        sentiment_score = random.uniform(-1, 1)
        title = f"{tkr} quarterly results beat expectations"
        description = f"{tkr} reported strong earnings growth in the last quarter."
        articles.append(
            {
                "time": now,
                "source": "dummy",
                "url": f"https://example.com/{tkr}/{int(now.timestamp())}",
                "title": title,
                "description": description,
                "tickers": [tkr],
                "sentiment_score": sentiment_score,
                "pos": max(0.0, sentiment_score),
                "neg": max(0.0, -sentiment_score),
                "neu": 1.0 - abs(sentiment_score),
            }
        )
    return articles


def init_finbert_model() -> Tuple[Any, Any]:
    """Load the FinBERT model and tokenizer.  Returns (tokenizer, model)."""
    if AutoTokenizer is None or AutoModelForSequenceClassification is None:
        raise RuntimeError("transformers library is not available in this environment")
    logger.info("Loading FinBERT model… this may take a while on first run")
    tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
    model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
    model.eval()
    return tokenizer, model


def infer_sentiment(model, tokenizer, text: str) -> Tuple[float, float, float, float]:
    """Compute positive, neutral and negative probabilities and the net sentiment score for a given text."""
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    with torch.no_grad():
        logits = model(**inputs).logits
        probs = F.softmax(logits, dim=1).squeeze().tolist()
    pos, neu, neg = probs[0], probs[1], probs[2]
    s_score = pos - neg
    return s_score, pos, neu, neg


def save_articles(conn, cache: CompanyCache, articles: List[Dict[str, Any]]):
    """Persist raw articles and sentiment scores into the database."""
    cur = conn.cursor()
    raw_rows = []
    sentiment_rows = []
    for art in articles:
        # Insert raw article
        raw_rows.append((art["time"], art["source"], art["url"], art["title"], art["description"], art["tickers"]))
        # Insert sentiment for each ticker
        for tkr in art["tickers"]:
            company_id = cache.get_company_id(conn, tkr)
            sentiment_rows.append((art["time"], company_id, art["source"], art["sentiment_score"], art["pos"], art["neu"], art["neg"], art["url"]))

    # Insert raw articles; ignore duplicates based on url
    psycopg2.extras.execute_batch(
        cur,
        "INSERT INTO news_raw (time, source, url, title, description, tickers) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING",
        raw_rows,
        page_size=100,
    )
    # Insert sentiment; ignore duplicates based on (time, company_id, url)
    psycopg2.extras.execute_batch(
        cur,
        "INSERT INTO news_sentiment (time, company_id, source, s_score, s_pos, s_neu, s_neg, url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (time, company_id, url) DO NOTHING",
        sentiment_rows,
        page_size=100,
    )
    conn.commit()
    logger.info(f"Inserted {len(raw_rows)} articles and {len(sentiment_rows)} sentiment rows")


def fetch_and_process_news(conn, model=None, tokenizer=None) -> List[Dict[str, Any]]:
    """Fetch new articles and run sentiment analysis (or generate dummy data)."""
    if USE_DUMMY_NEWS or not NEWS_API_KEY:
        articles = fetch_dummy_articles()
        return articles
    # Real NewsAPI fetch
    import requests
    url = "https://newsapi.org/v2/top-headlines"
    params = {
        "category": "business",
        "language": "en",
        "pageSize": 100,
        "apiKey": NEWS_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch news: {e}")
        return []
    items = []
    for article in data.get("articles", []):
        title = article.get("title") or ""
        desc = article.get("description") or ""
        text = f"{title}\n{desc}"
        tickers = extract_tickers(text)
        if not tickers:
            continue
        published_at = article.get("publishedAt")
        try:
            timestamp = _parse_iso8601(published_at) if published_at else dt.datetime.utcnow()
        except Exception:
            logger.warning(f"Unable to parse publishedAt value: {published_at!r}")
            timestamp = dt.datetime.utcnow()
        s_score, pos, neu, neg = infer_sentiment(model, tokenizer, text)
        items.append(
            {
                "time": timestamp,
                "source": "newsapi",
                "url": article.get("url", ""),
                "title": title,
                "description": desc,
                "tickers": tickers,
                "sentiment_score": s_score,
                "pos": pos,
                "neg": neg,
                "neu": neu,
            }
        )
    return items


def main():
    logger.info("Starting news ingestion service")
    conn = connect_db()
    cache = CompanyCache()
    tokenizer = None
    model = None
    if not USE_DUMMY_NEWS and NEWS_API_KEY:
        tokenizer, model = init_finbert_model()
    try:
        while True:
            articles = fetch_and_process_news(conn, model, tokenizer)
            if articles:
                save_articles(conn, cache, articles)
            else:
                logger.info("No new articles fetched")
            time.sleep(POLLING_INTERVAL)
    except KeyboardInterrupt:
        logger.info("News ingestion service terminated by user")
    finally:
        conn.close()


if __name__ == "__main__":
    main()