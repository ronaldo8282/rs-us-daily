"""Cache backfill for Sector/Industry/MarketCap when yfinance .info is blank.

No network. Run from the repo root:

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import overlap_vs_fred  # noqa: E402
import rank_rs  # noqa: E402


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Rank": 1,
                "Ticker": "AMD",
                "Sector": "",
                "Industry": "",
                "Exchange": "NASDAQ",
                "Relative Strength": 10.0,
                "Percentile": 96,
                "Price": 530.14,
                "MarketCap": None,
                "asof_date": "2026-11-18",
            },
            {
                "Rank": 2,
                "Ticker": "LIVE",
                "Sector": "",
                "Industry": "",
                "Exchange": "NYSE",
                "Relative Strength": 9.0,
                "Percentile": 95,
                "Price": 20.0,
                "MarketCap": None,
                "asof_date": "2026-11-18",
            },
            {
                "Rank": 3,
                "Ticker": "AAPL",
                "Sector": "",
                "Industry": "",
                "Exchange": "NASDAQ",
                "Relative Strength": 8.0,
                "Percentile": 87,
                "Price": 352.45,
                "MarketCap": None,
                "asof_date": "2026-11-18",
            },
            {
                "Rank": 4,
                "Ticker": "LOW",
                "Sector": "",
                "Industry": "",
                "Exchange": "NYSE",
                "Relative Strength": 1.0,
                "Percentile": 50,
                "Price": 10.0,
                "MarketCap": None,
                "asof_date": "2026-11-18",
            },
        ]
    )


def _write_cache(path: Path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


def _blank_fetcher(tickers):
    return {t: {"Sector": "", "Industry": "", "MarketCap": None} for t in tickers}


def _partial_fetcher(tickers):
    out = _blank_fetcher(tickers)
    if "LIVE" in out:
        out["LIVE"] = {
            "Sector": "LiveSector",
            "Industry": "LiveInd",
            "MarketCap": 5_000_000_000,
        }
    return out


class TestFundamentalsBackfill(unittest.TestCase):
    def test_cache_fills_blanks_and_does_not_overwrite_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "fundamentals_latest.csv"
            _write_cache(
                cache,
                [
                    {
                        "Ticker": "amd",
                        "Sector": "Technology",
                        "Industry": "Semiconductors",
                        "MarketCap": 833_176_600_576,
                        "updated_at": "2026-10-21",
                    },
                    {
                        "Ticker": "LIVE",
                        "Sector": "OldSector",
                        "Industry": "OldInd",
                        "MarketCap": 111,
                        "updated_at": "2026-10-21",
                    },
                    {
                        "Ticker": "AAPL",
                        "Sector": "Technology",
                        "Industry": "Consumer Electronics",
                        "MarketCap": 3_000_000_000_000,
                        "updated_at": "2026-10-21",
                    },
                    {
                        "Ticker": "LOW",
                        "Sector": "ShouldNot",
                        "Industry": "Appear",
                        "MarketCap": 9_000_000_000,
                        "updated_at": "2026-10-21",
                    },
                ],
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                out = rank_rs.fill_info_for_high_pct(
                    _frame(),
                    90,
                    fundamentals_path=cache,
                    info_fetcher=_partial_fetcher,
                )
            amd = out.loc[out["Ticker"] == "AMD"].iloc[0]
            live = out.loc[out["Ticker"] == "LIVE"].iloc[0]
            aapl = out.loc[out["Ticker"] == "AAPL"].iloc[0]
            low = out.loc[out["Ticker"] == "LOW"].iloc[0]
            self.assertEqual(amd["Sector"], "Technology")
            self.assertEqual(amd["Industry"], "Semiconductors")
            self.assertEqual(amd["MarketCap"], 833_176_600_576)
            self.assertEqual(live["Sector"], "LiveSector")
            self.assertEqual(live["Industry"], "LiveInd")
            self.assertEqual(live["MarketCap"], 5_000_000_000)
            self.assertEqual(aapl["Sector"], "")
            self.assertIsNone(aapl["MarketCap"])
            self.assertEqual(low["Sector"], "")
            self.assertIsNone(low["MarketCap"])
            log = stdout.getvalue()
            self.assertIn(
                "info fill Percentile>=90: attempted=2 live-filled=1 "
                "cache-backfilled=1 still-blank=0 sector-cache=1 industry-cache=1",
                log,
            )
            self.assertNotIn("WARNING: RS≥90", stderr.getvalue())
            self.assertNotIn("WARNING: RS≥90", log)

    def test_missing_cache_warns_when_rs90_mcap_is_blank(self):
        missing = Path(tempfile.gettempdir()) / "does-not-exist-fundamentals.csv"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            out = rank_rs.fill_info_for_high_pct(
                _frame(),
                90,
                fundamentals_path=missing,
                info_fetcher=_blank_fetcher,
            )
        rs90 = out[out["Percentile"] >= 90]
        self.assertTrue(all(v is None for v in rs90["MarketCap"]))
        log = stdout.getvalue()
        self.assertIn("attempted=2 live-filled=0 cache-backfilled=0 still-blank=2", log)
        self.assertIn(f"cache=missing:{missing}", log)
        self.assertIn("WARNING: RS≥90 MarketCap fill rate is 0/2 (0.0%)", stderr.getvalue())
        self.assertIn("::warning::", log)
        self.assertIn("Daily ###RS≥90 stays on Fred's CSV", log)

    def test_nonpositive_cache_mcap_is_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "fundamentals_latest.csv"
            _write_cache(
                cache,
                [
                    {"Ticker": "AMD", "Sector": "Technology", "Industry": "Semiconductors", "MarketCap": 0},
                    {"Ticker": "LIVE", "Sector": "", "Industry": "", "MarketCap": -5},
                    {"Ticker": "DUP", "Sector": "", "Industry": "", "MarketCap": ""},
                    {"Ticker": "DUP", "Sector": "Tech", "Industry": "Soft", "MarketCap": "4,000,000,000"},
                ],
            )
            frame = _frame()
            frame = pd.concat(
                [
                    frame,
                    pd.DataFrame(
                        [
                            {
                                "Rank": 5,
                                "Ticker": "DUP",
                                "Sector": None,
                                "Industry": float("nan"),
                                "Exchange": "NASDAQ",
                                "Relative Strength": 7.0,
                                "Percentile": 99,
                                "Price": 4.0,
                                "MarketCap": float("nan"),
                                "asof_date": "2026-11-18",
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                out = rank_rs.fill_info_for_high_pct(
                    frame,
                    90,
                    fundamentals_path=cache,
                    info_fetcher=_blank_fetcher,
                )
            amd = out.loc[out["Ticker"] == "AMD"].iloc[0]
            live = out.loc[out["Ticker"] == "LIVE"].iloc[0]
            dup = out.loc[out["Ticker"] == "DUP"].iloc[0]
            self.assertEqual(amd["Sector"], "Technology")
            self.assertIsNone(amd["MarketCap"])
            self.assertIsNone(live["MarketCap"])
            self.assertEqual(dup["Sector"], "Tech")
            self.assertEqual(dup["Industry"], "Soft")
            self.assertEqual(dup["MarketCap"], 4_000_000_000)
            self.assertIn("cache-backfilled=1", stdout.getvalue())
            self.assertIn("still-blank=2", stdout.getvalue())

    def test_fetch_info_batch_swallows_crumb_errors(self):
        class Boom:
            def __init__(self, ticker):
                self.ticker = ticker

            @property
            def info(self):
                raise RuntimeError("HTTP Error 401: Invalid Crumb")

        original = rank_rs.yf.Ticker
        rank_rs.yf.Ticker = Boom
        try:
            got = rank_rs.fetch_info_batch(["AMD"])
        finally:
            rank_rs.yf.Ticker = original
        self.assertEqual(got["AMD"], {"Sector": "", "Industry": "", "MarketCap": None})

    def test_nan_sector_column_from_csv_accepts_cache_strings(self):
        raw = _frame()
        raw["Sector"] = float("nan")
        raw["Industry"] = float("nan")
        raw["MarketCap"] = float("nan")
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "fundamentals_latest.csv"
            _write_cache(
                cache,
                [
                    {
                        "Ticker": "AMD",
                        "Sector": "Technology",
                        "Industry": "Semiconductors",
                        "MarketCap": 833_176_600_576,
                    }
                ],
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                out = rank_rs.fill_info_for_high_pct(
                    raw,
                    90,
                    fundamentals_path=cache,
                    info_fetcher=_blank_fetcher,
                )
            amd = out.loc[out["Ticker"] == "AMD"].iloc[0]
            self.assertEqual(amd["Sector"], "Technology")
            self.assertEqual(amd["MarketCap"], 833_176_600_576)
            low = out.loc[out["Ticker"] == "LOW"].iloc[0]
            self.assertTrue(rank_rs._blank_text(low["Sector"]))
            self.assertIsNone(rank_rs._positive_mcap(low["MarketCap"]))


class TestOverlapBlankMcapNote(unittest.TestCase):
    def test_jaccard_zero_from_blank_mcap_is_labeled(self):
        ours_raw = pd.DataFrame(
            [
                {"Ticker": "AMD", "Percentile": 96, "MarketCap": "", "Relative Strength": 1, "Exchange": "NASDAQ"},
                {"Ticker": "NVDA", "Percentile": 99, "MarketCap": None, "Relative Strength": 1, "Exchange": "NASDAQ"},
                {"Ticker": "AAPL", "Percentile": 87, "MarketCap": 1, "Relative Strength": 1, "Exchange": "NASDAQ"},
            ]
        )
        fred_raw = pd.DataFrame(
            [
                {"Ticker": "AMD", "Percentile": 96, "MarketCap": 8e11, "Relative Strength": 1, "Exchange": "NASDAQ"},
            ]
        )
        text, note = overlap_vs_fred.render_overlap_report(
            today="20261119",
            ours_label="output/rs_stocks.csv",
            fred_label=overlap_vs_fred.FRED_URL,
            ours_raw_n=len(ours_raw),
            fred_raw_n=len(fred_raw),
            ours=overlap_vs_fred.filter_rs90(ours_raw),
            fred=overlap_vs_fred.filter_rs90(fred_raw),
            ours_pct=overlap_vs_fred.filter_rs90_pct_only(ours_raw),
            fred_pct=overlap_vs_fred.filter_rs90_pct_only(fred_raw),
        )
        self.assertIsNotNone(note)
        self.assertIn("## Data issue", text)
        self.assertIn("blank on 2/2 names", text)
        self.assertIn("Filter: Percentile>=90 and MarketCap>3000000000", text)
        self.assertIn("Jaccard = 0.000", text)
        self.assertIn("Daily ###RS≥90 stays on Fred's public CSV", text)
        self.assertEqual(len(overlap_vs_fred.filter_rs90(ours_raw)), 0)
        self.assertEqual(len(overlap_vs_fred.filter_rs90_pct_only(ours_raw)), 2)

    def test_no_note_when_sets_differ_but_mcap_is_present(self):
        ours_raw = pd.DataFrame(
            [{"Ticker": "AMD", "Percentile": 96, "MarketCap": 8e11, "Relative Strength": 1, "Exchange": "NASDAQ"}]
        )
        fred_raw = pd.DataFrame(
            [{"Ticker": "NVDA", "Percentile": 99, "MarketCap": 4e12, "Relative Strength": 1, "Exchange": "NASDAQ"}]
        )
        text, note = overlap_vs_fred.render_overlap_report(
            today="20261119",
            ours_label="ours.csv",
            fred_label="fred.csv",
            ours_raw_n=1,
            fred_raw_n=1,
            ours=overlap_vs_fred.filter_rs90(ours_raw),
            fred=overlap_vs_fred.filter_rs90(fred_raw),
            ours_pct=overlap_vs_fred.filter_rs90_pct_only(ours_raw),
            fred_pct=overlap_vs_fred.filter_rs90_pct_only(fred_raw),
        )
        self.assertIsNone(note)
        self.assertNotIn("Data issue", text)
        self.assertIn("Jaccard = 0.000", text)
        self.assertIn("| Ours RS≥90 & >3B | 1 |", text)
        self.assertIn("| Overlap | 0 |", text)

    def test_filter_still_requires_percentile_and_mcap(self):
        df = pd.DataFrame(
            [
                {"Ticker": "BIG", "Percentile": 95, "MarketCap": 4_000_000_000, "Relative Strength": 1, "Exchange": "NYSE"},
                {"Ticker": "SMALL", "Percentile": 95, "MarketCap": 1_000_000_000, "Relative Strength": 1, "Exchange": "NYSE"},
                {"Ticker": "BLANK", "Percentile": 99, "MarketCap": "", "Relative Strength": 1, "Exchange": "NYSE"},
                {"Ticker": "LOW", "Percentile": 80, "MarketCap": 9_000_000_000, "Relative Strength": 1, "Exchange": "NYSE"},
            ]
        )
        got = overlap_vs_fred.filter_rs90(df)
        self.assertEqual(got["Ticker"].tolist(), ["BIG"])
        self.assertEqual(overlap_vs_fred.PCT_MIN, 90.0)
        self.assertEqual(overlap_vs_fred.MCAP_MIN, 3_000_000_000.0)
        self.assertEqual(overlap_vs_fred.FRED_URL, "https://raw.githubusercontent.com/Fred6725/rs-log/main/output/rs_stocks.csv")


if __name__ == "__main__":
    unittest.main()
