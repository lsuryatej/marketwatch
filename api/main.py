#!/usr/bin/env python3
"""
FastAPI backend for the Market Intelligence & Sentiment Analysis Dashboard.

This application exposes REST endpoints for listing companies and retrieving aggregated price and
sentiment history, as well as a simple Server‑Sent Events (SSE) endpoint for live updates.

All database interactions are performed asynchronously using asyncpg.  The database URL is
configured via the DATABASE_URL environment variable.
"""

import os
import asyncio
import json
import datetime as dt
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import asyncpg


DATABASE_URL = os.getenv("DATABASE_URL") or "postgresql://postgres:postgres@database:5432/market"

app = FastAPI(title="Market Intelligence API", version="0.1.0")


@app.on_event("startup")
async def startup_event():
    # Create a connection pool to the database
    app.state.pool = await asyncpg.create_pool(dsn=DATABASE_URL)


@app.on_event("shutdown")
async def shutdown_event():
    # Close the connection pool on shutdown
    await app.state.pool.close()


@app.get("/api/v1/companies", response_model=List[Dict[str, Any]])
async def list_companies():
    """Return all companies tracked by the system."""
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, ticker, name, exchange FROM company ORDER BY ticker")
        return [dict(row) for row in rows]


@app.get("/api/v1/company/{ticker}/history", response_model=List[Dict[str, Any]])
async def company_history(ticker: str, start: Optional[str] = None, end: Optional[str] = None, bucket: str = "15 minutes"):
    """
    Return historical aggregated price and sentiment for a given ticker.

    Parameters:
      - ticker: stock symbol
      - start: ISO8601 start timestamp (defaults to 7 days ago)
      - end: ISO8601 end timestamp (defaults to now)
      - bucket: time bucket for aggregation, e.g. '15 minutes', '1 hour'
    """
    # Parse dates
    try:
        end_dt = dt.datetime.fromisoformat(end) if end else dt.datetime.utcnow()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid end date format")
    try:
        start_dt = dt.datetime.fromisoformat(start) if start else end_dt - dt.timedelta(days=7)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid start date format")
    if start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="start must be before end")
    # Acquire connection
    async with app.state.pool.acquire() as conn:
        # Resolve company_id
        row = await conn.fetchrow("SELECT id FROM company WHERE ticker = $1", ticker.upper())
        if not row:
            raise HTTPException(status_code=404, detail=f"Ticker {ticker} not found")
        company_id = row["id"]
        # Query aggregated price and sentiment, joining on bucket time
        query = f"""
        WITH times AS (
          SELECT time_bucket($1, time) AS bucket,
                 AVG(s_score) AS avg_sentiment,
                 COUNT(*) AS article_count
          FROM news_sentiment
          WHERE company_id = $2 AND time >= $3 AND time < $4
          GROUP BY bucket
        )
        , price AS (
          SELECT time_bucket($1, time) AS bucket,
                 FIRST(open, time) AS open,
                 MAX(high) AS high,
                 MIN(low) AS low,
                 LAST(close, time) AS close,
                 SUM(volume) AS volume
          FROM price_bar
          WHERE company_id = $2 AND time >= $3 AND time < $4
          GROUP BY bucket
        )
        SELECT price.bucket AS time,
               price.open, price.high, price.low, price.close, price.volume,
               COALESCE(times.avg_sentiment, 0) AS avg_sentiment,
               COALESCE(times.article_count, 0) AS article_count
        FROM price
        LEFT JOIN times ON price.bucket = times.bucket
        ORDER BY price.bucket;
        """
        rows = await conn.fetch(query, bucket, company_id, start_dt, end_dt)
        # Convert datetime to ISO format
        result = []
        for r in rows:
            d = dict(r)
            d["time"] = d["time"].isoformat()
            result.append(d)
        return result


@app.get("/api/v1/stream")
async def stream_endpoint(request: Request, ticker: Optional[str] = None):
    """
    Simple server‑sent events (SSE) endpoint that streams the latest price and sentiment updates.
    Clients may optionally filter by ticker.  This implementation polls the database every 5 seconds
    and emits the most recent bar for each ticker.
    """
    interval = 5  # seconds
    async def event_generator():
        while True:
            # If client disconnects, stop generating events
            if await request.is_disconnected():
                break
            payload = []
            async with app.state.pool.acquire() as conn:
                if ticker:
                    tickers = [ticker.upper()]
                else:
                    rows = await conn.fetch("SELECT ticker FROM company")
                    tickers = [r["ticker"] for r in rows]
                for t in tickers:
                    row = await conn.fetchrow(
                        "SELECT id FROM company WHERE ticker = $1", t
                    )
                    if not row:
                        continue
                    cid = row["id"]
                    # Get most recent price bar
                    price_row = await conn.fetchrow(
                        "SELECT time, open, high, low, close, volume FROM price_bar WHERE company_id = $1 ORDER BY time DESC LIMIT 1",
                        cid,
                    )
                    # Get most recent sentiment
                    sentiment_row = await conn.fetchrow(
                        "SELECT time, s_score FROM news_sentiment WHERE company_id = $1 ORDER BY time DESC LIMIT 1",
                        cid,
                    )
                    payload.append({
                        "ticker": t,
                        "price": dict(price_row) if price_row else None,
                        "sentiment": dict(sentiment_row) if sentiment_row else None,
                    })
            # Yield event as JSON
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(interval)
    return StreamingResponse(event_generator(), media_type="text/event-stream")