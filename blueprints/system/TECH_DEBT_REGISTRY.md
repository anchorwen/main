# TECH_DEBT_REGISTRY — 架构技术债等候清单

> 已知架构技术债 — 当前不触发，未来清债路线图。  
> 每个条目包含触发条件，达到条件时自动升级为 Active Fix。
> 
> 提取自: `blueprints/system/FIX_REGISTRY.md` (2026-06-23 上下文膨胀歼灭战)

---

| Debt ID | Date Registered | Severity | Module | Summary | Trigger Condition |
|---------|----------------|----------|--------|---------|-------------------|
| TECH_DEBT-001 | 2026-06-15 | L3 | runtime-live | **`_build_mia_close_entry(symbol="XAUUSDc")` 幽灵默认值**: 调用方 line 684 已显式传参，BTC 不受影响。但默认值本身是 DQAF-20260615-006 同一模式的架构残留。 | 新增调用方未传 symbol 参数 |
| TECH_DEBT-002 | 2026-06-15 | L2/L3 | runtime-live | **Journal 全量扫描 O(N) 性能炸弹**: MIA Dedup 2 每次刷盘全量读取 `live_trade_journal.jsonl` 做 ticket 去重。journal >10,000 行时单次扫描 100-500ms → 主循环卡顿 → MIA 超时雪崩。应引入内存 Journal Index `set(closed_tickets)` O(1) 替代 O(N)。 | journal 行数 > 10,000 |
| TECH_DEBT-003 | 2026-06-15 | L2 | runtime-live | **三层去重过度设计**: MIA Dedup 1+2+3 (内存 set + journal 扫描 + bridge 已写检测) 是历史补丁堆叠产物。应建立 SSOT 仓位状态机引擎统一去重入口。 | 下次大版本重构 MIA 管线 |
| TECH_DEBT-004 | 2026-06-15 | ~~L3~~ **RESOLVED** | features, brains | ~~**btc_macro_enhanced_37 schema 维度分裂**~~ → **2026-06-15 清偿**: V4/V9/V12 全部用 41 维特征重训练完毕。H1: 60,545 samples, LGB WR=88.85%. M15: 83,016 samples, XGB WR=49.14%. Registry 37→41, configs 37→41. 沙箱推理通过。Commit 8741e23。 | ✅ RESOLVED |
| TECH_DEBT-006 | 2026-08-03 | ~~L3~~ **RESOLVED** | runtime-live | ~~**MIN_ECONOMIC_VOLUME=0.02 XAU 定制全局硬编码 (解除 XAU 霸权)**~~ → **2026-08-03 清偿 (FIX-20260803-001)**: `StrategyLineConfig.min_economic_volume` + `resolved_min_economic_volume` property (显式配置优先, BTC→base_volume 0.01, 其他→2×lot_step 0.02); strategy_builder 20 策略线透传 + `_validate_min_economic_floors` 静态校验; strategy_evaluator 终态闸门 per-strategy floor; live_btc.yaml 6 BTC 策略线显式 0.01。XAU 现状保持 (0.02)。60 单测全绿。 | ✅ RESOLVED — FIX-20260803-001 (DQAF-20260803-001 CLOSED) |
| TECH_DEBT-007 | 2026-08-06 | L3 | runtime-live | **close label 三路语义分叉 (Option C — 单源统一, DQAF-20260806-001 IC 裁决 Deferred)**. FIX-20260806-001 (Option A 外科) 已将 trail-aware 契约接入 ACTIVE producer (adapter) 并恢复 MIA fallback, 但 label 决策逻辑仍三处独立: `position_close_adapter.py:_build_event` / `reconciliation.py:reconcile_closed_positions` / `mia_close.py:enrich_mia_from_deals` — 各自硬编码 DEAL_REASON 分支 (sl_hit_first/sl_hit_trailed/watchdog/managed/broker), 三路未来再次演进时仍会漂移 (同类 DQAF-20260708-003 deal 选择分叉前科). 应提取单一 `resolve_close_label(deal_reason, deal_comment, trail_active)` 纯函数为 SSOT, 三路共同消费. | 8/19 Flow46 决战结束后架构重构期清偿 (IC 裁决: 决战前禁止大范围单源重构) |
| TECH_DEBT-008 | 2026-08-06 | L3 | deployment-lifecycle | **红线冻结 mypy 债 (RED_LINE_FROZEN_ALLOWANCE, A2 冻结登记)**: 5 个 8/19 红线锁定文件存在统一检查类型错误 8 处 — `core/runtime/market_ingress.py`×2 (`_compute_atr_from_rates` Any\|None arg, 需 None-guard), `core/runtime/live_cycle.py`×1 (`DataHealthService()` LIGHT-mode 缺 base_dir/symbol, fail-open), `scripts/live_intent_loop.py`×3 (**真实签名漂移 bug**: LiveAlertHub 传 `log_dir`/`ding_webhook_url`, 实际签名 `base_dir`; `.fire()` 应为 `evaluate_and_dispatch()` — zombie-fuse 熔断告警被 try/except 吞掉从未送达, 8/19 后必修), `scripts/live_shadow_ensemble.py`×1 (cross_assets dict 不变性), `scripts/training/governance_scheduler.py`×1 (FIX-043 leak 转换 _jm typing). 冻结机制: `scripts/_mypy_scope.py` RED_LINE_FROZEN_ALLOWANCE (红线文件零触碰). | 8/19 Flow46 决战结束后清偿: 修根因 → 删 RED_LINE_FROZEN_ALLOWANCE 对应条目 → `python scripts/pre_commit_mypy.py --update-baseline` 重生成 (含 live_intent_loop zombie-fuse 告警 bug 修复) |
| TECH_DEBT-009 | 2026-08-06 | L3 | testing | **unified 模式潜伏测试债 (A3 显性化登记)**: `python -m mypy core/ apps/ scripts/ tests/` 统一检查显示 **236 类型错误 / 62 测试文件** — 这些错误在 isolated per-file 模式 (follow_imports=skip 抹除导入泛型) 下不可见, 故 baseline (isolated 语义) 从未登记; tests/ 不在 verify --full 统一检查域内 → 当前无门禁阻断, 纯潜伏. 主要形态: `**dict[str,X]` spread 与关键字参数不匹配 (test_position_ownership 已修 13), 冻结 dataclass 赋值 misc, 方法 mock 注入 method-assign, dict[str,Any]\|None 可索引性, strict_equality 非重叠比较, `**kwargs: object`→构造器 arg-type (test_meta_exit_engine 等). Top 文件: test_regime_gate 16 / test_mt5_broker_adapter 12 / test_runtime_execution_pipeline 9 / test_production_scenarios 9 / test_full_integration 9 / test_communication_replay_service 9. | 8/19 Flow46 决战结束后清偿 (A3 方法论复用): 逐文件 isolated+unified 双模式清理 → 若将 tests/ 纳入 verify --full 统一检查域前必须先清偿. 触发信号: `python -m mypy --no-error-summary core/ apps/ scripts/ tests/ \| grep -c "^tests"` |

