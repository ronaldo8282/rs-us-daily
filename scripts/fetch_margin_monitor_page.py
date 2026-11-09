#!/usr/bin/env python3
"""Vendoring refresh for docs/margin-monitor/.

Curls the live marginMonitor data.js, history.js, breadth.js into
 docs/margin-monitor/ and regenerates prices.js from the cached
 docs/data/otc_fin_pressure.csv.

Run nightly after fetch_otc_fin.py so otc_fin_pressure.csv is current.
"""
from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "margin-monitor"
CSV_PATH = ROOT / "docs" / "data" / "otc_fin_pressure.csv"
BASE_URL = "https://twm-z3sodp.github.io/marginMonitor"
UA = "Mozilla/5.0 (compatible; rs-us-daily-otc/1.0)"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def generate_prices(csv_path: Path) -> str:
    prices: dict[str, dict[str, float]] = {}
    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            d = (row.get("date") or "").strip()
            if not d:
                continue
            yyyymmdd = d.replace("-", "")
            entry: dict[str, float] = {}
            for col, key in (("taiex_close", "taiex"), ("ewt_close", "ewt")):
                val = (row.get(col) or "").strip()
                if val:
                    try:
                        entry[key] = float(val)
                    except ValueError:
                        pass
            if entry:
                prices[yyyymmdd] = entry
    if not prices:
        raise ValueError("No prices found in " + str(csv_path))
    return f"window.MM_PRICES = {prices};\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh vendored marginMonitor files")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files")
    parser.add_argument("--skip-live", action="store_true", help="Skip curl; only regenerate prices.js")
    args = parser.parse_args()

    TARGET.mkdir(parents=True, exist_ok=True)

    files = {
        "data.js": f"{BASE_URL}/data.js",
        "history.js": f"{BASE_URL}/history.js",
        "breadth.js": f"{BASE_URL}/breadth.js",
    }

    for filename, url in files.items():
        if args.skip_live:
            print(f"skip live: {filename}")
            continue
        print(f"fetching {url}")
        data = fetch(url)
        if not args.dry_run:
            (TARGET / filename).write_bytes(data)
            print(f"  wrote {TARGET / filename} ({len(data)} bytes)")
        else:
            print(f"  would write {TARGET / filename} ({len(data)} bytes)")

    print(f"generating prices.js from {CSV_PATH}")
    prices_js = generate_prices(CSV_PATH)
    if not args.dry_run:
        (TARGET / "prices.js").write_text(prices_js, encoding="utf-8")
        print(f"  wrote {TARGET / 'prices.js'} ({len(prices_js)} bytes)")
    else:
        print(f"  would write {TARGET / 'prices.js'} ({len(prices_js)} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
