#!/usr/bin/env python3
"""Write docs/cot/data/price_history.json for the COT bar-chart overlay.

Pulls daily closes from the public Yahoo Finance chart API and aligns each
close to a CFTC report date (that session, or the previous session within
four calendar days). Yahoo's chart API does not send browser CORS headers,
so the page reads this file instead of calling Yahoo directly.

Does not change the CFTC fetch or the weekly COT workflow.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
COT_PATH = ROOT / "data" / "cot_history.json"
OUT_PATH = ROOT / "data" / "price_history.json"
NY = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (compatible; rs-us-daily-cot/1.0)"

# Page contract id, ticker, Yahoo symbols to try in order.
# A non-futures symbol is only a fallback when the future is missing.
CONTRACTS = [
    ("COPPER", "HG", ["HG=F"]),
    ("GOLD", "GC", ["GC=F"]),
    ("PALLADIUM", "PA", ["PA=F"]),
    ("PLATINUM", "PL", ["PL=F"]),
    ("SILVER", "SI", ["SI=F"]),
    ("CORN", "ZC", ["ZC=F"]),
    ("SOYBEAN_MEAL", "ZM", ["ZM=F"]),
    ("SOYBEAN_OIL", "ZL", ["ZL=F"]),
    ("SOYBEANS", "ZS", ["ZS=F"]),
    ("WHEAT", "ZW", ["ZW=F"]),
    ("2Y_NOTES", "ZT", ["ZT=F"]),
    ("5Y_NOTES", "ZF", ["ZF=F"]),
    ("10Y_NOTES", "ZN", ["ZN=F"]),
    ("30Y_BONDS", "ZB", ["ZB=F"]),
    ("DOW", "YM", ["YM=F"]),
    ("NASDAQ", "NQ", ["NQ=F"]),
    ("RUSSELL", "RTY", ["RTY=F"]),
    ("SP500", "ES", ["ES=F"]),
    ("CRUDE_OIL", "CL", ["CL=F"]),
    ("HEATING_OIL", "HO", ["HO=F"]),
    ("NATURAL_GAS", "NG", ["NG=F"]),
    ("UNLEADED_GAS", "RB", ["RB=F"]),
    ("DOLLAR_INDEX", "DX", ["DX=F", "DX-Y.NYB"]),
    ("AUD", "6A", ["6A=F"]),
    ("GBP", "6B", ["6B=F"]),
    ("CAD", "6C", ["6C=F"]),
    ("EUR", "6E", ["6E=F"]),
    ("JPY", "6J", ["6J=F"]),
    ("NZD", "6N", ["6N=F"]),
    ("CHF", "6S", ["6S=F"]),
]


def report_labels(cot):
    labels = None
    for row in cot.get("contracts", {}).values():
        if not row.get("matched") or not row.get("labels"):
            continue
        current = list(row["labels"])
        if labels is None:
            labels = current
        elif current != labels:
            raise SystemExit("COT contracts do not share one report-date axis")
    if not labels:
        raise SystemExit("No matched COT labels")
    return labels


def fetch_chart(symbol, period1, period2):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.request.quote(symbol)}?interval=1d&period1={period1}&period2={period2}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            chart = payload.get("chart") or {}
            if chart.get("error"):
                raise RuntimeError(str(chart["error"]))
            result = (chart.get("result") or [None])[0]
            if not result:
                raise RuntimeError("empty chart result")
            return result
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}")


def closes_by_date(result):
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    by_date = {}
    for ts, close in zip(timestamps, closes):
        if close is None or ts is None:
            continue
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN
            continue
        session = datetime.fromtimestamp(int(ts), NY).date()
        by_date[session] = round(value, 6)
    return by_date


def align(labels, by_date):
    prices = []
    for label in labels:
        day = datetime.strptime(label, "%Y-%m-%d").date()
        value = None
        for back in range(5):
            value = by_date.get(day - timedelta(days=back))
            if value is not None:
                break
        prices.append(value)
    covered = sum(value is not None for value in prices)
    return prices, covered


def series_for(ticker, symbols, labels, period1, period2):
    best = None
    errors = []
    for symbol in symbols:
        try:
            result = fetch_chart(symbol, period1, period2)
            prices, covered = align(labels, closes_by_date(result))
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        kind = "futures" if symbol.endswith("=F") else "index"
        candidate = {
            "ticker": ticker,
            "symbol": symbol,
            "kind": kind,
            "ok": covered >= max(8, int(len(labels) * 0.8)),
            "covered": covered,
            "prices": prices,
            "reason": "" if covered >= max(8, int(len(labels) * 0.8)) else "報價覆蓋不足",
        }
        if best is None or candidate["covered"] > best["covered"]:
            best = candidate
        if candidate["ok"] and kind == "futures":
            break
        time.sleep(0.15)
    if best and best["ok"]:
        return best
    if best:
        return best
    return {
        "ticker": ticker,
        "symbol": symbols[0] if symbols else "",
        "kind": "futures",
        "ok": False,
        "covered": 0,
        "prices": None,
        "reason": "Yahoo Finance 沒有此報價" if not errors else "報價下載失敗",
    }


def main():
    cot = json.loads(COT_PATH.read_text())
    labels = report_labels(cot)
    start = datetime.strptime(labels[0], "%Y-%m-%d") - timedelta(days=10)
    period1 = int(start.timestamp())
    period2 = int(time.time()) + 86400
    known = {item[0]: item for item in CONTRACTS}
    contracts = {}
    for contract_id, row in cot.get("contracts", {}).items():
        if not row.get("matched"):
            continue
        spec = known.get(contract_id)
        if not spec:
            contracts[contract_id] = {
                "ticker": "",
                "symbol": "",
                "kind": "futures",
                "ok": False,
                "covered": 0,
                "prices": None,
                "reason": "沒有公開報價代碼",
            }
            print(f"{contract_id}: unmapped")
            continue
        _, ticker, symbols = spec
        print(f"{contract_id} {symbols[0]} ...", flush=True)
        built = series_for(ticker, symbols, labels, period1, period2)
        contracts[contract_id] = built
        status = "ok" if built["ok"] else built["reason"]
        print(f"  {built['symbol']} {status} {built['covered']}/{len(labels)}")
        time.sleep(0.15)

    payload = {
        "source": "Yahoo Finance chart API",
        "source_url_pattern": "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d",
        "alignment": "Futures (or fallback index) close on the CFTC report date, else the previous session within four calendar days.",
        "generated_at": datetime.now(NY).isoformat(timespec="seconds"),
        "labels_from": labels[0],
        "labels_to": labels[-1],
        "label_count": len(labels),
        "contracts": contracts,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    failed = [key for key, row in contracts.items() if not row.get("ok")]
    print(f"wrote {OUT_PATH} failed={failed or 'none'}")
    if len(contracts) - len(failed) < 20:
        raise SystemExit("too few price series")


if __name__ == "__main__":
    main()
