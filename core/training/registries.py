"""Registries for loss functions, metrics, optimizers, and schedulers.

Every loss/metric/optimizer is registered by name so TrainingRecipe can
reference them by string and the training loop can resolve them dynamically.
All registries use the decorator pattern for ergonomic registration.

Usage:
    @register_loss("cross_entropy")
    def cross_entropy_loss(logits, targets, **kw):
        return F.cross_entropy(logits, targets)

    loss_fn = LOSS_REGISTRY["cross_entropy"]
    loss = loss_fn(logits, yb)

    # Also callable via get_loss_fn("cross_entropy") which raises
    # ValueError if not found instead of KeyError.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

# ═══════════════════════════════════════════════════════════════════════
# Loss Registry
# ═══════════════════════════════════════════════════════════════════════

LOSS_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_loss(name: str):
    """Decorator to register a loss function."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in LOSS_REGISTRY:
            raise ValueError(f"Loss '{name}' is already registered")
        LOSS_REGISTRY[name] = fn
        return fn

    return decorator


def get_loss_fn(name: str) -> Callable[..., Any]:
    if name not in LOSS_REGISTRY:
        raise ValueError(f"Unknown loss '{name}'. Registered: {list_registered_losses()}")
    return LOSS_REGISTRY[name]


def list_registered_losses() -> list[str]:
    return sorted(LOSS_REGISTRY.keys())


# ── Built-in loss functions ──


@register_loss("cross_entropy")
def _ce_loss(logits, targets, **kw):
    import torch

    return torch.nn.functional.cross_entropy(logits, targets)


@register_loss("mse")
def _mse_loss(pred, target, **kw):
    import torch

    return torch.nn.functional.mse_loss(pred, target)


@register_loss("binary_logloss")
def _binary_logloss(pred, target, **kw):
    """Binary logloss for LightGBM/XGBoost (computed in native code, stub here)."""
    import torch

    return torch.nn.functional.binary_cross_entropy_with_logits(pred, target)


@register_loss("huber")
def _huber_loss(pred, target, delta: float = 1.0, **kw):
    import torch

    return torch.nn.functional.huber_loss(pred, target, delta=delta)


# ═══════════════════════════════════════════════════════════════════════
# Metric Registry
# ═══════════════════════════════════════════════════════════════════════

METRIC_REGISTRY: dict[str, Callable[..., float]] = {}


def register_metric(name: str):
    """Decorator to register an evaluation metric."""

    def decorator(fn: Callable[..., float]) -> Callable[..., float]:
        if name in METRIC_REGISTRY:
            raise ValueError(f"Metric '{name}' is already registered")
        METRIC_REGISTRY[name] = fn
        return fn

    return decorator


def get_metric_fn(name: str) -> Callable[..., float]:
    if name not in METRIC_REGISTRY:
        raise ValueError(f"Unknown metric '{name}'. Registered: {list_registered_metrics()}")
    return METRIC_REGISTRY[name]


def list_registered_metrics() -> list[str]:
    return sorted(METRIC_REGISTRY.keys())


# ── Built-in metric functions ──


@register_metric("accuracy")
def _accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float((y_true == y_pred).mean())


