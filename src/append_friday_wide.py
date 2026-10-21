#!/usr/bin/env python3
"""Append one asof column of Rank/Percentile onto Friday wide CSVs.

Wide layout: Ticker + one column per asof date (YYYY-MM-DD).
Default: only append when asof_date is a Friday (US calendar date),
or when --force is set (bootstrap / backfill).

Does not replace daily output/rs_stocks.csv or output/history/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = ROOT / "output" / "rs_stocks.csv"
DEFAULT_OUT_DIR = ROOT / "output" / "friday"


def _is_friday(asof: str) -> bool:
    d = dt.date.fromisoformat(str(asof)[:10])
    return d.weekday() == 4  # Monday=0 … Friday=4


def _merge_wide(existing: Path, fresh: pd.DataFrame, col: str) -> pd.DataFrame:
    if existing.exists():
        wide = pd.read_csv(existing)
        wide["Ticker"] = wide["Ticker"].astype(str).str.upper()
        if col in wide.columns:
            print(f"column {col} already present in {existing.name}; skip", flush=True)
            return wide
        wide = wide.merge(fresh, on="Ticker", how="outer")
    else:
        wide = fresh.copy()
    wide = wide.sort_values("Ticker").reset_index(drop=True)
    date_cols = sorted(c for c in wide.columns if c != "Ticker")
    return wide[["Ticker"] + date_cols]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--force", action="store_true", help="append even if asof is not Friday")
    args = ap.parse_args(argv)

    if not args.src.exists():
        print(f"missing {args.src}", file=sys.stderr)
        return 1

    df = pd.read_csv(args.src)
    asof = str(df["asof_date"].iloc[0])[:10]
    if not args.force and not _is_friday(asof):
        print(f"asof {asof} is not Friday; skip (use --force to override)", flush=True)
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    base = df[["Ticker", "Rank", "Percentile"]].copy()
    base["Ticker"] = base["Ticker"].astype(str).str.upper()
    base = base.drop_duplicates("Ticker")

    ranks_path = args.out_dir / "rs_friday_ranks.csv"
    pct_path = args.out_dir / "rs_friday_percentiles.csv"

    ranks = _merge_wide(
        ranks_path, base[["Ticker", "Rank"]].rename(columns={"Rank": asof}), asof
    )
    pcts = _merge_wide(
        pct_path, base[["Ticker", "Percentile"]].rename(columns={"Percentile": asof}), asof
    )
    ranks.to_csv(ranks_path, index=False)
    pcts.to_csv(pct_path, index=False)
    print(f"wrote {ranks_path} ({len(ranks)} tickers, cols={list(ranks.columns)})", flush=True)
    print(f"wrote {pct_path} ({len(pcts)} tickers)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