---

## TECH_DEBT-001 Detail — MIA `symbol` 幽灵默认值

- **文件**: `core/runtime/live_cycle.py:2048`
- **现状**: `def _build_mia_close_entry(pos, known_entry, *, symbol: str = "XAUUSDc")`
- **调用方**: line 684 显式传 `symbol=config.symbol` — BTC 安全
- **修复**: 删除默认值，改为 `symbol: str` (必需参数)
- **关联**: DQAF-20260615-006/C1-C8 — 同一 L3 模式

## TECH_DEBT-002 Detail — Journal 全量扫描

- **文件**: `core/runtime/live_cycle.py:3788-3806`
- **现状**: 每次 MIA 刷盘 → `Path(jp).read_text()` → 逐行 `splitlines()` → 搜索 `"action": "close"` → 提取 ticket
- **场景**: 系统运行 6 个月, journal ~50,000 行 → 全量扫描 ~200ms → 主循环卡顿
- **修复方案**:
  1. 在 `LiveCycleState` 中增加 `_closed_tickets_cache: set[int]`
  2. 启动时从 journal 构建缓存
  3. journal 写入时同步更新缓存 (在 `record_mia_closes` 中 add)
  4. Dedup 2 改为 `ticket in state._closed_tickets_cache` — O(1)
- **关联**: 管道阶段 5 — Dedup 2

## TECH_DEBT-003 Detail — 三层去重过度设计

- **文件**: `core/runtime/live_cycle.py:3767-3813`
- **三层防御**:
  1. `_mia_processed_tickets` (session 级内存 set) — FIX-20260610-002
  2. journal 全量扫描去重 (检测 bridge 竞态) — FIX-20260612-024
  3. journal 行级 `action: close` 检测 — 同上
- **历史原因**: 每层都是独立事故后追加的补丁
- **修复方案**: 下一大版本建立统一的 `PositionStateMachine`，以 **`position_identifier` (不可变 MT5 POSITION_IDENTIFIER)** 为 SSOT key，所有状态变更 (open/close/MIA/modify) 通过状态机 → 去重是状态机内建属性而非外部防线
  - **⚠️ 键更正 (FIX-20260708-001 / DQAF-20260708-001)**: 本条目原写 "以 `position_ticket` 为 SSOT key" — **该键本身即缺陷**。`position_ticket` 是可变的 (MT5 在 partial-close/netting 时换号)，以其为生命周期 join 键会在每次换号时结构性制造孤儿平仓。正确 SSOT key 是不可变的 `position_identifier`。
  - **首个增量已交付 (FIX-20260708-001)**: `core/data/ticket_resolver.py::resolve_identity()` 建立单一不可变身份解析权威，读侧 join (JournalGate/auditor/reconciliation/label_builder) 已改 identity-keyed；写侧 PositionOpened 补发锚。完整 PositionStateMachine 仍 Deferred。
