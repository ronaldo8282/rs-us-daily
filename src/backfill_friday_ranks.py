#!/usr/bin/env python3
"""Backfill Friday wide Rank/Percentile columns from historical closes.

Downloads the Nasdaq Trader universe closes once (multi-year), then for each
target Friday asof (US calendar), slices the panel and computes IBD-style RS
(same 40/20/20/20 + SPY normalize → Percentile 1–99 as rank_rs.py). Appends
columns to output/friday/rs_friday_ranks.csv and rs_friday_percentiles.csv,
skipping dates already present. Writes after each Friday so kills are safe.

Examples:
  python src/backfill_friday_ranks.py --weeks 26
  python src/backfill_friday_ranks.py --weeks 104 --period 3y
  python src/backfill_friday_ranks.py --fridays 2026-04-24,2026-05-01
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rank_rs import (  # noqa: E402
    WINDOWS,
    download_closes,
    fetch_nasdaq_trader_universe,
    percentile_1_99,
    rs_score_from_closes,
)

OUT_DIR = ROOT / "output" / "friday"
RANKS_PATH = OUT_DIR / "rs_friday_ranks.csv"
PCTS_PATH = OUT_DIR / "rs_friday_percentiles.csv"


def _merge_wide(existing: Path, fresh: pd.DataFrame, col: str) -> pd.DataFrame:
    if existing.exists():
        wide = pd.read_csv(existing)
        wide["Ticker"] = wide["Ticker"].astype(str).str.upper()
        if col in wide.columns:
            print(f"  column {col} already in {existing.name}; skip write", flush=True)
            return wide
        wide = wide.merge(fresh, on="Ticker", how="outer")
    else:
        wide = fresh.copy()
    wide = wide.sort_values("Ticker").reset_index(drop=True)
    date_cols = sorted(c for c in wide.columns if c != "Ticker")
    return wide[["Ticker"] + date_cols]


def _existing_date_cols(path: Path) -> set[str]:
    if not path.exists():
        return set()
    cols = pd.read_csv(path, nrows=0).columns.tolist()
    return {c for c in cols if c != "Ticker"}


def friday_dates_ending(
    last: dt.date, weeks: int, closes_index: pd.DatetimeIndex | None = None
) -> list[dt.date]:
    """Return up to `weeks` Fridays on or before `last`, newest first then reversed for chrono fill."""
    # Walk back calendar Fridays
    d = last
    while d.weekday() != 4:
        d -= dt.timedelta(days=1)
    fridays: list[dt.date] = []
    while len(fridays) < weeks:
        fridays.append(d)
        d -= dt.timedelta(days=7)
    fridays = list(reversed(fridays))  # oldest → newest for stable append
    if closes_index is None or len(closes_index) == 0:
        return fridays
    # Snap each Friday to the last available trading day on or before that Friday
    # (holiday → use prior session). Keep the column label as the Friday ISO date
    # when the session exists; if Friday is a holiday, label with the session date
    # actually used so asof matches closes.
    snapped: list[dt.date] = []
    idx_dates = [ts.date() for ts in closes_index]
    idx_set = set(idx_dates)
    for fri in fridays:
        if fri in idx_set:
            snapped.append(fri)
            continue
        # find last trading day <= fri
        cand = None
        for td in reversed(idx_dates):
            if td <= fri:
                cand = td
                break
        if cand is not None and (fri - cand).days <= 4:
            # Use actual session date as column (holiday Friday → Thu)
            snapped.append(cand)
        # else skip (no data near that week)
    # unique preserve order
    seen: set[dt.date] = set()
    out: list[dt.date] = []
    for x in snapped:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def score_asof(
    closes: pd.DataFrame, asof: dt.date, tickers: list[str], normalize_spy: bool
) -> pd.DataFrame:
    """Return DataFrame Ticker, Rank, Percentile for panel sliced to asof."""
    # Normalize index to dates
    panel = closes.loc[: pd.Timestamp(asof)].copy()
    if panel.empty:
        return pd.DataFrame(columns=["Ticker", "Rank", "Percentile"])

    spy_score = None
    if normalize_spy:
        if "SPY" not in panel.columns:
            raise RuntimeError("SPY missing from closes panel")
        spy_score = rs_score_from_closes(panel["SPY"])
        if spy_score is None:
            return pd.DataFrame(columns=["Ticker", "Rank", "Percentile"])

    raw: dict[str, float] = {}
    for t in tickers:
        if t not in panel.columns:
            continue
        sc = rs_score_from_closes(panel[t])
        if sc is None:
            continue
        if normalize_spy:
            final = (1.0 + sc) / (1.0 + spy_score)
        else:
            final = sc
        raw[t] = final

    if not raw:
        return pd.DataFrame(columns=["Ticker", "Rank", "Percentile"])

    score_s = pd.Series(raw, dtype=float)
    pct_s = percentile_1_99(score_s)
    out = pd.DataFrame(
        {
            "Ticker": score_s.index.astype(str),
            "Relative Strength": score_s.values * 100.0,
            "Percentile": pct_s.astype(int).values,
        }
    )
    out = out.sort_values(
        ["Percentile", "Relative Strength", "Ticker"],
        ascending=[False, False, True],
    ).reset_index(drop=True)
    out.insert(0, "Rank", np.arange(1, len(out) + 1))
    return out[["Ticker", "Rank", "Percentile"]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill Friday wide RS ranks")
    ap.add_argument("--weeks", type=int, default=26, help="number of Fridays to ensure (default 26)")
    ap.add_argument("--period", default="3y", help="yfinance download period (default 3y)")
    ap.add_argument(
        "--end",
        default="",
        help="last Friday anchor YYYY-MM-DD (default: latest close date or today)",
    )
    ap.add_argument(
        "--fridays",
        default="",
        help="comma-separated explicit asof dates (overrides --weeks generation)",
    )
    ap.add_argument("--limit", type=int, default=0, help="cap universe size (0=all)")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--pause", type=float, default=0.35)
    ap.add_argument("--no-normalize-spy", action="store_true")
    ap.add_argument(
        "--reuse-cache",
        type=Path,
        default=ROOT / "output" / ".cache_closes.pkl",
        help="optional pickle cache for closes panel",
    )
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Prefer the ranked universe already in friday/rs_stocks so we do not
    # pull warrants/units from the full Nasdaq directory (~7k+) into the wide file.
    rs_path = ROOT / "output" / "rs_stocks.csv"
    if RANKS_PATH.exists():
        have = pd.read_csv(RANKS_PATH, usecols=["Ticker"])
        tick = have["Ticker"].astype(str).str.upper().str.strip()
        universe = pd.DataFrame({"Ticker": tick.unique(), "Exchange": ""})
        print(f"universe from friday ranks: {len(universe)}", flush=True)
    elif rs_path.exists():
        have = pd.read_csv(rs_path, usecols=["Ticker"])
        tick = have["Ticker"].astype(str).str.upper().str.strip()
        universe = pd.DataFrame({"Ticker": tick.unique(), "Exchange": ""})
        print(f"universe from rs_stocks: {len(universe)}", flush=True)
    else:
        print("fetching Nasdaq Trader listings …", flush=True)
        universe = fetch_nasdaq_trader_universe()
        print(f"listings: {len(universe)}", flush=True)
    # Drop junk symbols
    universe = universe[
        universe["Ticker"].str.len().gt(0)
        & ~universe["Ticker"].str.contains("FILE CREATION", case=False, na=False)
        & ~universe["Ticker"].str.contains(r"[\s:]", regex=True, na=False)
    ].copy()
    if args.limit and args.limit > 0:
        universe = universe.head(args.limit).copy()
        print(f"limited universe to {len(universe)}", flush=True)
    tickers = universe["Ticker"].astype(str).str.upper().tolist()
    need = list(dict.fromkeys(tickers + ["SPY"]))
    print(f"universe={len(tickers)} (+SPY) download period={args.period}", flush=True)

    closes = None
    if not args.no_cache and args.reuse_cache.exists():
        try:
            closes = pd.read_pickle(args.reuse_cache)
            print(f"loaded closes cache {args.reuse_cache} shape={closes.shape}", flush=True)
            missing = [t for t in need if t not in closes.columns]
            if missing:
                print(f"cache missing {len(missing)} tickers; re-download all", flush=True)
                closes = None
        except Exception as e:
            print(f"cache read failed ({e}); re-download", flush=True)
            closes = None

    if closes is None:
        t0 = time.time()
        closes = download_closes(need, period=args.period, batch_size=args.batch_size, pause_s=args.pause)
        print(f"download done in {time.time()-t0:.0f}s shape={closes.shape}", flush=True)
        if closes.empty:
            print("no closes downloaded", file=sys.stderr)
            return 1
        if not args.no_cache:
            try:
                args.reuse_cache.parent.mkdir(parents=True, exist_ok=True)
                closes.to_pickle(args.reuse_cache)
                print(f"wrote cache {args.reuse_cache}", flush=True)
            except Exception as e:
                print(f"cache write skipped: {e}", flush=True)

    # Normalize columns to upper
    closes.columns = [str(c).upper() for c in closes.columns]
    closes = closes.loc[:, ~closes.columns.duplicated()]
    closes.index = pd.to_datetime(closes.index).tz_localize(None)

    if args.end:
        end = dt.date.fromisoformat(args.end[:10])
    else:
        end = closes.index.max().date()

    if args.fridays.strip():
        targets = [dt.date.fromisoformat(x.strip()[:10]) for x in args.fridays.split(",") if x.strip()]
        targets = sorted(set(targets))
    else:
        targets = friday_dates_ending(end, args.weeks, closes.index)

    already = _existing_date_cols(RANKS_PATH) | _existing_date_cols(PCTS_PATH)
    todo = [d for d in targets if d.isoformat() not in already]
    print(
        f"targets={len(targets)} already={len(already)} todo={len(todo)} "
        f"range={targets[0] if targets else None}→{targets[-1] if targets else None}",
        flush=True,
    )

    normalize = not args.no_normalize_spy
    done = 0
    for asof in todo:
        col = asof.isoformat()
        print(f"scoring asof {col} …", flush=True)
        t0 = time.time()
        ranked = score_asof(closes, asof, tickers, normalize)
        if ranked.empty:
            print(f"  empty result for {col}; skip", flush=True)
            continue
        ranks = ranked[["Ticker", "Rank"]].rename(columns={"Rank": col})
        pcts = ranked[["Ticker", "Percentile"]].rename(columns={"Percentile": col})
        ranks_w = _merge_wide(RANKS_PATH, ranks, col)
        pcts_w = _merge_wide(PCTS_PATH, pcts, col)
        ranks_w.to_csv(RANKS_PATH, index=False)
        pcts_w.to_csv(PCTS_PATH, index=False)
        done += 1
        print(
            f"  wrote {col}: n={len(ranked)} cols_ranks={len(ranks_w.columns)-1} "
            f"in {time.time()-t0:.1f}s",
            flush=True,
        )

    # Summary
    final_cols = sorted(c for c in pd.read_csv(RANKS_PATH, nrows=0).columns if c != "Ticker")
    n_tickers = len(pd.read_csv(RANKS_PATH, usecols=["Ticker"]))
    print(
        f"DONE added={done} friday_cols={len(final_cols)} "
        f"first={final_cols[0] if final_cols else None} last={final_cols[-1] if final_cols else None} "
        f"tickers={n_tickers}",
        flush=True,
    )
    print(f"need {WINDOWS[-1]} trading days history per asof; period={args.period}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
