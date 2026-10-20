# rs-us-daily

Own IBD-style US Relative Strength ranker for Leung’s Daily `###RS≥90` section.

**Attribution:** Inspired by the publicly described IBD Relative Strength
methodology (40% most-recent quarter, 20% each of the prior three) and by
Fred’s public description of those weights. **Not a fork** of Fred6725/rs-log
or any other codebase — implemented independently from public methodology.

During the parallel-validation window (≥5 trading days), Daily scan still
consumes Fred’s public CSV. This repo produces a side-by-side CSV for overlap
checks before any cutover.

## Formula

```
P3  = close / close[63]  - 1
P6  = close / close[126] - 1
P9  = close / close[189] - 1
P12 = close / close[252] - 1

RS Score = 0.40*P3 + 0.20*P6 + 0.20*P9 + 0.20*P12
Final    = (1 + Score_stock) / (1 + Score_SPY)   # default; disable with --no-normalize-spy
Percentile = cross-sectional rank → integers 1–99
```

Tickers with fewer than 252 trading days of history are excluded from ranking.

`Relative Strength` in the CSV is `Final * 100` (readable scale; not IBD’s
proprietary 1–99 rating number — the `Percentile` column is the 1–99 rank).

## Universe

Default source: **Nasdaq Trader Symbol Directory** (free public text files)

- https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt
- https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt

Keeps NASDAQ, NYSE, and NYSE American common stocks; drops ETF / test issues.
Prices and (optional) fundamentals via **yfinance** (Yahoo Finance). MarketCap /
Sector / Industry are best-effort — blank is allowed; ranking still runs on RS.

## Output columns

Aligned for `filter_rs90.py` consumption:

| Column | Notes |
| --- | --- |
| Rank | 1 = highest Percentile / RS |
| Ticker | |
| Sector / Industry | blank unless `--fetch-info` |
| Exchange | NASDAQ / NYSE / NYSE American / NYSE ARCA |
| Relative Strength | Final×100 |
| Percentile | 1–99 |
| Price | last adjusted close |
| MarketCap | USD, blank if unknown |
| asof_date | last close date (ISO) |

File: `output/rs_stocks.csv`

## Local run

```bash
cd /workspace/rs-us-daily
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Smoke (~50 liquid names, no .info calls)
python src/rank_rs.py --smoke --out output/rs_stocks_smoke.csv

# Full listings (slow; add --limit N while testing; --fetch-info for mcap)
python src/rank_rs.py --out output/rs_stocks.csv

# Overlap vs Fred public CSV (needs ours CSV + network)
python src/overlap_vs_fred.py --ours output/rs_stocks.csv
```

## Actions schedule (intent)

US cash close ≈ 20:00 UTC. Job target **21:30 UTC Mon–Fri** so `output/rs_stocks.csv`
is ready by ≈ **05:30 Asia/Hong_Kong** next morning — ahead of Daily scan **07:56 HKT**.

Cron in `.github/workflows/daily.yml`:

```yaml
- cron: "30 21 * * 1-5"
```

Plus `workflow_dispatch` for manual runs. The workflow uploads an artifact and
commits the CSV when `contents:write` is available. **Do not push from this box
until the GitHub repo exists and auth is configured.**

Suggested remote: `ronaldo8282/rs-us-daily` (or another Leung-owned repo).

## Parallel validation

1. Keep Daily `###RS≥90` on Fred’s public CSV.
2. Run this ranker daily; write `reports/overlap_YYYYMMDD.md`.
3. After ≥5 trading days of stable overlap, Klaus/Leung decide cutover
   (`filter_rs90.py --csv` pointing at our file).

## Layout

```
rs-us-daily/
  README.md
  requirements.txt
  src/rank_rs.py
  src/overlap_vs_fred.py
  output/          # rs_stocks.csv (gitignored smoke file ok)
  reports/         # overlap_YYYYMMDD.md
  .github/workflows/daily.yml
```
