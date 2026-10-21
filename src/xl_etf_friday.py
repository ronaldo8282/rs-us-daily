#!/usr/bin/env python3
"""XL sector SPDR ETF Friday Relative Strength sidecar.

Nasdaq Trader universe in rank_rs drops ETF=Y, so XLF/XLK/… are absent from
output/friday/*.csv historically. This script downloads XL + SPY closes and,
for each Friday column already present in rs_friday_ranks.csv, computes the
same IBD RS + SPY-normalize Relative Strength (Final×100) as rank_rs.py —
without re-ranking the full ~6k stock universe.

Also ranks the 11 XL ETFs among themselves (XlRank 1 = strongest RS that week).

Outputs (long):
  output/xl/xl_sector_etf_weekly.csv
  docs/data/xl_sector_etf_weekly.csv  (Pages copy)

Columns: asof_date,Ticker,SectorLabel,RelativeStrength,XlRank
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rank_rs import (  # noqa: E402
    XL_SECTOR_ETFS,
    XL_SECTOR_LABELS,
    download_closes,
    rs_score_from_closes,
)

OUT_DIR = ROOT / "output" / "xl"
OUT_PATH = OUT_DIR / "xl_sector_etf_weekly.csv"
DOCS_PATH = ROOT / "docs" / "data" / "xl_sector_etf_weekly.csv"
FRIDAY_RANKS = ROOT / "output" / "friday" / "rs_friday_ranks.csv"


def friday_dates_from_ranks(path: Path) -> list[dt.date]:
    if not path.exists():
        raise FileNotFoundError(f"missing Friday wide file: {path}")
    cols = pd.read_csv(path, nrows=0).columns.tolist()
    dates: list[dt.date] = []
    for c in cols:
        if c == "Ticker":
            continue
        try:
            dates.append(dt.date.fromisoformat(str(c)[:10]))
        except ValueError:
            continue
    return sorted(dates)


def rs_asof(closes: pd.DataFrame, asof: dt.date, tickers: list[str]) -> dict[str, float]:
    """SPY-normalized Relative Strength (Final×100) for tickers as of asof."""
    panel = closes.loc[: pd.Timestamp(asof)].copy()
    if panel.empty or "SPY" not in panel.columns:
        return {}
    spy_score = rs_score_from_closes(panel["SPY"])
    if spy_score is None:
        return {}
    out: dict[str, float] = {}
    for t in tickers:
        if t not in panel.columns:
            continue
        sc = rs_score_from_closes(panel[t])
        if sc is None:
            continue
        final = (1.0 + sc) / (1.0 + spy_score)
        out[t] = round(float(final * 100.0), 4)
    return out


def build_rows(closes: pd.DataFrame, dates: list[dt.date]) -> pd.DataFrame:
    tickers = list(XL_SECTOR_ETFS)
    records: list[dict] = []
    for asof in dates:
        scores = rs_asof(closes, asof, tickers)
        if not scores:
            print(f"  skip {asof}: no scores", flush=True)
            continue
        # XlRank among the 11: 1 = highest Relative Strength
        ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        rank_map = {t: i + 1 for i, (t, _) in enumerate(ordered)}
        for t in tickers:
            if t not in scores:
                continue
            records.append(
                {
                    "asof_date": asof.isoformat(),
                    "Ticker": t,
                    "SectorLabel": XL_SECTOR_LABELS.get(t, ""),
                    "RelativeStrength": scores[t],
                    "XlRank": rank_map[t],
                }
            )
        print(f"  {asof}: {len(scores)} XL ETFs", flush=True)
    if not records:
        return pd.DataFrame(
            columns=["asof_date", "Ticker", "SectorLabel", "RelativeStrength", "XlRank"]
        )
    return pd.DataFrame.from_records(records)


def write_outputs(df: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    df = df.sort_values(["asof_date", "XlRank", "Ticker"]).reset_index(drop=True)
    df.to_csv(OUT_PATH, index=False)
    df.to_csv(DOCS_PATH, index=False)
    print(f"wrote {len(df)} rows → {OUT_PATH}", flush=True)
    print(f"pages  → {DOCS_PATH}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="XL sector ETF Friday RS sidecar")
    ap.add_argument("--period", default="3y", help="yfinance download period (default 3y)")
    ap.add_argument(
        "--friday-ranks",
        type=Path,
        default=FRIDAY_RANKS,
        help="wide Friday ranks CSV whose date columns define asof dates",
    )
    ap.add_argument(
        "--asof",
        default="",
        help="optional single asof YYYY-MM-DD (upsert into existing long file)",
    )
    ap.add_argument(
        "--dates",
        default="",
        help="comma-separated asof dates (overrides friday-ranks date list)",
    )
    args = ap.parse_args(argv)

    if args.dates.strip():
        dates = [dt.date.fromisoformat(x.strip()[:10]) for x in args.dates.split(",") if x.strip()]
    elif args.asof.strip():
        dates = [dt.date.fromisoformat(args.asof.strip()[:10])]
    else:
        dates = friday_dates_from_ranks(args.friday_ranks)

    if not dates:
        print("no Friday dates to process", flush=True)
        return 1

    need = list(XL_SECTOR_ETFS) + ["SPY"]
    print(f"downloading closes for {len(need)} symbols ({args.period}) …", flush=True)
    closes = download_closes(need, period=args.period, batch_size=20, pause_s=1.0)
    if closes.empty:
        raise RuntimeError("no XL/SPY closes downloaded")
    missing = [t for t in need if t not in closes.columns]
    if missing:
        print(f"warning: missing columns: {missing}", flush=True)

    fresh = build_rows(closes, dates)
    if fresh.empty:
        print("no rows produced", flush=True)
        return 1

    # Upsert: replace rows for asof dates we just computed; keep others
    if OUT_PATH.exists() and (args.asof.strip() or args.dates.strip()):
        old = pd.read_csv(OUT_PATH)
        old["asof_date"] = old["asof_date"].astype(str).str.slice(0, 10)
        drop_dates = set(fresh["asof_date"].astype(str))
        keep = old[~old["asof_date"].isin(drop_dates)]
        combined = pd.concat([keep, fresh], ignore_index=True)
    else:
        combined = fresh

    write_outputs(combined)

    # Sample latest asof for stdout
    latest = combined["asof_date"].max()
    sample = combined[combined["asof_date"] == latest].sort_values("XlRank")
    print(f"\nlatest asof {latest}:", flush=True)
    print(sample.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
