# Smoke test — 2026-10-21 (Asia/Hong_Kong)

Command:
```
.venv/bin/python src/rank_rs.py --smoke --out output/rs_stocks_smoke.csv --fetch-info
```

- Universe: 50 liquid US names + SPY for normalization
- Rows written: **50** (all had ≥252 bars)
- SPY RS score: ~0.0957
- asof_date: **2026-10-20** (last close in the yfinance panel)
- Top 10 by Percentile: AMD, MRK, NOW, ACN, LLY, XOM, ABBV, CRM, TMO, KO
- MarketCap / Sector / Industry populated via `--fetch-info`
- Note: with n=50, Percentile steps are coarse (~2 pts); full-universe run will densify 1–99

Pipeline OK. Full Nasdaq Trader universe + GitHub push still pending (no gh auth / no remote yet).
