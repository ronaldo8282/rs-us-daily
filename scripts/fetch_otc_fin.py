#!/usr/bin/env python3
"""Cache the 斷頭壓力儀表 series for GitHub Pages.

The page reads only docs/data/otc_fin_pressure.csv.

Columns (otc_fin_pressure.v2), one row per session date:

    date          YYYY-MM-DD
    otc_fin       marginMonitor 上櫃融資風險指標, cost / 成數.
                  Short rolling seed (~60 sessions). Not the Hermes print.
    mkt_fin       FinLab combined TSE+OTC mark-to-market percent,
                  Σ(融資餘額×收盤價)/融資金額. Powers 1Y/2Y/5Y/ALL.
    taiex_close   TAIEX / 加權. marginMonitor series.TSE[].close on seed
                  dates; Yahoo ^TWII on the longer file.
    ewt_close     EWT (iShares MSCI Taiwan ETF) USD daily close.

These are two 口徑. On 2026-11-06 marginMonitor otc_fin is about 122 and
mkt_fin is about 150. Do not average them. Leung's 145/150 and 205/210
levels belong to the marginMonitor line only.

Seed: https://twm-z3sodp.github.io/marginMonitor/history.js
      window.MARGIN_HISTORY series.OTC[].fin and series.TSE[].close.
      Without --merge, the seed overwrites otc_fin and taiex_close on
      dates it still publishes and never deletes older rows or mkt_fin.

Longer history:

    python scripts/fetch_otc_fin.py --merge path/to/history.csv
    python scripts/fetch_otc_fin.py --merge path/to/history.json --replace

--merge upserts by date. A non-empty incoming value replaces that field.
Empty incoming cells do not wipe a cached value.

A Hermes / FinLab dump (header or keys include tse_fin or source_otc_tse)
uses the same mark-to-market formula for its otc_fin and tse_fin columns.
Those are partial self-calcs, not marginMonitor. Default --merge stores
mkt_fin, taiex_close, and ewt_close from that file and leaves the cached
marginMonitor otc_fin in place. --overwrite-otc-fin is the explicit switch
that copies the file's otc_fin onto the marginMonitor column.

A file without those Hermes markers keeps the old aliases (fin, finRatio,
taiex, tse_close, ewt) and may write otc_fin. That is the marginMonitor-shaped
history, including this cache itself (date, otc_fin, mkt_fin, taiex_close,
ewt_close).

EWT can be refilled from Yahoo for every cached date the chart API returns.
No MacroMicro scrape.

    python scripts/fetch_otc_fin.py
    python scripts/fetch_otc_fin.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import json
import tempfile
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
FIELDS = ("date", "otc_fin", "mkt_fin", "taiex_close", "ewt_close")
VALUE_KEYS = ("otc_fin", "mkt_fin", "taiex_close", "ewt_close")
# Nightly `python scripts/fetch_otc_fin.py` may refresh these only.
# mkt_fin is written by --merge, never by the marginMonitor seed.
SEED_KEYS = ("otc_fin", "taiex_close")
SCHEMA = "otc_fin_pressure.v2"
# Present on the Hermes long file. Absent on this cache and on marginMonitor-shaped merges.
HERMES_MARKERS = ("tse_fin", "source_otc_tse")

INDEX_CHOICE = (
    "otc_fin is marginMonitor series.OTC[].fin (上櫃, cost / 成數, short seed). "
    "mkt_fin is the FinLab TSE+OTC mark-to-market ratio "
    "Σ(融資餘額×收盤價)/融資金額, used for 1Y/2Y/5Y/ALL. "
    "taiex_close prefers marginMonitor series.TSE[].close on seed dates and "
    "Yahoo ^TWII otherwise. ewt_close is Yahoo EWT, the USD Taiwan equity ETF "
    "proxy for the 0050/正二 thesis. The two fin columns are different 口徑."
)

DATE_KEYS = ("date", "d", "Date", "DATE", "asof", "asof_date")
FIN_KEYS = ("otc_fin", "fin", "finRatio", "fin_ratio", "otc_fin_ratio")
MKT_KEYS = ("mkt_fin", "finlab_mkt", "combined_mkt_fin")
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
    return {
        "date": date,
        "otc_fin": None,
        "mkt_fin": None,
        "taiex_close": None,
        "ewt_close": None,
    }


def pick(raw: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in raw and raw[key] not in ("", None):
            return raw[key]
    return None


def table_is_hermes(fieldnames) -> bool:
    names = {str(name).strip() for name in (fieldnames or []) if name}
    return any(marker in names for marker in HERMES_MARKERS)


def mapping_is_hermes(payload, fieldnames=None) -> bool:
    """True when this table is a Hermes mark-to-market dump, not our cache."""
    if table_is_hermes(fieldnames):
        return True
    sample = []
    if isinstance(payload, list):
        sample = payload[:30]
    elif isinstance(payload, dict):
        if table_is_hermes(payload.keys()):
            return True
        for key in ("rows", "series", "data", "history"):
            block = payload.get(key)
            if isinstance(block, list):
                sample = block[:30]
                break
    for item in sample:
        if isinstance(item, dict) and table_is_hermes(item.keys()):
            return True
    return False


def row_from_mapping(raw: dict, *, hermes: bool = False, overwrite_otc: bool = False) -> dict | None:
    if not isinstance(raw, dict):
        return None
    date = norm_date(pick(raw, DATE_KEYS))
    if not date:
        return None
    # Hermes otc_fin is a partial self-calc of the mkt_fin formula. It must not
    # replace marginMonitor's cost/成數 value unless the caller opted in.
    otc = None if hermes and not overwrite_otc else num(pick(raw, FIN_KEYS))
    return {
        "date": date,
        "otc_fin": otc,
        "mkt_fin": num(pick(raw, MKT_KEYS)),
        "taiex_close": num(pick(raw, TAIEX_KEYS)),
        "ewt_close": num(pick(raw, EWT_KEYS)),
    }


def upsert(base: dict[str, dict], rows: list[dict], overwrite: bool, keys=VALUE_KEYS) -> None:
    """Insert or update by date. None never clears a stored number.

    keys limits which fields this writer may touch. The nightly seed passes
    SEED_KEYS so a marginMonitor refresh cannot blank or replace mkt_fin.
    """
    for row in rows:
        date = row["date"]
        current = base.get(date)
        if current is None:
            base[date] = blank_row(date)
            current = base[date]
        for key in keys:
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


def load_table(path: Path, overwrite_otc: bool = False) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith(("{", "[")):
        payload = json.loads(text)
        hermes = mapping_is_hermes(payload)
        mapped = []
        for item in iter_json_rows(payload):
            row = row_from_mapping(item, hermes=hermes, overwrite_otc=overwrite_otc)
            if row:
                mapped.append(row)
        return mapped
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ValueError(f"{path} has no header")
    hermes = table_is_hermes(reader.fieldnames)
    mapped = []
    for raw in reader:
        row = row_from_mapping(raw, hermes=hermes, overwrite_otc=overwrite_otc)
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


def json_num(value, places: int):
    if value is None:
        return None
    return round(float(value), places)


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
                fmt(row.get("mkt_fin"), 2),
                fmt(row.get("taiex_close"), 2),
                fmt(row.get("ewt_close"), 4),
            ])
    last_otc = next((row for row in reversed(rows) if row.get("otc_fin") is not None), None)
    last_mkt = next((row for row in reversed(rows) if row.get("mkt_fin") is not None), None)
    payload = {
        "schema": SCHEMA,
        "columns": {
            "date": "YYYY-MM-DD session date",
            "otc_fin": "marginMonitor OTC fin (cost / 成數). Short seed. Bands 145/150 and 205/210 apply only here.",
            "mkt_fin": "FinLab TSE+OTC mark-to-market percent, Σ(融資餘額×收盤價)/融資金額. Long series for 1Y/2Y/5Y/ALL.",
            "taiex_close": "TAIEX / 加權 close (marginMonitor TSE.close on seed dates, Yahoo ^TWII otherwise)",
            "ewt_close": "EWT USD daily close",
        },
        "calibers": {
            "otc_fin": "marginMonitor cost / 成數",
            "mkt_fin": "FinLab / Hermes mark-to-market",
        },
        "index_choice": INDEX_CHOICE,
        "page": "docs/index.html reads the CSV only. This JSON is the same rows plus the column contract.",
        "merge": (
            "python scripts/fetch_otc_fin.py --merge FILE [--replace] [--overwrite-otc-fin]. "
            "Hermes files (tse_fin or source_otc_tse) fill mkt_fin and prices but do not "
            "replace marginMonitor otc_fin unless --overwrite-otc-fin. "
            "Aliases on non-Hermes files: fin, finRatio, taiex, tse_close, ewt."
        ),
        "seed": HISTORY_URL,
        "generated_note": generated,
        "asof": None if last_otc is None else last_otc["date"],
        "otc_fin": None if last_otc is None else json_num(last_otc.get("otc_fin"), 2),
        "mkt_asof": None if last_mkt is None else last_mkt["date"],
        "mkt_fin": None if last_mkt is None else json_num(last_mkt.get("mkt_fin"), 2),
        "row_count": len(rows),
        "otc_fin_count": sum(1 for row in rows if row.get("otc_fin") is not None),
        "mkt_fin_count": sum(1 for row in rows if row.get("mkt_fin") is not None),
        "rows": [
            {
                "date": row["date"],
                "otc_fin": json_num(row.get("otc_fin"), 2),
                "mkt_fin": json_num(row.get("mkt_fin"), 2),
                "taiex_close": json_num(row.get("taiex_close"), 2),
                "ewt_close": json_num(row.get("ewt_close"), 4),
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
        "mkt_fin": 160.0,
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
    assert base["2020-01-02"]["mkt_fin"] == 160.0
    assert base["2026-11-06"]["otc_fin"] == 122.03
    assert base["2026-11-06"]["mkt_fin"] is None
    upsert(base, [{
        "date": "2020-01-02",
        "otc_fin": 175.5,
        "taiex_close": None,
        "ewt_close": 41.2,
    }], True)
    assert base["2020-01-02"]["otc_fin"] == 175.5
    assert base["2020-01-02"]["taiex_close"] == 12000.0
    assert base["2020-01-02"]["mkt_fin"] == 160.0
    assert "2026-11-06" in base
    upsert(base, [{
        "date": "2020-01-02",
        "otc_fin": 1.0,
        "mkt_fin": 1.0,
        "taiex_close": 1.0,
        "ewt_close": 1.0,
    }], False)
    assert base["2020-01-02"]["otc_fin"] == 175.5
    assert base["2020-01-02"]["mkt_fin"] == 160.0
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
        "mkt_fin": None,
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
        "mkt_fin": None,
        "taiex_close": 18000.0,
        "ewt_close": 48.5,
    }
    # Short seed must not drop a longer cached date or clear mkt_fin.
    longer: dict[str, dict] = {}
    upsert(longer, [{
        "date": "2018-01-02",
        "otc_fin": 170.0,
        "mkt_fin": 155.0,
        "taiex_close": 10800.0,
        "ewt_close": 39.0,
    }], True)
    upsert(longer, seeded, overwrite=False)
    upsert(longer, seeded, overwrite=True)
    assert "2018-01-02" in longer and longer["2026-11-06"]["otc_fin"] == 122.03
    assert longer["2018-01-02"]["mkt_fin"] == 155.0
    # Hermes otc_fin (mark-to-market self-calc) must not wipe marginMonitor.
    hermes_row = row_from_mapping({
        "date": "2026-11-06",
        "otc_fin": "144.2367",
        "tse_fin": "152.1394",
        "mkt_fin": "149.98",
        "taiex_close": "39701.64",
        "ewt_close": "89.24",
        "source_otc_tse": "twse_tpex_selfcalc",
    }, hermes=True, overwrite_otc=False)
    assert hermes_row["otc_fin"] is None
    assert hermes_row["mkt_fin"] == 149.98
    assert hermes_row["taiex_close"] == 39701.64
    assert hermes_row["ewt_close"] == 89.24
    allowed = row_from_mapping({
        "date": "2026-11-06",
        "otc_fin": "144.2367",
        "tse_fin": "152.1394",
        "mkt_fin": "149.98",
        "source_otc_tse": "twse_tpex_selfcalc",
    }, hermes=True, overwrite_otc=True)
    assert allowed["otc_fin"] == 144.2367
    protected: dict[str, dict] = {}
    upsert(protected, seeded, overwrite=True)
    upsert(protected, [hermes_row], overwrite=True)
    assert protected["2026-11-06"]["otc_fin"] == 122.03
    assert protected["2026-11-06"]["mkt_fin"] == 149.98
    # Our own v2 cache has mkt_fin and otc_fin, and is not a Hermes dump.
    cache_row = row_from_mapping({
        "date": "2026-11-06",
        "otc_fin": "122.03",
        "mkt_fin": "149.98",
        "taiex_close": "39701.64",
        "ewt_close": "89.24",
    })
    assert cache_row["otc_fin"] == 122.03 and cache_row["mkt_fin"] == 149.98
    hermes_csv = (
        "date,otc_fin,tse_fin,mkt_fin,taiex_close,ewt_close,source_otc_tse\n"
        "2026-11-06,144.2367,152.1394,149.98,39701.64,89.24,twse_tpex_selfcalc\n"
        "2009-01-05,,,149.60,5000.00,16.1000,\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "hermes.csv"
        path.write_text(hermes_csv, encoding="utf-8")
        loaded = load_table(path)
        by_date = {row["date"]: row for row in loaded}
        assert by_date["2026-11-06"]["otc_fin"] is None
        assert by_date["2026-11-06"]["mkt_fin"] == 149.98
        assert by_date["2009-01-05"]["mkt_fin"] == 149.60
        forced = load_table(path, overwrite_otc=True)
        assert {row["date"]: row for row in forced}["2026-11-06"]["otc_fin"] == 144.2367
        cache_csv = (
            "date,otc_fin,mkt_fin,taiex_close,ewt_close\n"
            "2026-11-06,122.03,149.98,39701.64,89.2400\n"
        )
        cache_path = Path(tmp) / "cache.csv"
        cache_path.write_text(cache_csv, encoding="utf-8")
        cached = load_table(cache_path)
        assert cached[0]["otc_fin"] == 122.03
        assert cached[0]["mkt_fin"] == 149.98
    # No-arg daily refresh: seed may update otc_fin and taiex_close only.
    daily: dict[str, dict] = {}
    upsert(daily, [{
        "date": "2009-01-05",
        "mkt_fin": 149.60,
        "taiex_close": 5000.0,
        "ewt_close": 16.1,
    }], True)
    upsert(daily, [{
        "date": "2026-11-06",
        "otc_fin": 122.03,
        "mkt_fin": 149.98,
        "taiex_close": 39701.64,
        "ewt_close": 89.24,
    }], True)
    upsert(daily, [{
        "date": "2026-11-06",
        "otc_fin": 121.50,
        "mkt_fin": 1.0,
        "taiex_close": 39700.0,
        "ewt_close": 1.0,
    }], True, keys=SEED_KEYS)
    assert daily["2026-11-06"]["otc_fin"] == 121.50
    assert daily["2026-11-06"]["taiex_close"] == 39700.0
    assert daily["2026-11-06"]["mkt_fin"] == 149.98
    assert daily["2026-11-06"]["ewt_close"] == 89.24
    assert daily["2009-01-05"]["mkt_fin"] == 149.60
    assert daily["2009-01-05"]["otc_fin"] is None
    print("self-test ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh docs/data/otc_fin_pressure.csv")
    parser.add_argument("--merge", type=Path, help="CSV or JSON history to upsert (or use as the base with --replace)")
    parser.add_argument("--replace", action="store_true", help="Ignore the current cache and start from --merge")
    parser.add_argument("--no-seed", action="store_true", help="Do not read marginMonitor history.js")
    parser.add_argument("--seed-overwrite", action="store_true", help="Let the seed replace cached fin/TAIEX even when --merge is set")
    parser.add_argument(
        "--overwrite-otc-fin",
        action="store_true",
        help="On a Hermes dump (tse_fin or source_otc_tse), copy that file's otc_fin onto the marginMonitor column",
    )
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
        upsert(base, seeded, overwrite=overwrite, keys=SEED_KEYS)
        print(f"  seed rows={len(seeded)} overwrite={overwrite} keys={','.join(SEED_KEYS)} generated={generated}")

    if args.merge:
        incoming = load_table(args.merge, overwrite_otc=args.overwrite_otc_fin)
        if not incoming:
            raise SystemExit(f"no rows in {args.merge}")
        upsert(base, incoming, overwrite=True)
        print(
            f"merged {len(incoming)} rows from {args.merge} "
            f"replace={args.replace} overwrite_otc_fin={args.overwrite_otc_fin}"
        )

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
    last_mkt = next((row for row in reversed(rows) if row.get("mkt_fin") is not None), None)
    otc_n = sum(1 for row in rows if row.get("otc_fin") is not None)
    mkt_n = sum(1 for row in rows if row.get("mkt_fin") is not None)
    print(f"wrote {CSV_PATH} rows={len(rows)} otc_fin={otc_n} mkt_fin={mkt_n} first={rows[0]['date']} last={rows[-1]['date']}")
    if last:
        print(f"otc asof {last['date']} otc_fin {last.get('otc_fin')} taiex {last.get('taiex_close')} ewt {last.get('ewt_close')}")
    if last_mkt:
        print(f"mkt asof {last_mkt['date']} mkt_fin {last_mkt.get('mkt_fin')}")
    print(f"wrote {JSON_PATH}")


if __name__ == "__main__":
    main()
