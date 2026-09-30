"""Load BTC minute prices and turn them into the bars the backtests use.

Two sources give the same frame (UTC minute index; open, high, low, close,
volume):

- Binance BTCUSDT 1-minute klines from data.binance.vision (monthly zips).
- Bitstamp BTC/USD 1-minute candles from github.com/ff137/bitstamp-btcusd-minute-data,
  used when Binance can't be reached.

    python data.py --source bitstamp --repo ~/ff137/bitstamp-btcusd-minute-data
    python data.py --source binance --start 2023-01
"""

from __future__ import annotations

import argparse
import io
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

WORK = Path.home() / "tfm_work"
BAR_RULES = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}
BINANCE_ZIP = "https://data.binance.vision/data/spot/{period}/klines/BTCUSDT/1m/BTCUSDT-1m-{stamp}.zip"


def load_bitstamp(repo: Path) -> pd.DataFrame:
    parts = [
        pd.read_csv(repo / "data/historical/btcusd_bitstamp_1min_2012-2025.csv.gz"),
        pd.read_csv(repo / "data/updates/btcusd_bitstamp_1min_latest.csv"),
    ]
    df = pd.concat(parts, ignore_index=True)
    df.index = pd.to_datetime(df.pop("timestamp"), unit="s", utc=True)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _binance_month(url: str) -> pd.DataFrame:
    with urllib.request.urlopen(url, timeout=60) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        raw = pd.read_csv(zf.open(zf.namelist()[0]), header=None)
    # Files from 2025 on stamp open times in microseconds, older ones in ms.
    unit = "us" if raw[0].max() > 1e14 else "ms"
    df = raw[[1, 2, 3, 4, 5]].astype(float)
    df.columns = ["open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(raw[0], unit=unit, utc=True)
    return df


def load_binance(start: str) -> pd.DataFrame:
    """Monthly zips from `start` (YYYY-MM), then daily zips for the current month."""
    today = pd.Timestamp.now(tz="UTC").normalize()
    frames = []
    for month in pd.period_range(start, today.to_period("M") - 1, freq="M"):
        url = BINANCE_ZIP.format(period="monthly", stamp=month.strftime("%Y-%m"))
        frames.append(_binance_month(url))
    for day in pd.date_range(today.replace(day=1), today - pd.Timedelta(days=1), freq="D"):
        try:
            url = BINANCE_ZIP.format(period="daily", stamp=day.strftime("%Y-%m-%d"))
            frames.append(_binance_month(url))
        except OSError:
            break  # the newest day is published a few hours after midnight
    df = pd.concat(frames)
    return df[~df.index.duplicated(keep="last")].sort_index()


def make_bars(minutes: pd.DataFrame, rule: str) -> pd.DataFrame:
    """OHLCV bars labelled by open time; a partial last bar is dropped."""
    agg = minutes.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    agg["close"] = agg["close"].ffill()
    for col in ("open", "high", "low"):
        agg[col] = agg[col].fillna(agg["close"])
    last_minute = minutes.index[-1]
    if agg.index[-1] + pd.Timedelta(rule) > last_minute + pd.Timedelta("1min"):
        agg = agg.iloc[:-1]
    return agg


def realized_vol(minutes: pd.DataFrame) -> pd.Series:
    """Daily realized volatility in %: sqrt of summed squared 5-minute log returns."""
    close5 = minutes["close"].resample("5min", label="left", closed="left").last().ffill()
    r = np.log(close5).diff().dropna()
    rv = np.sqrt((r ** 2).resample("1D").sum()) * 100
    days = rv.index
    complete = days + pd.Timedelta("1D") <= minutes.index[-1] + pd.Timedelta("1min")
    return rv[complete].rename("rv")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=("bitstamp", "binance"), required=True)
    ap.add_argument("--repo", type=Path, default=Path.home() / "ff137/bitstamp-btcusd-minute-data")
    ap.add_argument("--start", default="2019-01", help="first Binance month (YYYY-MM)")
    args = ap.parse_args()

    minutes = load_bitstamp(args.repo) if args.source == "bitstamp" else load_binance(args.start)
    out = WORK / args.source
    out.mkdir(parents=True, exist_ok=True)
    print(f"{args.source}: {len(minutes):,} minutes, {minutes.index[0]} to {minutes.index[-1]}")
    for name, rule in BAR_RULES.items():
        bars = make_bars(minutes, rule)
        bars.to_pickle(out / f"bars_{name}.pkl")
        print(f"  {name}: {len(bars):,} bars, last {bars.index[-1]}")
    rv = realized_vol(minutes)
    rv.to_pickle(out / "rv_1d.pkl")
    print(f"  realized vol: {len(rv):,} days, last {rv.index[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
