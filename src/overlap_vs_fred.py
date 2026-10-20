#!/usr/bin/env python3
"""Compare our RS≥90 & MarketCap>3B set vs Fred6725/rs-log public CSV.

Reads:
  - local:  output/rs_stocks.csv  (ours)
  - remote: https://raw.githubusercontent.com/Fred6725/rs-log/main/output/rs_stocks.csv

Filters both with Percentile >= 90 and MarketCap > 3e9, then reports
overlap / only-ours / only-Fred counts and writes reports/overlap_YYYYMMDD.md.

This is a validation aid for the parallel-run period before switching
###RS≥90 input off Fred's public CSV.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

HKT = ZoneInfo("Asia/Hong_Kong")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OURS = ROOT / "output" / "rs_stocks.csv"
FRED_URL = (
    "https://raw.githubusercontent.com/Fred6725/rs-log/main/output/rs_stocks.csv"
)
PCT_MIN = 90.0
MCAP_MIN = 3_000_000_000.0


def _num(s) -> float | None:
    try:
        if s is None or (isinstance(s, float) and pd.isna(s)):
            return None
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def load_csv(path_or_url: str | Path) -> pd.DataFrame:
    if isinstance(path_or_url, Path) or not str(path_or_url).startswith("http"):
        return pd.read_csv(path_or_url)
    r = requests.get(str(path_or_url), timeout=120)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))


def filter_rs90(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in df.iterrows():
        pct = _num(row.get("Percentile"))
        mcap = _num(row.get("MarketCap"))
        if pct is None or mcap is None:
            continue
        if pct >= PCT_MIN and mcap > MCAP_MIN:
            t = str(row.get("Ticker") or "").strip().upper()
            if not t:
                continue
            rows.append(
                {
                    "Ticker": t,
                    "Percentile": pct,
                    "MarketCap": mcap,
                    "Relative Strength": _num(row.get("Relative Strength")),
                    "Exchange": str(row.get("Exchange") or "").strip(),
                }
            )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.drop_duplicates(subset=["Ticker"]).reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ours", type=Path, default=DEFAULT_OURS)
    ap.add_argument("--fred", default=FRED_URL, help="Fred CSV path or URL")
    ap.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / "reports",
    )
    args = ap.parse_args(argv)

    if not args.ours.exists():
        print(f"missing ours CSV: {args.ours}", file=sys.stderr)
        return 1

    ours_raw = load_csv(args.ours)
    fred_raw = load_csv(args.fred)
    ours = filter_rs90(ours_raw)
    fred = filter_rs90(fred_raw)

    ours_set = set(ours["Ticker"]) if len(ours) else set()
    fred_set = set(fred["Ticker"]) if len(fred) else set()
    both = sorted(ours_set & fred_set)
    only_ours = sorted(ours_set - fred_set)
    only_fred = sorted(fred_set - ours_set)

    today = dt.datetime.now(HKT).strftime("%Y%m%d")
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report = args.report_dir / f"overlap_{today}.md"

    def _pct_map(df: pd.DataFrame) -> dict[str, float]:
        if df.empty:
            return {}
        return dict(zip(df["Ticker"], df["Percentile"]))

    o_pct = _pct_map(ours)
    f_pct = _pct_map(fred)

    lines = [
        f"# RS≥90 ∩ MarketCap>3B overlap — {today} (Asia/Hong_Kong)",
        "",
        f"- Ours file: `{args.ours}` (raw rows={len(ours_raw)})",
        f"- Fred source: `{args.fred}` (raw rows={len(fred_raw)})",
        f"- Filter: Percentile>={PCT_MIN:.0f} and MarketCap>{MCAP_MIN:.0f}",
        "",
        "## Counts",
        "",
        f"| Set | Count |",
        f"| --- | ---: |",
        f"| Ours RS≥90 & >3B | {len(ours_set)} |",
        f"| Fred RS≥90 & >3B | {len(fred_set)} |",
        f"| Overlap | {len(both)} |",
        f"| Only ours | {len(only_ours)} |",
        f"| Only Fred | {len(only_fred)} |",
        "",
    ]
    if ours_set or fred_set:
        union = len(ours_set | fred_set)
        jaccard = (len(both) / union) if union else 0.0
        lines += [f"Jaccard = {jaccard:.3f}", ""]

    lines += ["## Overlap sample (first 40)", ""]
    if both:
        lines.append("| Ticker | Ours Pct | Fred Pct |")
        lines.append("| --- | ---: | ---: |")
        for t in both[:40]:
            lines.append(f"| {t} | {o_pct.get(t, '')} | {f_pct.get(t, '')} |")
    else:
        lines.append("_(none)_")
    lines.append("")

    lines += ["## Only ours (first 40)", "", ", ".join(only_ours[:40]) or "_(none)_", ""]
    lines += ["## Only Fred (first 40)", "", ", ".join(only_fred[:40]) or "_(none)_", ""]

    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"ours_rs90_mcap={len(ours_set)}")
    print(f"fred_rs90_mcap={len(fred_set)}")
    print(f"overlap={len(both)}")
    print(f"only_ours={len(only_ours)}")
    print(f"only_fred={len(only_fred)}")
    print(f"report={report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