@register_metric("direction_accuracy")
def _direction_accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Accuracy on directional signal (up/down/flat)."""
    # Map to direction: -1, 0, +1
    y_true_dir = np.sign(y_true.astype(np.float64) - 1.0)  # center at 0
    y_pred_dir = np.sign(y_pred.astype(np.float64) - 1.0)
    return float((y_true_dir == y_pred_dir).mean())


@register_metric("precision")
def _precision(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    tp = ((y_pred == 1) & (y_true == 1)).sum()
    fp = ((y_pred == 1) & (y_true != 1)).sum()
    if tp + fp == 0:
        return 0.0
    return float(tp / (tp + fp))


@register_metric("recall")
def _recall(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    tp = ((y_pred == 1) & (y_true == 1)).sum()
    fn = ((y_pred != 1) & (y_true == 1)).sum()
    if tp + fn == 0:
        return 0.0
    return float(tp / (tp + fn))


@register_metric("f1")
def _f1(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    prec = _precision(y_true, y_pred)
    rec = _recall(y_true, y_pred)
    if prec + rec == 0:
        return 0.0
    return float(2 * prec * rec / (prec + rec))


@register_metric("mae")
def _mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.abs(y_true.astype(np.float64) - y_pred.astype(np.float64)).mean())


@register_metric("rmse")
def _rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.sqrt(((y_true.astype(np.float64) - y_pred.astype(np.float64)) ** 2).mean()))


@register_metric("r2")
def _r2(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    ss_res = ((y_true - y_pred) ** 2).sum()
    ss_tot = ((y_true - y_true.mean()) ** 2).sum()
    if ss_tot == 0:
        return 0.0
    return float(1.0 - ss_res / ss_tot)


# ═══════════════════════════════════════════════════════════════════════
# Optimizer Registry
# ═══════════════════════════════════════════════════════════════════════

OPTIMIZER_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_optimizer(name: str):
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in OPTIMIZER_REGISTRY:
            raise ValueError(f"Optimizer '{name}' is already registered")
        OPTIMIZER_REGISTRY[name] = fn
        return fn

    return decorator


def get_optimizer(name: str, params: Any, **kw) -> Any:
    if name not in OPTIMIZER_REGISTRY:
        raise ValueError(
            f"Unknown optimizer '{name}'. Registered: {sorted(OPTIMIZER_REGISTRY.keys())}"
        )
    return OPTIMIZER_REGISTRY[name](params, **kw)


# ── Built-in optimizers ──


@register_optimizer("adam")
def _adam(params, lr: float = 0.001, weight_decay: float = 0.0, **kw):
    import torch

    return torch.optim.Adam(params, lr=lr, weight_decay=weight_decay)


@register_optimizer("adamw")
def _adamw(params, lr: float = 0.001, weight_decay: float = 1e-4, **kw):
    import torch

    return torch.optim.AdamW(params, lr=lr, weight_decay=weight_decay)


@register_optimizer("sgd")
def _sgd(params, lr: float = 0.001, momentum: float = 0.9, weight_decay: float = 0.0, **kw):
    import torch

    return torch.optim.SGD(params, lr=lr, momentum=momentum, weight_decay=weight_decay)


# ═══════════════════════════════════════════════════════════════════════
# Scheduler Registry
# ═══════════════════════════════════════════════════════════════════════

SCHEDULER_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_scheduler(name: str):
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in SCHEDULER_REGISTRY:
            raise ValueError(f"Scheduler '{name}' is already registered")
        SCHEDULER_REGISTRY[name] = fn
        return fn

    return decorator


def get_scheduler(name: str, optimizer: Any, **kw) -> Any:
    if name not in SCHEDULER_REGISTRY:
        raise ValueError(
            f"Unknown scheduler '{name}'. Registered: {sorted(SCHEDULER_REGISTRY.keys())}"
        )
    return SCHEDULER_REGISTRY[name](optimizer, **kw)


# ── Built-in schedulers ──


@register_scheduler("one_cycle")
def _one_cycle(
    optimizer,
    max_lr: float = 0.001,
    epochs: int = 200,
    steps_per_epoch: int = 100,
    pct_start: float = 0.1,
    **kw,
):
    import torch

    return torch.optim.lr_scheduler.OneCycleLR(
        optimizer,
        max_lr=max_lr,
        epochs=epochs,
        steps_per_epoch=steps_per_epoch,
        pct_start=pct_start,
    )


@register_scheduler("cosine")
def _cosine(optimizer, T_max: int = 200, **kw):
    import torch

    return torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=T_max)


@register_scheduler("reduce_on_plateau")
def _reduce_on_plateau(optimizer, patience: int = 10, factor: float = 0.5, **kw):
    import torch

    return torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=patience, factor=factor)
