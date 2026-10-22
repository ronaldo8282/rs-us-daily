#!/usr/bin/env python3
"""Pack docs/tpo/app.js into the YM and NQ Pages and cache-bust all three.

HSIF app.js is the source of truth. YM/NQ copies keep their own default
symbol and do not force the Hong Kong timezone on a TV payload.

This does not copy index.html. Titles and symbol inputs stay in each page.
Only the app.js script query (?v=) is rewritten.

Usage:
  python3 scripts/pack_us_pages.py week-letters-1
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "tpo" / "app.js"

TZ_HSIF = (
    'if (payload.source === "futu" || payload.source === "tv" || '
    '(payload.symbol || "").includes("HSI")) {'
)
TZ_US = 'if ((payload.symbol || "").includes("HSI")) {'
SYM_RE = re.compile(r'symbol: \(\$\("symbol"\)\.value \|\| "([^"]+)"\)\.trim\(\),')
SCRIPT_RE = re.compile(r"app\.js\?v=[^\"']+")

PAGES = ("tpo", "tpo-ym", "tpo-nq")
US_SYMBOL = {"tpo-ym": "YM1!", "tpo-nq": "NQ1!"}


def pack_app(src: str, symbol: str) -> str:
    text, n = SYM_RE.subn(
        f'symbol: ($("symbol").value || "{symbol}").trim(),',
        src,
        count=1,
    )
    if n != 1:
        raise SystemExit(f"default symbol replace failed for {symbol}")
    if TZ_HSIF not in text:
        raise SystemExit("HSIF timezone guard missing from app.js; packer out of date")
    text = text.replace(TZ_HSIF, TZ_US, 1)
    if text.count(TZ_US) < 1:
        raise SystemExit("US timezone guard was not written")
    return text


def cache_bust(html_path: Path, version: str) -> None:
    raw = html_path.read_text()
    raw2, n = SCRIPT_RE.subn(f"app.js?v={version}", raw, count=1)
    if n != 1:
        raise SystemExit(f"cache-bust failed: {html_path}")
    html_path.write_text(raw2)


def main() -> None:
    version = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not version or not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise SystemExit("usage: pack_us_pages.py <cache-bust-token>")
    src = SRC.read_text()
    if '|| "HSIF")' not in src:
        raise SystemExit("docs/tpo/app.js must keep the HSIF default symbol")
    for page, symbol in US_SYMBOL.items():
        out = ROOT / "docs" / page / "app.js"
        out.write_text(pack_app(src, symbol))
        print(f"wrote {out} symbol={symbol}")
    for page in PAGES:
        html = ROOT / "docs" / page / "index.html"
        cache_bust(html, version)
        print(f"cache-bust {html} ?v={version}")


if __name__ == "__main__":
    main()
