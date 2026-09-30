"""Time one TimesFM model on this machine: load, first forecast, then steady speed.

    python bench.py timesfm25 --weights /path/to/timesfm-2.5-200m-flax
    python bench.py timesfm30 --weights google/timesfm-3.0-pytorch

Prints one JSON line: seconds to load, seconds for the first batch (includes
compiling), forecasts per second after that, and the process's peak memory.
"""

from __future__ import annotations

import argparse
import json
import resource
import time

import numpy as np

from backtest import TimesFM25, TimesFM30


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("model", choices=("timesfm25", "timesfm30"))
    ap.add_argument("--weights", required=True)
    ap.add_argument("--context", type=int, default=168)
    ap.add_argument("--horizon", type=int, default=24)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--rounds", type=int, default=4)
    args = ap.parse_args()

    rng = np.random.default_rng(0)
    series = [80_000 * np.exp(np.cumsum(rng.normal(0, 0.004, args.context)))
              for _ in range(args.batch)]
    t0 = time.time()
    model = (TimesFM25 if args.model == "timesfm25" else TimesFM30)(args.weights)
    loaded = time.time() - t0
    t0 = time.time()
    model.forecast(series, args.horizon)
    first = time.time() - t0
    t0 = time.time()
    for _ in range(args.rounds):
        model.forecast(series, args.horizon)
    steady = args.rounds * args.batch / (time.time() - t0)
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(json.dumps({"model": args.model, "context": args.context, "horizon": args.horizon,
                      "load_s": round(loaded, 1), "first_batch_s": round(first, 1),
                      "forecasts_per_s": round(steady, 1), "peak_memory_mb": round(peak_mb)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
