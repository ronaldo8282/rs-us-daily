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

Files:

- `output/rs_stocks.csv` — latest full-universe ranks (overwritten daily)
- `output/history/rs_stocks_YYYYMMDD.csv` — dated keep-record copy (`YYYYMMDD` = asof close date)

Retention: every trading day is committed to git under `output/history/` (no auto-prune; ~260KB/day). Prune policy can be added later if the repo grows too large.

## Local run

```bash
cd /workspace/rs-us-daily
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Smoke (~50 liquid names, no .info calls)
python src/rank_rs.py --smoke --out output/rs_stocks_smoke.csv

# Full listings (slow; add --limit N while testing; --fetch-info for mcap)
python src/rank_rs.py --out output/rs_stocks.csv --history-dir output/history

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
commits the CSV when `contents:write` is available.

Repo: https://github.com/ronaldo8282/rs-us-daily

## Parallel validation

1. Keep Daily `###RS≥90` on Fred’s public CSV.
2. Run this ranker daily; write `reports/overlap_YYYYMMDD.md`.
3. After ≥5 trading days of stable overlap, Klaus/Leung decide cutover
   (`filter_rs90.py --csv` pointing at our file).



## Market breadth (Stockbee MM–inspired MVP)

Daily US market-breadth series inspired by the **publicly described** Stockbee
Market Monitor columns (Pradeep / [stockbee.blogspot.com/p/mm.html](https://stockbee.blogspot.com/p/mm.html)).
**Not a copy** of Stockbee Teechart tables — Yahoo/yfinance closes approximate
the same definitions.

### Columns (`output/mm/mm_breadth_daily.csv`)

One row per trading session:

| Column | Definition |
| --- | --- |
| `date` | Session date (ISO) |
| `universe_n` | Stocks with enough history that day (see filters) |
| `up_4pct` / `down_4pct` | Count with day return ≥+4% / ≤−4% |
| `ratio_5d` | Σ up_4pct over last 5 sessions ÷ Σ down_4pct over last 5 |
| `ratio_10d` | Same over 10 sessions |
| `up_25pct_month` / `up_50pct_month` | Count with return vs **~21** trading days ago ≥+25% / ≥+50% |
| `up_25pct_quarter` / `down_25pct_quarter` | vs **~63** trading days ago ≥+25% / ≤−25% |
| `pct_above_40dma` | % of universe with close > SMA(40) (T2108-style approx) |

If the 5d/10d down-sum is **0**, the ratio is left blank (null) — not a sentinel.

### Filters (MVP)

- Universe = Nasdaq Trader common stocks (same as RS ranker); `--smoke` / `--limit` for tests
- Drop tickers with non-positive closes or insufficient bars for month/quarter/SMA40 on that day
- **No** volume / V>V1 filter yet (future enhancement)

### Cron

Runs in the same Actions job as daily RS (`21:30 UTC` Mon–Fri / ~05:30 HKT),
**after** the rank step (`python src/mm_breadth.py`). Re-downloads closes for MVP
(no shared cache yet). Upserts the last 15 sessions so ratios stay consistent.

```bash
python src/mm_breadth.py --out output/mm/mm_breadth_daily.csv
python src/mm_breadth.py --smoke   # ~50 tickers
python src/mm_breadth.py --limit 200 --rewrite-days 15
```

Historical multi-year backfill and a Pages table are **out of scope** for this MVP.

## Layout

```
rs-us-daily/
  README.md
  requirements.txt
  src/rank_rs.py
  src/mm_breadth.py
  src/append_friday_wide.py
  src/backfill_friday_ranks.py
  src/update_fundamentals.py
  src/overlap_vs_fred.py
  output/          # rs_stocks.csv, friday/, history/, mm/, fundamentals_latest.csv
  docs/            # GitHub Pages (index.html + data/)
  reports/         # overlap_YYYYMMDD.md
  .github/workflows/daily.yml
```


## Friday rank history (wide)

Leung’s continuous Friday full-universe rank table (GitHub, not Sheets):

- `output/friday/rs_friday_ranks.csv` — Ticker × asof date columns (Rank)
- `output/friday/rs_friday_percentiles.csv` — same layout (Percentile)

Appended automatically by Actions when the daily asof date is a **Friday**
(`src/append_friday_wide.py`). Bootstrap / non-Friday seed: `--force`.

## GitHub Pages MVP

Interactive Friday browser (no build step) with two tabs:

- Site root: `docs/index.html` (enable Pages: Settings → Pages → Deploy from branch `main` / folder `/docs`)
- Suggested URL: https://ronaldo8282.github.io/rs-us-daily/
- Same-origin data under `docs/data/`:
  - `rs_friday_ranks.csv` — wide Ticker × Friday Rank
  - `rs_friday_percentiles.csv` — wide Ticker × Friday Percentile
  - `fundamentals_latest.csv` — Ticker, Sector, Industry, MarketCap, updated_at

### Tab: Ranks

Range buttons (6M/1Y/2Y/3Y/5Y), week dropdown, sort by Rank/Percentile/Ticker/MarketCap, ticker search, join latest fundamentals.

### Tab: RS90–99

Weekly dashboard for names with **Percentile ≥ 90** on the as-of Friday:

- Period presets `5w` / `10w` / `20w`, plus From / As-of week dropdowns
- Summary cards: RS90–99 count, WoW Δ, period average count
- Sector & Industry count tables (% of RS90 set); blank fundamentals → `(unknown)`
- WoW adds / drops lists (ticker, percentile, sector, industry); shows “no prior week” if the previous Friday column is missing
- Optional toggle **MKT CAP >3B only** (default **off** so incomplete MarketCap does not understate counts)

Daily Actions copies the three CSVs into `docs/data/` and commits `docs/` with the rank outputs.

### Fundamentals fill

```bash
python src/update_fundamentals.py --resume --limit 400 --prioritize-friday
```

Merges any Sector/Industry/MarketCap already on `output/rs_stocks.csv`, then yfinance `.info` for gaps. Actions runs a best-effort `--limit 400` each day (`continue-on-error`). Full ~6k universe takes many runs; blank cells are OK in the UI.

### Friday history backfill

```bash
# Ensure ≥26 Friday columns (downloads ~3y closes once, caches to output/.cache_closes.pkl)
python src/backfill_friday_ranks.py --weeks 26 --period 3y
# Longer history when rate limits allow:
python src/backfill_friday_ranks.py --weeks 104 --period 5y
```

Skips columns already present; writes after each Friday. Then refresh Pages copies:

```bash
mkdir -p docs/data
cp -f output/friday/rs_friday_ranks.csv docs/data/
cp -f output/friday/rs_friday_percentiles.csv docs/data/
cp -f output/fundamentals_latest.csv docs/data/
```
