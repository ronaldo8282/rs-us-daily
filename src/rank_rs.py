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
ETF/test issues (except force-included XL sector SPDRs) and names with
insufficient history (<252 bars) are dropped from ranking.
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
DEFAULT_FUNDAMENTALS = ROOT / "output" / "fundamentals_latest.csv"

# Overlap vs Fred gates on Percentile>=90. Warn only; this does not change
# who is ranked or which rows the daily job asks to fill.
RS90_WARN_PCT = 90.0
# Treat a fill rate under 1% as ~0 (the 2026-11-19 outage was 0/566).
RS90_MCAP_FILL_WARN_BELOW = 0.01

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

# Select Sector SPDR ETFs force-included even though Nasdaq Trader marks ETF=Y.
# Kept out of the general ETF drop so Friday ranks can carry full-universe Percentile later.
XL_SECTOR_ETFS = (
    "XLC",   # Communication Services
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLE",   # Energy
    "XLF",   # Financials
    "XLV",   # Health Care
    "XLI",   # Industrials
    "XLB",   # Materials
    "XLRE",  # Real Estate
    "XLK",   # Technology
    "XLU",   # Utilities
)
XL_SECTOR_LABELS = {
    "XLC": "Communication",
    "XLY": "Consumer Disc",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLK": "Technology",
    "XLU": "Utilities",
}

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
    Filters: exclude ETF (except XL_SECTOR_ETFS), test issues, and non-common stock categories.
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
    # Force-include XL sector SPDRs (normally dropped by ETF=Y filter).
    have = set(uni["Ticker"].astype(str).str.upper())
    extra = [
        {"Ticker": t, "Exchange": "NYSE ARCA"}
        for t in XL_SECTOR_ETFS
        if t not in have
    ]
    if extra:
        uni = pd.concat([uni, pd.DataFrame(extra)], ignore_index=True)
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
    pause_s: float = 3.0,
) -> pd.DataFrame:
    """Download adjusted closes via yfinance; columns = tickers."""
    frames: list[pd.DataFrame] = []
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        data = pd.DataFrame()
        for attempt in range(4):
            data = yf.download(
                batch,
                period=period,
                group_by="column",
                auto_adjust=True,
                threads=False,
                progress=False,
            )
            # Treat all-NaN / zero-close batches as failures (YF rate-limit often returns empty cols)
            usable = False
            if not data.empty:
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        if "Close" in data.columns.get_level_values(0):
                            chk = data["Close"]
                        elif "Close" in data.columns.get_level_values(1):
                            chk = data.xs("Close", axis=1, level=1)
                        else:
                            chk = data
                    else:
                        chk = data[["Close"]] if "Close" in data.columns else data
                    usable = bool(chk.notna().any().any())
                except Exception:
                    usable = not data.empty
            if usable:
                break
            wait = pause_s * (3 + attempt * 3)
            print(f"  rate/empty batch @ {i}/{len(tickers)} attempt {attempt+1}/4; sleep {wait:.0f}s", flush=True)
            time.sleep(wait)
        if data.empty or not usable:
            print(f"  skip failed batch @ {i}/{len(tickers)}: {batch[:3]}…", flush=True)
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


