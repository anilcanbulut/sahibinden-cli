"""Score the saved backtest runs and write the tables behind the report's charts.

    python summarize.py --source bitstamp

Every score comes with a 95% range from a moving-block bootstrap over the
forecast origins, so neighbouring (overlapping) forecasts don't count as
independent evidence.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import EXPERIMENTS, LEVELS, WORK

OUT = Path(__file__).resolve().parents[1] / "results" / "analysis"
EXP = {e.name: e for e in EXPERIMENTS}
PRICE_LEADS = {"price_15m": [1, 4, 16, 96], "price_1h": [1, 6, 24, 72, 168],
               "price_4h": [1, 6, 42], "price_1d": [1, 7, 30]}
TFM = ("timesfm25", "timesfm30")
FEE = 0.0005  # per side, per unit of position change (Binance taker fees are 0.05-0.10%)


def lead_label(td: pd.Timedelta) -> str:
    hours = td / pd.Timedelta("1h")
    if hours < 1:
        return f"{round(hours * 60)}m"
    return f"{hours / 24:g}d" if hours >= 24 else f"{hours:g}h"


def load(source: str, name: str):
    run = WORK / source / "runs" / name
    truth = dict(np.load(run / "truth.npz"))
    models = {p.stem: np.load(p)["q"].astype(np.float64)
              for p in sorted(run.glob("*.npz")) if p.stem != "truth"}
    seconds = {p.stem: float(np.load(p)["seconds"])
               for p in sorted(run.glob("timesfm*.npz"))}
    return truth, models, seconds


def boot(stat, n: int, block: int, reps: int = 1000, seed: int = 0) -> tuple[float, float]:
    """95% range of stat(idx) over moving-block resamples of 0..n-1."""
    rng = np.random.default_rng(seed)
    block = max(1, min(block, n))
    k = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(reps, k))
    idx = (starts[:, :, None] + np.arange(block)).reshape(reps, -1)[:, :n]
    vals = np.array([stat(i) for i in idx])
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def block_len(exp, lead: int) -> int:
    bar = pd.Timedelta(exp.bars.replace("m", "min").replace("d", "D"))
    return 2 * max(1, math.ceil(lead * bar / pd.Timedelta(exp.every)))


def skill(err: np.ndarray, ref: np.ndarray, block: int) -> tuple[float, float, float]:
    """1 - mean(err)/mean(ref) in %, with its bootstrap range."""
    point = 100 * (1 - err.mean() / ref.mean())
    lo, hi = boot(lambda i: 100 * (1 - err[i].mean() / ref[i].mean()), len(err), block)
    return point, lo, hi


def pinball(y: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Mean quantile loss over the nine deciles; y (N,), q (N, 9)."""
    d = y[:, None] - q
    return np.maximum(LEVELS * d, (LEVELS - 1) * d).mean(axis=1)


def price_tables(source: str) -> dict[str, pd.DataFrame]:
    rows_skill, rows_dir, rows_cov, rows_cal = [], [], [], []
    for name, leads in PRICE_LEADS.items():
        exp = EXP[name]
        truth, models, _ = load(source, name)
        last, actual = truth["last"], truth["actual"].astype(np.float64)
        bar = pd.Timedelta(exp.bars.replace("m", "min").replace("d", "D"))
        rw = models["rw"]
        for h in leads:
            y, b = actual[:, h - 1], block_len(exp, h)
            label = lead_label(h * bar)
            e_rw = np.abs(rw[:, h - 1, 4] - y) / last * 100
            p_rw = pinball(y, rw[:, h - 1]) / last * 100
            move = np.sign(y - last)
            for m, q in models.items():
                med = q[:, h - 1, 4]
                e = np.abs(med - y) / last * 100
                base = {"bars": exp.bars, "lead": label, "lead_hours": h * bar / pd.Timedelta("1h"),
                        "model": m, "n": len(y)}
                if m != "rw":
                    s, lo, hi = skill(e, e_rw, b)
                    rows_skill.append({**base, "mae_pct": e.mean(), "rw_mae_pct": e_rw.mean(),
                                       "skill_pct": s, "lo": lo, "hi": hi})
                    call = np.sign(med - last)
                    ok = (move != 0) & (call != 0)
                    hit = (call == move)[ok].astype(float)
                    hlo, hhi = boot(lambda i: 100 * hit[i].mean(), len(hit), b)
                    rows_dir.append({**base, "hit_pct": 100 * hit.mean(), "lo": hlo, "hi": hhi,
                                     "n": int(ok.sum()), "up_calls_pct": 100 * (call > 0).mean()})
                if m in TFM or m == "rw":
                    inside = ((q[:, h - 1, 0] <= y) & (y <= q[:, h - 1, 8])).astype(float)
                    pb = pinball(y, q[:, h - 1]) / last * 100
                    ps, plo, phi = skill(pb, p_rw, b) if m != "rw" else (0.0, 0.0, 0.0)
                    rows_cov.append({**base, "coverage80_pct": 100 * inside.mean(),
                                     "width80_pct": ((q[:, h - 1, 8] - q[:, h - 1, 0]) / last * 100).mean(),
                                     "pinball_pct": pb.mean(), "pinball_skill_pct": ps,
                                     "lo": plo, "hi": phi})
                    for k, lvl in enumerate(LEVELS):
                        rows_cal.append({**base, "level": lvl,
                                         "below_pct": 100 * (y <= q[:, h - 1, k]).mean()})
    return {"price_skill": pd.DataFrame(rows_skill), "direction": pd.DataFrame(rows_dir),
            "uncertainty": pd.DataFrame(rows_cov), "calibration": pd.DataFrame(rows_cal)}


