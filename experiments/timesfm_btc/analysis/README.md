# TimesFM on BTC: the full backtest

The code behind the report "TimesFM on Bitcoin: Evaluation Report". It makes
26,000 forecasts per model from Jan 2024 to Sep 2026, each using only the data
before it, and scores them against simple baselines.

| Step | Command | Output |
| --- | --- | --- |
| 1. Bars | `python data.py --source binance` (or `--source bitstamp --repo <clone>`) | `~/tfm_work/<source>/bars_*.pkl`, `rv_1d.pkl` |
| 2. Forecasts | `python backtest.py --source binance --models baselines timesfm25` | `~/tfm_work/<source>/runs/<experiment>/<model>.npz` |
| 3. TimesFM 3.0 | `python backtest.py --source binance --models timesfm30` | same folders, `timesfm30.npz` |
| 4. Scores | `python summarize.py --source binance` | `../results/analysis/<source>/*.csv` |
| 5. Speed | `python bench.py timesfm25 --weights <dir or HF id>` | one JSON line |

`--weights25` takes the Hugging Face id `google/timesfm-2.5-200m-flax` or a
local copy; `--weights30` defaults to `google/timesfm-3.0-pytorch`. TimesFM
3.0's weights are for non-commercial use when you run them yourself.

## What each experiment is

| Name | Series | History | Ahead | Forecasts |
| --- | --- | --- | --- | --- |
| `price_15m` | close, 15-minute bars | 7 days | up to 1 day | every 6h |
| `price_1h` | close, hourly bars | 7 days | up to 7 days | every 6h |
| `price_4h` | close, 4-hour bars | 30 days | up to 7 days | daily |
| `price_1d` | close, daily bars | 365 days | up to 30 days | daily |
| `ctx_1h_<n>` | close, hourly bars | n hours (1 to 180 days) | 24h | every 12h |
| `volume_1h` | volume, hourly bars | 7 days | 24h | daily |
| `rv_1d` | daily realized volatility (5-minute returns) | 365 days | 7 days | daily |

Baselines: `rw` (price stays put, range from the context's volatility),
`drift` (recent trend continues), `snaive`/`smean` (same hour yesterday, or the
7-day average of that hour), `naive`/`mean30`/`har` for volatility.

Every score in `summarize.py` has a 95% range from a moving-block bootstrap
over the forecast origins, so overlapping forecasts aren't counted as
independent evidence.
