"""Rolling-origin backtests of TimesFM and simple baselines on BTC bars.

Each experiment makes forecasts at many past cut-offs ("origins"), using only
the data before each cut-off, and saves what every model said next to what
actually happened. summarize.py turns the saved runs into scores.

    python backtest.py --source bitstamp --models timesfm25 baselines
    python backtest.py --source bitstamp --models timesfm30 --weights30 DIR --only price_1h

Runs land in ~/tfm_work/<source>/runs/<experiment>/<model>.npz.
"""

from __future__ import annotations

import argparse
import math
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

WORK = Path.home() / "tfm_work"
TEST_START = pd.Timestamp("2024-01-01", tz="UTC")
LEVELS = np.arange(1, 10) / 10  # every model reports the q10..q90 deciles
Z = norm.ppf(LEVELS)

warnings.filterwarnings("ignore", message="Some donated buffers were not usable")


@dataclass(frozen=True)
class Experiment:
    name: str
    target: str     # "close", "volume" or "rv"
    bars: str       # "15m", "1h", "4h" or "1d"
    context: int    # bars of history each forecast sees
    horizon: int    # bars forecast
    every: str      # time between origins
    baselines: tuple[str, ...]


EXPERIMENTS = [
    Experiment("price_15m", "close", "15m", 672, 96, "6h", ("rw",)),
    Experiment("price_1h", "close", "1h", 168, 168, "6h", ("rw", "drift")),
    Experiment("price_4h", "close", "4h", 180, 42, "1D", ("rw",)),
    Experiment("price_1d", "close", "1d", 365, 30, "1D", ("rw",)),
    *[Experiment(f"ctx_1h_{c}", "close", "1h", c, 24, "12h", ("rw",))
      for c in (24, 72, 168, 336, 720, 2160, 4320)],
    Experiment("volume_1h", "volume", "1h", 168, 24, "1D", ("snaive", "smean")),
    Experiment("rv_1d", "rv", "1d", 365, 7, "1D", ("naive", "har", "mean30")),
]


def load_series(source: str, exp: Experiment) -> pd.Series:
    if exp.target == "rv":
        return pd.read_pickle(WORK / source / "rv_1d.pkl")
    return pd.read_pickle(WORK / source / f"bars_{exp.bars}.pkl")[exp.target]


def origins(index: pd.DatetimeIndex, exp: Experiment, end: pd.Timestamp | None) -> np.ndarray:
    """Positions c whose bar opens a forecast: aligned to `every`, inside the test period."""
    every = pd.Timedelta(exp.every)
    aligned = ((index - pd.Timestamp(0, tz="UTC")) % every) == pd.Timedelta(0)
    in_test = index >= TEST_START
    if end is not None:
        in_test &= index <= end
    pos = np.arange(len(index))
    ok = aligned & in_test & (pos >= exp.context) & (pos + exp.horizon <= len(index))
    return pos[ok]


# ---------------------------------------------------------------------------
# Baselines. Each returns quantiles shaped (origins, horizon, 9); point-only
# baselines repeat their point across the nine columns.

def rw(y: np.ndarray, cs: np.ndarray, exp: Experiment, drift: bool = False) -> np.ndarray:
    """Random walk: the price stays put, spread by the context's own volatility."""
    h = np.arange(1, exp.horizon + 1)[:, None]
    out = np.empty((len(cs), exp.horizon, 9), np.float32)
    for i, c in enumerate(cs):
        r = np.diff(np.log(y[c - exp.context:c]))
        mu = r.mean() if drift else 0.0
        out[i] = y[c - 1] * np.exp(mu * h + r.std(ddof=1) * np.sqrt(h) * Z)
    return out


def seasonal(y: np.ndarray, cs: np.ndarray, exp: Experiment, mean: bool) -> np.ndarray:
    """Same hour yesterday, or the same hour averaged over the context's days."""
    season = 24
    out = np.empty((len(cs), exp.horizon, 9), np.float32)
    for i, c in enumerate(cs):
        ctx = y[c - exp.context:c]
        days = ctx[len(ctx) % season:].reshape(-1, season)
        profile = days.mean(axis=0) if mean else days[-1]
        point = np.resize(profile, exp.horizon)
        out[i] = point[:, None]
    return out


def har(y: np.ndarray, cs: np.ndarray, exp: Experiment) -> np.ndarray:
    """HAR-RV (Corsi 2009) on log volatility, refit on all days before each origin."""
    logy = np.log(np.maximum(y, 1e-6))
    s = pd.Series(logy)
    feats = np.column_stack([
        np.ones(len(y)), logy,
        s.rolling(5).mean().to_numpy(), s.rolling(22).mean().to_numpy(),
    ])
    out = np.empty((len(cs), exp.horizon, 9), np.float32)
    for i, c in enumerate(cs):
        for k in range(1, exp.horizon + 1):
            # Feature day t, target day t + k, both before c; the last three years only.
            rows = np.arange(max(22, c - k - 3 * 365), c - k)
            beta, *_ = np.linalg.lstsq(feats[rows], logy[rows + k], rcond=None)
            out[i, k - 1] = np.exp(feats[c - 1] @ beta)
    return out


