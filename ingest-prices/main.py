#!/usr/bin/env python3
"""
Generate and ingest price bars into the database.

This service simulates one‑minute OHLCV bars for a set of companies.  It is designed for local
development; in a production deployment you would replace the dummy generator with a connector
to a streaming price feed such as Finnhub or Polygon.  When USE_DUMMY_PRICES is set to false
and FINNHUB_API_KEY or POLYGON_API_KEY is defined, the script will attempt to fetch the latest
price data via REST as a fallback.  If neither key is provided, dummy data is always used.
"""

import os
import time
import random
import logging
import datetime as dt
from typing import Dict, List, Tuple

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest-prices")


DATABASE_URL = os.getenv("DATABASE_URL") or "postgresql://postgres:postgres@database:5432/market"
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
USE_DUMMY_PRICES = os.getenv("USE_DUMMY_PRICES", "true").lower() in ("1", "true", "yes")
POLLING_INTERVAL = int(os.getenv("PRICE_POLLING_INTERVAL_SECONDS", "60"))


def connect_db():
    return psycopg2.connect(DATABASE_URL)


def get_tickers(conn) -> List[str]:
    """Fetch all ticker symbols from the company table.  If none exist, return a default set."""
    with conn.cursor() as cur:
        cur.execute("SELECT ticker FROM company")
        rows = cur.fetchall()
        tickers = [row[0] for row in rows]
    if not tickers:
        tickers = ["AAPL", "GOOG", "MSFT", "AMZN", "TSLA", "NFLX"]
    return tickers


def init_price_state(tickers: List[str]) -> Dict[str, float]:
    """Initialise a base price for each ticker."""
    state: Dict[str, float] = {}
    for t in tickers:
        # Start around 100–500 dollars
        state[t] = random.uniform(100.0, 500.0)
    return state


def generate_bar(prev_close: float) -> Tuple[float, float, float, float, int]:
    """Generate a random OHLCV bar based on previous close price."""
    # Simulate drift
    change = random.gauss(0, 0.5)
    open_price = prev_close
    close_price = max(0.01, open_price + change)
    # High and low within the range of open and close plus some volatility
    high_price = max(open_price, close_price) + abs(random.gauss(0, 0.2))
    low_price = min(open_price, close_price) - abs(random.gauss(0, 0.2))
    volume = random.randint(1000, 10000)
    return open_price, high_price, low_price, close_price, volume


def save_price_bars(conn, rows: List[Tuple]):
    """Batch insert price bars into the database."""
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "INSERT INTO price_bar (time, company_id, open, high, low, close, volume) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            rows,
            page_size=100,
        )
        conn.commit()
    logger.info(f"Inserted {len(rows)} price bars")


def get_company_id(conn, ticker: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO company (ticker) VALUES (%s) ON CONFLICT (ticker) DO NOTHING RETURNING id",
            (ticker,),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("SELECT id FROM company WHERE ticker = %s", (ticker,))
        row2 = cur.fetchone()
        if not row2:
            raise RuntimeError(f"Unable to find or insert company {ticker}")
        return row2[0]


def fetch_real_price(ticker: str) -> float:
    """Fetch the latest price for a ticker from Finnhub or Polygon via REST.  Returns None on failure."""
    import requests
    # Prefer Polygon if API key is provided because it includes after‑hours trading
    if POLYGON_API_KEY:
        url = f"https://api.polygon.io/v1/last/stocks/{ticker}"
        params = {"apiKey": POLYGON_API_KEY}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("last", {}).get("price", 0.0))
        except Exception as e:
            logger.warning(f"Polygon price fetch for {ticker} failed: {e}")
    if FINNHUB_API_KEY:
        url = f"https://finnhub.io/api/v1/quote"
        params = {"symbol": ticker, "token": FINNHUB_API_KEY}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("c", 0.0))
        except Exception as e:
            logger.warning(f"Finnhub price fetch for {ticker} failed: {e}")
    return None


def main():
    logger.info("Starting price ingestion service")
    conn = connect_db()
    tickers = get_tickers(conn)
    price_state = init_price_state(tickers)
    try:
        while True:
            # Refresh tickers from DB periodically
            tickers = get_tickers(conn)
            now = dt.datetime.utcnow().replace(second=0, microsecond=0)
            rows = []
            for tkr in tickers:
                company_id = get_company_id(conn, tkr)
                if not USE_DUMMY_PRICES:
                    # Try to fetch real price via API; fallback to dummy generation on failure
                    price = fetch_real_price(tkr)
                    if price is not None and price > 0:
                        price_state[tkr] = price
                # Generate a new bar using the current price state
                open_price, high_price, low_price, close_price, volume = generate_bar(price_state[tkr])
                # Update state for next period
                price_state[tkr] = close_price
                rows.append((now, company_id, open_price, high_price, low_price, close_price, volume))
            save_price_bars(conn, rows)
            time.sleep(POLLING_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Price ingestion service terminated by user")
    finally:
        conn.close()


if __name__ == "__main__":
    main()