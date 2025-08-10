-- TimescaleDB schema for the Market Intelligence & Sentiment Analysis Dashboard

-- Enable the TimescaleDB extension.  On first start the container will create the extension automatically.
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Companies master table.  Each company maps a ticker to a human readable name and exchange.
CREATE TABLE IF NOT EXISTS company (
  id SERIAL PRIMARY KEY,
  ticker TEXT UNIQUE NOT NULL,
  name TEXT,
  exchange TEXT
);

-- One‑minute price bars for each company.  Use a hypertable so that inserts and queries scale.
CREATE TABLE IF NOT EXISTS price_bar (
  time TIMESTAMPTZ NOT NULL,
  company_id INT NOT NULL REFERENCES company(id),
  open NUMERIC,
  high NUMERIC,
  low NUMERIC,
  close NUMERIC,
  volume BIGINT,
  PRIMARY KEY (time, company_id)
);
-- Convert to hypertable
SELECT create_hypertable('price_bar','time', if_not_exists => TRUE);

-- Raw news articles as ingested from external APIs.  Includes the list of tickers extracted from the headline/description.
CREATE TABLE IF NOT EXISTS news_raw (
  id BIGSERIAL PRIMARY KEY,
  time TIMESTAMPTZ NOT NULL,
  source TEXT,
  url TEXT UNIQUE,
  title TEXT,
  description TEXT,
  tickers TEXT[]
);
SELECT create_hypertable('news_raw','time', if_not_exists => TRUE);

-- Sentiment scores derived from news articles.  Each record is tied to a company and contains the probability of positive, neutral and negative sentiment.
CREATE TABLE IF NOT EXISTS news_sentiment (
  id BIGSERIAL PRIMARY KEY,
  time TIMESTAMPTZ NOT NULL,
  company_id INT NOT NULL REFERENCES company(id),
  source TEXT,
  s_score REAL,
  s_pos REAL,
  s_neu REAL,
  s_neg REAL,
  url TEXT,
  UNIQUE (time, company_id, url)
);
SELECT create_hypertable('news_sentiment','time', if_not_exists => TRUE);

-- Continuous aggregate: 15‑minute sentiment summary per company
CREATE MATERIALIZED VIEW IF NOT EXISTS ca_sentiment_15m
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('15 minutes', time) AS bucket,
  company_id,
  AVG(s_score) AS avg_sentiment,
  COUNT(*) AS article_count
FROM news_sentiment
GROUP BY bucket, company_id;

-- Continuous aggregate: 15‑minute price summary per company
CREATE MATERIALIZED VIEW IF NOT EXISTS ca_price_15m
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('15 minutes', time) AS bucket,
  company_id,
  first(open, time) AS open,
  max(high) AS high,
  min(low) AS low,
  last(close, time) AS close,
  sum(volume) AS volume
FROM price_bar
GROUP BY bucket, company_id;

-- Configure continuous aggregate refresh policies.  These settings refresh new buckets every minute and
-- drop old data after 30 days.  Adjust retention periods to fit your needs.
SELECT add_continuous_aggregate_policy('ca_sentiment_15m',
  start_offset => INTERVAL '7 days',
  end_offset   => INTERVAL '0 seconds',
  schedule_interval => INTERVAL '1 minute'
);
SELECT add_continuous_aggregate_policy('ca_price_15m',
  start_offset => INTERVAL '7 days',
  end_offset   => INTERVAL '0 seconds',
  schedule_interval => INTERVAL '1 minute'
);