- **关联**: Iron Law #12 — 禁止补丁累积 (同模块 Deferred Architecture Fix >3 禁止继续补丁)

## TECH_DEBT-004 Detail — btc_macro_enhanced_37 Schema 维度分裂

- **文件**: `core/features/schemas/registry.py:47`, `configs/brains_btc/BTC_Swing_V*.json`
- **现状**:
  - Schema 定义 (`btc_macro_enhanced_schema.py`): **41 维** (FIX-B3-feat 新增 4 个 regime derivatives: TF_delta_OU, TF_delta_Hurst, TF_OU_x_Hurst, TF_OU_div_ADX)
  - 模型文件 (V4/V9/V12): **37 维** (从未用 41 维特征重训练)
  - Registry (`btc_macro_enhanced_37`): **回滚至 37** (战术撤退 — DQAF-20260615-009)
  - V12 config `n_features`: **回滚至 37** (之前被手动改为 41 但模型未重训)
- **DQAF-20260615-006/C5 教训**: C5 将 registry 修正为 41 → BrainFactory 双重校验拒绝全部 3 个 BTC 大脑 → ML 信号归零。回滚 registry 至 37 恢复了系统，但 schema(41) ≠ registry(37) 的矛盾被挂账。
- **修复路径**:
  1. 用 41 维特征 (`BTC_MACRO_ENHANCED_37_FEATURES` 全量) 重训练 V4, V9, V12
  2. 验证重训练模型 `num_feature() == 41`
  3. 运行一键恢复脚本: `python scripts/restore_btc_schema_41.py`
  4. 重启 BTC 进程验证 `brain_count: 3, brain_ids: [V4, V9, V12]`
- **一键恢复脚本**: `scripts/restore_btc_schema_41.py` (待模型重训练完成后执行)
  - 将 registry `btc_macro_enhanced_37`: 37 → 41
  - 将 V4/V9/V12 config `n_features`: 37 → 41
  - 将 V4/V9/V12 config `features` 列表扩充至 41 (追加 4 个 regime derivatives)
  - 执行前自动检查模型 `num_feature() == 41`，任一大脑不满足则拒绝执行
- **关联**: DQAF-20260615-006/C5, DQAF-20260615-009, FIX-B3-feat
- **补丁豁免 (PATCH_NOT_ARCHITECTURE)**: 回滚 registry 至 37 是 L1 补丁。L3 修复需重训练 3 个大脑模型 (41 维)。
- **✅ 2026-06-15 RESOLVED**: V4/V9/V12 全部用 41 维重训练 + 部署 + 沙箱推理通过 + 实盘 BTC 重启验证通过 (brain_count=3, 0 dimension_mismatch, 0 BrainFactory errors, brain predictions active). Commit 8741e23.

## TECH_DEBT-006 Detail — MIN_ECONOMIC_VOLUME XAU 定制全局硬编码

- **文件**: `core/runtime/strategy_evaluator.py:1141-1145`
- **现状**: `_MIN_ECONOMIC_VOLUME = 0.02` 全局常量 (FIX-20260730-010 Ω Phase 2), L1141 注释 "For XAU: lot_step=0.01, 2× lot_step = 0.02 minimum economic" — 全管线唯一 volume 下限
- **影响**: BTC config base_volume=0.01 → 标准手 0.01 结构性 < 0.02 → 任何降级因子 (GodsEye health `×max(0.25, health)`, session/regime mult) 使 volume 跌破下限 → 终态闸门击杀 → 08-03 双品种防护性零开单实证 (BTC 0.0033 / XAU 0.0066)。**量化铁证 (2026-08-02)**: BTC kelly 步进体积 0.01 × 健康 1.0 = 0.01 < 0.02 → 即使健康满分也永久封杀 = 结构性植物人状态, 非"健康分上去就能开单"
- **修复方案**: 下沉为 per-symbol / per-strategy-line 可配置 `min_economic_volume` (live.yaml strategy_configs), 默认按资产自身 lot_step/base_volume 派生; BTC 显式下限 0.01, XAU 保持 0.02; 附静态跨品种校验 (对每个 enabled 策略线断言 `base_volume × worst_case_factor ≥ min_economic(asset)` 或记录故意下限)
- **⚠️ 禁止全局降级**: IC 否决将 `_MIN_ECONOMIC_VOLUME` 全局改为 0.01 — 会拆掉 XAU 盈亏平衡地板, 让低胜率单被点差生吞
- **状态**: ✅ **RESOLVED 2026-08-03** — FIX-20260803-001 清偿 (60 单测全绿 + ruff clean + mypy baseline clean)
- **关联**: DQAF-20260803-001 (CLOSED), ReB-20260803-XAU_CENTRIC_HARDCODED_GLOBAL_THRESHOLD (RESOLVED), Iron Law #14 (per-symbol SSOT 同族)
