#!/usr/bin/env python3
"""IBD-style US Relative Strength ranker.

Implements the publicly described IBD RS weighting:
  RS Score = 0.40*P3 + 0.20*P6 + 0.20*P9 + 0.20*P12
where Pn = close / close[n] - 1 for n in {63, 126, 189, 252} trading days.

Optional market-relative normalization vs SPY:
  Final = (1 + Score_stock) / (1 + Score_SPY)
then cross-sectional percentile rank mapped to 1–99.

Inspired by IBD RS methodology and Fred's public description of the
40/20/20/20 weights — not a fork of any private or public codebase.

Universe (default): NASDAQ + NYSE / NYSE American common stocks from the
free Nasdaq Trader Symbol Directory
  https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt
  https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt
ETF/test issues and names with insufficient history (<252 bars) are dropped
from ranking.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import sys
import time
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

HKT = ZoneInfo("Asia/Hong_Kong")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "output" / "rs_stocks.csv"

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

# Trading-day lookbacks for the four IBD-style windows
WINDOWS = (63, 126, 189, 252)
WEIGHTS = (0.40, 0.20, 0.20, 0.20)

# Columns aligned for /workspace/scans/fred_rs_log/filter_rs90.py consumption
OUT_COLUMNS = [
    "Rank",
    "Ticker",
    "Sector",
    "Industry",
    "Exchange",
    "Relative Strength",
    "Percentile",
    "Price",
    "MarketCap",
    "asof_date",
]


def _today_hkt() -> dt.date:
    return dt.datetime.now(HKT).date()


def fetch_nasdaq_trader_universe() -> pd.DataFrame:
    """Download NASDAQ + NYSE/NYSE American common-stock listings.

    Source: Nasdaq Trader Symbol Directory (free, public text files).
    Filters: exclude ETF, test issues, and non-common stock categories.
    """
    rows: list[dict] = []

    # nasdaqlisted.txt: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
    r = requests.get(NASDAQ_LISTED, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), sep="|")
    df = df[df["Symbol"].notna() & (df["Symbol"] != "File Creation Time")]
    if "Test Issue" in df.columns:
        df = df[df["Test Issue"].astype(str).str.upper() != "Y"]
    if "ETF" in df.columns:
        df = df[df["ETF"].astype(str).str.upper() != "Y"]
    for _, row in df.iterrows():
        sym = str(row["Symbol"]).strip().upper()
        if not sym or "^" in sym:
            continue
        rows.append({"Ticker": sym, "Exchange": "NASDAQ"})

    # otherlisted.txt: ACT Symbol|Security Name|Exchange|...|ETF|...|NASDAQ Symbol
    # Exchange codes: A=NYSE American, N=NYSE, P=NYSE Arca, Z=BATS, V=IEX
    r = requests.get(OTHER_LISTED, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), sep="|")
    df = df[df["ACT Symbol"].notna() & (df["ACT Symbol"] != "File Creation Time")]
    if "Test Issue" in df.columns:
        df = df[df["Test Issue"].astype(str).str.upper() != "Y"]
    if "ETF" in df.columns:
        df = df[df["ETF"].astype(str).str.upper() != "Y"]
    exch_map = {"A": "NYSE American", "N": "NYSE", "P": "NYSE ARCA"}
    for _, row in df.iterrows():
        code = str(row.get("Exchange", "")).strip()
        if code not in exch_map:
            continue
        sym = str(row["ACT Symbol"]).strip().upper()
        # Skip preferred / warrant / unit suffixes that break yfinance
        if not sym or any(ch in sym for ch in ("^", "$", " ", ".")):
            # Keep Class shares like BRK.B → BRK-B for Yahoo
            if "." in sym and sym.count(".") == 1:
                sym = sym.replace(".", "-")
            else:
                continue
        rows.append({"Ticker": sym, "Exchange": exch_map[code]})

    uni = pd.DataFrame(rows).drop_duplicates(subset=["Ticker"], keep="first")
    return uni.reset_index(drop=True)


def smoke_universe() -> pd.DataFrame:
    """Small liquid set for pipeline proof (not a full ranking universe)."""
    tickers = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B",
        "JPM", "V", "UNH", "XOM", "MA", "LLY", "AVGO", "JNJ", "WMT", "PG",
        "HD", "COST", "ORCL", "ABBV", "CRM", "BAC", "KO", "MRK", "PEP",
        "AMD", "NFLX", "ADBE", "CSCO", "TMO", "ACN", "MCD", "ABT", "DHR",
        "LIN", "TXN", "PM", "DIS", "INTU", "QCOM", "AMAT", "ISRG", "GE",
        "CAT", "BKNG", "AXP", "IBM", "NOW",
    ]
    # Prefer NASDAQ for tech, NYSE otherwise — approximate for smoke
    nasdaq = {
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
        "COST", "ORCL", "AMD", "NFLX", "ADBE", "CSCO", "TXN", "INTU",
        "QCOM", "AMAT", "ISRG", "BKNG",
    }
    rows = [
        {"Ticker": t, "Exchange": "NASDAQ" if t in nasdaq else "NYSE"}
        for t in tickers
    ]
    return pd.DataFrame(rows)


def download_closes(
    tickers: list[str],
    period: str = "2y",
    batch_size: int = 50,
    pause_s: float = 0.4,
) -> pd.DataFrame:
    """Download adjusted closes via yfinance; columns = tickers."""
    frames: list[pd.DataFrame] = []
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        data = yf.download(
            batch,
            period=period,
            group_by="column",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        if data.empty:
            continue
        if isinstance(data.columns, pd.MultiIndex):
            # Multi-ticker: ('Close', 'AAPL') or ('AAPL', 'Close') depending on version
            if "Close" in data.columns.get_level_values(0):
                closes = data["Close"].copy()
            elif "Close" in data.columns.get_level_values(1):
                closes = data.xs("Close", axis=1, level=1).copy()
            else:
                # Fallback: take last level named Close-like
                closes = data.loc[:, pd.IndexSlice[:, "Close"]].copy()
                closes.columns = closes.columns.droplevel(1)
        else:
            # Single ticker
            closes = data[["Close"]].copy()
            closes.columns = [batch[0]]
        frames.append(closes)
        if i + batch_size < len(tickers):
            time.sleep(pause_s)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1)
    # Drop duplicate columns if any
    out = out.loc[:, ~out.columns.duplicated()]
    return out.sort_index()


def rs_score_from_closes(closes: pd.Series) -> float | None:
    """Compute weighted RS score for one ticker; None if insufficient history."""
    s = closes.dropna()
    if len(s) < WINDOWS[-1] + 1:
        return None
    last = float(s.iloc[-1])
    if last <= 0:
        return None
    parts = []
    for n, w in zip(WINDOWS, WEIGHTS):
        past = float(s.iloc[-(n + 1)])
        if past <= 0:
            return None
        parts.append(w * (last / past - 1.0))
    return float(sum(parts))


def percentile_1_99(scores: pd.Series) -> pd.Series:
    """Cross-sectional rank percentile mapped to integers 1–99."""
    # Average rank so ties share rank; pct = rank / n * 99, clipped to [1, 99]
    ranks = scores.rank(method="average", ascending=True)
    n = scores.notna().sum()
    if n <= 1:
        return pd.Series(50, index=scores.index, dtype=float)
    pct = (ranks / n * 99.0).clip(1, 99)
    return pct.round(0)


def fetch_info_batch(tickers: Iterable[str]) -> dict[str, dict]:
    """Best-effort Sector / Industry / MarketCap from yfinance .info (slow).

    For smoke / small runs only. Full-universe runs should prefer
    --skip-info and leave MarketCap/Sector/Industry blank, or a bulk feed.
    """
    out: dict[str, dict] = {}
    for t in tickers:
        info: dict = {}
        try:
            raw = yf.Ticker(t).info or {}
            info["Sector"] = raw.get("sector") or ""
            info["Industry"] = raw.get("industry") or ""
            mcap = raw.get("marketCap")
            info["MarketCap"] = float(mcap) if mcap else None
        except Exception:
            info = {"Sector": "", "Industry": "", "MarketCap": None}
        out[t] = info
        time.sleep(0.05)
    return out


def fill_info_for_high_pct(df: pd.DataFrame, pct_min: float) -> pd.DataFrame:
    """Fill Sector/Industry/MarketCap via yfinance for rows with Percentile >= pct_min.

    Full-universe .info is too slow; post-rank top-percentile fill keeps Actions under
    timeout while enabling RS≥90 & MarketCap>3B overlap vs Fred.
    """
    if pct_min <= 0 or df.empty:
        return df
    mask = df["Percentile"].astype(float) >= pct_min
    tickers = df.loc[mask, "Ticker"].tolist()
    if not tickers:
        return df
    print(f"fetching info for {len(tickers)} tickers with Percentile>={pct_min:g} …", flush=True)
    info = fetch_info_batch(tickers)
    out = df.copy()
    for i, row in out.loc[mask].iterrows():
        meta = info.get(row["Ticker"], {})
        if meta.get("Sector"):
            out.at[i, "Sector"] = meta["Sector"]
        if meta.get("Industry"):
            out.at[i, "Industry"] = meta["Industry"]
        if meta.get("MarketCap") is not None:
            out.at[i, "MarketCap"] = meta["MarketCap"]
    return out


def run_rank(
    universe: pd.DataFrame,
    *,
    normalize_spy: bool = True,
    fetch_info: bool = False,
    asof: dt.date | None = None,
) -> pd.DataFrame:
    tickers = universe["Ticker"].tolist()
    need = list(dict.fromkeys(tickers + (["SPY"] if normalize_spy else [])))
    print(f"downloading closes for {len(need)} symbols …", flush=True)
    closes = download_closes(need)
    if closes.empty:
        raise RuntimeError("no price data downloaded")

    spy_score = None
    if normalize_spy:
        if "SPY" not in closes.columns:
            raise RuntimeError("SPY closes missing; cannot normalize")
        spy_score = rs_score_from_closes(closes["SPY"])
        if spy_score is None:
            raise RuntimeError("SPY has insufficient history")
        print(f"SPY RS score={spy_score:.6f}", flush=True)

    raw_scores: dict[str, float] = {}
    prices: dict[str, float] = {}
    for t in tickers:
        if t not in closes.columns:
            continue
        sc = rs_score_from_closes(closes[t])
        if sc is None:
            continue
        if normalize_spy:
            final = (1.0 + sc) / (1.0 + spy_score)
        else:
            final = sc
        raw_scores[t] = final
        last = closes[t].dropna()
        prices[t] = float(last.iloc[-1])

    if not raw_scores:
        raise RuntimeError("no tickers produced a valid RS score")

    score_s = pd.Series(raw_scores, dtype=float)
    pct_s = percentile_1_99(score_s)

    # Scale "Relative Strength" similarly to public IBD-style tables:
    # keep the normalized score * 100 for readability (not IBD's proprietary scale).
    rs_display = score_s * 100.0

    info = fetch_info_batch(raw_scores.keys()) if fetch_info else {}

    asof_date = asof or _today_hkt()
    # Prefer last close date from the panel when available
    if len(closes.index):
        asof_date = closes.index.max().date()

    exch = dict(zip(universe["Ticker"], universe["Exchange"]))
    records = []
    for t, sc in raw_scores.items():
        meta = info.get(t, {})
        records.append(
            {
                "Ticker": t,
                "Sector": meta.get("Sector") or "",
                "Industry": meta.get("Industry") or "",
                "Exchange": exch.get(t, ""),
                "Relative Strength": round(float(rs_display[t]), 2),
                "Percentile": int(pct_s[t]),
                "Price": round(prices[t], 4),
                "MarketCap": meta.get("MarketCap"),
                "asof_date": asof_date.isoformat(),
            }
        )

    out = pd.DataFrame.from_records(records)
    out = out.sort_values(
        ["Percentile", "Relative Strength", "Ticker"],
        ascending=[False, False, True],
    ).reset_index(drop=True)
    out.insert(0, "Rank", np.arange(1, len(out) + 1))
    return out[OUT_COLUMNS]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="IBD-style US RS ranker")
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="output CSV path (default: output/rs_stocks.csv)",
    )
    p.add_argument(
        "--smoke",
        action="store_true",
        help="rank a small ~50-ticker liquid set instead of full listings",
    )
    p.add_argument(
        "--no-normalize-spy",
        action="store_true",
        help="use raw weighted return score (skip SPY relative normalization)",
    )
    p.add_argument(
        "--fetch-info",
        action="store_true",
        help="pull Sector/Industry/MarketCap via yfinance .info (slow)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="optional cap on universe size after listing download (0=all)",
    )
    p.add_argument(
        "--fetch-info-pct",
        type=float,
        default=0.0,
        help="after ranking, fetch Sector/Industry/MarketCap for Percentile>=N (0=off)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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

    df = run_rank(
        universe,
        normalize_spy=not args.no_normalize_spy,
        fetch_info=args.fetch_info,
    )
    if args.fetch_info_pct and args.fetch_info_pct > 0:
        df = fill_info_for_high_pct(df, args.fetch_info_pct)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)
    print(f"wrote {len(df)} rows → {args.out}", flush=True)
    print("top 10:", flush=True)
    print(df.head(10).to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
