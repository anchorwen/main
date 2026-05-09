"""Training infrastructure — Dataset, Protocol, Checkpoint, ModelCard, ExperimentTracker, Registries."""

from core.training.checkpoint import CheckpointInfo, CheckpointManager
from core.training.dataset import TrainingDataset, train_val_test_split, walk_forward_splits
from core.training.experiment_tracker import ExperimentTracker, RunInfo, tracker
from core.training.model_card import ModelCard, ModelCardGenerator
from core.training.registries import (
    LOSS_REGISTRY,
    METRIC_REGISTRY,
    OPTIMIZER_REGISTRY,
    SCHEDULER_REGISTRY,
    get_loss_fn,
    get_metric_fn,
    get_optimizer,
    get_scheduler,
    list_registered_losses,
    list_registered_metrics,
)
from core.training.trainer_protocol import (
    TRAINER_REGISTRY,
    TrainerProtocol,
    TrainResult,
    register_trainer,
)

__all__ = [
    # Dataset
    "TrainingDataset",
    "train_val_test_split",
    "walk_forward_splits",
    # Trainer Protocol
    "TrainResult",
    "TrainerProtocol",
    "TRAINER_REGISTRY",
    "register_trainer",
    # Checkpoint
    "CheckpointManager",
    "CheckpointInfo",
    # Model Card
    "ModelCard",
    "ModelCardGenerator",
    # Experiment Tracker
    "ExperimentTracker",
    "RunInfo",
    "tracker",
    # Registries
    "LOSS_REGISTRY",
    "METRIC_REGISTRY",
    "OPTIMIZER_REGISTRY",
    "SCHEDULER_REGISTRY",
    "get_loss_fn",
    "get_metric_fn",
    "get_optimizer",
    "get_scheduler",
    "list_registered_losses",
    "list_registered_metrics",
]
