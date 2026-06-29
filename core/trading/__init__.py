"""V6 Shared Trading Infrastructure — brain-agnostic position management layers.

Layer A (Signal Refinement Gate): RegimeSuitability + SignalQualityScorer
    + MultiTFConfirmation — gates entry signals before order dispatch.

Layer B (Position Lifecycle Manager): 5-Stage Gate + 7-Level Exit Priority
    Queue + Ratchet Risk — manages open positions through their full lifecycle.

Design principle: All layers consume standard contracts (BrainSignal,
ConsensusResult, ActivePosition) and are config-gated.  When disabled,
the system behaves identically to the pre-V6 code path (Delta-Zero Law).

References:
  - God's Eye V6.0 (E:\\ai\\V6) — empirical OU mean-reversion trading system
  - v6_integration_blueprint.pdf — institutional architecture committee design
"""