def baseline(name: str, y: np.ndarray, cs: np.ndarray, exp: Experiment) -> np.ndarray:
    if name == "rw":
        return rw(y, cs, exp)
    if name == "drift":
        return rw(y, cs, exp, drift=True)
    if name in ("snaive", "smean"):
        return seasonal(y, cs, exp, mean=name == "smean")
    if name == "naive":
        return np.repeat(y[cs - 1][:, None, None], exp.horizon, 1).repeat(9, 2).astype(np.float32)
    if name == "mean30":
        pts = np.array([y[c - 30:c].mean() for c in cs], np.float32)
        return np.repeat(pts[:, None, None], exp.horizon, 1).repeat(9, 2)
    if name == "har":
        return har(y, cs, exp)
    raise ValueError(name)


# ---------------------------------------------------------------------------
# TimesFM.

class TimesFM25:
    name = "timesfm25"

    def __init__(self, weights: str, batch: int = 64):
        import timesfm
        self.timesfm, self.batch = timesfm, batch
        self.model = timesfm.TimesFM_2p5_200M_flax.from_pretrained(weights)
        self.compiled = None

    def forecast(self, contexts: list[np.ndarray], horizon: int) -> np.ndarray:
        key = (max(map(len, contexts)), horizon)
        if self.compiled != key:
            self.model.compile(self.timesfm.ForecastConfig(
                max_context=math.ceil(key[0] / 32) * 32,
                max_horizon=math.ceil(horizon / 128) * 128,
                normalize_inputs=True,
                per_core_batch_size=self.batch,
                use_continuous_quantile_head=True,
                force_flip_invariance=True,
                infer_is_positive=True,
                fix_quantile_crossing=True,
            ))
            self.compiled = key
        _, q = self.model.forecast(horizon=horizon, inputs=list(contexts))
        return q[:, :, 1:10].astype(np.float32)


class TimesFM30:
    name = "timesfm30"

    def __init__(self, weights: str, batch: int = 32):
        import torch
        from timesfm3 import TimesFM3Forecaster
        torch.set_num_threads(max(1, torch.get_num_threads()))
        self.model = TimesFM3Forecaster.from_pretrained(
            weights, device="cpu", per_core_batch_size=batch)

    def forecast(self, contexts: list[np.ndarray], horizon: int) -> np.ndarray:
        outs = self.model.predict_batch(
            contexts=list(contexts), horizon=horizon,
            return_quantiles=True, make_positive=True)
        return np.stack([np.asarray(o.quantiles).reshape(horizon, 9) for o in outs]).astype(np.float32)


def run_model(model, y: np.ndarray, cs: np.ndarray, exp: Experiment, chunk: int = 512) -> np.ndarray:
    parts = []
    for start in range(0, len(cs), chunk):
        batch = cs[start:start + chunk]
        parts.append(model.forecast([y[c - exp.context:c] for c in batch], exp.horizon))
        print(f"    {exp.name} {model.name}: {start + len(batch)}/{len(cs)}", flush=True)
    return np.concatenate(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", default="bitstamp")
    ap.add_argument("--models", nargs="+", default=["baselines", "timesfm25"],
                    choices=["baselines", "timesfm25", "timesfm30"])
    ap.add_argument("--only", nargs="*", help="experiment names (default: all)")
    ap.add_argument("--weights25", default=str(Path("/home/user/timesfm-weights/timesfm-2.5-200m-flax")))
    ap.add_argument("--weights30", default="google/timesfm-3.0-pytorch")
    ap.add_argument("--end", help="last origin (UTC date); default: as late as the data allows")
    ap.add_argument("--limit", type=int, help="use only the last N origins (smoke tests)")
    args = ap.parse_args()

    end = pd.Timestamp(args.end, tz="UTC") if args.end else None
    exps = [e for e in EXPERIMENTS if not args.only or e.name in args.only]
    models = []
    for name in args.models:
        t0 = time.time()
        if name == "timesfm25":
            models.append(TimesFM25(args.weights25))
        elif name == "timesfm30":
            models.append(TimesFM30(args.weights30))
        if name != "baselines":
            print(f"loaded {name} in {time.time() - t0:.1f}s")

    for exp in exps:
        series = load_series(args.source, exp)
        y = series.to_numpy(np.float64)
        cs = origins(series.index, exp, end)
        if args.limit:
            cs = cs[-args.limit:]
        out = WORK / args.source / "runs" / exp.name
        out.mkdir(parents=True, exist_ok=True)
        actual = np.stack([y[c:c + exp.horizon] for c in cs]).astype(np.float32)
        np.savez_compressed(out / "truth.npz", origins=series.index[cs].as_unit("ns").asi8, last=y[cs - 1],
                            actual=actual, context=exp.context, horizon=exp.horizon)
        print(f"{exp.name}: {len(cs)} origins, {series.index[cs[0]]} to {series.index[cs[-1]]}")
        if "baselines" in args.models:
            for b in exp.baselines:
                np.savez_compressed(out / f"{b}.npz", q=baseline(b, y, cs, exp))
        for model in models:
            t0 = time.time()
            q = run_model(model, y, cs, exp)
            secs = time.time() - t0
            np.savez_compressed(out / f"{model.name}.npz", q=q, seconds=secs)
            print(f"  {model.name}: {secs:.0f}s ({len(cs) / secs:.1f} forecasts/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
