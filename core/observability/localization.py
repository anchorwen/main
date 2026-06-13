"""Iron Law #13 / D2: Schema-Driven Localization — SSOT Rule Registry.

The Single Source of Truth for all rule metadata: Chinese display names,
context-key translations, severity labels, and runbook mappings.

Core principle (D2 — Edge Rendering Pipeline):
  Producers emit structured ``BaseTelemetryEvent`` objects.  They know
  NOTHING about Chinese, Markdown, or DingTalk syntax.  All localization
  and rendering lives at the edge (channel layer), consuming this registry.

Backward compatibility:
  ``alert_channels.py`` re-exports the dicts it needs from here, so
  existing code that accesses ``DingTalkAlertChannel.RULE_NAME_CN``
  continues to work without changes.

Usage::

    from core.observability.localization import RuleRegistry

    cn_name = RuleRegistry.rule_name("data_source_critical_failure")
    # → "数据源严重故障"

    cn_key = RuleRegistry.context_key("data_health_critical_fail_count")
    # → "严重故障源数"

    runbook_id = RuleRegistry.runbook_for("RULE-012")
    # → "RB-012"
"""

from __future__ import annotations

from typing import ClassVar

# ── Severity labels ─────────────────────────────────────────────────────────

SEVERITY_PREFIX: dict[str, str] = {
    "critical": "🔴 [CRITICAL]",
    "error": "🟠 [ERROR]",
    "warning": "🟡 [WARNING]",
    "info": "🔵 [INFO]",
}

SEVERITY_CN: dict[str, str] = {
    "critical": "严重",
    "error": "错误",
    "warning": "警告",
    "info": "信息",
}

# ── Rule name localizations ─────────────────────────────────────────────────
# All 17 rules: 11 original + 6 data-health (RULE-012 through RULE-016) + trade_notification

RULE_NAME_CN: dict[str, str] = {
    # Original 11
    "high_error_rate": "高错误率",
    "circuit_breaker_open": "断路器已断开",
    "bridge_heartbeat_missed": "MT5桥接心跳丢失",
    "brain_frozen": "大脑冻结",
    "position_limit_near": "仓位接近上限",
    "cycle_stall": "周期停滞",
    "system_online": "系统上线",
    "daily_loss_exceeded": "日亏损超限",
    "consecutive_losses": "连续亏损超限",
    "win_rate_collapse": "胜率崩塌",
    "strategy_degradation": "策略性能下降",
    # Data health (RULE-012 through RULE-016)
    "data_source_critical_failure": "数据源严重故障",
    "data_health_degraded": "数据健康降级",
    "cross_source_discrepancy": "跨源数据不一致",
    "orphan_subsystem_detected": "检测到孤立子系统",
    "state_file_stale": "状态文件过期",
    # Trade notification
    "trade_notification": "交易通知",
}

# ── Context key localizations ───────────────────────────────────────────────
# 22 keys: 14 original + 8 data-health

CONTEXT_KEY_CN: dict[str, str] = {
    # Original 14 (trade / system context)
    "error_rate": "周期错误率",
    "circuit_state": "断路器状态",
    "frozen_brain_count": "冻结大脑数",
    "position_utilization": "仓位利用率",
    "bridge_last_ack_seconds": "桥接最后响应",
    "cycle_duration_seconds": "周期耗时",
    "message": "消息",
    "rules_active": "活跃规则",
    "channels": "通知通道",
    "daily_pnl_usd": "当日盈亏(USD)",
    "consecutive_losses": "连续亏损次数",
    "rolling_win_rate": "滚动胜率",
    "strategy_pnl": "最差大脑累计PnL(R)",
    "strategy_win_rate": "最差大脑胜率",
    "worst_brain_id": "最差大脑ID",
    "total_trades_window": "窗口内交易数",
    # Data health context (RULE-012 through RULE-016)
    "data_health_critical_fail_count": "严重故障源数",
    "data_health_warn_count": "警告源数",
    "cross_source_discrepancy_count": "跨源不一致数",
    "orphan_subsystem_count": "孤立子系统数",
    "stale_state_file_count": "过期状态文件数",
    "data_health_overall": "总体评估",
    # Structured payload keys (rendered as dedicated sections, not kv pairs)
    "data_health_failed_sources": "故障详情",
    "data_health_warned_sources": "警告详情",
}

