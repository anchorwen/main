# INTERFACE CONTRACTS — 关键模块接口契约

> **最后更新**: 2026-05-02T11:08:00Z (UTC)  
> **图例**: 输入参数 / 输出格式 / 错误处理 / 性能约束

---

## 契约 1: FeatureService → FeatureVector

| 项目 | 契约内容 |
|------|---------|
| **调用者** | `BrainRunService`, `RuntimeLoop`, `DecisionCycleOrchestrator` |
| **被调用者** | `core/features/feature_service.py` → `FeatureService` |
| **入口方法** | `build_feature_vector(trigger: dict \| None) → np.ndarray` |
| **输入** | `trigger`: `{"symbol": str, "venue": str}` 或 `None`（使用默认值 `XAUUSD`/`MT5`） |
| **输出** | `np.ndarray` 形状 `(40,)` dtype `float32`，对应 `V9_INSTITUTIONAL_40_FEATURES` 顺序 |
| **三级回退** | Tier 1: `LocalFeatureStore.latest()` 缓存命中 → 通过 `V9FeatureAdapter.build_model_input()` 归一化 → 返回 `(40,)` 向量<br>Tier 2: `V9LiveFeatureComputer.compute_all()` → 实时计算 + 异步回写到 Store → 通过 `V9FeatureAdapter` 归一化 → 返回 `(40,)` 向量<br>Tier 3: `np.zeros(40, dtype=np.float32)` 零向量安全回退 |
| **错误处理** | Tier 1/2 异常被 `try/except` 静默吞掉，自动降级到下一 Tier；Tier 3 永不失败 |
| **性能约束** | Tier 1 期望 <1ms（纯内存读）；Tier 2 期望 <50ms（含 MT5 IPC + 计算）；Tier 3 为 <0.1ms |

---

## 契约 2: BrainFactory → BrainAdapter

| 项目 | 契约内容 |
|------|---------|
| **调用者** | `BrainFactory`, `BrainRunService` |
| **被调用者** | `core/brains/adapters/base_adapter.py` → `BaseBrainAdapter`（抽象基类） |
| **构造** | `BaseBrainAdapter(brain_entry: dict)` — `brain_entry` 包含 `brain_id`, `brain_type`, `artifact_path`, `brain_role`, `status`, `model_version`, `deployment_scope`, `enable_onnxruntime` 等字段 |
| **强制实现** | 1. `load() → None` — 加载模型文件，设置 `self._backend`（如 `"onnxruntime"`, `"xgboost:json"`, `"stub:disabled"`）<br>2. `infer(feature_vector: np.ndarray) → dict[str, Any]` — 对 `(40,)` 浮点向量运行推理<br>3. `get_signal(raw_output: dict[str, Any]) → BrainDecisionProposal` — 将原始输出映射为统一提案 |
| **infer() 输出规范** | 各 Adapter 的 `raw_output` dict 键名不同，但 `get_signal()` 必须能消费：<br>- ONNX: `{"out_dir": np.ndarray, "out_risk": float, "out_vol": float, "runtime_ms": float, "fallback": bool}`<br>- XGBoost: `{"raw_score": float, "feature_count": int, "runtime_ms": float, "fallback": bool}`<br>- OU Params: `{"z_score": float, "theta": float, "mu": float, "half_life": float, ...}` |
| **get_signal() 输出规范** | 必须返回 `BrainDecisionProposal`，含 `prediction.direction_bias ∈ {"long", "short", "neutral"}`, `prediction.up_probability`, `prediction.down_probability`, `prediction.confidence`, `prediction.uncertainty`, `health.fallback_used`, `health.runtime_ms`, `health.backend` |
| **便利方法** | `inference(feature_vector: np.ndarray \| None) → BrainDecisionProposal` — 链式调用 `infer()` → `get_signal()`，作为主流水线统一入口 |
| **错误处理** | `load()` 从磁盘加载失败时 `self._backend = f"stub:{type(exc).__name__}"` 实现优雅降级；`_run_inference()` 必须有确定性回退逻辑（当 `self._session is None` 时返回合理的 stub 输出） |

---

## 契约 3: BrainDecisionProposal → ParliamentService

