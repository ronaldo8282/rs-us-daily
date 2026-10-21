#!/usr/bin/env python3
"""Maintain output/fundamentals_latest.csv (Ticker, Sector, Industry, MarketCap, updated_at).

Merges any non-null Sector/Industry/MarketCap from output/rs_stocks.csv first,
then fills gaps via yfinance (same fields as rank_rs.fetch_info_batch).

Usage:
  python src/update_fundamentals.py --resume --limit 400
  python src/update_fundamentals.py --resume --limit 1500 --prioritize-friday
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "output" / "fundamentals_latest.csv"
RS_PATH = ROOT / "output" / "rs_stocks.csv"
FRIDAY_RANKS = ROOT / "output" / "friday" / "rs_friday_ranks.csv"
COLS = ["Ticker", "Sector", "Industry", "MarketCap", "updated_at"]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_ticker(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.upper()


def _is_filled(row: pd.Series) -> bool:
    mcap = row.get("MarketCap")
    if mcap is None or (isinstance(mcap, float) and pd.isna(mcap)):
        return False
    try:
        return float(mcap) > 0
    except (TypeError, ValueError):
        return False


def load_or_empty(path: Path) -> pd.DataFrame:
    if path.exists():
        df = pd.read_csv(path, dtype={"Ticker": str, "Sector": str, "Industry": str, "updated_at": str})
        df["Ticker"] = _norm_ticker(df["Ticker"])
        for c in ("Sector", "Industry", "updated_at"):
            if c not in df.columns:
                df[c] = ""
            df[c] = df[c].fillna("").astype(str).replace({"nan": "", "None": ""})
        if "MarketCap" not in df.columns:
            df["MarketCap"] = pd.NA
        df["MarketCap"] = pd.to_numeric(df["MarketCap"], errors="coerce")
        return df[COLS].drop_duplicates("Ticker", keep="last")
    return pd.DataFrame(
        {
            "Ticker": pd.Series(dtype=str),
            "Sector": pd.Series(dtype=str),
            "Industry": pd.Series(dtype=str),
            "MarketCap": pd.Series(dtype="float64"),
            "updated_at": pd.Series(dtype=str),
        }
    )


def seed_from_rs(fund: pd.DataFrame, rs_path: Path) -> pd.DataFrame:
    if not rs_path.exists():
        return fund
    rs = pd.read_csv(rs_path)
    rs["Ticker"] = _norm_ticker(rs["Ticker"])
    keep = ["Ticker"]
    for c in ("Sector", "Industry", "MarketCap"):
        if c in rs.columns:
            keep.append(c)
    rs = rs[keep].drop_duplicates("Ticker", keep="first")
    for c in ("Sector", "Industry"):
        if c in rs.columns:
            rs[c] = rs[c].where(rs[c].notna(), "").astype(str).replace({"nan": "", "None": ""})
    if "MarketCap" in rs.columns:
        rs["MarketCap"] = pd.to_numeric(rs["MarketCap"], errors="coerce")

    if fund.empty:
        out = rs.copy()
        if "Sector" not in out.columns:
            out["Sector"] = ""
        if "Industry" not in out.columns:
            out["Industry"] = ""
        if "MarketCap" not in out.columns:
            out["MarketCap"] = pd.NA
        out["updated_at"] = _now_iso()
        return out[COLS]

    # Avoid pandas StringDtype setitem vs float NaN from RS blanks
    for c in ("Sector", "Industry", "updated_at"):
        if c in fund.columns:
            fund[c] = fund[c].astype(object).where(fund[c].notna(), "").map(
                lambda x: "" if str(x) in ("nan", "None") else str(x)
            )
    fund["MarketCap"] = pd.to_numeric(fund.get("MarketCap"), errors="coerce")

    merged = fund.merge(rs, on="Ticker", how="outer", suffixes=("", "_rs"))
    for c in ("Sector", "Industry"):
        rs_c = f"{c}_rs"
        if rs_c not in merged.columns:
            continue
        merged[c] = merged[c].astype(object)
        merged[rs_c] = merged[rs_c].astype(object).where(merged[rs_c].notna(), "").map(
            lambda x: "" if str(x) in ("nan", "None") else str(x)
        )
        blank = merged[c].isna() | (merged[c].astype(str).str.strip() == "") | (
            merged[c].astype(str) == "nan"
        )
        merged.loc[blank, c] = merged.loc[blank, rs_c].values
        merged.drop(columns=[rs_c], inplace=True)
    if "MarketCap_rs" in merged.columns:
        need = merged["MarketCap"].isna()
        merged.loc[need, "MarketCap"] = merged.loc[need, "MarketCap_rs"].values
        merged.drop(columns=["MarketCap_rs"], inplace=True)
    if "updated_at" not in merged.columns:
        merged["updated_at"] = ""
    merged["updated_at"] = merged["updated_at"].fillna("").astype(object)
    for c in ("Sector", "Industry", "updated_at"):
        merged[c] = merged[c].astype(object).where(merged[c].notna(), "").map(
            lambda x: "" if str(x) in ("nan", "None") else str(x)
        )
    return merged[COLS]


def universe_tickers(prioritize_friday: bool) -> list[str]:
    tickers: list[str] = []
    if prioritize_friday and FRIDAY_RANKS.exists():
        fr = pd.read_csv(FRIDAY_RANKS, usecols=["Ticker"])
        tickers.extend(_norm_ticker(fr["Ticker"]).tolist())
    if RS_PATH.exists():
        rs = pd.read_csv(RS_PATH, usecols=["Ticker"])
        tickers.extend(_norm_ticker(rs["Ticker"]).tolist())
    # unique, preserve order (friday first)
    seen: set[str] = set()
    out: list[str] = []
    for t in tickers:
        if t and t not in seen and t != "NAN":
            seen.add(t)
            out.append(t)
    return out


def fetch_one(ticker: str) -> dict:
    try:
        raw = yf.Ticker(ticker).info or {}
        mcap = raw.get("marketCap")
        return {
            "Sector": raw.get("sector") or "",
            "Industry": raw.get("industry") or "",
            "MarketCap": float(mcap) if mcap else None,
        }
    except Exception:
        return {"Sector": "", "Industry": "", "MarketCap": None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Update fundamentals_latest.csv")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=0, help="max yfinance fetches this run (0=all pending)")
    ap.add_argument("--resume", action="store_true", help="skip tickers already filled with MarketCap")
    ap.add_argument("--prioritize-friday", action="store_true", help="fetch friday-rank tickers first")
    ap.add_argument("--pause", type=float, default=0.05, help="sleep between .info calls")
    ap.add_argument("--seed-only", action="store_true", help="merge from rs_stocks only; no yfinance")
    args = ap.parse_args(argv)

    fund = load_or_empty(args.out)
    fund = seed_from_rs(fund, RS_PATH)

    # Ensure every universe ticker has a row
    tickers = universe_tickers(args.prioritize_friday or True)
    existing = set(fund["Ticker"].tolist()) if not fund.empty else set()
    missing_rows = [t for t in tickers if t not in existing]
    if missing_rows:
        add = pd.DataFrame(
            {
                "Ticker": missing_rows,
                "Sector": "",
                "Industry": "",
                "MarketCap": None,
                "updated_at": "",
            }
        )
        fund = pd.concat([fund, add], ignore_index=True)

    fund = fund.drop_duplicates("Ticker", keep="last").reset_index(drop=True)
    for c in ("Sector", "Industry", "updated_at"):
        fund[c] = fund[c].fillna("").astype(object)
        fund[c] = fund[c].map(lambda x: "" if x is None or (isinstance(x, float) and pd.isna(x)) or str(x) in ("nan", "None") else str(x))
    fund["MarketCap"] = pd.to_numeric(fund["MarketCap"], errors="coerce").astype(object)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    if args.seed_only:
        fund.to_csv(args.out, index=False)
        filled = fund.apply(_is_filled, axis=1).sum()
        print(f"seed-only → {args.out} rows={len(fund)} filled_mcap={filled}", flush=True)
        return 0

    # Pending: no MarketCap yet
    pending_mask = ~fund.apply(_is_filled, axis=1)
    pending = fund.loc[pending_mask, "Ticker"].tolist()
    # Prefer friday order
    order = {t: i for i, t in enumerate(tickers)}
    pending.sort(key=lambda t: order.get(t, 10**9))

    if args.resume:
        print(f"resume: {pending_mask.sum()} pending of {len(fund)}", flush=True)
    if args.limit and args.limit > 0:
        pending = pending[: args.limit]
    print(f"fetching info for {len(pending)} tickers …", flush=True)

    now = _now_iso()
    filled_n = 0
    # Use positional updates on object columns to avoid dtype setitem traps
    ticker_to_pos = {t: i for i, t in enumerate(fund["Ticker"].tolist())}
    sectors = fund["Sector"].tolist()
    industries = fund["Industry"].tolist()
    mcaps = fund["MarketCap"].tolist()
    updated = fund["updated_at"].tolist()
    for i, t in enumerate(pending, 1):
        meta = fetch_one(t)
        pos = ticker_to_pos.get(t)
        if pos is None:
            continue
        if meta["Sector"]:
            sectors[pos] = str(meta["Sector"])
        if meta["Industry"]:
            industries[pos] = str(meta["Industry"])
        if meta["MarketCap"] is not None:
            mcaps[pos] = float(meta["MarketCap"])
            filled_n += 1
        updated[pos] = str(now)
        if i % 50 == 0 or i == len(pending):
            fund["Sector"] = sectors
            fund["Industry"] = industries
            fund["MarketCap"] = mcaps
            fund["updated_at"] = updated
            fund.to_csv(args.out, index=False)
            print(f"  [{i}/{len(pending)}] checkpoint filled_this_run={filled_n}", flush=True)
        time.sleep(args.pause)
    fund["Sector"] = sectors
    fund["Industry"] = industries
    fund["MarketCap"] = mcaps
    fund["updated_at"] = updated

    fund.to_csv(args.out, index=False)
    total_filled = int(fund.apply(_is_filled, axis=1).sum())
    print(
        f"wrote {args.out} rows={len(fund)} filled_mcap={total_filled} new={filled_n}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
