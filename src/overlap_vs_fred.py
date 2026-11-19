#!/usr/bin/env python3
"""Compare our RS≥90 & MarketCap>3B set vs Fred6725/rs-log public CSV.

Reads:
  - local:  output/rs_stocks.csv  (ours)
  - remote: https://raw.githubusercontent.com/Fred6725/rs-log/main/output/rs_stocks.csv

Filters both with Percentile >= 90 and MarketCap > 3e9, then reports
overlap / only-ours / only-Fred counts and writes reports/overlap_YYYYMMDD.md.

This is a validation aid for the parallel-run period before switching
###RS≥90 input off Fred's public CSV. It does not change that Daily path.

When Jaccard is 0 because our RS≥90 MarketCap column is blank, the report
says so. The filter stays Percentile >= 90 and MarketCap > 3e9.
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
# Share of RS≥90 rows with a blank MarketCap that means Jaccard 0 is a
# missing-fundamentals problem rather than a disjoint universe.
BLANK_MCAP_ISSUE_FRACTION = 0.95


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



def filter_rs90_pct_only(df: pd.DataFrame) -> pd.DataFrame:
    """Percentile >= 90 only (MarketCap may be blank — useful before mcap fill)."""
    rows = []
    for _, row in df.iterrows():
        pct = _num(row.get("Percentile"))
        if pct is None or pct < PCT_MIN:
            continue
        t = str(row.get("Ticker") or "").strip().upper()
        if not t:
            continue
        rows.append({"Ticker": t, "Percentile": pct, "MarketCap": _num(row.get("MarketCap"))})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.drop_duplicates(subset=["Ticker"]).reset_index(drop=True)

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


def blank_mcap_data_issue(
    ours: pd.DataFrame,
    ours_pct: pd.DataFrame,
    *,
    jaccard_is_zero: bool,
) -> str | None:
    """Label a Jaccard of 0 that is caused by a blank MarketCap column.

    The overlap filter is not changed. Returns None when MarketCap is present
    or the sets differ for another reason.
    """
    if not jaccard_is_zero or len(ours) != 0 or len(ours_pct) == 0:
        return None
    if "MarketCap" not in ours_pct.columns:
        return None
    blank = sum(_num(value) is None for value in ours_pct["MarketCap"])
    n = len(ours_pct)
    if blank / n < BLANK_MCAP_ISSUE_FRACTION:
        return None
    return (
        f"**Data issue:** Ours RS≥90 MarketCap is blank on {blank}/{n} names, "
        f"so the MarketCap>{MCAP_MIN:.0f} filter matches 0 and Jaccard is 0. "
        f"This is a fundamentals fill failure, not an empty RS≥90 universe. "
        f"The filter is unchanged: Percentile>={PCT_MIN:.0f} and MarketCap>{MCAP_MIN:.0f}. "
        f"Daily ###RS≥90 stays on Fred's public CSV."
    )


def render_overlap_report(
    *,
    today: str,
    ours_label: str,
    fred_label: str,
    ours_raw_n: int,
    fred_raw_n: int,
    ours: pd.DataFrame,
    fred: pd.DataFrame,
    ours_pct: pd.DataFrame,
    fred_pct: pd.DataFrame,
) -> tuple[str, str | None]:
    ours_set = set(ours["Ticker"]) if len(ours) else set()
    fred_set = set(fred["Ticker"]) if len(fred) else set()
    ours_pct_set = set(ours_pct["Ticker"]) if len(ours_pct) else set()
    fred_pct_set = set(fred_pct["Ticker"]) if len(fred_pct) else set()
    both = sorted(ours_set & fred_set)
    only_ours = sorted(ours_set - fred_set)
    only_fred = sorted(fred_set - ours_set)
    union_n = len(ours_set | fred_set)
    note = blank_mcap_data_issue(
        ours,
        ours_pct,
        jaccard_is_zero=union_n > 0 and len(both) == 0,
    )

    def _pct_map(df: pd.DataFrame) -> dict[str, float]:
        if df.empty:
            return {}
        return dict(zip(df["Ticker"], df["Percentile"]))

    o_pct = _pct_map(ours)
    f_pct = _pct_map(fred)

    lines = [
        f"# RS≥90 ∩ MarketCap>3B overlap — {today} (Asia/Hong_Kong)",
        "",
        f"- Ours file: `{ours_label}` (raw rows={ours_raw_n})",
        f"- Fred source: `{fred_label}` (raw rows={fred_raw_n})",
        f"- Filter: Percentile>={PCT_MIN:.0f} and MarketCap>{MCAP_MIN:.0f}",
        "",
        "## Counts",
        "",
        "| Set | Count |",
        "| --- | ---: |",
        f"| Ours RS≥90 & >3B | {len(ours_set)} |",
        f"| Fred RS≥90 & >3B | {len(fred_set)} |",
        f"| Overlap | {len(both)} |",
        f"| Only ours | {len(only_ours)} |",
        f"| Only Fred | {len(only_fred)} |",
        f"| Ours RS≥90 (any mcap) | {len(ours_pct_set)} |",
        f"| Fred RS≥90 (any mcap) | {len(fred_pct_set)} |",
        f"| Overlap RS≥90 only | {len(ours_pct_set & fred_pct_set)} |",
        "",
    ]
    if ours_set or fred_set:
        jaccard = (len(both) / union_n) if union_n else 0.0
        lines += [f"Jaccard = {jaccard:.3f}", ""]
    if note:
        lines += ["## Data issue", "", note, ""]

    lines += ["## Overlap sample (first 40)", ""]
    if both:
        lines.append("| Ticker | Ours Pct | Fred Pct |")
        lines.append("| --- | ---: | ---: |")
        for ticker in both[:40]:
            lines.append(f"| {ticker} | {o_pct.get(ticker, '')} | {f_pct.get(ticker, '')} |")
    else:
        lines.append("_(none)_")
    lines.append("")

    lines += ["## Only ours (first 40)", "", ", ".join(only_ours[:40]) or "_(none)_", ""]
    lines += ["## Only Fred (first 40)", "", ", ".join(only_fred[:40]) or "_(none)_", ""]
    return "\n".join(lines) + "\n", note


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
    ours_pct = filter_rs90_pct_only(ours_raw)
    fred_pct = filter_rs90_pct_only(fred_raw)

    today = dt.datetime.now(HKT).strftime("%Y%m%d")
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report = args.report_dir / f"overlap_{today}.md"
    text, note = render_overlap_report(
        today=today,
        ours_label=str(args.ours),
        fred_label=str(args.fred),
        ours_raw_n=len(ours_raw),
        fred_raw_n=len(fred_raw),
        ours=ours,
        fred=fred,
        ours_pct=ours_pct,
        fred_pct=fred_pct,
    )
    report.write_text(text, encoding="utf-8")

    ours_n = 0 if ours.empty else ours["Ticker"].nunique()
    fred_n = 0 if fred.empty else fred["Ticker"].nunique()
    both_n = 0
    if ours_n and fred_n:
        both_n = len(set(ours["Ticker"]) & set(fred["Ticker"]))
    only_ours_n = ours_n - both_n
    only_fred_n = fred_n - both_n
    print(f"ours_rs90_mcap={ours_n}")
    print(f"fred_rs90_mcap={fred_n}")
    print(f"overlap={both_n}")
    print(f"only_ours={only_ours_n}")
    print(f"only_fred={only_fred_n}")
    if note:
        print(note, flush=True)
    print(f"report={report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
