#!/usr/bin/env python3
"""Build the COT Crowded Market Report history from official CFTC data.

Source: Legacy Commitments of Traders, Futures and Options Combined.
Annual text files published at
https://www.cftc.gov/files/dea/history/deahistfo{year}.zip
and listed on the CFTC Historical Compressed page.

Commercial / large-speculator / small-speculator nets are the legacy
categories the dashboard already charts:

- commercial = Commercial Long (All) - Commercial Short (All)
- large speculator = Noncommercial Long (All) - Noncommercial Short (All)
- small speculator = Nonreportable Long (All) - Nonreportable Short (All)

The COT index is the 156-week stochastic of each net series, including the
current week. A point is published only after a full 156-week window exists,
so the latest index is not a shorter-window approximation.

Writes only docs/cot/data/cot_history.json.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://www.cftc.gov/files/dea/history/deahistfo{year}.zip"
REPORT_NAME = "CFTC Legacy Commitments of Traders — Futures and Options Combined"
INDEX_WEEKS = 156
START_YEAR = 2018
# Tuesday as-of dates are released the following Friday. A Saturday run should
# still see a report from the last few weeks; older than this means the fetch
# did not reach the current file.
MAX_STALE_DAYS = 21
USER_AGENT = "rs-us-daily-cot/1.0 (public CFTC historical archive; GitHub Actions)"

# Page contract id -> CFTC Contract Market Code. `expect` must appear in the
# latest official market name so a reused code cannot silently attach the
# wrong contract. `reject` drops a name that contains the expected token but
# is a different product (micro, ultra, calendar spread).
CONTRACTS: dict[str, dict[str, object]] = {
    "COPPER": {"code": "085692", "expect": "COPPER"},
    "GOLD": {"code": "088691", "expect": "GOLD", "reject": ["MICRO"]},
    "PALLADIUM": {"code": "075651", "expect": "PALLADIUM"},
    "PLATINUM": {"code": "076651", "expect": "PLATINUM"},
    "SILVER": {"code": "084691", "expect": "SILVER", "reject": ["MICRO"]},
    "CORN": {"code": "002602", "expect": "CORN", "reject": ["CSO"]},
    "SOYBEAN_MEAL": {"code": "026603", "expect": "SOYBEAN MEAL"},
    "SOYBEAN_OIL": {"code": "007601", "expect": "SOYBEAN OIL"},
    "SOYBEANS": {"code": "005602", "expect": "SOYBEANS", "reject": ["CSO"]},
    "WHEAT": {"code": "001602", "expect": "WHEAT-SRW"},
    "2Y_NOTES": {"code": "042601", "expect": "UST 2Y"},
    "5Y_NOTES": {"code": "044601", "expect": "UST 5Y"},
    "10Y_NOTES": {"code": "043602", "expect": "UST 10Y", "reject": ["ULTRA"]},
    "30Y_BONDS": {"code": "020601", "expect": "UST BOND", "reject": ["ULTRA"]},
    "DOW": {"code": "124603", "expect": "DJIA", "reject": ["MICRO", "REAL ESTATE"]},
    "NASDAQ": {"code": "209742", "expect": "NASDAQ", "reject": ["MICRO"]},
    "RUSSELL": {"code": "239742", "expect": "RUSSELL", "reject": ["MICRO", "1000"]},
    "SP500": {"code": "13874A", "expect": "E-MINI S&P 500", "reject": ["MICRO"]},
    "CRUDE_OIL": {"code": "067651", "expect": "WTI", "reject": ["FINANCIAL", "SPREAD", "DIFF"]},
    "HEATING_OIL": {"code": "022651", "expect": "ULSD"},
    "NATURAL_GAS": {"code": "023651", "expect": "NAT GAS", "reject": ["E-MINI", "PENULT"]},
    "UNLEADED_GAS": {"code": "111659", "expect": "RBOB"},
    "DOLLAR_INDEX": {"code": "098662", "expect": "INDEX"},
    "AUD": {"code": "232741", "expect": "AUSTRALIAN DOLLAR"},
    "GBP": {"code": "096742", "expect": "BRITISH POUND"},
    "CAD": {"code": "090741", "expect": "CANADIAN DOLLAR"},
    "EUR": {"code": "099741", "expect": "EURO FX", "reject": ["XRATE", "SHORT TERM"]},
    "JPY": {"code": "097741", "expect": "JAPANESE YEN"},
    "NZD": {"code": "112741", "expect": "NZ DOLLAR"},
    "CHF": {"code": "092741", "expect": "SWISS FRANC"},
}

NEEDED_COLUMNS = (
    "Market and Exchange Names",
    "As of Date in Form YYYY-MM-DD",
    "CFTC Contract Market Code",
    "CFTC Market Code in Initials",
    "Open Interest (All)",
    "Noncommercial Positions-Long (All)",
    "Noncommercial Positions-Short (All)",
    "Commercial Positions-Long (All)",
    "Commercial Positions-Short (All)",
    "Nonreportable Positions-Long (All)",
    "Nonreportable Positions-Short (All)",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_int(raw: str) -> int:
    text = (raw or "").strip().replace(",", "")
    if text in {"", ".", "NA", "N/A", "-"}:
        raise ValueError("blank position")
    return int(float(text))


def download_year(year: int, timeout: int = 90) -> bytes | None:
    url = BASE_URL.format(year=year)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"CFTC download failed for {year}: HTTP {exc.code} {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CFTC download failed for {year}: {exc.reason} {url}") from exc
    if not payload:
        raise RuntimeError(f"CFTC download for {year} was empty: {url}")
    return payload


def read_year_rows(payload: bytes, year: int) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [
            info
            for info in archive.infolist()
            if info.filename.lower().endswith((".txt", ".csv")) and not info.is_dir()
        ]
        if not members:
            raise RuntimeError(f"{year} zip has no text report")
        member = max(members, key=lambda info: info.file_size)
        raw = archive.read(member)
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise RuntimeError(f"{year} report has no header")
    reader.fieldnames = [name.strip() for name in reader.fieldnames]
    missing = [name for name in NEEDED_COLUMNS if name not in reader.fieldnames]
    if missing:
        raise RuntimeError(f"{year} report missing columns: {', '.join(missing)}")
    wanted = {spec["code"] for spec in CONTRACTS.values()}
    rows: list[dict[str, str]] = []
    for record in reader:
        code = (record.get("CFTC Contract Market Code") or "").strip()
        if code not in wanted:
            continue
        rows.append(record)
    return rows


def name_ok(market: str, spec: dict[str, object]) -> bool:
    upper = market.upper()
    expect = str(spec["expect"]).upper()
    if expect not in upper:
        return False
    for token in spec.get("reject", []):
        if str(token).upper() in upper:
            return False
    return True


def cot_index(value: int, low: int, high: int) -> int:
    if high == low:
        return 50
    raw = (value - low) / (high - low) * 100
    if raw < 0:
        raw = 0.0
    elif raw > 100:
        raw = 100.0
    return int(raw + 0.5)


def stochastic(series: list[int]) -> list[int] | None:
    """156-week stochastic for each point that has a full window."""
    if len(series) < INDEX_WEEKS:
        return None
    indexes: list[int] = []
    for end in range(INDEX_WEEKS - 1, len(series)):
        window = series[end - INDEX_WEEKS + 1 : end + 1]
        indexes.append(cot_index(series[end], min(window), max(window)))
    return indexes


def build_contract(code: str, history: dict[str, dict[str, int | str]]) -> dict | None:
    if not history:
        return None
    ordered = [history[day] for day in sorted(history)]
    comm = [int(row["comm_net"]) for row in ordered]
    large = [int(row["large_net"]) for row in ordered]
    small = [int(row["small_net"]) for row in ordered]
    comm_idx = stochastic(comm)
    large_idx = stochastic(large)
    small_idx = stochastic(small)
    if comm_idx is None or large_idx is None or small_idx is None:
        return {
            "matched": False,
            "cftc_code": code,
            "cftc_market": ordered[-1]["market"],
            "reason": f"only {len(ordered)} weekly observations; 156-week COT index is not defined",
            "observations": len(ordered),
        }
    # Drop the lookback weeks that exist only to seed the first valid index.
    offset = INDEX_WEEKS - 1
    labels = [str(row["date"]) for row in ordered[offset:]]
    latest = ordered[-1]
    if not (len(labels) == len(comm_idx) == len(large_idx) == len(small_idx)):
        raise RuntimeError(f"series length mismatch for {code}")
    return {
        "matched": True,
        "cftc_code": code,
        "cftc_market": latest["market"],
        "cftc_initials": latest["initials"],
        "index_lookback_weeks": INDEX_WEEKS,
        "observations": len(ordered),
        "index_weeks": len(labels),
        "history_start": ordered[0]["date"],
        "series_start": labels[0],
        "series_end": labels[-1],
        "latest": {
            "date": labels[-1],
            "comm": comm_idx[-1],
            "large": large_idx[-1],
            "small": small_idx[-1],
            "comm_net": comm[-1],
            "large_net": large[-1],
            "small_net": small[-1],
            "open_interest": int(latest["open_interest"]),
        },
        "labels": labels,
        "comm_net": comm[offset:],
        "large_net": large[offset:],
        "small_net": small[offset:],
        "comm_idx": comm_idx,
        "large_idx": large_idx,
        "small_idx": small_idx,
    }


def collect(start_year: int, end_year: int) -> tuple[dict[str, dict[str, dict[str, int | str]]], list[int]]:
    by_code: dict[str, dict[str, dict[str, int | str]]] = {
        str(spec["code"]): {} for spec in CONTRACTS.values()
    }
    loaded: list[int] = []
    for year in range(start_year, end_year + 1):
        payload = download_year(year)
        if payload is None:
            if year == end_year and date.today().month <= 2:
                print(f"No deahistfo{year}.zip yet; continuing with prior years", file=sys.stderr)
                continue
            raise RuntimeError(
                f"Missing official file https://www.cftc.gov/files/dea/history/deahistfo{year}.zip"
            )
        rows = read_year_rows(payload, year)
        loaded.append(year)
        print(f"Loaded {year}: {len(rows)} rows for mapped contracts", file=sys.stderr)
        for record in rows:
            code = record["CFTC Contract Market Code"].strip()
            as_of = record["As of Date in Form YYYY-MM-DD"].strip()
            try:
                comm_long = parse_int(record["Commercial Positions-Long (All)"])
                comm_short = parse_int(record["Commercial Positions-Short (All)"])
                large_long = parse_int(record["Noncommercial Positions-Long (All)"])
                large_short = parse_int(record["Noncommercial Positions-Short (All)"])
                small_long = parse_int(record["Nonreportable Positions-Long (All)"])
                small_short = parse_int(record["Nonreportable Positions-Short (All)"])
                open_interest = parse_int(record["Open Interest (All)"])
            except ValueError:
                continue
            by_code[code][as_of] = {
                "date": as_of,
                "market": record["Market and Exchange Names"].strip(),
                "initials": (record.get("CFTC Market Code in Initials") or "").strip(),
                "comm_net": comm_long - comm_short,
                "large_net": large_long - large_short,
                "small_net": small_long - small_short,
                "open_interest": open_interest,
            }
    if not loaded:
        raise RuntimeError("No CFTC annual files were downloaded")
    return by_code, loaded


def assemble(by_code: dict[str, dict[str, dict[str, int | str]]], years: list[int]) -> dict:
    contracts: dict[str, dict] = {}
    unmatched: list[dict[str, str]] = []
    for contract_id, spec in CONTRACTS.items():
        code = str(spec["code"])
        history = by_code.get(code, {})
        if not history:
            payload = {
                "matched": False,
                "cftc_code": code,
                "reason": f"CFTC contract market code {code} was not in the downloaded combined reports",
            }
            contracts[contract_id] = payload
            unmatched.append({"id": contract_id, "reason": payload["reason"]})
            continue
        latest_name = str(history[max(history)]["market"])
        if not name_ok(latest_name, spec):
            reason = (
                f"code {code} latest market name {latest_name!r} "
                f"did not match expected {spec['expect']!r}"
            )
            contracts[contract_id] = {
                "matched": False,
                "cftc_code": code,
                "cftc_market": latest_name,
                "reason": reason,
            }
            unmatched.append({"id": contract_id, "reason": reason})
            continue
        built = build_contract(code, history)
        if built is None or not built.get("matched"):
            reason = (built or {}).get("reason", "index could not be computed")
            contracts[contract_id] = built or {"matched": False, "cftc_code": code, "reason": reason}
            unmatched.append({"id": contract_id, "reason": reason})
            continue
        # Official long and short columns occasionally miss the open-interest
        # identity by a contract or two. A large residual means the wrong
        # columns were read; a 1-lot residual is left as published.
        net_sum = (
            built["latest"]["comm_net"]
            + built["latest"]["large_net"]
            + built["latest"]["small_net"]
        )
        tolerance = max(10, int(built["latest"]["open_interest"] * 0.002))
        if abs(net_sum) > tolerance:
            raise RuntimeError(
                f"{contract_id} latest nets sum to {net_sum}, "
                f"outside tolerance {tolerance}"
            )
        contracts[contract_id] = built

    matched_rows = [row for row in contracts.values() if row.get("matched")]
    if not matched_rows:
        raise RuntimeError("No page commodities matched a CFTC contract")
    as_of_dates = {row["latest"]["date"] for row in matched_rows}
    if len(as_of_dates) != 1:
        raise RuntimeError(f"Matched contracts do not share one as-of date: {sorted(as_of_dates)}")
    as_of = date.fromisoformat(as_of_dates.pop())
    age = date.today() - as_of
    if age > timedelta(days=MAX_STALE_DAYS):
        raise RuntimeError(
            f"Latest CFTC as-of {as_of.isoformat()} is {age.days} days old "
            f"(limit {MAX_STALE_DAYS}). Refusing to publish a stale snapshot."
        )
    index_weeks = min(row["index_weeks"] for row in matched_rows)
    if index_weeks < 26:
        raise RuntimeError(f"Shortest published series is {index_weeks} weeks; need at least 26")
    return {
        "source": REPORT_NAME,
        "source_url_pattern": BASE_URL,
        "source_page": "https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm",
        "report": "legacy_futures_options_combined",
        "index_lookback_weeks": INDEX_WEEKS,
        "index_method": "100 * (net - min(net, 156)) / (max(net, 156) - min(net, 156)), current week included",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "as_of": as_of.isoformat(),
        "years": years,
        "history_start": min(row["history_start"] for row in matched_rows),
        "series_start": min(row["series_start"] for row in matched_rows),
        "series_end": max(row["series_end"] for row in matched_rows),
        "index_weeks_min": index_weeks,
        "observations_min": min(row["observations"] for row in matched_rows),
        "contracts": contracts,
        "unmatched": unmatched,
    }


def default_output() -> Path:
    return repo_root() / "docs" / "cot" / "data" / "cot_history.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--out", type=Path, default=default_output())
    args = parser.parse_args()
    if args.start_year > args.end_year:
        print("start year is after end year", file=sys.stderr)
        return 2
    out = args.out.resolve()
    data_root = (repo_root() / "docs" / "cot" / "data").resolve()
    if data_root not in out.parents and out != data_root:
        print(f"Refusing to write outside docs/cot/data: {out}", file=sys.stderr)
        return 2

    by_code, years = collect(args.start_year, args.end_year)
    payload = assemble(by_code, years)
    out.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    out.write_text(encoded + "\n", encoding="utf-8")

    print(f"Wrote {out}")
    print(f"as_of={payload['as_of']}")
    print(
        "weeks_observed_min="
        f"{payload['observations_min']} index_weeks_min={payload['index_weeks_min']} "
        f"history={payload['history_start']}..{payload['series_end']} "
        f"chart={payload['series_start']}..{payload['series_end']}"
    )
    print("matched:")
    for contract_id, row in payload["contracts"].items():
        if not row.get("matched"):
            continue
        latest = row["latest"]
        print(
            f"  {contract_id} code={row['cftc_code']} market={row['cftc_market']} "
            f"weeks={row['index_weeks']} obs={row['observations']} "
            f"comm={latest['comm']} large={latest['large']} small={latest['small']}"
        )
    if payload["unmatched"]:
        print("unmatched:")
        for item in payload["unmatched"]:
            print(f"  {item['id']}: {item['reason']}")
    else:
        print("unmatched: none")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — top-level CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
