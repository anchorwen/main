"""Training infrastructure — Dataset, Protocol, Checkpoint, ModelCard, ExperimentTracker, Registries."""

from core.training.checkpoint import CheckpointInfo, CheckpointManager
from core.training.cpcv import (
    CPCVFold,
    CPCVResult,
    combinatorial_purged_cv,
    cpcv_summary,
    n_combinatorial_folds,
)
from core.training.custom_objectives import (
    compute_sample_weights,
    lightgbm_sharpe_eval,
    lightgbm_sharpe_obj,
    make_xgb_sharpe_obj,
    profit_factor_approx,
    weighted_logloss,
)
from core.training.dataset import TrainingDataset, train_val_test_split, walk_forward_splits
from core.training.evaluation_report import (
    SHAPReport,
    TrainingEvalReport,
    check_shap_stability,
    compute_financial_metrics,
    compute_overfit_gap,
    compute_regime_breakdown,
    run_shap_analysis,
)
from core.training.experiment_tracker import ExperimentTracker, RunInfo, tracker
from core.training.model_card import ModelCard, ModelCardGenerator
from core.training.model_hashing import hash_model_file, hash_models_ensemble, verify_model_hash
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
from core.training.training_registry import TrainingRegistry, TrainingRunRecord, create_registry

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
    # CPCV
    "CPCVFold",
    "CPCVResult",
    "combinatorial_purged_cv",
    "cpcv_summary",
    "n_combinatorial_folds",
    # Custom Objectives
    "compute_sample_weights",
    "lightgbm_sharpe_eval",
    "lightgbm_sharpe_obj",
    "make_xgb_sharpe_obj",
    "profit_factor_approx",
    "weighted_logloss",
    # Evaluation Report
    "SHAPReport",
    "TrainingEvalReport",
    "check_shap_stability",
    "compute_financial_metrics",
    "compute_overfit_gap",
    "compute_regime_breakdown",
    "run_shap_analysis",
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
    # Model Hashing
    "hash_model_file",
    "hash_models_ensemble",
    "verify_model_hash",
    # Training Registry
    "TrainingRegistry",
    "TrainingRunRecord",
    "create_registry",
]