| 项目 | 契约内容 |
|------|---------|
| **调用者** | `BrainRunService`（将多个 Adapter 的输出汇总后传入） |
| **被调用者** | `core/parliament/parliament_service.py` → `ParliamentService` |
| **入口方法** | `build_candidate(feature_snapshot, proposals: list[BrainDecisionProposal], control_snapshot) → DecisionCandidate` |
| **输入** | `proposals`: 所有活跃 Brain 的 `BrainDecisionProposal` 列表，每个必须包含 `prediction.direction_bias`, `prediction.up_probability`, `prediction.down_probability`, `prediction.confidence`, `health.fallback_used`, `health.risk_score`, `brain_id` |
| **输出** | `DecisionCandidate` 含 `consensus.aggregated_bias ∈ {"long", "short", "neutral"}`, `consensus.consensus_score`, `consensus.disagreement_score`, `consensus.voter_count`, `consensus.majority_ratio`, `execution_feasibility.is_feasible`, `supporting_brains`, `opposing_brains` |
| **内部逻辑** | 1. `_filter_active_proposals()`: 根据 `GovernanceService.get_active_brain_ids()` 过滤<br>2. `_compute_consensus()`: 加权投票（权重 = confidence × 0.5 if fallback_used else confidence × 1.0），输出加权 bias + score<br>3. `_classify_brains()`: 与 consensus bias 同向 → supporting，反向且非 neutral → opposing<br>4. `_assess_feasibility()`: mode ∈ {halted, observe_only} → is_feasible=False |
| **错误处理** | 无 proposal 时返回 neutral consensus (score=0.5)；GovernanceService 不可用时跳过过滤；总权重为 0 时除数为 1.0（安全回退） |

---

## 契约 4: ParliamentService → DecisionCompiler

| 项目 | 契约内容 |
|------|---------|
| **调用者** | `DecisionCycleOrchestrator`, `RuntimeLoop` |
| **被调用者** | `core/protocol/services/decision_compiler.py` → `DecisionCompiler` |
| **入口方法** | `compile_intent(candidate: DecisionCandidate, mode_state, active_overrides) → DecisionIntent` |
| **输入** | `candidate`: `DecisionCandidate`（含 `candidate_summary.symbol`, `.venue`, `.up_probability`, `.down_probability`）<br>`mode_state`: `SystemModeState`（含 `current_mode`）<br>`active_overrides`: `ProtocolOverride` 列表（每个含 `adjustments` dict） |
| **输出** | `DecisionIntent` 含 `action ∈ {OPEN, CLOSE, OBSERVE, ABSTAIN}`, `side ∈ {LONG, SHORT, FLAT}`, `conviction: float`, `reason_tags: list[str]`, `symbol`, `venue` |
| **内部逻辑** | 1. `_build_effective_policy()`: base_policy + overrides 叠加 + mode 调整阈值（CAUTIOUS: threshold ≥ 0.74; DEGRADED: ≥ 0.78）<br>2. `_materialize_action()`: `adjusted_up = (up + shift) × scale`, `adjusted_down = (down - shift) × scale`；≥ threshold → OPEN；< threshold → ABSTAIN；OBSERVE_ONLY mode → OBSERVE<br>3. force_passive=True（HALTED/LIQUIDATION_ONLY/OBSERVE_ONLY 模式）→ OBSERVE + FLAT + conviction=0.0 |
| **base_policy 默认值** | `{"entry_long_threshold": 0.70, "entry_short_threshold": 0.70, "probability_shift": 0.0, "probability_scale": 1.0}` |
| **错误处理** | 所有概率计算被 `max(0.0, min(1.0, ...))` 夹逼到 [0, 1]；active_overrides 中的 `adjustments` 为 None 时跳过 |

---

## 契约 5: DecisionCompiler → RiskEvaluationService

| 项目 | 契约内容 |
|------|---------|
| **调用者** | `DecisionCycleOrchestrator`, `RuntimeLoop`（先 compile → 再 evaluate） |
| **被调用者** | `core/risk/risk_evaluation_service.py` → `RiskEvaluationService` |
| **入口方法** | `evaluate(intent: DecisionIntent, control_snapshot, *, context: dict \| None = None) → RiskVerdict` |
| **输入** | `intent`: `DecisionIntent`（含 `action`, `side`, `conviction`, `symbol`）<br>`control_snapshot`: 含 `mode_state.current_mode`<br>`context`: 可选的附加上下文 dict |
| **输出** | `RiskVerdict` 含 `status ∈ {ALLOW, ALLOW_LIMITED, DEFER, FORCE_REDUCE, LIQUIDATE_ONLY, DENY}`, `risk_tier ∈ {minimal, standard, cautious, elevated, critical}`, `blocking_reasons: list[str]`, `warning_reasons: list[str]`, `constraints: dict` |
| **严重性排序** | `DENY < LIQUIDATE_ONLY < FORCE_REDUCE < DEFER < ALLOW_LIMITED < ALLOW` — 最终取所有 policy 中最严格的 |
| **策略链** | 5 条策略依次执行：`ModePolicy`, `DrawdownPolicy`, `ExposurePolicy`, `ConcentrationPolicy`, `PositionLimitPolicy`；每条返回 `{"status": RiskDecisionStatus, "reason": str, "tier": str, "constraint": dict \| None}` |
| **特殊处理** | `intent.is_passive()` (OBSERVE/ABSTAIN/CLOSE) → 直接返回 DENY + risk_tier="minimal" + blocking_reasons=["passive_intent"]，跳过所有策略 |
| **错误处理** | 无策略时返回 ALLOW + "standard"；blocking_reasons 在最终状态为 ALLOW/ALLOW_LIMITED 时降级为 warning_reasons（不阻塞但记录警告） |