def _positive_mcap(value) -> float | None:
    """Positive MarketCap, or None when blank / non-numeric / not positive.

    Zero and negative values are not usable caps and are not written.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    if isinstance(value, str):
        text = value.replace(",", "").strip()
        if text == "" or text.lower() in {"nan", "none"}:
            return None
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def _blank_text(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        return False
    text = str(value).strip()
    return text == "" or text.lower() in {"nan", "none"}


def _cache_text(value) -> str:
    if _blank_text(value):
        return ""
    return str(value).strip()


def _load_fundamentals_lookup(path: Path) -> dict[str, dict] | None:
    """Ticker → Sector/Industry/MarketCap. None if the file cannot be read.

    A later duplicate fills a field only when it has a real value, so a blank
    duplicate does not wipe an earlier cap.
    """
    try:
        raw = pd.read_csv(path)
    except Exception as exc:
        print(f"WARNING: could not read fundamentals cache {path}: {exc}", file=sys.stderr, flush=True)
        return None
    if "Ticker" not in raw.columns:
        print(f"WARNING: fundamentals cache {path} has no Ticker column", file=sys.stderr, flush=True)
        return {}
    lookup: dict[str, dict] = {}
    for rec in raw.to_dict("records"):
        ticker = str(rec.get("Ticker") or "").strip().upper()
        if not ticker or ticker == "NAN":
            continue
        sector = _cache_text(rec.get("Sector"))
        industry = _cache_text(rec.get("Industry"))
        mcap = _positive_mcap(rec.get("MarketCap"))
        prev = lookup.get(ticker)
        if prev is None:
            lookup[ticker] = {"Sector": sector, "Industry": industry, "MarketCap": mcap}
            continue
        if sector:
            prev["Sector"] = sector
        if industry:
            prev["Industry"] = industry
        if mcap is not None:
            prev["MarketCap"] = mcap
    return lookup


def backfill_blanks_from_fundamentals(
    df: pd.DataFrame,
    mask: pd.Series,
    path: Path,
) -> dict[str, int | bool]:
    """Fill blank Sector/Industry/MarketCap on `mask` from the fundamentals cache.

    Live-filled cells are left alone. Rows outside `mask` are left alone.
    Missing file → zero fills and present=False.
    """
    stats: dict[str, int | bool] = {
        "mcap": 0,
        "sector": 0,
        "industry": 0,
        "present": path.exists(),
        "readable": False,
    }
    if not path.exists():
        return stats
    lookup = _load_fundamentals_lookup(path)
    if lookup is None:
        return stats
    stats["readable"] = True
    for i in df.index[mask]:
        ticker = str(df.at[i, "Ticker"]).strip().upper()
        meta = lookup.get(ticker)
        if not meta:
            continue
        if _blank_text(df.at[i, "Sector"]) and meta["Sector"]:
            df.at[i, "Sector"] = meta["Sector"]
            stats["sector"] = int(stats["sector"]) + 1
        if _blank_text(df.at[i, "Industry"]) and meta["Industry"]:
            df.at[i, "Industry"] = meta["Industry"]
            stats["industry"] = int(stats["industry"]) + 1
        if _positive_mcap(df.at[i, "MarketCap"]) is None and meta["MarketCap"] is not None:
            df.at[i, "MarketCap"] = meta["MarketCap"]
            stats["mcap"] = int(stats["mcap"]) + 1
    return stats


def _warn_rs90_mcap_fill(df: pd.DataFrame, cache_path: Path, pct_min: float) -> None:
    """Loud warning when the overlap slice still has ~0 MarketCap after both sources."""
    if "Percentile" not in df.columns or df.empty:
        return
    # Only judge rows this run was asked to fill that also sit in the RS≥90 gate.
    floor = max(float(pct_min), RS90_WARN_PCT)
    pct = pd.to_numeric(df["Percentile"], errors="coerce")
    mask = pct >= floor
    n = int(mask.sum())
    if n == 0:
        return
    filled = sum(1 for i in df.index[mask] if _positive_mcap(df.at[i, "MarketCap"]) is not None)
    rate = filled / n
    if filled != 0 and rate >= RS90_MCAP_FILL_WARN_BELOW:
        return
    label = "RS≥90" if floor <= RS90_WARN_PCT else f"Percentile>={floor:g}"
    msg = (
        f"WARNING: {label} MarketCap fill rate is {filled}/{n} ({rate:.1%}) "
        f"after live yfinance .info and fundamentals cache ({cache_path}). "
        f"Overlap vs Fred uses Percentile>=90 and MarketCap>3B and will collapse "
        f"toward Jaccard 0. Ranks are unchanged, and Daily ###RS≥90 stays on Fred's CSV."
    )
    print(msg, flush=True)
    print(msg, file=sys.stderr, flush=True)
    # Actions surfaces this on the job summary without failing the rank step.
    print(f"::warning::{msg}", flush=True)


def fill_info_for_high_pct(
    df: pd.DataFrame,
    pct_min: float,
    *,
    fundamentals_path: Path | None = None,
    info_fetcher=None,
) -> pd.DataFrame:
    """Fill Sector/Industry/MarketCap for rows with Percentile >= pct_min.

    Live yfinance `.info` runs first. Remaining blanks on that slice are filled
    from output/fundamentals_latest.csv when the file exists. A live value is
    never replaced by the cache. Rows below pct_min stay blank (the daily job
    only requests info for the overlap slice).

    Full-universe .info is too slow; post-rank top-percentile fill keeps Actions
    under timeout while enabling RS≥90 & MarketCap>3B overlap vs Fred.
    """
    if pct_min <= 0 or df.empty:
        return df
    mask = pd.to_numeric(df["Percentile"], errors="coerce") >= pct_min
    tickers = df.loc[mask, "Ticker"].tolist()
    if not tickers:
        return df
    cache_path = Path(fundamentals_path) if fundamentals_path is not None else DEFAULT_FUNDAMENTALS
    fetcher = info_fetcher or fetch_info_batch
    print(f"fetching info for {len(tickers)} tickers with Percentile>={pct_min:g} …", flush=True)

    info = fetcher(tickers)
    out = df.copy()
    # A CSV of all-blank Sector/Industry is read as float NaN. Force text columns
    # so a cache string can be written, including when this frame was reloaded.
    for col in ("Sector", "Industry"):
        if col in out.columns:
            out[col] = [
                "" if _blank_text(value) else str(value).strip()
                for value in out[col].tolist()
            ]
    if "MarketCap" in out.columns:
        # Keep object dtype so missing caps stay None instead of NaN. NaN would
        # count as already filled under an `is None` check.
        out["MarketCap"] = pd.Series(
            [_positive_mcap(value) for value in out["MarketCap"].tolist()],
            index=out.index,
            dtype=object,
        )
    blank_before = {
        i for i in out.index[mask] if _positive_mcap(out.at[i, "MarketCap"]) is None
    }
    for i, row in out.loc[mask].iterrows():
        meta = info.get(row["Ticker"], {}) or {}
        if meta.get("Sector"):
            out.at[i, "Sector"] = meta["Sector"]
        if meta.get("Industry"):
            out.at[i, "Industry"] = meta["Industry"]
        mcap = _positive_mcap(meta.get("MarketCap"))
        if mcap is not None:
            out.at[i, "MarketCap"] = mcap

    live_filled = sum(1 for i in blank_before if _positive_mcap(out.at[i, "MarketCap"]) is not None)
    cache_stats = backfill_blanks_from_fundamentals(out, mask, cache_path)
    still_blank = sum(1 for i in out.index[mask] if _positive_mcap(out.at[i, "MarketCap"]) is None)
    if not cache_stats["present"]:
        cache_note = f" cache=missing:{cache_path}"
    elif not cache_stats["readable"]:
        cache_note = f" cache=unreadable:{cache_path}"
    else:
        cache_note = ""
    already = int(mask.sum()) - len(blank_before)
    already_note = f" already-filled={already}" if already else ""
    print(
        f"info fill Percentile>={pct_min:g}: attempted={len(tickers)} "
        f"live-filled={live_filled} cache-backfilled={cache_stats['mcap']} "
        f"still-blank={still_blank} "
        f"sector-cache={cache_stats['sector']} industry-cache={cache_stats['industry']}"
        f"{already_note}{cache_note}",
        flush=True,
    )
    _warn_rs90_mcap_fill(out, cache_path, pct_min)
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
    p.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="also write output/history/rs_stocks_YYYYMMDD.csv from asof_date (keep record)",
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
    if args.history_dir is not None:
        args.history_dir.mkdir(parents=True, exist_ok=True)
        asof = str(df["asof_date"].iloc[0]).replace("-", "")
        hist = args.history_dir / f"rs_stocks_{asof}.csv"
        df.to_csv(hist, index=False)
        print(f"history → {hist}", flush=True)
    print("top 10:", flush=True)
    print(df.head(10).to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
