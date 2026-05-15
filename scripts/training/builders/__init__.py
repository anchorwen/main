"""Training dataset builders — per-lane, per-timeframe, backward-only standardization.

Builders produce NPZ or CSV outputs consumed by trainers (xgb_trainer,
transformer_trainer, arb_trainer, sur_trainer).

Subclasses:
    MicrostructureDatasetBuilder — 9-feat × 32-bar sequences, barrier labels
    ArbDatasetBuilder            — OHLC pass-through for OU optimization
"""

from scripts.training.builders.arb import ArbDatasetBuilder
from scripts.training.builders.base import BaseDatasetBuilder
from scripts.training.builders.microstructure import MicrostructureDatasetBuilder

__all__ = [
    "BaseDatasetBuilder",
    "MicrostructureDatasetBuilder",
    "ArbDatasetBuilder",
]
