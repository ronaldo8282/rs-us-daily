#!/usr/bin/env python3
"""Write lastClose onto baked TPO session profiles.

lastClose is the close of the last 30m bar in that session — the same bar
lastBarT already points at. The Pages price-axis tag and the newest-day
trend footer read this field. Rows, POC, VA, and the hist CSV OHLC are
left unchanged.

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


def load_closes(hist: Path, time_col: str, tz_name: str) -> dict[int, float]:
    tz = ZoneInfo(tz_name)
    closes: dict[int, float] = {}
    with hist.open(newline="") as f:
        for row in csv.DictReader(f):
            closes[bar_epoch_ms(row[time_col], tz)] = float(row["close"])
    return closes


def attach_last_close(profile: dict, close: float) -> None:
    """Set lastClose beside lastBarT. Other profile fields stay put."""
    rebuilt: dict = {}
    for key, value in profile.items():
        if key == "lastClose":
            continue
        rebuilt[key] = value
        if key == "lastBarT":
            rebuilt["lastClose"] = close
    if "lastClose" not in rebuilt:
        rebuilt["lastClose"] = close
    profile.clear()
    profile.update(rebuilt)


def bake_file(profiles_path: Path, hist_path: Path, time_col: str, tz_name: str) -> tuple[int, str, float]:
    closes = load_closes(hist_path, time_col, tz_name)
    raw = profiles_path.read_text()
    data = json.loads(raw)
    profiles = data.get("profiles") or []
    missing = []
    for profile in profiles:
        ms = profile.get("lastBarT")
        close = closes.get(ms)
        if close is None:
            missing.append(str(profile.get("sessionId")))
            continue
        attach_last_close(profile, close)
    if missing:
        raise SystemExit(
            f"{profiles_path}: no hist bar for lastBarT on {len(missing)} session(s): "
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
        print(f"{name}: {n} sessions, {sid} lastClose={close} ({spec['profiles'].relative_to(ROOT)})")


if __name__ == "__main__":
    main(sys.argv[1:])
