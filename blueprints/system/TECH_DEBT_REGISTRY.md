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
| TECH_DEBT-011 | 2026-08-08 | L2 | scripts (audit) | **DCI Auditor Calendar Awareness — 审计工具休市盲区 (The Calendar Blindspot)**: `scripts/audit_data_chain_integrity.py` 停滞阈值**固定 12h, 无市场日历感知** (Iron Law #11 全链体检发现, 2026-08-08). 每周六/休市跑 `--baseline-read` → 固定误报: XAU `S1_FEATURE_STALE`+`S4_GM_STALE` (forex_24_5 周五 20:54 UTC 收盘休市, 历史 12 周实证) 报 **退化 -5 BLOCKED**, BTC `S6_PRECHECK_STALE` (预检工作日-only 设计). 已证伪为假阳性 — 数据本身零损坏, 是监控工具的日历盲区. **若接入 CI 自动阻断 → 周末寸步难行 (盲区保安)**. | 8/19 Flow46 决战结束后清偿 (IC 裁决: 决战前零触碰审计工具, 维持手工 --baseline-read): 加市场日历感知 — 参考 `core/execution/pre_trade_guards.py:46-47` (forex_24_5/crypto_24_7), 休市期阈值放宽至最近收盘时间, 或 `--now` 锚定周五收盘. |
| TECH_DEBT-012 | 2026-08-08 | L3 | features | **Feature Writer 休市重写抑制 (The Phantom Ticks)**: BTC 域 `feature_store/records/symbol=XAUUSDc` 特征在休市期**每 ~4h 以冻结收盘值重复落盘** (值逐位一致, mt5_live source, 2026-08-08 实证 3 条重复记录). 上游 Feeder/Aggregator 边界问题 (时钟驱动周末特征计算 + last-value freeze). **已证伪数值污染** — 冗余数据无害, 无逻辑毒药. 风险仅在未来特征重算改"增量写入"时产生重复行. | 8/19 Flow46 决战结束后清偿 (纯防御): 特征写入侧加 `market_closed → 跳过落盘` 守卫 (或写入侧 last-value 指纹去重). 非必需, 低优先级. |
| TECH_DEBT-013 | 2026-08-11 | L3 | runtime-live | **休市期 intent 阻塞被 watchdog 误杀 (The 360s vs 300s 超时悖论)**: XAU 每日 21:00-22:00 UTC 休市 (纽约 17:00 收盘) 期间, intent `bar_sync` 等待新 M5 bar 阻塞 (**timeout=360s**), watchdog 硬杀超时 **300s** → 360 > 300 结构性必被杀 → 每交易日 **11-14 次进程硬杀重启** + 全量启动序列噪音 + 休市期 `JOURNAL_PNL_NULL_RATE_HIGH` 假告警. **零交易损失** (休市本不能交易), 纯状态机盲区. 全史 905 条击杀中 57% 集中于该窗 (21:00Z n=455 / 22:00Z n=64). 早前误标 "每日 1h 交易空窗 = 死锁退化" 已被用户质询+实证推翻 (2026-08-11 官方修正). 完整证据: `references/DQAF_MEMO_20260811_WATCHDOG_MARKET_SYNC.md`. | 8/19 Flow46 决战结束后清偿 (IC 投委会裁决): intent 识别 `market_closed` → 休市期优雅 idle; 或 bar_sync 超时 < watchdog 超时; 或 watchdog 休市豁免窗. 决战前**严禁触碰 Intent Loop / watchdog** 消除假警报. |
| TECH_DEBT-014 | 2026-08-11 | L2 | runtime-live | **非休市时段背景零星击杀 (低频偶发阻塞)**: 全史 **386/905 (43%)** 击杀散布 0-23h, 典型 1-3 次/日, 已被 launcher 自动重启吸收, 不构成阻断级威胁. 高峰段 17:00Z n=33 / 20:00Z n=39 / 11-12:00Z n=23-29. 另有**每日 12:15→13:00 逐日 +5min 漂移单杀** (连续 12 天精确 +5min/天后封顶 13:00, 来源未定). | 8/19 Flow46 决战结束后随 TECH_DEBT-013 一并排查: 逐条击杀时刻 × intent 阻塞点关联, 确认低频阻塞根因与漂移单杀来源. 低优先级. |
| TECH_DEBT-015 | 2026-08-11 | L2 | deployment | **launcher 停机无自动恢复 (运维空窗, DevOps Debt)**: 08-10 02:44 北京 外部 CTRL_C_EVENT (SIGINT) 广播停机双 launcher (DQAF-20260811-001), 因 launcher 是子进程 supervisor **自身无重启机制**, 停机后 **5.8h 无跟进重启** (历史 ~20 次 SIGINT 均 0.1-0.2min 内被拉起, 本次唯一例外). 系统行为正确 (优雅排空, 零数据损坏), 缺陷在运维恢复链. | 8/19 Flow46 决战结束后清偿: ① 运维纪律 — SIGINT 后 5min 内拉起双 launcher; ② launcher 级心跳 supervisor (独立 schtasks 探测 + 原子拉起, 需双开防护锁防止双实例). 决战前**零代码**. |
| TECH_DEBT-016 | 2026-08-11 | L2 | deployment | **one_click_supervisor 跨系统误匹配 (双开风险, DevOps Debt)**: `D:\cursor\scripts\one_click_supervisor.ps1` (常驻 PID 2544) 用 **`*scripts/live_intent_loop.py*` 全局通配** 识别 intent — 会误认 D:\future 的 intent 为其管理对象, 若其管理逻辑触发 → 与 D:\future launcher 双重拉起 → **intent 双实例风险**. 当前每品种单 intent 无重复 (未爆发), 属潜伏隐患. | 8/19 Flow46 决战结束后清偿: supervisor 匹配白名单化 (只匹配 D:\cursor 完整路径, 不跨 RepoRoot 匹配). 低优先级, 潜伏未爆发. |
| TECH_DEBT-017 | 2026-08-13 | L3 | runtime-live | **intent_loop 降级路径 UnboundLocalError 崩溃 (The Unbound Local)**: XAU intent_loop 8/11 00:31 → 8/13 00:45 共 **38 次** `intent exited with code 1` — MT5 not initialised → `positions_get` 抛 RuntimeError → `FaultTolerantContext [DEGRADE]` 降级路径访问未绑定局部变量 → `UnboundLocalError: '_positions'` (`live_cycle.py:1520`) + `'_EVENT_STREAM_MODE'` (`live_intent_loop.py:2732`). 每日 21:00-22:00Z 休市窗 11 连崩 (8/11、8/12 连续) + 启动竞态崩溃 (8/12 18:25 / 8/13 00:45). launcher 5-30s 自动恢复兜底, **零实盘交易损失** (8/12 首单 01:20-05:10 窗口引擎正常). 副作用: intent log 重启后句柄丢失 → 8/11 后双链 intent log 断流, 诊断线索黑盒化. | 8/19 Flow46 决战结束后与 TECH_DEBT-008/013 合并清偿: DEGRADE 降级路径变量初始化补齐 (入口 try 前绑定 `_positions`/`_EVENT_STREAM_MODE` 或重构降级分支) + 红线文件 mypy 债同批. 红线冻结前 **8/19 前零触碰**. |

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

## TECH_DEBT-011 Detail — DCI Auditor Calendar Awareness (审计工具休市盲区)

- **文件**: `scripts/audit_data_chain_integrity.py` (停滞阈值固定 12h, 无市场日历感知)
- **现状**: 2026-08-08 (周六) 全数据链体检实证 — `--baseline-read` 报 XAU 指数 92→87 **-5 退化 BLOCKED** (`S1_FEATURE_STALE` + `S4_GM_STALE`) + BTC `S6_PRECHECK_STALE`. 全部为周末/休市假阳性:
  - XAUUSDc = `forex_24_5` (周五 20:54 UTC 收盘, 周末零交易, 历史 12 周实证) — [core/execution/pre_trade_guards.py:46-47](core/execution/pre_trade_guards.py#L46-L47)
  - 每日预检 = 工作日-only (周一至五 04:03, "weekend excluded") — [scripts/daily_flow46_precheck.py:3](scripts/daily_flow46_precheck.py#L3)
- **影响**: 数据零损坏, 纯工具盲区. 若接入 CI 自动阻断 → 每个周末误触发, 监控失信.
- **修复方案** (8/19 后): 市场日历感知 — 按资产日历类型 (forex_24_5/crypto_24_7) 计算最近有效收盘时间, 休市期阈值放宽锚定收盘; 或 `--now` 参数锚定周五收盘基准.
- **关联**: Iron Law #11 (脚本 stdout 唯一合法证据), 2026-08-08 全链体检报告 (S1/S4/S6 假阳性已证伪)

## TECH_DEBT-012 Detail — Feature Writer 休市重写抑制 (跨品种特征重复落盘)

- **现状**: BTC 域 `data_btc/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl` 休市期 (08-08 周六 01:14/05:20/09:26) 3 条记录值**逐位一致** (M5_Ret_1=0.027181, M5_Price_ZScore=-0.167581, 与 08-07 21:08 收盘相同) — 时钟驱动周末特征计算 + last-value freeze.
- **定性**: 上游 Feeder/Aggregator 边界问题; 冗余数据无害, 无逻辑毒药 (数值无变化, 不制造假信号). 已证伪跨资产污染.
- **风险**: 仅未来特征重算改"增量写入/事件驱动"时会产生重复行污染. 当前 append 重复值语义安全.
- **修复方案** (8/19 后, 纯防御): 特征写入侧 `market_closed → 跳过落盘` 守卫; 或 last-value 指纹 (hash 逐位一致 → 跳过) 幂等去重.
- **关联**: 2026-08-08 全链体检报告 (隐患 ②), pre_trade_guards.py 市场日历 (同 TECH_DEBT-011)

## TECH_DEBT-013 Detail — 休市期 intent 阻塞被 watchdog 误杀 (The 360s vs 300s 超时悖论)

- **实证链**: `references/DQAF_MEMO_20260811_WATCHDOG_MARKET_SYNC.md` (完整 4 路对证) + `scripts/_probe_market_break_vs_watchdog_20260811.py` (Untracked 探针).
- **市场日历**: XAUUSDc (`forex_24_5`) 每日 21:00-22:00 UTC 休市 (纽约 17:00 收盘, 夏令时), 周五 22:00 UTC 收市至周一. BTC (`crypto_24_7`) 无日休 — 对照组柱密度证实.
- **证据链** (Iron Law #11 脚本 stdout):
  - M5 柱: 07-29/07-30 工作日 21:00-21:55 零柱, 22:00 恢复; 周五 22:00+ 零柱; BTC 同窗全程有柱.
  - 击杀: 全史 905 条, 21:00Z n=455 (50%) + 22:00Z n=64 = 57%; 每交易日 21:00-21:54 恰 11 连杀, 间隔 ~5.5min.
  - intent 日志: 21:00:17/21:05:42 全量重启序列 (`bar_sync_initialized timeout_seconds: 360.0` → 阻塞 → watchdog elapsed≈300-308s 硬杀 → launcher 重启).
- **根因**: `bar_sync` 等待超时 360s > watchdog 硬杀超时 300s → 休市期 bar_sync 必被 watchdog 先杀, intent 永远无法自行优雅超时. 状态机缺 `market_closed` 态.
- **代价**: 11-14 次/日进程硬杀重启 + 启动噪音 + 休市期 `JOURNAL_PNL_NULL_RATE_HIGH` 假告警 (08-07T21:03 健康报告 `trade_journal: fail, pnl_null_rate 0.91`). **零交易损失** (休市本不能交易).
- **修复方案** (8/19 后, 首选→备选):
  1. intent 检测 `market_closed` (复用 `core/execution/pre_trade_guards.py` 市场日历) → 休市期跳过 bar_sync 等待, 低功耗 idle, 不触发 watchdog.
  2. bar_sync 超时降至 watchdog 之下 (结构对齐, 让 intent 自行优雅超时).
  3. watchdog 加 `market_closed` 豁免窗 (休市期不杀, 重开后复位).
- **纪律**: IC 裁决决战前**严禁触碰 Intent Loop / watchdog** 消除假警报; 仅 8/19 后清偿.
- **关联**: DQAF-20260811-001 (Sev 2), TECH_DEBT-011 (同族市场日历盲区), ReB 候选 `WATCHDOG_MARKET_BREAK_MISKILL`

## TECH_DEBT-014 Detail — 非休市时段背景零星击杀 (低频偶发阻塞)

- **范围**: 386/905 (43%) 击杀散布 0-23h; 典型 1-3 次/日; launcher 自动重启已吸收, 无停机.
- **高峰段**: 17:00Z n=33, 20:00Z n=39, 11-12:00Z n=23-29 — 需逐条关联 intent 阻塞点定性.
- **漂移单杀**: 每交易日单次击杀时刻 12:15→13:00 逐日 +5min (07-29 12:15 → 08-08 12:59 → 08-09/10 13:00 封顶), 连续 12 天精确 +5min/天 — 来源未定, 可能与某每日任务/数据刷新碰撞.
- **修复方案** (8/19 后, 低优先级): 逐条击杀时刻 × intent 日志关联, 确认是否同源 (MT5 IPC 偶发) 或独立缺陷.
- **关联**: TECH_DEBT-013 (同 watchdog 域)

## TECH_DEBT-015 Detail — launcher 停机无自动恢复 (运维空窗)

- **事件**: 2026-08-10T18:44:11.113288Z (02:44 北京) 双 launcher 收到外部 CTRL_C_EVENT (SIGINT), 优雅停机 (DQAF-20260811-001). 用户 01:44 入睡 → 排除人为; 系统事件/调度任务/launcher 自重启/内部 watchdog/仓库停机脚本 全部证伪 → 信号源未 100% 锁定 (最可能: 后台 agent/工具停机-重启循环的停机半步).
- **异常点**: 历史 ~20 次 SIGINT 停机均 0.1-0.2min 内被拉起 (重启生效节奏); 本次 **5.8h 无跟进重启** — 唯一差异 = 运维恢复链空窗. launcher 设计为子进程 supervisor, **自身无重启机制**.
- **修复方案** (8/19 后):
  1. 运维纪律: 任何 SIGINT 后 5min 内拉起双 launcher (或记录"故意停机").
  2. launcher 级心跳 supervisor: 独立 schtasks 探针 (5min) + 原子拉起, **必须双开防护锁** (防止探针与手动重启竞态双实例).
- **关联**: DQAF-20260811-001 (Sev 2), ReB 候选 `EXTERNAL_SIGINT_NO_FOLLOWUP_RESTART`

## TECH_DEBT-016 Detail — one_click_supervisor 跨系统误匹配 (双开风险)

- **现状**: `D:\cursor\scripts\one_click_supervisor.ps1` (常驻 PID 2544) 以 `*scripts/live_intent_loop.py*` **全局通配** 识别 intent — D:\future 的 intent 进程命令行同样匹配.
- **风险**: 若该 supervisor 检测到 D:\future intent 死亡并尝试拉起 → 与 D:\future launcher 双重拉起 → **双实例 intent**. 当前每品种单 intent (XAU 12000 / BTC 13996) 无重复 = 未爆发, 纯潜伏.
- **修复方案** (8/19 后, 低优先级): supervisor 匹配白名单化 — 只匹配 `D:\cursor\scripts\live_intent_loop.py` 完整路径, 不跨 RepoRoot 匹配.
- **关联**: DQAF-20260811-001 (启动健康核查环节发现)

## TECH_DEBT-017 Detail — intent_loop 降级路径 UnboundLocalError 崩溃 (The Unbound Local)

- **现象**: XAU intent_loop 8/11 00:31 → 8/13 00:45 共 **38 次** `[launcher] intent exited with code 1`; launcher 每轮自动 respawn (5s→30s 递增, restart 1/50→11/50 多轮循环).
- **完整 traceback** (`data/logs/live_launcher_20260811T003149Z.log` L66455-66472):
  ```
  [intent] ERROR:core.runtime.fault_handler:FaultTolerantContext [DEGRADE] component=MT5_IPC:positions_get:startup_reconciliation error=RuntimeError: MT5 not initialised (command=positions_get)
  [intent] {"event": "fatal_error", ... "type": "<class 'UnboundLocalError'>", "message": "cannot access local variable '_EVENT_STREAM_MODE'..."}
  [intent]   File "D:\future\scripts\live_intent_loop.py", line 2319, in main
  [intent]   File "D:\future\core\runtime\live_cycle.py", line 1520, in execute_live_cycle
  [intent]     _open_tickets = {p.ticket for p in _positions}
  [intent] UnboundLocalError: cannot access local variable '_positions' where it is not associated with a value
  [intent]   File "D:\future\scripts\live_intent_loop.py", line 2732, in main
  [intent]     if not _EVENT_STREAM_MODE:
  [intent] UnboundLocalError: cannot access local variable '_EVENT_STREAM_MODE' where it is not associated with a value
  ```
- **根因**: **L3 架构缺陷** — `FaultTolerantContext [DEGRADE]` 路径 (MT5 IPC 未初始化 → `positions_get` 抛 `RuntimeError`) 访问未绑定局部变量 `_positions`/`_EVENT_STREAM_MODE` → 降级分支崩溃. 降级处理缺变量初始化兜底.
- **触发规律**: ① 每日 21:00-22:00Z 休市窗 11 连崩 (8/11、8/12 连续, 与 TECH_DEBT-013 watchdog 击杀窗重叠); ② 启动竞态 (8/12 18:25 MT5 未初始化、8/13 00:45 启动).
- **代价**: 决策循环中断, 但 `execution_state` 周期保存 + launcher 5-30s 恢复 + 重启后 journal 引导 `known_open_tickets` → **服务连续性保持** (8/12 首单 SHORT m30 @4391 01:20-05:10 完整闭环, 引擎正常). **零实盘交易损失**. 副作用: intent log 重启后文件句柄丢失 → 8/11 后双链 intent log 停写 → 诊断线索断流 (黑盒观测, 仅依赖 execution_state + journal).
- **红线**: `core/runtime/live_cycle.py` + `scripts/live_intent_loop.py` 均属 **RED_LINE_FROZEN_ALLOWANCE** (TECH_DEBT-008) — **8/19 前零触碰** (IC 雷霆裁决, 2026-08-13: 为修休市报错改核心 live_cycle 极度违背红线纪律).
- **修复方案** (8/19 后, 与 TECH_DEBT-008/013 合并): ① DEGRADE 降级路径变量初始化补齐 (入口 try 前绑定 `_positions = []` / `_EVENT_STREAM_MODE` 默认值, 或重构降级分支为独立函数); ② 同批清偿红线文件 mypy 债 (TECH_DEBT-008); ③ 与休市市场日历适配合并 (TECH_DEBT-013).
- **关联**: TECH_DEBT-008 (红线冻结同文件域), TECH_DEBT-013 (休市窗叠加), TECH_DEBT-014 (背景击杀), ReB 候选 `INTENT_DEGRADE_UNBOUND_LOCAL_CRASH`