# ── Runbook registry ────────────────────────────────────────────────────────
# Maps rule_id → runbook_id for closed-loop remediation (D3).
# The RemediationEngine (future phase) reads runbook_id and executes
# the corresponding SOP before waking a human.

RUNBOOK_REGISTRY: dict[str, str] = {
    "RULE-012": "RB-012",  # data_source_critical_failure
    "RULE-013": "RB-013",  # data_health_degraded
    "RULE-014": "RB-014",  # cross_source_discrepancy
    "RULE-015": "RB-015",  # orphan_subsystem_detected
    "RULE-016": "RB-016",  # state_file_stale
    "daily_loss_exceeded": "RB-DLE",  # daily loss limit
    "strategy_degradation": "RB-SD",  # strategy degradation
    "circuit_breaker_open": "RB-CB",  # circuit breaker
    "bridge_heartbeat_missed": "RB-BH",  # bridge heartbeat
    "brain_frozen": "RB-BF",  # brain frozen
}


# ── Structured context keys (rendered as dedicated sections) ────────────────
# Context keys whose values are list[dict] (not scalars or strings).
# The DingTalk _format() method detects these and renders them as
# dedicated bullet-list sections rather than inline ``k: v`` pairs.

STRUCTURED_CONTEXT_KEYS: set[str] = {
    "data_health_failed_sources",
    "data_health_warned_sources",
}


# ── RuleRegistry class ──────────────────────────────────────────────────────
# Thin static class that provides a single import point for rule metadata.
# All channel code should call RuleRegistry.rule_name() rather than
# accessing RULE_NAME_CN directly — this allows future dynamic loading
# from YAML or a database without changing call sites.


class RuleRegistry:
    """SSOT for rule metadata, Chinese localization, and runbook bindings.

    All methods are static — the registry is a pure lookup table.
    Channel code imports this class instead of reaching into the module-level
    dicts directly, enabling future migration to YAML- or DB-backed storage.
    """

    # Re-export module-level dicts as class attributes for backward compat
    SEVERITY_PREFIX: ClassVar[dict[str, str]] = SEVERITY_PREFIX
    SEVERITY_CN: ClassVar[dict[str, str]] = SEVERITY_CN
    RULE_NAME_CN: ClassVar[dict[str, str]] = RULE_NAME_CN
    CONTEXT_KEY_CN: ClassVar[dict[str, str]] = CONTEXT_KEY_CN
    RUNBOOK_REGISTRY: ClassVar[dict[str, str]] = RUNBOOK_REGISTRY
    STRUCTURED_CONTEXT_KEYS: ClassVar[set[str]] = STRUCTURED_CONTEXT_KEYS

    @staticmethod
    def rule_name(rule_id: str) -> str:
        """Return the Chinese display name for a rule, or the raw ID if unknown."""
        return RULE_NAME_CN.get(rule_id, rule_id)

    @staticmethod
    def context_key(key: str) -> str:
        """Return the Chinese label for a context key, or the raw key if unknown."""
        return CONTEXT_KEY_CN.get(key, key)

    @staticmethod
    def severity_cn(severity: str) -> str:
        """Return the Chinese severity label, or the raw value if unknown."""
        return SEVERITY_CN.get(severity, severity)

    @staticmethod
    def severity_prefix(severity: str) -> str:
        """Return the emoji prefix for a severity level, or a generic icon."""
        return SEVERITY_PREFIX.get(severity, "⚪")

    @staticmethod
    def runbook_for(rule_id: str) -> str | None:
        """Return the runbook ID bound to a rule, or None."""
        return RUNBOOK_REGISTRY.get(rule_id)

    @staticmethod
    def is_structured_key(key: str) -> bool:
        """Return True if this context key carries structured list data."""
        return key in STRUCTURED_CONTEXT_KEYS
