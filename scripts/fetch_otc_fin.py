#!/usr/bin/env python3
"""Cache the 斷頭壓力儀表 series for GitHub Pages.

The page reads only docs/data/otc_fin_pressure.csv. It does not know whether
a row came from the short public seed or from a longer history file.

Columns (otc_fin_pressure.v1), one row per session date:

    date          YYYY-MM-DD
    otc_fin       上櫃融資維持率 / finRatio
    taiex_close   TAIEX / 加權. Prefer marginMonitor series.TSE[].close so the
                  index shares dates with otc_fin. Not a stand-in ticker.
    ewt_close     EWT (iShares MSCI Taiwan ETF) USD daily close. Yahoo chart
                  API, joined on the US session date. USD proxy for the
                  0050 / 正二 thesis.

Seed: https://twm-z3sodp.github.io/marginMonitor/history.js
      window.MARGIN_HISTORY series.OTC[].fin and series.TSE[].close.
      That file is a short rolling window. This script upserts it into the
      local cache and leaves older dates in place.

Longer history (Hermes or any other dump) uses the same path:

    python scripts/fetch_otc_fin.py --merge path/to/history.csv
    python scripts/fetch_otc_fin.py --merge path/to/history.json --replace

--merge upserts by date. A non-empty incoming value replaces that field.
Empty incoming cells do not wipe a cached value. --replace starts from the
incoming file instead of the current cache, then fill-only from the seed
for dates the file does not have. CSV or JSON. Aliases: fin, finRatio,
taiex, tse_close, ewt.

Without --merge, the seed overwrites otc_fin and taiex_close on dates it
still publishes (daily tail refresh) and never deletes older rows.

EWT is filled from Yahoo for every cached date the chart API returns.
No MacroMicro scrape.

    python scripts/fetch_otc_fin.py
    python scripts/fetch_otc_fin.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "docs" / "data"
CSV_PATH = DATA_DIR / "otc_fin_pressure.csv"
JSON_PATH = DATA_DIR / "otc_fin_pressure.json"
HISTORY_URL = "https://twm-z3sodp.github.io/marginMonitor/history.js"
YAHOO_HOSTS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/",
    "https://query2.finance.yahoo.com/v8/finance/chart/",
)
NY = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (compatible; rs-us-daily-otc/1.0)"
FIELDS = ("date", "otc_fin", "taiex_close", "ewt_close")
SCHEMA = "otc_fin_pressure.v1"

INDEX_CHOICE = (
    "taiex_close is marginMonitor series.TSE[].close (TAIEX / 加權), "
    "date-aligned with OTC fin. ewt_close is Yahoo EWT, the USD Taiwan "
    "equity ETF proxy for the 0050/正二 thesis."
)

DATE_KEYS = ("date", "d", "Date", "DATE", "asof", "asof_date")
FIN_KEYS = ("otc_fin", "fin", "finRatio", "fin_ratio", "otc_fin_ratio")
TAIEX_KEYS = ("taiex_close", "taiex", "tse_close", "twse_close", "taiex_px")
EWT_KEYS = ("ewt_close", "ewt", "EWT")


def norm_date(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("/", "-")
    if len(text) >= 8 and text[:8].isdigit() and "-" not in text[:8]:
        text = text[:4] + "-" + text[4:6] + "-" + text[6:8]
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


def num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    text = str(value).strip().replace(",", "")
    if text == "" or text.lower() in {"null", "none", "nan", "na", "—", "-"}:
        return None
    try:
        out = float(text)
    except ValueError:
        return None
    return None if out != out else out


def blank_row(date: str) -> dict:
    return {"date": date, "otc_fin": None, "taiex_close": None, "ewt_close": None}


def pick(raw: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in raw and raw[key] not in ("", None):
            return raw[key]
    return None


def row_from_mapping(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    date = norm_date(pick(raw, DATE_KEYS))
    if not date:
        return None
    return {
        "date": date,
        "otc_fin": num(pick(raw, FIN_KEYS)),
        "taiex_close": num(pick(raw, TAIEX_KEYS)),
        "ewt_close": num(pick(raw, EWT_KEYS)),
    }


def upsert(base: dict[str, dict], rows: list[dict], overwrite: bool) -> None:
    """Insert or update by date. None never clears a stored number."""
    for row in rows:
        date = row["date"]
        current = base.get(date)
        if current is None:
            base[date] = {
                "date": date,
                "otc_fin": row.get("otc_fin"),
                "taiex_close": row.get("taiex_close"),
                "ewt_close": row.get("ewt_close"),
            }
            continue
        for key in ("otc_fin", "taiex_close", "ewt_close"):
            value = row.get(key)
            if value is None:
                continue
            if overwrite or current.get(key) is None:
                current[key] = value


def parse_margin_history(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("window.MARGIN_HISTORY"):
        raw = raw.split("=", 1)[1].strip()
    if raw.endswith(";"):
        raw = raw[:-1].strip()
    data = json.loads(raw)
    if not isinstance(data, dict) or "series" not in data:
        raise ValueError("MARGIN_HISTORY has no series object")
    return data


def rows_from_margin(data: dict) -> list[dict]:
    series = data.get("series") or {}
    by_date: dict[str, dict] = {}
    for item in series.get("OTC") or []:
        date = norm_date(item.get("d"))
        if not date:
            continue
        row = by_date.setdefault(date, blank_row(date))
        fin = num(item.get("fin"))
        if fin is not None:
            row["otc_fin"] = fin
    for item in series.get("TSE") or []:
        date = norm_date(item.get("d"))
        if not date:
            continue
        row = by_date.setdefault(date, blank_row(date))
        close = num(item.get("close"))
        if close is not None:
            row["taiex_close"] = close
    return [by_date[key] for key in sorted(by_date)]


def iter_json_rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("rows", "series", "data", "history"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise ValueError("JSON must be a list or an object with rows/series/data")


def load_table(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith(("{", "[")):
        payload = json.loads(text)
        mapped = []
        for item in iter_json_rows(payload):
            row = row_from_mapping(item)
            if row:
                mapped.append(row)
        return mapped
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ValueError(f"{path} has no header")
    mapped = []
    for raw in reader:
        row = row_from_mapping(raw)
        if row:
            mapped.append(row)
    return mapped


def load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    base: dict[str, dict] = {}
    upsert(base, load_table(path), overwrite=True)
    return base


def fmt(value, places: int) -> str:
    if value is None:
        return ""
    return f"{float(value):.{places}f}"


def sorted_rows(base: dict[str, dict]) -> list[dict]:
    return [base[key] for key in sorted(base)]


def write_cache(base: dict[str, dict], generated: str | None) -> list[dict]:
    rows = sorted_rows(base)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FIELDS)
        for row in rows:
            writer.writerow([
                row["date"],
                fmt(row.get("otc_fin"), 2),
                fmt(row.get("taiex_close"), 2),
                fmt(row.get("ewt_close"), 4),
            ])
    last = next((row for row in reversed(rows) if row.get("otc_fin") is not None), None)
    payload = {
        "schema": SCHEMA,
        "columns": {
            "date": "YYYY-MM-DD session date",
            "otc_fin": "OTC (上櫃) margin maintenance ratio / finRatio",
            "taiex_close": "TAIEX / 加權 close",
            "ewt_close": "EWT USD daily close",
        },
        "index_choice": INDEX_CHOICE,
        "page": "docs/index.html reads the CSV only. This JSON is the same rows plus the column contract.",
        "merge": (
            "python scripts/fetch_otc_fin.py --merge FILE "
            "[--replace]. Same four columns. Aliases: fin, finRatio, taiex, tse_close, ewt."
        ),
        "seed": HISTORY_URL,
        "generated_note": generated,
        "asof": None if last is None else last["date"],
        "otc_fin": None if last is None else last.get("otc_fin"),
        "row_count": len(rows),
        "rows": [
            {
                "date": row["date"],
                "otc_fin": row.get("otc_fin"),
                "taiex_close": row.get("taiex_close"),
                "ewt_close": row.get("ewt_close"),
            }
            for row in rows
        ],
    }
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rows


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, UnicodeError) as exc:
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"{url}: {last_error}")


def fetch_chart(symbol: str, period1: int, period2: int) -> dict:
    last_error = None
    quoted = urllib.request.quote(symbol)
    for host in YAHOO_HOSTS:
        url = f"{host}{quoted}?interval=1d&period1={period1}&period2={period2}"
        request = urllib.request.Request(url, headers={"User-Agent": UA})
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
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}")


def ewt_by_date(result: dict) -> dict[str, float]:
    """Map Yahoo daily bars to YYYY-MM-DD.

    US daily bars are timestamped at session open (NY morning) or, less
    often, at 00:00 UTC. A NY-local date before 09:00 is that UTC-midnight
    case; use the UTC date so the print stays on the session it belongs to.
    """
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    out: dict[str, float] = {}
    for ts, close in zip(timestamps, closes):
        value = num(close)
        if value is None or ts is None:
            continue
        stamp = datetime.fromtimestamp(int(ts), NY)
        if stamp.hour < 9:
            day = datetime.fromtimestamp(int(ts), tz=ZoneInfo("UTC")).date()
        else:
            day = stamp.date()
        out[day.isoformat()] = round(value, 4)
    return out


def apply_ewt(base: dict[str, dict], prices: dict[str, float]) -> int:
    filled = 0
    for date, row in base.items():
        value = prices.get(date)
        if value is None:
            continue
        row["ewt_close"] = value
        filled += 1
    return filled


def self_test() -> None:
    base: dict[str, dict] = {}
    upsert(base, [{
        "date": "2020-01-02",
        "otc_fin": 180.0,
        "taiex_close": 12000.0,
        "ewt_close": 40.0,
    }], True)
    upsert(base, [{
        "date": "2026-11-06",
        "otc_fin": 122.03,
        "taiex_close": 39701.64,
        "ewt_close": None,
    }], True)
    assert base["2020-01-02"]["otc_fin"] == 180.0
    assert base["2026-11-06"]["otc_fin"] == 122.03
    upsert(base, [{
        "date": "2020-01-02",
        "otc_fin": 175.5,
        "taiex_close": None,
        "ewt_close": 41.2,
    }], True)
    assert base["2020-01-02"]["otc_fin"] == 175.5
    assert base["2020-01-02"]["taiex_close"] == 12000.0
    assert "2026-11-06" in base
    upsert(base, [{
        "date": "2020-01-02",
        "otc_fin": 1.0,
        "taiex_close": 1.0,
        "ewt_close": 1.0,
    }], False)
    assert base["2020-01-02"]["otc_fin"] == 175.5
    upsert(base, [{
        "date": "2019-06-03",
        "otc_fin": 160.0,
        "taiex_close": 10000.0,
        "ewt_close": None,
    }], False)
    assert base["2019-06-03"]["otc_fin"] == 160.0
    text = (
        'window.MARGIN_HISTORY = {"generated":"2026-11-06 21:01","series":'
        '{"TSE":[{"d":"20261106","close":39701.64,"fin":130.55}],'
        '"OTC":[{"d":"20261106","close":304.02,"fin":122.03}]}};'
    )
    seeded = rows_from_margin(parse_margin_history(text))
    assert seeded == [{
        "date": "2026-11-06",
        "otc_fin": 122.03,
        "taiex_close": 39701.64,
        "ewt_close": None,
    }]
    aliased = row_from_mapping({
        "d": "20240102",
        "finRatio": "151.2",
        "tse_close": "18000",
        "ewt": "48.5",
    })
    assert aliased == {
        "date": "2024-01-02",
        "otc_fin": 151.2,
        "taiex_close": 18000.0,
        "ewt_close": 48.5,
    }
    # Short seed must not drop a longer cached date.
    longer: dict[str, dict] = {}
    upsert(longer, [{
        "date": "2018-01-02",
        "otc_fin": 170.0,
        "taiex_close": 10800.0,
        "ewt_close": 39.0,
    }], True)
    upsert(longer, seeded, overwrite=False)
    upsert(longer, seeded, overwrite=True)
    assert "2018-01-02" in longer and longer["2026-11-06"]["otc_fin"] == 122.03
    print("self-test ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh docs/data/otc_fin_pressure.csv")
    parser.add_argument("--merge", type=Path, help="CSV or JSON history to upsert (or use as the base with --replace)")
    parser.add_argument("--replace", action="store_true", help="Ignore the current cache and start from --merge")
    parser.add_argument("--no-seed", action="store_true", help="Do not read marginMonitor history.js")
    parser.add_argument("--seed-overwrite", action="store_true", help="Let the seed replace cached fin/TAIEX even when --merge is set")
    parser.add_argument("--no-ewt", action="store_true", help="Do not call Yahoo for EWT")
    parser.add_argument("--self-test", action="store_true", help="Run merge checks and exit")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.replace and not args.merge:
        raise SystemExit("--replace requires --merge")

    if args.replace:
        base = {}
    else:
        base = load_cache(CSV_PATH)

    generated = None
    if not args.no_seed:
        print(f"seed {HISTORY_URL}")
        data = parse_margin_history(fetch_text(HISTORY_URL))
        generated = data.get("generated")
        seeded = rows_from_margin(data)
        if not seeded:
            raise SystemExit("marginMonitor history.js returned no OTC/TSE rows")
        overwrite = args.seed_overwrite or not args.merge
        upsert(base, seeded, overwrite=overwrite)
        print(f"  seed rows={len(seeded)} overwrite={overwrite} generated={generated}")

    if args.merge:
        incoming = load_table(args.merge)
        if not incoming:
            raise SystemExit(f"no rows in {args.merge}")
        upsert(base, incoming, overwrite=True)
        print(f"merged {len(incoming)} rows from {args.merge} replace={args.replace}")

    if not base:
        raise SystemExit("cache is empty (no seed and no --merge)")

    if not args.no_ewt:
        dates = sorted(base)
        start = datetime.strptime(dates[0], "%Y-%m-%d") - timedelta(days=10)
        period1 = int(start.timestamp())
        period2 = int(time.time()) + 2 * 86400
        try:
            prices = ewt_by_date(fetch_chart("EWT", period1, period2))
        except RuntimeError as exc:
            print(f"EWT download failed; keeping cached ewt_close ({exc})")
        else:
            filled = apply_ewt(base, prices)
            print(f"EWT prints={len(prices)} joined={filled}")

    rows = write_cache(base, str(generated) if generated else None)
    last = next((row for row in reversed(rows) if row.get("otc_fin") is not None), None)
    print(f"wrote {CSV_PATH} rows={len(rows)} first={rows[0]['date']} last={rows[-1]['date']}")
    if last:
        print(f"asof {last['date']} otc_fin {last.get('otc_fin')} taiex {last.get('taiex_close')} ewt {last.get('ewt_close')}")
    print(f"wrote {JSON_PATH}")


if __name__ == "__main__":
    main()
