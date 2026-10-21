#!/usr/bin/env python3
"""Stockbee Market Monitor–inspired US market breadth (MVP).

Computes a daily one-row-per-session breadth table inspired by the publicly
described Stockbee Market Monitor columns (Pradeep / stockbee.blogspot.com).
Numbers are approximations from Yahoo/yfinance closes — not a copy of Stockbee
Teechart tables. Attribution: inspired by Stockbee MM.

Lookbacks (trading days):
  month ≈ 21, quarter ≈ 63, T2108-approx = % above SMA(40).

Universe: same Nasdaq Trader common-stock listings as rank_rs.py.
Filters (MVP): drop tickers with non-positive last close or insufficient history
for the metrics computed that day. No V>V1 volume filter yet (future).

Output: output/mm/mm_breadth_daily.csv (upsert by date; rewrites last N days
so ratio_5d / ratio_10d stay consistent).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rank_rs import (  # noqa: E402
    download_closes,
    fetch_nasdaq_trader_universe,
    smoke_universe,
)

DEFAULT_OUT = ROOT / "output" / "mm" / "mm_breadth_daily.csv"

MONTH_LB = 21
QUARTER_LB = 63
SMA_LB = 40
# Need prior bar for day return + max lookback
MIN_BARS = QUARTER_LB + 1

OUT_COLUMNS = [
    "date",
    "universe_n",
    "up_4pct",
    "down_4pct",
    "ratio_5d",
    "ratio_10d",
    "up_25pct_month",
    "up_50pct_month",
    "up_25pct_quarter",
    "down_25pct_quarter",
    "pct_above_40dma",
]


def _ratio(up_sum: float, down_sum: float) -> float | None:
    """5d/10d bounce ratio. Denom 0 → None (CSV empty); documented in README."""
    if down_sum <= 0:
        return None
    return round(float(up_sum) / float(down_sum), 4)


def compute_breadth_panel(
    closes: pd.DataFrame,
    *,
    rewrite_days: int = 15,
    asof: dt.date | None = None,
) -> pd.DataFrame:
    """Compute breadth rows for the last `rewrite_days` sessions (or through asof).

    For each session date D, universe = tickers with positive closes at D and at
    every required lookback bar (D-1, D-21, D-63, and SMA window). All counts
    and pct_above_40dma use that same set.
    """
    if closes.empty:
        return pd.DataFrame(columns=OUT_COLUMNS)

    # Align to dates only; drop all-NaN columns
    c = closes.copy()
    c.index = pd.to_datetime(c.index).tz_localize(None)
    c = c.sort_index()
    c = c.dropna(axis=1, how="all")
    dates = c.index
    values = c.to_numpy(dtype=float)  # (T, N)
    T, N = values.shape
    if T < MIN_BARS + 1:
        raise RuntimeError(
            f"need ≥{MIN_BARS + 1} trading days of closes; got {T}"
        )

    # Determine end index
    if asof is not None:
        asof_ts = pd.Timestamp(asof)
        # last session on or before asof
        eligible = np.where(dates.normalize() <= asof_ts.normalize())[0]
        if len(eligible) == 0:
            raise RuntimeError(f"no closes on or before asof {asof}")
        end_i = int(eligible[-1])
    else:
        end_i = T - 1

    start_i = max(MIN_BARS, end_i - rewrite_days + 1)

    # Precompute day returns at each bar i (return from i-1 → i); invalid → nan
    day_ret = np.full((T, N), np.nan, dtype=float)
    day_ret[1:] = values[1:] / values[:-1] - 1.0
    # Guard divide-by-zero / non-positive prior
    bad_prev = ~(values[:-1] > 0)
    day_ret[1:][bad_prev] = np.nan

    def universe_mask(i: int) -> tuple[np.ndarray, np.ndarray]:
        """Return (ok mask, SMA40 window) for session index i."""
        ok = (
            np.isfinite(values[i])
            & (values[i] > 0)
            & np.isfinite(values[i - 1])
            & (values[i - 1] > 0)
            & np.isfinite(values[i - MONTH_LB])
            & (values[i - MONTH_LB] > 0)
            & np.isfinite(values[i - QUARTER_LB])
            & (values[i - QUARTER_LB] > 0)
        )
        window = values[i - SMA_LB + 1 : i + 1]  # (40, N)
        ok &= np.all(np.isfinite(window) & (window > 0), axis=0)
        return ok, window

    # Precompute bounce counts for the ratio window (full universe when possible)
    daily_up: dict[int, int] = {}
    daily_down: dict[int, int] = {}
    bounce_from = max(MIN_BARS, start_i - 9)
    for j in range(bounce_from, end_i + 1):
        ok_j, _ = universe_mask(j)
        if not ok_j.any():
            daily_up[j] = 0
            daily_down[j] = 0
            continue
        dr_j = day_ret[j]
        daily_up[j] = int(np.nansum(ok_j & (dr_j >= 0.04)))
        daily_down[j] = int(np.nansum(ok_j & (dr_j <= -0.04)))

    rows: list[dict] = []
    for i in range(start_i, end_i + 1):
        ok, window = universe_mask(i)
        n = int(ok.sum())
        if n == 0:
            continue

        dr = day_ret[i]
        up4 = int(daily_up.get(i, int(np.nansum(ok & (dr >= 0.04)))))
        down4 = int(daily_down.get(i, int(np.nansum(ok & (dr <= -0.04)))))

        ret_m = values[i] / values[i - MONTH_LB] - 1.0
        ret_q = values[i] / values[i - QUARTER_LB] - 1.0
        sma40 = window.mean(axis=0)

        up25_m = int(np.nansum(ok & (ret_m >= 0.25)))
        up50_m = int(np.nansum(ok & (ret_m >= 0.50)))
        up25_q = int(np.nansum(ok & (ret_q >= 0.25)))
        down25_q = int(np.nansum(ok & (ret_q <= -0.25)))
        above40 = int(np.nansum(ok & (values[i] > sma40)))
        pct40 = round(100.0 * above40 / n, 2) if n else None

        def sum_bounce(k: int) -> tuple[float, float]:
            idxs = range(i - k + 1, i + 1)
            u = sum(daily_up.get(j, 0) for j in idxs)
            d = sum(daily_down.get(j, 0) for j in idxs)
            return float(u), float(d)

        u5, d5 = sum_bounce(5)
        u10, d10 = sum_bounce(10)

        rows.append(
            {
                "date": dates[i].date().isoformat(),
                "universe_n": n,
                "up_4pct": up4,
                "down_4pct": down4,
                "ratio_5d": _ratio(u5, d5),
                "ratio_10d": _ratio(u10, d10),
                "up_25pct_month": up25_m,
                "up_50pct_month": up50_m,
                "up_25pct_quarter": up25_q,
                "down_25pct_quarter": down25_q,
                "pct_above_40dma": pct40,
            }
        )

    return pd.DataFrame(rows, columns=OUT_COLUMNS)


def upsert_csv(path: Path, fresh: pd.DataFrame) -> pd.DataFrame:
    """Merge fresh rows by date (replace overlapping dates; keep older)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        old = pd.read_csv(path)
        if "date" not in old.columns:
            old = fresh.copy()
        else:
            drop_dates = set(fresh["date"].astype(str))
            old = old[~old["date"].astype(str).isin(drop_dates)]
            old = pd.concat([old, fresh], ignore_index=True)
    else:
        old = fresh.copy()
    old = old.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    # Ensure column order
    for col in OUT_COLUMNS:
        if col not in old.columns:
            old[col] = None
    old = old[OUT_COLUMNS].reset_index(drop=True)
    old.to_csv(path, index=False)
    return old


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Stockbee MM–inspired US market breadth (MVP)"
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="output CSV (default: output/mm/mm_breadth_daily.csv)",
    )
    p.add_argument(
        "--asof",
        type=str,
        default=None,
        help="YYYY-MM-DD; use last close on or before this date (default: latest)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="cap universe size after listings (0=all)",
    )
    p.add_argument(
        "--smoke",
        action="store_true",
        help="use ~50 liquid smoke tickers instead of full listings",
    )
    p.add_argument(
        "--rewrite-days",
        type=int,
        default=15,
        help="recompute / upsert this many trailing sessions (default 15)",
    )
    p.add_argument(
        "--period",
        type=str,
        default="1y",
        help="yfinance download period (default 1y; need ≥~70 trading days)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="yfinance batch size",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    asof = dt.date.fromisoformat(args.asof) if args.asof else None

    if args.smoke:
        universe = smoke_universe()
        print(f"smoke universe: {len(universe)} tickers", flush=True)
    else:
        print("fetching Nasdaq Trader listings …", flush=True)
        universe = fetch_nasdaq_trader_universe()
        print(f"listings: {len(universe)} tickers", flush=True)
        if args.limit and args.limit > 0:
            universe = universe.head(args.limit).copy()
            print(f"limited to {len(universe)}", flush=True)

    tickers = universe["Ticker"].tolist()
    print(f"downloading closes ({args.period}) for {len(tickers)} symbols …", flush=True)
    closes = download_closes(tickers, period=args.period, batch_size=args.batch_size)
    if closes.empty:
        raise RuntimeError("no price data downloaded")
    print(f"panel: {closes.shape[0]} days × {closes.shape[1]} tickers", flush=True)

    fresh = compute_breadth_panel(
        closes, rewrite_days=args.rewrite_days, asof=asof
    )
    if fresh.empty:
        raise RuntimeError("no breadth rows produced")

    out = upsert_csv(args.out, fresh)
    print(f"wrote {len(fresh)} fresh / {len(out)} total rows → {args.out}", flush=True)
    print("latest row:", flush=True)
    print(out.tail(1).to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
