"""Forecast BTC/USDT with Google's TimesFM 2.5 and check it against what happened.

Each run does two things with the same one-week context length:

1. Backtest. At several recent cut-off points it gives the model the week
   before the cut-off, asks for the next N bars, and compares the answer with
   the bars that actually came. A "last price stays put" forecast is scored
   alongside it, since that is the bar any price forecast has to clear.
2. Forecast. It gives the model the latest week and asks for the next N bars.

    python btc_forecast.py                                  # Binance BTCUSDT, 1h bars
    python btc_forecast.py --interval 15m --horizon 48
    python btc_forecast.py --csv prices.csv                 # any timestamp,close CSV
    python btc_forecast.py --weights path/to/timesfm-2.5-200m-flax
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")

# Binance interval -> pandas resample rule.
INTERVALS = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1D",
}

DEFAULT_WEIGHTS = {
    "flax": "google/timesfm-2.5-200m-flax",
    "torch": "google/timesfm-2.5-200m-pytorch",
}

# TimesFM 2.5 reads input in 32-bar patches and writes output in 128-bar ones.
INPUT_PATCH, OUTPUT_PATCH = 32, 128
# Column 0 of the quantile output is the mean; columns 1..9 are q10..q90.
Q10, Q50, Q90 = 1, 5, 9
# Backtest windows drawn on the chart and listed one per row; the rest are
# still scored and written to backtest.csv.
SHOWN_WINDOWS = 7

# JAX can't reuse donated buffers on CPU and says so on every compile.
warnings.filterwarnings("ignore", message="Some donated buffers were not usable")


def span(td: pd.Timedelta) -> str:
    """24h, 7d, 15m: a Timedelta the way a trader would write it."""
    minutes = int(td.total_seconds() // 60)
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def fetch_binance(symbol: str, interval: str, bars: int) -> pd.Series:
    """Closed candles for `symbol`, oldest first, the last `bars` of them."""
    step_ms = int(pd.Timedelta(INTERVALS[interval]).total_seconds() * 1000)
    now_ms = int(time.time() * 1000)
    # One spare bar, because the newest candle is still open and gets dropped.
    start_ms = now_ms - (bars + 1) * step_ms
    last_error = None
    for host in BINANCE_HOSTS:
        rows: list[list] = []
        cursor = start_ms
        try:
            while cursor < now_ms:
                query = urllib.parse.urlencode({
                    "symbol": symbol, "interval": interval,
                    "startTime": cursor, "limit": 1000,
                })
                with urllib.request.urlopen(f"{host}/api/v3/klines?{query}",
                                            timeout=20) as resp:
                    page = json.load(resp)
                if not page:
                    break
                rows.extend(page)
                cursor = page[-1][0] + step_ms
        except OSError as e:
            last_error = e
            continue
        # kline row: [open_time, open, high, low, close, volume, close_time, ...]
        rows = [r for r in rows if r[6] < now_ms]
        if not rows:
            raise SystemExit(f"Binance returned no closed {interval} candles for {symbol}")
        closes = pd.Series(
            [float(r[4]) for r in rows],
            index=pd.to_datetime([r[0] for r in rows], unit="ms", utc=True),
            name="close",
        )
        return closes[~closes.index.duplicated()].iloc[-bars:]
    raise SystemExit(f"Could not reach Binance ({', '.join(BINANCE_HOSTS)}): {last_error}\n"
                     "Download a CSV some other way and pass it with --csv.")


def _to_utc(stamps: pd.Series) -> pd.DatetimeIndex:
    """Parse epoch seconds/ms/us or date strings into a UTC index."""
    if pd.api.types.is_numeric_dtype(stamps):
        biggest = stamps.abs().max()
        unit = "us" if biggest > 1e14 else "ms" if biggest > 1e11 else "s"
        return pd.DatetimeIndex(pd.to_datetime(stamps, unit=unit, utc=True))
    return pd.DatetimeIndex(pd.to_datetime(stamps, utc=True))


def load_csv(path: Path, interval: str) -> pd.Series:
    """Close prices from a CSV, resampled to `interval` bars.

    Takes either a CSV with a header naming a time column (timestamp,
    open_time, time, date or datetime) and a close column, or a headerless
    Binance kline dump from data.binance.vision.
    """
    head = pd.read_csv(path, nrows=1, header=None).iloc[0].astype(str).str.lower().tolist()
    time_cols = ("timestamp", "open_time", "time", "date", "datetime")
    if "close" in head:
        df = pd.read_csv(path)
        df.columns = [c.lower() for c in df.columns]
        time_col = next((c for c in time_cols if c in df.columns), None)
        if time_col is None:
            raise SystemExit(f"{path}: no time column (expected one of {', '.join(time_cols)})")
        stamps, closes = df[time_col], df["close"]
    else:
        df = pd.read_csv(path, header=None)
        stamps, closes = df[0], df[4]

    raw = pd.Series(closes.astype(float).to_numpy(), index=_to_utc(stamps)).sort_index()
    raw = raw[~raw.index.duplicated(keep="last")]
    rule = INTERVALS[interval]
    bars = raw.resample(rule, label="left", closed="left").last().ffill()

    # Drop the newest bar if the file stops partway through it: its "close"
    # would be whatever price the file ended on, not the bar's close.
    raw_step = raw.index.to_series().diff().median()
    if bars.index[-1] + pd.Timedelta(rule) > raw.index[-1] + raw_step:
        bars = bars.iloc[:-1]
    return bars.rename("close")


def load_model(backend: str, weights: str, context: int, horizon: int, batch: int):
    import timesfm

    cls = {"flax": "TimesFM_2p5_200M_flax", "torch": "TimesFM_2p5_200M_torch"}[backend]
    if not hasattr(timesfm, cls):
        raise SystemExit(f"timesfm.{cls} is not importable; install the {backend} extras.")
    model = getattr(timesfm, cls).from_pretrained(weights)
    model.compile(timesfm.ForecastConfig(
        max_context=math.ceil(context / INPUT_PATCH) * INPUT_PATCH,
        max_horizon=math.ceil(horizon / OUTPUT_PATCH) * OUTPUT_PATCH,
        normalize_inputs=True,
        per_core_batch_size=batch,
        use_continuous_quantile_head=True,
        force_flip_invariance=True,
        infer_is_positive=True,
        fix_quantile_crossing=True,
    ))
    return model


def score(context: np.ndarray, actual: np.ndarray, quantiles: np.ndarray) -> dict:
    last = context[-1]
    median = quantiles[:, Q50]
    return {
        "mae": np.mean(np.abs(median - actual)),
        "mape": np.mean(np.abs(median - actual) / actual) * 100,
        "naive_mae": np.mean(np.abs(last - actual)),
        "direction_hit": np.sign(median[-1] - last) == np.sign(actual[-1] - last),
        "coverage80": np.mean((quantiles[:, Q10] <= actual) & (actual <= quantiles[:, Q90])),
        "move_pred": median[-1] - last,
        "move_true": actual[-1] - last,
    }


def plot(path: Path, series: pd.Series, context: int, windows: list[dict],
         future_index: pd.DatetimeIndex, future_q: np.ndarray, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    surface, ink, muted, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    actual_c, forecast_c = "#2a78d6", "#eb6834"

    fig, axes = plt.subplots(2, 1, figsize=(12, 8.5), facecolor=surface)
    for ax in axes:
        ax.set_facecolor(surface)
        ax.grid(True, color=grid, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(grid)
        ax.tick_params(colors=muted, labelsize=9)
        ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))

    ax = axes[0]
    recent = series.iloc[-context:]
    ax.plot(recent.index, recent.values, color=actual_c, linewidth=1.6, label="Actual close")
    ax.fill_between(future_index, future_q[:, Q10], future_q[:, Q90], color=forecast_c,
                    alpha=0.18, linewidth=0, label="80% range (q10 to q90)")
    ax.plot(future_index, future_q[:, Q50], color=forecast_c, linewidth=1.6,
            label="Forecast (median)")
    ax.axvline(recent.index[-1], color=muted, linewidth=0.8, linestyle=":")
    ax.annotate(f"${future_q[-1, Q50]:,.0f}", (future_index[-1], future_q[-1, Q50]),
                xytext=(6, 0), textcoords="offset points", va="center",
                fontsize=9, color=ink)
    ax.set_title(f"Next {len(future_index)} bars, forecast from the {context} bars before",
                 loc="left", fontsize=11, color=ink)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=ink)

    ax = axes[1]
    windows = windows[-SHOWN_WINDOWS:]
    start = windows[0]["index"][0] - (series.index[1] - series.index[0]) * 24
    shown = series[start:windows[-1]["index"][-1]]
    ax.plot(shown.index, shown.values, color=actual_c, linewidth=1.6, label="Actual close")
    for i, w in enumerate(windows):
        ax.fill_between(w["index"], w["q"][:, Q10], w["q"][:, Q90], color=forecast_c,
                        alpha=0.18, linewidth=0,
                        label="80% range (q10 to q90)" if i == 0 else None)
        ax.plot(w["index"], w["q"][:, Q50], color=forecast_c, linewidth=1.6,
                label="Forecast made at each cut-off" if i == 0 else None)
        ax.axvline(w["index"][0], color=muted, linewidth=0.6, linestyle=":")
    ax.set_title(f"Backtest: the last {len(windows)} forecasts vs what happened "
                 "(dotted lines are the cut-offs)", loc="left", fontsize=11, color=ink)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=ink)

    fig.suptitle(title, x=0.01, ha="left", fontsize=13, color=ink)
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=surface)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1h", choices=INTERVALS)
    ap.add_argument("--csv", type=Path,
                    help="read prices from this CSV instead of the Binance API")
    ap.add_argument("--context-days", type=float, default=7,
                    help="how much history the model sees (default: 7 days)")
    ap.add_argument("--horizon", type=int, default=24,
                    help="bars to forecast (default: 24)")
    ap.add_argument("--backtests", type=int, default=7,
                    help="number of backtest cut-offs (default: 7)")
    ap.add_argument("--step", type=int, default=None,
                    help="bars between cut-offs (default: the horizon)")
    ap.add_argument("--backend", default="flax", choices=DEFAULT_WEIGHTS)
    ap.add_argument("--weights", default=None,
                    help="Hugging Face repo id or local checkpoint directory")
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "results")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    bar = pd.Timedelta(INTERVALS[args.interval])
    context = int(pd.Timedelta(days=args.context_days) / bar)
    horizon, step = args.horizon, args.step or args.horizon
    needed = context + horizon + (args.backtests - 1) * step

    if args.csv:
        series = load_csv(args.csv, args.interval)
        source = f"{args.csv.name}, {args.interval} bars"
    else:
        series = fetch_binance(args.symbol, args.interval, needed)
        source = f"Binance {args.symbol}, {args.interval} bars"
    if len(series) < context + horizon:
        raise SystemExit(f"Need at least {context + horizon} bars, have {len(series)}.")
    series = series.iloc[-needed:]
    print(f"Data: {source}, {len(series)} bars, "
          f"{series.index[0]:%Y-%m-%d %H:%M} to {series.index[-1]:%Y-%m-%d %H:%M} UTC")

    values = series.to_numpy(dtype=np.float32)
    n = len(values)
    cutoffs = sorted(n - horizon - k * step for k in range(args.backtests)
                     if n - horizon - k * step >= context)
    inputs = [values[c - context:c] for c in cutoffs] + [values[-context:]]

    weights = args.weights or DEFAULT_WEIGHTS[args.backend]
    print(f"Model: TimesFM 2.5 200M ({args.backend}) from {weights}")
    t0 = time.time()
    model = load_model(args.backend, weights, context, horizon, batch=min(len(inputs), 64))
    t1 = time.time()
    _, quantiles = model.forecast(horizon=horizon, inputs=list(inputs))
    t2 = time.time()
    print(f"Loaded + compiled in {t1 - t0:.1f}s, forecast {len(inputs)} series in {t2 - t1:.1f}s")

    args.out.mkdir(parents=True, exist_ok=True)
    windows, rows, frames = [], [], []
    for c, q in zip(cutoffs, quantiles[:-1]):
        actual = values[c:c + horizon]
        idx = series.index[c:c + horizon]
        windows.append({"index": idx, "q": q, "actual": actual, "last": values[c - 1]})
        rows.append(score(values[c - context:c], actual, q))
        frames.append(pd.DataFrame({
            "forecast_made": idx[0], "timestamp": idx, "actual": actual,
            "q10": q[:, Q10], "median": q[:, Q50], "q90": q[:, Q90],
        }))
    pd.concat(frames).to_csv(args.out / "backtest.csv", index=False)

    print(f"\nBacktest: {len(rows)} forecasts, each from {context} bars "
          f"({span(context * bar)}) of history, {horizon} bars ({span(horizon * bar)}) ahead")
    if len(rows) > SHOWN_WINDOWS:
        print(f"  (the last {SHOWN_WINDOWS} are listed; all of them are in backtest.csv)")
    print(f"  {'made at (UTC)':<17} {'last':>9} {'pred move':>10} {'real move':>10} "
          f"{'dir':>4} {'MAE':>8} {'naive MAE':>10} {'MAPE':>6} {'in 80%':>7}")
    for w, s in list(zip(windows, rows))[-SHOWN_WINDOWS:]:
        print(f"  {w['index'][0]:%Y-%m-%d %H:%M} {w['last']:>9,.0f} "
              f"{s['move_pred']:>+10,.0f} {s['move_true']:>+10,.0f} "
              f"{'yes' if s['direction_hit'] else 'no':>4} {s['mae']:>8,.0f} "
              f"{s['naive_mae']:>10,.0f} {s['mape']:>5.2f}% {s['coverage80']:>6.0%}")

    mae = np.mean([r["mae"] for r in rows])
    naive = np.mean([r["naive_mae"] for r in rows])
    hits = sum(bool(r["direction_hit"]) for r in rows)
    print(f"\n  TimesFM MAE          ${mae:,.0f}   (MAPE {np.mean([r['mape'] for r in rows]):.2f}%)")
    print(f"  'No change' MAE      ${naive:,.0f}")
    print(f"  Skill vs 'no change' {1 - mae / naive:+.1%}   (positive = TimesFM was closer)")
    print(f"  Direction at horizon {hits}/{len(rows)} right ({hits / len(rows):.0%})")
    print(f"  Inside 80% range     {np.mean([r['coverage80'] for r in rows]):.0%} of bars "
          "(well calibrated would be ~80%)")

    # Error by how far ahead the bar is: forecasts usually beat "no change"
    # early or not at all.
    err = np.array([np.abs(w["q"][:, Q50] - w["actual"]) for w in windows])
    naive_err = np.array([np.abs(w["last"] - w["actual"]) for w in windows])
    print(f"\n  {'bars ahead':<12} {'TimesFM MAE':>12} {'no-change MAE':>14} {'skill':>7}")
    for k in sorted({1, 3, 6, 12, horizon} & set(range(1, horizon + 1))):
        m, b = err[:, k - 1].mean(), naive_err[:, k - 1].mean()
        print(f"  {f'{k} ({span(k * bar)})':<12} {m:>12,.0f} {b:>14,.0f} {1 - m / b:>+7.1%}")

    future_index = pd.date_range(series.index[-1] + bar, periods=horizon, freq=bar)
    future_q = quantiles[-1]
    pd.DataFrame({
        "timestamp": future_index, "q10": future_q[:, Q10],
        "median": future_q[:, Q50], "q90": future_q[:, Q90],
    }).to_csv(args.out / "forecast.csv", index=False)
    print(f"\nForecast made at {future_index[0]:%Y-%m-%d %H:%M} UTC "
          f"(last close ${values[-1]:,.0f}):")
    for i in sorted({min(k, horizon) - 1 for k in (1, 6, 12, horizon // 2, horizon)}):
        print(f"  {future_index[i]:%Y-%m-%d %H:%M}  median ${future_q[i, Q50]:>9,.0f}   "
              f"80% range ${future_q[i, Q10]:,.0f} to ${future_q[i, Q90]:,.0f}")

    if not args.no_plot:
        png = args.out / "btc_forecast.png"
        plot(png, series, context, windows, future_index, future_q,
             f"TimesFM 2.5 on {source}")
        print(f"\nChart: {png}")
    print(f"CSVs:  {args.out / 'forecast.csv'}, {args.out / 'backtest.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