---

## 契约 6: CommunicationDispatcher → CommunicationAdapter

| 项目 | 契约内容 |
|------|---------|
| **调用者** | `RuntimeLoop`, `DecisionCycleOrchestrator`（通过 `CommunicationDispatcher.dispatch()`） |
| **被调用者** | `core/protocol/services/communication_adapter.py` → `CommunicationAdapter`（Protocol） |
| **入口方法** | `dispatch(request: DispatchRequest, envelope: CommunicationEnvelope) → DispatchResult` |
| **输入** | `request`: `DispatchRequest`（含 `dispatch_id`, `envelope`, `requested_at`, `route_policy`, `transport_hints`, `governance`）<br>`envelope`: `CommunicationEnvelope`（含 `message_id`, `correlation_id`, `target`, `message_type`, `payload`, `deadline_at`, `idempotency_key`） |
| **输出** | `DispatchResult` 含 `status ∈ {SUCCEEDED, FAILED, DEGRADED}`, `adapter_name`, `failure_reason`, `attempts`, `trace` |
| **4 种实现** | 1. `StubCommunicationAdapter` — 记录日志但不实际发送（测试/回退用）<br>2. `FileQueueCommunicationAdapter` — 写入 JSONL 文件队列到 `outbox/`<br>3. `MT5CommunicationAdapter` — 通过 TCP/IP 发送到 `mt5_bridge_worker`<br>4. `FixCommunicationAdapter` — FIX 协议消息（框架存在，实盘未全覆盖） |
| **分发前闸口** | 1. `live_read_only` → 直接 FAILED (reason: `LIVE_READ_ONLY`)<br>2. `live_dispatch_enabled=False` → 直接 FAILED (reason: `LIVE_DISPATCH_DISABLED`)<br>3. `symbol not in live_allowed_symbols` → 直接 FAILED (reason: `SYMBOL_NOT_LIVE_ENABLED`)<br>4. `idempotency_key` 重复 → 直接 FAILED (reason: `DUPLICATE_IDEMPOTENCY_KEY`)<br>5. `deadline_at` 已过期 → 直接 FAILED (reason: `DEADLINE_EXCEEDED`) |
| **回退策略** | 主 Adapter 失败时查找 `route_policy.fallback_adapter`，通过 `CommunicationAdapterRegistry.resolve()` 获取回退适配器并重试；成功则 `status=DEGRADED`，失败则 `status=FAILED` |
| **错误处理** | 所有 Adapter 异常被 `try/except` 捕获；主适配器失败 + 回退适配器失败 → FAILED + 两个 adapter 的错误原因合并 |

---

## 契约交叉引用矩阵

| 上游 → 下游 | 数据载体 | 关键约束 |
|-------------|---------|---------|
| FeatureService → BrainAdapter | `np.ndarray(40,) float32` | 必须按 `V9_INSTITUTIONAL_40_FEATURES` 顺序；三级回退永不抛异常 |
| BrainAdapter → ParliamentService | `BrainDecisionProposal` | `direction_bias ∈ {long, short, neutral}`; `confidence ∈ [0,1]` |
| ParliamentService → DecisionCompiler | `DecisionCandidate` | `execution_feasibility.is_feasible` 决定是否继续 |
| DecisionCompiler → RiskEvaluationService | `DecisionIntent` | `action/side/conviction` 由 mode 调整的阈值决定 |
| RiskEvaluationService → DecisionCompiler | `RiskVerdict` | 最终 `status` 为最严格策略结果 |
| CommunicationDispatcher → Adapter | `DispatchRequest + CommunicationEnvelope` | 5 道闸口全部通过后才到达 Adapter |

---

> **维护规则**: 新增模块或修改接口签名时必须同步更新此文件。每次 PR 涉及以上 6 对接口时，需在 CHANGELOG 中记录变更。