def signal_strength(source: str) -> pd.DataFrame:
    """Hit rate by how big a move the model called (1h bars, 24h ahead)."""
    exp = EXP["price_1h"]
    truth, models, _ = load(source, "price_1h")
    last, y = truth["last"], truth["actual"][:, 23].astype(np.float64)
    rows = []
    for m in (k for k in models if k in TFM):
        called = models[m][:, 23, 4] / last - 1
        bucket = pd.qcut(np.abs(called), 5, labels=False)
        for k in range(5):
            sel = bucket == k
            hit = (np.sign(called[sel]) == np.sign(y[sel] - last[sel])).astype(float)
            lo, hi = boot(lambda i: 100 * hit[i].mean(), len(hit), block_len(exp, 24) // 5 or 1)
            rows.append({"model": m, "bucket": k + 1,
                         "called_move_pct": 100 * np.abs(called[sel]).mean(),
                         "hit_pct": 100 * hit.mean(), "lo": lo, "hi": hi, "n": int(sel.sum())})
    return pd.DataFrame(rows)


def context_table(source: str) -> pd.DataFrame:
    rows = []
    for exp in (e for e in EXPERIMENTS if e.name.startswith("ctx_1h_")):
        truth, models, _ = load(source, exp.name)
        last, actual = truth["last"], truth["actual"].astype(np.float64)
        for h in (1, 6, 24):
            y, b = actual[:, h - 1], block_len(exp, h)
            e_rw = np.abs(models["rw"][:, h - 1, 4] - y) / last
            p_rw = pinball(y, models["rw"][:, h - 1])
            for m in (k for k in models if k in TFM):
                q = models[m][:, h - 1]
                s, lo, hi = skill(np.abs(q[:, 4] - y) / last, e_rw, b)
                ps, plo, phi = skill(pinball(y, q), p_rw, b)
                rows.append({"context_bars": exp.context, "context_days": exp.context / 24,
                             "lead": f"{h}h", "model": m, "skill_pct": s, "lo": lo, "hi": hi,
                             "pinball_skill_pct": ps, "pinball_lo": plo, "pinball_hi": phi,
                             "coverage80_pct": 100 * ((q[:, 0] <= y) & (y <= q[:, 8])).mean(),
                             "n": len(y)})
    return pd.DataFrame(rows)


def regime_table(source: str) -> pd.DataFrame:
    """Scores at 24h ahead (1h bars) split by the week before each forecast."""
    exp = EXP["price_1h"]
    truth, models, _ = load(source, "price_1h")
    bars = pd.read_pickle(WORK / source / "bars_1h.pkl")["close"]
    bars.index = bars.index.as_unit("ns")
    pos = bars.index.get_indexer(pd.to_datetime(truth["origins"], utc=True))
    logp = np.log(bars.to_numpy())
    vol = np.array([np.diff(logp[c - 168:c]).std() for c in pos])
    trend = np.array([logp[c - 1] - logp[c - 169] for c in pos])
    last, y = truth["last"], truth["actual"][:, 23].astype(np.float64)
    groups = {
        "Calm week": vol <= np.quantile(vol, 1 / 3),
        "Normal week": (vol > np.quantile(vol, 1 / 3)) & (vol <= np.quantile(vol, 2 / 3)),
        "Volatile week": vol > np.quantile(vol, 2 / 3),
        "Rising week": trend > 0,
        "Falling week": trend <= 0,
    }
    e_rw = np.abs(models["rw"][:, 23, 4] - y) / last
    rows = []
    for m in (k for k in models if k in TFM):
        q = models[m][:, 23]
        e = np.abs(q[:, 4] - y) / last
        hit = (np.sign(q[:, 4] - last) == np.sign(y - last)).astype(float)
        inside = ((q[:, 0] <= y) & (y <= q[:, 8])).astype(float)
        for g, sel in groups.items():
            idx = np.flatnonzero(sel)
            s, lo, hi = skill(e[idx], e_rw[idx], block_len(exp, 24))
            h = hit[idx]
            hlo, hhi = boot(lambda i: 100 * h[i].mean(), len(h), block_len(exp, 24))
            rows.append({"model": m, "regime": g, "n": len(idx), "skill_pct": s, "lo": lo, "hi": hi,
                         "hit_pct": 100 * h.mean(), "hit_lo": hlo, "hit_hi": hhi,
                         "coverage80_pct": 100 * inside[idx].mean(),
                         "daily_vol_pct": 100 * vol[idx].mean() * math.sqrt(24)})
    return pd.DataFrame(rows)


def volume_table(source: str) -> pd.DataFrame:
    exp = EXP["volume_1h"]
    truth, models, _ = load(source, "volume_1h")
    y = truth["actual"].astype(np.float64)
    ref = np.abs(models["snaive"][:, :, 4] - y).mean(axis=1)
    rows = []
    for m, q in models.items():
        e = np.abs(q[:, :, 4] - y).mean(axis=1)  # mean over the 24 hours of each day
        s, lo, hi = skill(e, ref, block_len(exp, 24))
        row = {"model": m, "mae_btc": e.mean(), "skill_vs_yesterday_pct": s, "lo": lo, "hi": hi,
               "n": len(e)}
        if m in TFM:
            row["coverage80_pct"] = 100 * ((q[:, :, 0] <= y) & (y <= q[:, :, 8])).mean()
        rows.append(row)
    return pd.DataFrame(rows)


def rv_table(source: str) -> pd.DataFrame:
    exp = EXP["rv_1d"]
    truth, models, _ = load(source, "rv_1d")
    y = truth["actual"].astype(np.float64)
    rows = []
    for label, cols in (("next day", slice(0, 1)), ("next 7 days", slice(0, 7))):
        yy = y[:, cols].mean(axis=1)
        ref = {k: np.abs(models[k][:, cols, 4].mean(axis=1) - yy) for k in ("naive", "har")}
        for m, q in models.items():
            f = q[:, cols, 4].mean(axis=1)
            e = np.abs(f - yy)
            s, lo, hi = skill(e, ref["naive"], block_len(exp, 7))
            sh, hlo, hhi = skill(e, ref["har"], block_len(exp, 7))
            ratio = (yy / f) ** 2
            rows.append({"target": label, "model": m, "mae_pp": e.mean(),
                         "qlike": (ratio - np.log(ratio) - 1).mean(),
                         "skill_vs_yesterday_pct": s, "lo": lo, "hi": hi,
                         "skill_vs_har_pct": sh, "har_lo": hlo, "har_hi": hhi, "n": len(e)})
    return pd.DataFrame(rows)


def trading(source: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Go long or short on each forecast's direction, hold to the next forecast, pay fees on changes.

    Two versions: a trade every 6 hours (6h-ahead forecasts) and once a day at
    00:00 UTC (24h-ahead forecasts). The daily one is what the chart shows.
    """
    truth, models, _ = load(source, "price_1h")
    origins = pd.to_datetime(truth["origins"], utc=True)
    rng = np.random.default_rng(1)
    stats, curves = [], {}
    for label, lead, sel in (("every 6 hours", 6, np.ones(len(origins), bool)),
                             ("once a day", 24, origins.hour == 0)):
        last = truth["last"][sel]
        r = np.log(truth["actual"][sel, lead - 1] / last)
        rand = rng.choice([-1.0, 1.0], size=(2000, len(r)))
        rand_paths = np.cumsum(rand * r - FEE * np.abs(np.diff(rand, prepend=0, axis=1)), axis=1)
        rand_net = rand_paths[:, -1]
        if label == "once a day":
            curves["Buy and hold"] = np.cumsum(r)
            curves["Coin flip, 5th percentile"] = np.percentile(rand_paths, 5, axis=0)
            curves["Coin flip, 95th percentile"] = np.percentile(rand_paths, 95, axis=0)
            dates = origins[sel]
        for m in (k for k in models if k in TFM):
            pos = np.sign(models[m][sel, lead - 1, 4] - last)
            turnover = np.abs(np.diff(pos, prepend=0))
            gross, net = pos * r, pos * r - FEE * turnover
            if label == "once a day":
                curves[f"{m} before fees"] = np.cumsum(gross)
                curves[f"{m} after fees"] = np.cumsum(net)
            stats.append({"strategy": m, "trades": label, "decisions": len(r),
                          "position_changes": int((turnover > 0).sum()),
                          "hit_pct": 100 * (np.sign(r) == pos)[r != 0].mean(),
                          "gross_return_pct": 100 * (math.exp(gross.sum()) - 1),
                          "net_return_pct": 100 * (math.exp(net.sum()) - 1),
                          "random_p05_pct": 100 * (math.exp(np.percentile(rand_net, 5)) - 1),
                          "random_p50_pct": 100 * (math.exp(np.percentile(rand_net, 50)) - 1),
                          "random_p95_pct": 100 * (math.exp(np.percentile(rand_net, 95)) - 1),
                          "share_of_random_beaten_pct": 100 * (rand_net < net.sum()).mean()})
        stats.append({"strategy": "Buy and hold", "trades": label, "decisions": len(r),
                      "net_return_pct": 100 * (math.exp(r.sum()) - 1)})
    # Balance after each day's trade closes, starting from 0% the day before the first.
    closes = (dates + pd.Timedelta("1D")).insert(0, dates[0])
    daily = pd.DataFrame({k: 100 * (np.exp(np.concatenate([[0.0], v])) - 1)
                          for k, v in curves.items()}, index=closes)
    return daily.reset_index(names="date"), pd.DataFrame(stats)


def example(source: str) -> pd.DataFrame:
    """The latest 24h forecast from a week of 1h bars, next to what happened."""
    truth, models, _ = load(source, "ctx_1h_168")
    bars = pd.read_pickle(WORK / source / "bars_1h.pkl")["close"]
    bars.index = bars.index.as_unit("ns")
    origin = pd.to_datetime(truth["origins"][-1], utc=True)
    c = bars.index.get_loc(origin)
    rows = [{"time": t, "series": "Past week", "value": v}
            for t, v in bars.iloc[c - 168:c].items()]
    future = bars.index[c:c + 24]
    q = models["timesfm25"][-1]
    for i, t in enumerate(future):
        rows.append({"time": t, "series": "What happened", "value": bars.iloc[c + i]})
        for name, k in (("Forecast low (10%)", 0), ("Forecast middle", 4), ("Forecast high (90%)", 8)):
            rows.append({"time": t, "series": name, "value": q[i, k]})
    return pd.DataFrame(rows)


def speed(source: str) -> pd.DataFrame:
    rows = []
    for exp in EXPERIMENTS:
        truth, _, seconds = load(source, exp.name)
        for m, s in seconds.items():
            n = len(truth["origins"])
            rows.append({"experiment": exp.name, "model": m, "context": exp.context,
                         "horizon": exp.horizon, "forecasts": n, "seconds": s,
                         "per_second": n / s})
    return pd.DataFrame(rows)


def overview(source: str) -> dict:
    total = sum(len(load(source, e.name)[0]["origins"]) for e in EXPERIMENTS)
    bars = pd.read_pickle(WORK / source / "bars_1d.pkl")["close"]
    test = bars[bars.index >= pd.Timestamp("2024-01-01", tz="UTC")]
    return {"forecasts_per_model": total, "test_start": str(test.index[0].date()),
            "test_end": str(test.index[-1].date()), "low": float(test.min()),
            "low_date": str(test.idxmin().date()), "high": float(test.max()),
            "high_date": str(test.idxmax().date()), "first": float(test.iloc[0]),
            "last": float(test.iloc[-1])}


NAMES = {"timesfm25": "TimesFM 2.5", "timesfm30": "TimesFM 3.0", "rw": "No change + recent volatility",
         "drift": "Recent trend continues", "snaive": "Same hour yesterday",
         "smean": "7-day average of each hour", "naive": "Yesterday’s volatility",
         "har": "HAR model", "mean30": "30-day average"}
BARS = {"15m": "15-minute bars", "1h": "Hourly bars", "4h": "4-hour bars", "1d": "Daily bars"}


def chart_files(t: dict[str, pd.DataFrame], out: Path) -> None:
    """Small tidy CSVs, one per chart, so each chart reads its numbers from a file.

    Works on whichever tables are present, so charts can be drawn as runs finish.
    """
    def named(df):
        return df.assign(model=df["model"].map(NAMES))

    def by_bars(df):
        df = df.copy()
        df["bars"] = pd.Categorical(df["bars"].map(BARS), list(BARS.values()), ordered=True)
        return df.sort_values(["bars", "lead_hours"], kind="stable")

    def write(df, name):
        df.to_csv(out / name, index=False, float_format="%.2f")

    if "price_skill" in t:
        ps = by_bars(t["price_skill"])
        write(named(ps[ps.model.isin(TFM)])[["model", "bars", "lead", "skill_pct", "lo", "hi"]]
              .rename(columns={"skill_pct": "skill"}), "chart_price_skill.csv")
    if "direction" in t:
        d = by_bars(t["direction"])
        write(named(d[d.model.isin(TFM)])[["model", "bars", "lead", "hit_pct", "lo", "hi"]]
              .rename(columns={"hit_pct": "hit"}), "chart_direction.csv")
    if "signal_strength" in t:
        write(named(t["signal_strength"])[["model", "bucket", "called_move_pct", "hit_pct", "lo", "hi"]]
              .rename(columns={"called_move_pct": "move", "hit_pct": "hit"}), "chart_signal.csv")
    if "uncertainty" in t:
        cov = by_bars(t["uncertainty"])
        cov = cov.assign(order=cov["model"].map({"timesfm25": 0, "timesfm30": 1, "rw": 2})) \
            .sort_values(["order"], kind="stable")
        write(named(cov)[["model", "bars", "lead", "coverage80_pct"]]
              .rename(columns={"coverage80_pct": "coverage"}), "chart_coverage.csv")
    if "context" in t:
        ctx = t["context"][t["context"].model == "timesfm25"]
        write(named(ctx)[["model", "context_days", "lead", "skill_pct", "lo", "hi"]]
              .rename(columns={"context_days": "days", "skill_pct": "skill"}), "chart_context.csv")
        write(named(ctx)[["model", "context_days", "lead", "coverage80_pct"]]
              .rename(columns={"context_days": "days", "coverage80_pct": "coverage"}),
              "chart_context_coverage.csv")
    if "volume" in t and "volatility" in t:
        vol = t["volume"].assign(group="Volume, next 24 hours")
        vol = vol[vol.model != "snaive"][["group", "model", "skill_vs_yesterday_pct", "lo", "hi"]]
        rv = t["volatility"].assign(group=lambda x: "Volatility, " + x["target"])
        rv = rv[rv.model != "naive"][["group", "model", "skill_vs_yesterday_pct", "lo", "hi"]]
        write(named(pd.concat([vol, rv])).rename(columns={"skill_vs_yesterday_pct": "skill"}),
              "chart_helps.csv")
    if "equity" in t:
        eq = t["equity"].set_index("date")
        keep = (eq.index.dayofweek == 0) | (eq.index == eq.index[0]) | (eq.index == eq.index[-1])
        weekly = eq[keep].rename(columns=lambda c: c.lower().replace("coin flip, ", "coin_flip_p")
                                 .replace("th percentile", "").replace(" ", "_"))
        weekly = weekly.rename(columns={"coin_flip_p5": "coin_flip_p05"})
        weekly.index = weekly.index.strftime("%Y-%m-%d")
        weekly.reset_index().to_csv(out / "chart_equity.csv", index=False, float_format="%.1f")
    if "example" in t:
        ex = t["example"].copy()
        stamp = pd.to_datetime(ex["time"])
        past = ex["series"] == "Past week"
        # Every other hour of the past week (plus its last hour) keeps the chart light.
        keep = ~past | (stamp.dt.hour % 2 == 0) | (stamp == stamp[past].max())
        ex = ex[keep].assign(time=stamp[keep].dt.strftime("%Y-%m-%dT%H:%M"))
        ex.to_csv(out / "chart_example.csv", index=False, float_format="%.0f")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", default="bitstamp")
    args = ap.parse_args()
    out = OUT / args.source
    out.mkdir(parents=True, exist_ok=True)
    tables = price_tables(args.source)
    tables["signal_strength"] = signal_strength(args.source)
    tables["context"] = context_table(args.source)
    tables["regimes"] = regime_table(args.source)
    tables["volume"] = volume_table(args.source)
    tables["volatility"] = rv_table(args.source)
    tables["equity"], tables["trading"] = trading(args.source)
    tables["example"] = example(args.source)
    tables["speed"] = speed(args.source)
    for name, df in tables.items():
        df.to_csv(out / f"{name}.csv", index=False, float_format="%.4f")
    chart_files(tables, out)
    (out / "overview.json").write_text(json.dumps(overview(args.source), indent=2))
    pd.set_option("display.width", 200)
    for name in ("price_skill", "direction", "uncertainty", "context", "regimes", "volume",
                 "volatility", "trading", "signal_strength", "speed"):
        print(f"\n== {name}\n{tables[name].round(2).to_string(index=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
