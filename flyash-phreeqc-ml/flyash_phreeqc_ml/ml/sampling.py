"""Latin-hypercube sampling over the surrogate input space (config-declared).

Produces a reproducible design matrix over the PHREEQC input space declared in
``config.SURROGATE_INPUT_SPACE`` — continuous axes (NaOH molarity, L/S ratio,
temperature) plus a categorical CO₂-scenario axis ``{atm, low, none}``. The design
is what ``scripts/10`` runs through :mod:`phreeqc_runner` to build the surrogate
training set. Pure (no PHREEQC, no model): given a seed, the matrix is deterministic.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import qmc

from .. import config


def _split_axes(space: dict):
    """Partition the input space into continuous ``(name,(lo,hi))`` and categorical."""
    continuous = [(k, v) for k, v in space.items() if isinstance(v, (tuple,)) and len(v) == 2]
    categorical = [(k, list(v)) for k, v in space.items() if isinstance(v, list)]
    return continuous, categorical


def latin_hypercube_design(n_samples: int, *, seed: int = 0,
                           space: dict | None = None) -> pd.DataFrame:
    """A reproducible Latin-hypercube design over the input space.

    Continuous axes are scaled into their ``(lo, hi)`` range; each categorical axis is
    drawn by binning its hypercube coordinate into equal-probability category bins.
    Columns follow the declaration order in ``space`` (a ``sample_id`` is prepended).
    Same ``seed`` → identical matrix.
    """
    if n_samples < 1:
        raise ValueError("n_samples must be >= 1")
    space = space or config.SURROGATE_INPUT_SPACE
    continuous, categorical = _split_axes(space)
    d = len(continuous) + len(categorical)

    sampler = qmc.LatinHypercube(d=d, seed=seed)
    unit = sampler.random(n_samples)  # (n_samples, d) in [0, 1)

    cols: dict[str, object] = {}
    idx = 0
    for name, (lo, hi) in continuous:
        cols[name] = lo + unit[:, idx] * (hi - lo)
        idx += 1
    for name, choices in categorical:
        k = len(choices)
        bins = np.minimum((unit[:, idx] * k).astype(int), k - 1)
        cols[name] = [choices[b] for b in bins]
        idx += 1

    df = pd.DataFrame(cols, columns=[name for name, _ in continuous]
                      + [name for name, _ in categorical])
    df.insert(0, "sample_id", [f"S{i:04d}" for i in range(n_samples)])
    return df
