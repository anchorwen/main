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
- **修复方案**: 下一大版本建立统一的 `PositionStateMachine`，以 `position_ticket` 为 SSOT key，所有状态变更 (open/close/MIA/modify) 通过状态机 → 去重是状态机内建属性而非外部防线
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
