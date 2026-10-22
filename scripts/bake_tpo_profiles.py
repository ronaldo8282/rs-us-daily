#!/usr/bin/env python3
"""Write session OHLC onto baked TPO profiles.

lastClose is the close of the last 30m bar in that session — the same bar
lastBarT already points at. The Pages price-axis tag and the newest-day
trend footer read this field.

open / high / low feed the letter-wall candle left of each HG column.
The session window is the `barCount` bars ending at lastBarT (inclusive):
that is the TPO session itself. On HSIF the CSV also contains a pre-open
print just before the first 30m bar; barCount stops that print from
becoming the session open. Open is the first bar's open, high/low are the
window extremes, close is the last bar (stored as lastClose).

Rows, POC, and VA are left unchanged. The hist CSV is not rewritten.

Shared by the HSIF live 30m bake and the YM/NQ pages (same engine, different
session clock and CSV timestamp column).

Usage:
  python3 scripts/bake_tpo_profiles.py hsif
  python3 scripts/bake_tpo_profiles.py ym nq
  python3 scripts/bake_tpo_profiles.py --all
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]

# Presets match the static Pages snapshots under docs/.
PRESETS = {
    "hsif": {
        "profiles": ROOT / "docs" / "tpo" / "data" / "profiles.json",
        "hist": ROOT / "docs" / "tpo" / "data" / "HSIF_HKEX_HSI1_30m.csv",
        "time_col": "time_hk",
        "tz": "Asia/Hong_Kong",
    },
    "ym": {
        "profiles": ROOT / "docs" / "tpo-ym" / "data" / "profiles.json",
        "hist": ROOT / "docs" / "tpo-ym" / "data" / "YM_CBOT_MINI_YM1_30m.csv",
        "time_col": "time_ct",
        "tz": "America/Chicago",
    },
    "nq": {
        "profiles": ROOT / "docs" / "tpo-nq" / "data" / "profiles.json",
        "hist": ROOT / "docs" / "tpo-nq" / "data" / "NQ_CME_MINI_NQ1_30m.csv",
        "time_col": "time_ct",
        "tz": "America/Chicago",
    },
}


def bar_epoch_ms(stamp: str, tz: ZoneInfo) -> int:
    """Epoch milliseconds of a naive exchange timestamp in `tz`."""
    t = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    return int(t.timestamp() * 1000)


def load_bars(hist: Path, time_col: str, tz_name: str) -> list[dict]:
    tz = ZoneInfo(tz_name)
    bars: list[dict] = []
    with hist.open(newline="") as f:
        for row in csv.DictReader(f):
            bars.append(
                {
                    "ms": bar_epoch_ms(row[time_col], tz),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
    bars.sort(key=lambda b: b["ms"])
    return bars


def session_ohlc(bars: list[dict], index_by_ms: dict[int, int], profile: dict) -> dict | None:
    """OHLC of the TPO session: `barCount` bars ending at lastBarT."""
    last = profile.get("lastBarT")
    n = profile.get("barCount")
    if not isinstance(n, int) or n < 1 or last not in index_by_ms:
        return None
    end = index_by_ms[last]
    start = end - n + 1
    if start < 0:
        return None
    window = bars[start : end + 1]
    if len(window) != n or window[-1]["ms"] != last:
        return None
    high = max(b["high"] for b in window)
    low = min(b["low"] for b in window)
    open_ = window[0]["open"]
    close = window[-1]["close"]
    if not (low <= open_ <= high and low <= close <= high):
        return None
    return {"open": open_, "high": high, "low": low, "close": close}


def attach_session_ohlc(profile: dict, ohlc: dict) -> None:
    """Set open/high/low/lastClose beside lastBarT. Other fields stay put."""
    rebuilt: dict = {}
    for key, value in profile.items():
        if key in ("open", "high", "low", "lastClose"):
            continue
        rebuilt[key] = value
        if key == "lastBarT":
            rebuilt["open"] = ohlc["open"]
            rebuilt["high"] = ohlc["high"]
            rebuilt["low"] = ohlc["low"]
            rebuilt["lastClose"] = ohlc["close"]
    if "lastClose" not in rebuilt:
        rebuilt["open"] = ohlc["open"]
        rebuilt["high"] = ohlc["high"]
        rebuilt["low"] = ohlc["low"]
        rebuilt["lastClose"] = ohlc["close"]
    profile.clear()
    profile.update(rebuilt)


def bake_file(profiles_path: Path, hist_path: Path, time_col: str, tz_name: str) -> tuple[int, str, float]:
    bars = load_bars(hist_path, time_col, tz_name)
    index_by_ms = {b["ms"]: i for i, b in enumerate(bars)}
    raw = profiles_path.read_text()
    data = json.loads(raw)
    profiles = data.get("profiles") or []
    missing = []
    for profile in profiles:
        ohlc = session_ohlc(bars, index_by_ms, profile)
        if ohlc is None:
            missing.append(str(profile.get("sessionId")))
            continue
        attach_session_ohlc(profile, ohlc)
    if missing:
        raise SystemExit(
            f"{profiles_path}: no session OHLC for {len(missing)} session(s): "
            + ", ".join(missing[:8])
        )
    text = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    if text != raw:
        profiles_path.write_text(text)
    last = profiles[-1]
    return len(profiles), str(last.get("sessionId")), float(last["lastClose"])


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "presets",
        nargs="*",
        choices=sorted(PRESETS),
        help="hsif, ym, and/or nq (default: hsif)",
    )
    parser.add_argument("--all", action="store_true", help="bake HSIF, YM, and NQ")
    args = parser.parse_args(argv)
    names = list(PRESETS) if args.all else (args.presets or ["hsif"])
    for name in names:
        spec = PRESETS[name]
        n, sid, close = bake_file(spec["profiles"], spec["hist"], spec["time_col"], spec["tz"])
        print(
            f"{name}: {n} sessions, {sid} lastClose={close} "
            f"({spec['profiles'].relative_to(ROOT)})"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
