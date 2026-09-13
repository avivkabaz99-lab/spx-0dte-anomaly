"""Write synthetic sessions to disk in the shared bars layout.

Usage:
    .venv/bin/python -m spx0dte.synth --days 200 --seed 0 --out data/synth/bars
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from spx0dte.features.bars import write_bars
from spx0dte.synth.spx import SynthConfig, generate

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic SPX / VIX bars.")
    parser.add_argument("--days", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=Path("data/synth/bars"))
    parser.add_argument("--no-premium", action="store_true", help="leakage control set")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    config = SynthConfig(n_days=args.days, seed=args.seed)
    if args.no_premium:
        config = SynthConfig(
            n_days=args.days, seed=args.seed,
            premium_const=0.0, premium_minutes=0.0, premium_vol=0.0, forecast_noise=0.0,
        )
    data = generate(config)
    paths = write_bars(data.bars, args.out)
    logger.info("%d sessions, %d event days, %d part files", args.days, len(data.event_dates),
                len(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
