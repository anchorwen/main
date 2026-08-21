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
| TECH_DEBT-007 | 2026-08-06 | ~~L3~~ **RESOLVED** | runtime-live | **close label 五生产者语义分叉 (Option C — 单源统一, DQAF-20260806-001 IC 裁决 Deferred)**. FIX-20260806-001 (Option A 外科) 已将 trail-aware 契约接入 ACTIVE producer (adapter) 并恢复 MIA fallback, 但 label 决策逻辑仍多处独立: `position_close_adapter.py:_build_event` / `reconciliation.py:reconcile_closed_positions` / `mia_close.py:enrich_mia_from_deals` / `settlement_queue.py:_handle_settled` / `mt5_bridge_worker.py:_derive_label` — 各自硬编码 DEAL_REASON 分支 (sl_hit_first/sl_hit_trailed/watchdog/managed/broker) + 三套 watchdog shortcode + None-reason 伪造 + settlement_queue sl_hit_first 硬编码 (trail 盲点复活), 多路演进必再漂移 (同类 DQAF-20260708-003 deal 选择分叉前科). 应提取单一 `resolve_close_label(deal_reason, deal_comment, trail_active)` 纯函数为 SSOT, 全部 deal-informed producer 共同消费. | ✅ RESOLVED — FIX-20260821-002 (DQAF-20260821-001 CLOSED, IC 三项裁决): `core/runtime/close_label.py` SSOT leaf + 五生产者全收敛 + trail_advances 透传 6/6 enqueue + 回归锁 46 + 全量 5225 passed |
| TECH_DEBT-008 | 2026-08-06 | ~~L3~~ **RESOLVED** | deployment-lifecycle | **红线冻结 mypy 债 (RED_LINE_FROZEN_ALLOWANCE, A2 冻结登记)**: 5 个 8/19 红线锁定文件存在统一检查类型错误 8 处 — `core/runtime/market_ingress.py`×2 (`_compute_atr_from_rates` Any\|None arg, 需 None-guard), `core/runtime/live_cycle.py`×1 (`DataHealthService()` LIGHT-mode 缺 base_dir/symbol, fail-open), `scripts/live_intent_loop.py`×3 (**真实签名漂移 bug**: LiveAlertHub 传 `log_dir`/`ding_webhook_url`, 实际签名 `base_dir`; `.fire()` 应为 `evaluate_and_dispatch()` — zombie-fuse 熔断告警被 try/except 吞掉从未送达, 8/19 后必修), `scripts/live_shadow_ensemble.py`×1 (cross_assets dict 不变性), `scripts/training/governance_scheduler.py`×1 (FIX-043 leak 转换 _jm typing). 冻结机制: `scripts/_mypy_scope.py` RED_LINE_FROZEN_ALLOWANCE (红线文件零触碰). | ✅ **已清偿 FIX-20260819-003 (8/19)**: 8 处根因全清 (mypy unified EXIT=0) — 核心 **live_intent_loop zombie-fuse 告警静默 bug 根治** (LiveAlertHub 构造签名漂移 `log_dir`/`ding_webhook_url` → `base_dir`/`symbol`/`dingtalk_url`/`dingtalk_secret` + 方法漂移 `.fire()` → `.send_critical(reason, detail)` + `state._alert_hub` 恒 None → TypeError/AttributeError 被 BLE001:FOG 吞掉, 熔断信号从未送达; 修复复用 main `alert_hub` (args.alert=True 带真实 webhook) 或 fallback 构造 → 告警送达钉钉/落盘不再全静默). 其余 7 处: market_ingress 签名补 None 声明 ×2 / live_cycle 调用处 list() 转换 + DataHealthService 补 base_dir/symbol/mode=light ×2 / shadow_ensemble cross_assets 去 str ×1 / governance_scheduler `_jm` 变量名复用 ×1. **RED_LINE_FROZEN_ALLOWANCE 全数删除** (空 dict) + baseline 重生成 (仅剩 test_tech_debt_010 隔离模式 unused-ignore 伪差 1, 非新债). 回归锁 3 (test_tech_debt_008_alert_hub_contract) + 针对性回归 296 全绿. |
| TECH_DEBT-009 | 2026-08-06 | ~~L3~~ **RESOLVED** | testing | **unified 模式潜伏测试债 (A3 显性化登记)**: `python -m mypy core/ apps/ scripts/ tests/` 统一检查显示 **236 类型错误 / 62 测试文件** — 这些错误在 isolated per-file 模式 (follow_imports=skip 抹除导入泛型) 下不可见, 故 baseline (isolated 语义) 从未登记; tests/ 不在 verify --full 统一检查域内 → 当前无门禁阻断, 纯潜伏. 主要形态: `**dict[str,X]` spread 与关键字参数不匹配 (test_position_ownership 已修 13), 冻结 dataclass 赋值 misc, 方法 mock 注入 method-assign, dict[str,Any]\|None 可索引性, strict_equality 非重叠比较, `**kwargs: object`→构造器 arg-type (test_meta_exit_engine 等). Top 文件: test_regime_gate 16 / test_mt5_broker_adapter 12 / test_runtime_execution_pipeline 9 / test_production_scenarios 9 / test_full_integration 9 / test_communication_replay_service 9. | ✅ **已清偿 FIX-20260819-005 (8/19, 清偿序列末块)**: 236 → 0 (tests/ 235 + core/execution/strategy_line.py 1). 五批分治: Batch1 strategy_line+unit+runtime+protocol (23) / Batch2 execution (40) / Batch3-4 engine (141) / Batch5 features+data+contracts+resilience+backtest+training (35). 统一模式: **cast-not-ignore** (warn_unused_ignores=true 下 type:ignore 双模式互斥 → cast(...) 纯类型层 no-op 双模式干净) + assert 契约收窄. tests/ 零运行时行为变化 (逐测试回归 + 全量 pytest). 残留 179 全在 scripts/archive+_audit forensic 豁免域 (hash-lock). 触发信号现返回 0. |
| TECH_DEBT-010 | 2026-08-06 | ~~L3~~ **RESOLVED** | runtime-live | **影子风暴 ZMQ 隔离缺陷 + 跨域串台 (The Storm Forensics)**: v9 shadow 容器检测到 live.yaml `mt5_zmq` 时非 `--live-dispatch` 短路隔离不彻底 (FIX-20260806-006 已上, 但**根因未除**) → 批量/回测/阴影进程连真 ZMQ 桥 → rejected 风暴. **🔴 复发实锤 (8/19 解封法证)**: 8/6 裁决后仍复发 3 次 (8/6 10:28Z 47 + 8/7 03:51Z 47 + 8/7 14:30Z 46 = 140 条); 全月风暴特征 (symbol=XAUUSD 缺c + `message_` 前缀 + magic=null) 281 条. **跨域串台变种**: BTC `modify_sltp` rejected 10 条 (magic 90460/90430) 混入主 journal (data/), BTC 命令跨域写入 XAU 账本. **零成交污染** (golden_master = 0). | ✅ **已清偿 FIX-20260819-002 (8/19)**: **A** Shadow Veto — bootstrap_v9 遇生产网络适配器 (mt5_zmq 等) → DataIntegrityError 硬宕机, 杜绝批量/回测连真桥; **B** Journal Firewall — mt5_bridge_worker 唯一写盘 chokepoint 前域判定 (XAU↔XAUUSD(c)/BTC↔BTCUSDc), 跨域记录写 cross_domain_warnings.jsonl 绝不进 SSOT; **C** Death of Defaults — service_container/live_launcher 无 endpoint fail-fast (废除 5556 兜底), zmq_adapter 构造必传 endpoint, 全 dispatch 调用面 (modify+7 close 点) 补 per-symbol endpoint; **D** 风险敞口核验 — 227 条被拒 BTC Trail 请求 → 213 PAIRED (双路径重复下发) + 11 trail-covered + **3 TRUE EXPOSURE** (短命微仓 4287199887/4331501668/4335847505 ≈-0.6~-1.8 USD, MT5 SL/TP 兜底). 回归锁 20 测试. |
| TECH_DEBT-011 | 2026-08-08 | ~~L2~~ **RESOLVED** | scripts (audit) | **DCI Auditor Calendar Awareness — 审计工具休市盲区 (The Calendar Blindspot)**: `scripts/audit_data_chain_integrity.py` 停滞阈值**固定 12h, 无市场日历感知** (Iron Law #11 全链体检发现, 2026-08-08). 每周六/休市跑 `--baseline-read` → 固定误报: XAU `S1_FEATURE_STALE`+`S4_GM_STALE` (forex_24_5 周五 20:54 UTC 收盘休市, 历史 12 周实证) 报 **退化 -5 BLOCKED**, BTC `S6_PRECHECK_STALE` (预检工作日-only 设计). 已证伪为假阳性 — 数据本身零损坏, 是监控工具的日历盲区. **若接入 CI 自动阻断 → 周末寸步难行 (盲区保安)**. | ✅ **已清偿 FIX-20260821-001 (8/21, DQAF-20260820-005 IC 全面批准 The Calendar Batch)**: `core/market/calendar.py` 网格 API (MARKET_TYPE_PRESETS forex_24_5/crypto_24_7 + staleness_anchor 单点时钟) — audit_data_chain_integrity.py 7 处硬编码停滞阈值全收敛 (S3 dormant 24h 保留防反转) + health_checks POST_OUTAGE 1440min 一并替换. XAU 周六 grade 🟢92 stale_faults=[] / BTC 84 仅 S3 零回归. 回归锁 58 测试. |
| TECH_DEBT-012 | 2026-08-08 | ~~L3~~ **RESOLVED** | features | **Feature Writer 休市重写抑制 (The Phantom Ticks)**: BTC 域 `feature_store/records/symbol=XAUUSDc` 特征在休市期**每 ~4h 以冻结收盘值重复落盘** (值逐位一致, mt5_live source, 2026-08-08 实证 3 条重复记录). 上游 Feeder/Aggregator 边界问题 (时钟驱动周末特征计算 + last-value freeze). **已证伪数值污染** — 冗余数据无害, 无逻辑毒药. 风险仅在未来特征重算改"增量写入"时产生重复行. | ✅ **已清偿 FIX-20260821-001 (8/21, DQAF-20260820-005 IC 全面批准)**: `local_feature_store.write_records` 尾行指纹去重 (canonical JSON 逐位对比排除 `ingested_at`), last-value freeze 重复落盘跳过; `compact()` 失效尾缓存防数据丢失; **零触碰 live_cycle**. 回归锁 7 测试. |
| TECH_DEBT-013 | 2026-08-11 | ~~L3~~ **RESOLVED** | runtime-live | **休市期 intent 阻塞被 watchdog 误杀 (The 360s vs 300s 超时悖论)**: XAU 每日 21:00-22:00 UTC 休市 (纽约 17:00 收盘) 期间, intent `bar_sync` 等待新 M5 bar 阻塞 (**timeout=360s**), watchdog 硬杀超时 **300s** → 360 > 300 结构性必被杀 → 每交易日 **11-14 次进程硬杀重启** + 全量启动序列噪音 + 休市期 `JOURNAL_PNL_NULL_RATE_HIGH` 假告警. **零交易损失** (休市本不能交易), 纯状态机盲区. 全史 905 条击杀中 57% 集中于该窗 (21:00Z n=455 / 22:00Z n=64). 早前误标 "每日 1h 交易空窗 = 死锁退化" 已被用户质询+实证推翻 (2026-08-11 官方修正). 完整证据: `references/DQAF_MEMO_20260811_WATCHDOG_MARKET_SYNC.md`. | ✅ **已清偿 FIX-20260820-001 (8/20, DQAF-20260820-001 CLOSED)**: **heartbeat_refresh 心跳脉冲** (BarSyncPoller 等待期刷新 `state.last_heartbeat`) + **degraded deadline 结构对齐**. M5 悖论 (bar 周期 300s == watchdog 阈值 300s) → 纯超时压缩会提前降级破坏正常交易 → **脉冲是正解**: watchdog 全程见 "alive", 休市等待永不被误判死锁. 无脉冲时 degraded 封顶 270s<300s (防误接线硬杀). `bar_sync_timeout` 360→240 双路对齐 (live.yaml+live_btc.yaml). 回归锁 8 (休市阻塞期 + BTC crypto_24_7 对照组 + degraded cap + pulse 失败容错 + 配置对齐). ReB: MARKET_CLOSED_BLOCK_MISCLASSIFIED_AS_DEADLOCK. |
| TECH_DEBT-014 | 2026-08-11 | L2 | runtime-live | **非休市时段背景零星击杀 (低频偶发阻塞)**: 全史 **386/905 (43%)** 击杀散布 0-23h, 典型 1-3 次/日, 已被 launcher 自动重启吸收, 不构成阻断级威胁. 高峰段 17:00Z n=33 / 20:00Z n=39 / 11-12:00Z n=23-29. 另有**每日 12:15→13:00 逐日 +5min 漂移单杀** (连续 12 天精确 +5min/天后封顶 13:00, 来源未定). | 8/19 Flow46 决战结束后随 TECH_DEBT-013 一并排查: 逐条击杀时刻 × intent 阻塞点关联, 确认低频阻塞根因与漂移单杀来源. 低优先级. |
| TECH_DEBT-015 | 2026-08-11 | ~~L2~~ **RESOLVED** | deployment | **launcher 停机无自动恢复 (运维空窗, DevOps Debt)**: 08-10 02:44 北京 外部 CTRL_C_EVENT (SIGINT) 广播停机双 launcher (DQAF-20260811-001), 因 launcher 是子进程 supervisor **自身无重启机制**, 停机后 **5.8h 无跟进重启** (历史 ~20 次 SIGINT 均 0.1-0.2min 内被拉起, 本次唯一例外). 系统行为正确 (优雅排空, 零数据损坏), 缺陷在运维恢复链. | ✅ **已清偿 FIX-20260821-004 (8/21, P9, 投委会 8/18 战术推进令 + 孤儿策略裁决 fail-safe)**: `scripts/launcher_supervisor.py` 独立心跳探针 (schtasks `QuantOS_Launcher_Guard` 5min) + 三态机 (HEALTHY 无动作 / DEGRADED 只告警不杀不重启 / RECOVERY 原子拉起) + **双开防护三重防线** (O_EXCL 锁 + 锁内重查 + 降级态永不叠 hub). 目标细化: 真正失活的是 **HUB `main.py live`** (hub 自身已负责重启 launcher; 8/10 空窗 = hub 被杀无 restarter), 故监控对象 = hub. TDD 40 测试 + 实盘干跑 HEALTHY hub=16004 launcher=[15368, 8776] 吻合. 原方案①运维纪律保留, ②launcher 级心跳 supervisor → hub 级心跳 supervisor (机制同源, 监控对象更精确). |
| TECH_DEBT-016 | 2026-08-11 | ~~L2~~ **RESOLVED** | deployment | **one_click_supervisor 跨系统误匹配 (双开风险, DevOps Debt)**: `D:\cursor\scripts\one_click_supervisor.ps1` 用 **`*scripts/live_intent_loop.py*` 全局通配** 识别 intent — 会误认 D:\future 的 intent 为其管理对象, 若其管理逻辑触发 → 与 D:\future launcher 双重拉起 → **intent 双实例风险**. 8/21 实测该误匹配**已在实盘显形**: supervisor (PID 2864) 将 D:\future XAU intent (PID 11804) 误认为自己的 → attach 模式 → 自己的 intent 停机不自知. | ✅ **RESOLVED — FIX-20260821-003 (2026-08-21, P8, D:\cursor 已退役 IC 裁决)**: (1) `Get-LiveIntentProcess` 白名单化 — `*$RepoRoot\scripts\live_intent_loop.py*` 完整路径匹配 (D:\future 绝对路径 `D:\future\scripts\...` 天然排除, 实测 4/4 PASS); (2) spawn 改 `Join-Path $RepoRoot` 绝对路径自标识 (注册档案"只匹配完整路径"按字面改会失灵 — 原 spawn 为相对路径, CommandLine 无盘符); (3) **僵尸 supervisor PID 2864 停止** (IC 裁决: D:\cursor 管线 4/30 起休眠已退役; 无计划任务拉起, 停止持久). D:\cursor 本地 commit 3f3c85d (单文件, 不 push 共享 main — D:\cursor HEAD 停在 4/30 历史分歧). |
| TECH_DEBT-017 | 2026-08-13 | L3 | runtime-live | **intent_loop 降级路径 UnboundLocalError 崩溃 (The Unbound Local)**: XAU intent_loop 8/11 00:31 → 8/13 00:45 共 **38 次** `intent exited with code 1` — MT5 not initialised → `positions_get` 抛 RuntimeError → `FaultTolerantContext [DEGRADE]` 降级路径访问未绑定局部变量 → `UnboundLocalError: '_positions'` (`live_cycle.py:1520`) + `'_EVENT_STREAM_MODE'` (`live_intent_loop.py:2732`). 每日 21:00-22:00Z 休市窗 11 连崩 (8/11、8/12 连续) + 启动竞态崩溃 (8/12 18:25 / 8/13 00:45). launcher 5-30s 自动恢复兜底, **零实盘交易损失** (8/12 首单 01:20-05:10 窗口引擎正常). 副作用: intent log 重启后句柄丢失 → 8/11 后双链 intent log 断流, 诊断线索黑盒化. | 8/19 Flow46 决战结束后与 TECH_DEBT-008/013 合并清偿: DEGRADE 降级路径变量初始化补齐 (入口 try 前绑定 `_positions`/`_EVENT_STREAM_MODE` 或重构降级分支) + 红线文件 mypy 债同批. 红线冻结前 **8/19 前零触碰**. |
| TECH_DEBT-018 | 2026-08-13 | L3 | observability | **`META_FILTER_WIRED_STALE` 假阳性 — 崩溃循环下 intent log 停写盲区 (The Silent Monad)**: `check_meta_filter_state` (health_checks.py:394-486) head-read 最新 2 个 `intent_*.log` 前 64KB 定位 `meta_pipeline_wired` 事件, wired_age > 360min → WARN. TECH_DEBT-017 崩溃循环 (8/11 00:31 → 8/13 00:45) 致 XAU intent log 8/11 08:32 后**停写** (stdout 落 launcher log `[intent]` 行, 不轮换新 intent_*.log) → health check 读到 **8/11 00:31:59Z 陈旧事件** → wired_age=2808min → 每日假 WARN (2026-08-12T23:19:59Z 钉钉实测). **MetaFilter 实际健康**: 当前进程 PID 18052 于 8/13 00:45:12.684Z `meta_pipeline_wired` 成功 (lgb_loaded=true, micro_scaler_loaded=true, dims=40) + long/short 塔加载 + meta_filter_gate_init (conformal_warm). 8/12 21:11-21:55 休市 11 连崩每次重启也均成功 wired. 非 MetaFilter 故障, 纯监控工具盲区. | 8/19 Flow46 决战结束后随 TECH_DEBT-017 清偿: health check 崩溃循环下回退读 launcher log `[intent]` 行 (wire 时间戳可跨崩溃恢复) 或引入 `meta_pipeline_wired` 独立持久化 SSOT 事件文件. 决战前**零代码**. |
| TECH_DEBT-019 | 2026-08-17 | L3 | execution-orders | **TP/SL 动态追踪解耦 RR 坍缩 (The Decoupled Bracket)**: `compute_trail_tp` (FIX-20260713-008) 在 ATR 收缩 ≤0.80×entry_atr 时把 TP 向内收窄且**只缩不放**，但缩窄下限与 SL 距离/RR **零耦合** — TP Floor (`tp_min_distance_atr×bracket_atr`) 用 `max()` 语义仅防"太激进(太远)"不保"RR≥1"；Proximity Gate 仅防末程移动。8/17 实证 (DQAF-20260817-001): h1_swing RR 1.73→**0.527** / m15_swing RR 0.98→**0.385** (策略 `min_rr_ratio=0.85` 均跌破), SL 全生命周期未动而盈利曾达 R=+2.108 → 止盈空间<止损空间负期望结构. FIX-20260709-004 曾修同类 RR 1.66→0.08, 仅堵 candidate 距离未堵 RR 耦合缺口 → 复发. | ✅ **已清偿 FIX-20260819-001 (8/19, IC ①②③ 全量上线)**: ①RR 硬底线 `compute_rr_floor_price` (entry 参照系, 单收敛点) — compute_trail_tp 收紧分支 + trail_dispatch 下发前 RR Guard 双保险; ②SL_Volatility_Trail 波动率对称收紧 (atr_ratio≤0.80 同步收紧 SL, 鼠轮/max_lock/min_step 守卫); ③弹性恢复 (atr_ratio≥0.85 外向复原至 initial_tp, 0.80-0.85 迟滞带防双向振荡, Proximity 70% 共享). `tp_min_rr_ratio` 经 TrailPolicy 单点注入 (position_registration ← sl.min_rr_ratio), save_state v3 持久化跨重启. min_rr=0 → 逐分支零变化 (structural_swing_v1 兼容). 回归锁 580+111. |
| TECH_DEBT-020 | 2026-08-20 | ~~L2~~ **RESOLVED** | scripts (data pipeline) | **training_readiness 命中空/损坏 npz → EOFError (The Empty NPZ)**: `check_training_readiness.py:722` `np.load(_npz_path, allow_pickle=True)` 对 `training_pipeline_xau_metafilter_v1` 的 stage-3 npz (空/损坏) 抛 `EOFError: No data left in file` — **每 XAU daily_ops 运行确定性命中** (DQAF-20260820-004 取证, 与并发无关). 被 fail_open_guard 捕获 → 管线继续 (errors=0), **非阻断**; 但特征工程产物损坏 → 训练就绪评估数据缺失. 次生影响: 该 traceback 由 `logging.exception` (last-resort handler) 例行写入 stderr → 曾污染 FIX-20260820-003 launcher 成功谓词信号 (stderr Traceback 非崩溃特异性). | ✅ **已清偿 FIX-20260821-006 (8/21, Phase 3 P1 首优 The Vanguard, IC 三位一体防御裁决)** — 根因修正: **写入侧健康** (XAU feature store M5 43,580 记录), 真因=契约缺 `builder_args` → builder 默认 symbol=BTCUSDc 在 data/ 空转 → 静默早退 rc=0 → validator 预建空 NamedTemporaryFile → np.load EOFError. 三腿: ① 契约补 `builder_args` (`training_pipeline_xau_metafilter_v1.json`, --symbol XAUUSDc) ② Builder 静默早退→`_fail()` 非零退出 (build_btc_metafilter_v2_dataset.py) ③ Reader `np.load` 捕获 (EOFError,ValueError,OSError,pickle.UnpicklingError,zipfile.BadZipFile)+空文件守卫 → FAIL verdict 非 traceback. 回归锁 7 (tests/scripts/test_training_readiness_xau_metafilter.py). 实盘复现: EOFError 消除, builder 真实产出 1046 样本 40 维 (asof_join_rate 22.3% 诚实暴露, 非崩溃). ReB: `EMPTY_NPZ_EOF_READINESS_HARNESS`. |

---

## TECH_DEBT-010 Detail — 影子风暴 ZMQ 隔离缺陷 + 跨域串台 (The Storm Forensics)

> **状态**: ✅ **已清偿 FIX-20260819-002 (2026-08-19)** — 投委会雷霆裁决执行完毕 (清偿序列 010 → 008 → 017 → 009; 010 已清偿, 008/017/009 后续跟进). 以下原始裁决、8/19 解封法证、修复蓝图完整保留供审计.

### 原始裁决 (2026-08-06, incident_shadow_storm_resolved)
- **定性**: 8/6 出单风暴 4× rejected 共 187 条 = **本系统 v9 shadow 误触发** (非外部攻击), **零成交污染** (golden_master/ledger 0 命中). 投委会裁决收口.
- **风暴签名**: `message_id` = `message_<uuid32>` (唯一由 `ids.py:32-33 new_message_id()` 生成), symbol=`XAUUSD` (缺 `c`), magic=`null`, strategy=`''`, sl/tp 空, `ack_status=rejected`.
- **裁决时已知时刻** (4 次): 07-08 14:56Z (46) / 08-03 09:18Z (47) / 08-04 15:14Z (47) / 08-05 13:13Z (47).
- **缓解措施**: FIX-20260806-006 `build_v9_shadow_container` 检测 live.yaml `mt5_zmq` 时非 `--live-dispatch` 短路 (禁真 ZMQ 断言). **注意: 该 FIX 仅上缓解, 根因未除.**

### 8/19 解封法证 — 复发实锤证据链 (Iron Law #11, `_audit_storm_sender_20260806.py` + 本会话 3 轮脚本)
1. **全月风暴总量超裁决**: 8 月 rejected **293 条** (8/3→8/7 五天分布: 47/57/49/47/93), 其中风暴特征 (XAUUSD 缺c + `message_` 前缀 + rejected) **281 条** — 远超 8/6 裁决时 4 次 187 条.
2. **8/6 裁决后复发 3 次新风暴 (140 条)** — 🔴 核心证据:
   - `2026-08-06 10:28Z` — **47 条**
   - `2026-08-07 03:51-03:52Z` — **47 条**
   - `2026-08-07 14:30-14:31Z` — **46 条**
   - 6 个峰值时刻全天分布: 03 / 09 / 10 / 13 / 14 / 15 UTC — 疑似**批量/回测调度触发的周期性发射** (同簇两次 47 条精确重复).
3. **跨域串台变种 (BTC 污染 XAU 账本)**: 主 journal (data/live_trade_journal.jsonl) 内 10 条 BTC `modify_sltp` rejected — `live_` 前缀 (非 message_), `btc_swing_h1_v2`×6 / `btc_swing_m30`×4, magic **90460/90430** (真实 BTC magic), 8/4 零星混入. 对照: data_btc/live_trade_journal.jsonl 8 月仅 22 条且全 `accepted/closed` 正常 → **BTC 命令错误写入 XAU 域主 journal**, 串台方向确认.
4. **零成交污染 (唯一安全边际)**: golden_master 8/6-8/7 = **0 记录** → 新风暴仍未穿透到成交台账; 防护网 (ZMQ 桥 dedup/symbol guard) 持续生效.
5. **源头暂歇未消失**: 8/8 → 8/19 **零 rejected** — 触发源头 (疑似某批量/回测/阴影命令) 未再运行, **非已消失**.

### 待 DQAF Sev 2 回答的投委会双核心问题 (The Storm Forensics)
- **Q1 谁扣动扳机?**: 对准 8/6 10:28Z / 8/7 03:51Z / 8/7 14:30Z 三时刻, 在任务计划 (Cron/Scheduler) / Windows 事件 / 回测执行历史中定位自动化源头.
- **Q2 管道如何串联?**: 核查 ZMQ 端口硬编码 (XAU 5556 vs BTC 5558 是否被配置解析合并) 与 Logger/Journal 直写路径, 找出 BTC 污染 XAU 账本的真实代码裂缝.

### 修复蓝图 (清偿时按此展开, FIX 编号待 DQAF 后分配)
1. **Shadow 禁真 ZMQ 断言加固**: `build_v9_shadow_container` 非 `--live-dispatch` 一律短路 + **断言级物理拦截** (ZMQ 端口探测回绝), 杜绝批量/回测进程连真桥.
2. **journal 域隔离**: 主 journal 写入侧按 `config.symbol` 域路由 (XAU→data/, BTC→data_btc/), 跨域 symbol 记录硬阻断 + 诊断日志.
3. **端口解析合并防御**: 单收敛点解析 ZMQ 端口 (杜绝 5556/5558 被解析错误合并), 配置校验器断言双品种端口互异.

- **关联**: FIX-20260806-006 (缓解已上), ReB `SHADOW_STORM_ZMQ_ISOLATION_DEFECT` (待登记), incident 记忆 `incident_shadow_storm_resolved_20260806`, TECH_DEBT-007 (journal 写侧同族), hash-lock `_audit_storm_sender_20260806.py`

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
- **状态**: ✅ **CLOSED (FIX-20260821-001, 2026-08-21)** — The Calendar Batch (DQAF-20260820-005, IC 全面批准): 7 处硬编码停滞阈值全收敛为 `staleness_anchor` 单点调用 (audit_data_chain_integrity.py S1 feature per-symbol / S1 bar_sync / S2 regime 12h / S4 ledger 24h / S4 gm 6h / S5 state 24h / S6 precheck 30h), 核心 `core/market/calendar.py` 网格 API (forex_24_5/crypto_24_7). XAU 周六实证 grade 🟢92 stale_faults=[] (原 -5 假阳性消除); BTC 84 仅 S3 零回归; S3 dormant 24h 机制保留防语义反转. 回归锁 58 测试.

## TECH_DEBT-012 Detail — Feature Writer 休市重写抑制 (跨品种特征重复落盘)

- **现状**: BTC 域 `data_btc/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl` 休市期 (08-08 周六 01:14/05:20/09:26) 3 条记录值**逐位一致** (M5_Ret_1=0.027181, M5_Price_ZScore=-0.167581, 与 08-07 21:08 收盘相同) — 时钟驱动周末特征计算 + last-value freeze.
- **定性**: 上游 Feeder/Aggregator 边界问题; 冗余数据无害, 无逻辑毒药 (数值无变化, 不制造假信号). 已证伪跨资产污染.
- **风险**: 仅未来特征重算改"增量写入/事件驱动"时会产生重复行污染. 当前 append 重复值语义安全.
- **修复方案** (8/19 后, 纯防御): 特征写入侧 `market_closed → 跳过落盘` 守卫; 或 last-value 指纹 (hash 逐位一致 → 跳过) 幂等去重.
- **关联**: 2026-08-08 全链体检报告 (隐患 ②), pre_trade_guards.py 市场日历 (同 TECH_DEBT-011)
- **状态**: ✅ **CLOSED (FIX-20260821-001, 2026-08-21)** — `local_feature_store.write_records` 尾行指纹去重 (canonical JSON 逐位对比, 排除 `ingested_at` 写入时元数据; sort_keys 归一化), 休市时钟驱动 last-value freeze 重复落盘跳过; `compact()` 重写后失效尾缓存防数据丢失. **零触碰 live_cycle**. 回归锁 TestWriteDedupTailFingerprint 7 测试.

## TECH_DEBT-013 Detail — 休市期 intent 阻塞被 watchdog 误杀 (The 360s vs 300s 超时悖论)

- **实证链**: `references/DQAF_MEMO_20260811_WATCHDOG_MARKET_SYNC.md` (完整 4 路对证) + `scripts/_probe_market_break_vs_watchdog_20260811.py` (Untracked 探针).
- **市场日历**: XAUUSDc (`forex_24_5`) 每日 21:00-22:00 UTC 休市 (纽约 17:00 收盘, 夏令时), 周五 22:00 UTC 收市至周一. BTC (`crypto_24_7`) 无日休 — 对照组柱密度证实.
- **证据链** (Iron Law #11 脚本 stdout):
  - M5 柱: 07-29/07-30 工作日 21:00-21:55 零柱, 22:00 恢复; 周五 22:00+ 零柱; BTC 同窗全程有柱.
  - 击杀: 全史 905 条, 21:00Z n=455 (50%) + 22:00Z n=64 = 57%; 每交易日 21:00-21:54 恰 11 连杀, 间隔 ~5.5min.
  - intent 日志: 21:00:17/21:05:42 全量重启序列 (`bar_sync_initialized timeout_seconds: 360.0` → 阻塞 → watchdog elapsed≈300-308s 硬杀 → launcher 重启).
- **根因**: `bar_sync` 等待超时 360s > watchdog 硬杀超时 300s → 休市期 bar_sync 必被 watchdog 先杀, intent 永远无法自行优雅超时. 状态机缺 `market_closed` 态.
- **代价**: 11-14 次/日进程硬杀重启 + 启动噪音 + 休市期 `JOURNAL_PNL_NULL_RATE_HIGH` 假告警 (08-07T21:03 健康报告 `trade_journal: fail, pnl_null_rate 0.91`). **零交易损失** (休市本不能交易).
- **清偿 (FIX-20260820-001, 2026-08-20, IC 批准方案 1+2 组合 The Resilient Pulse)**:
  - **方案① 心跳穿透 (Heartbeat Delegation) — 主修复**: `BarSyncPoller.__init__` 新增 `heartbeat_refresh: Callable[[], None] | None` (生产 live_intent_loop 传 `lambda: setattr(state, "last_heartbeat", time.time())`). 轮询循环顶部 / 休市 session-off sleep / MT5 不可用 fallback sleep / 持久错误 fallback sleep 全部调用 `_refresh_heartbeat()` (BLE001:FOG 容错) → watchdog 等待期全程见 "alive", 休市阻塞永不被误判死锁.
  - **方案② 超时对齐 (Timeout Inversion Corrected) — 结构防线**: M5 bar 周期(300s) == watchdog 阈值(300s), 纯超时压缩会提前降级破坏正常交易 → **无脉冲时 degraded deadline 封顶 270s<300s** (`_degraded_wait_seconds()`: `min(bar+10, 270)`, 防误接线硬杀); **有脉冲时保持 bar+10s=310s** (bar-boundary 语义, 脉冲保 watchdog 安睡). `bar_sync_timeout` 360→240 双路对齐 (live.yaml + live_btc.yaml, IC 双路同步令).
  - **关键前提**: FIX-20260725-002 (CME 窗口 cycle 入口守卫) 已挡住 cycle 内 MT5 调用, 但击杀发生在 **cycle 之间 bar_sync 等待段** (`live_intent_loop.py:2615-2661`, execute_live_cycle 之外) — 本修复补上该层. 周六/周日 gate 命中 `risk_tier=="off"` 走 session-off sleep, 亦由脉冲保护.
  - **回归锁**: `tests/unit/test_event_bar_sync_heartbeat.py` 8 测试 (休市阻塞期 pulse 循环 / session-off pulse / degraded cap 270<300 / pulse 时 310 / pulse 失败容错 / BTC crypto_24_7 永不为 off / BTC 轮询 pulse / 双路配置对齐 240).
  - **四维**: Stability ↑ (纯增量, 回调零线程零IO, min_rr 类门禁思想); Repairability ↑ (watchdog_kill.log 休市窗击杀应归零, 可脚本审计); Decoupling → (接口签名向后兼容, `heartbeat_refresh` 默认 None); Iterability ↑ (deadline 逻辑收敛 `_degraded_wait_seconds()` 单点).
  - **ReB**: MARKET_CLOSED_BLOCK_MISCLASSIFIED_AS_DEADLOCK (扩展 ReB-20260811 同源).
- **纪律**: IC 裁决决战前**严禁触碰 Intent Loop / watchdog** 消除假警报; 仅 8/19 后清偿.
- **关联**: DQAF-20260811-001 (Sev 2), TECH_DEBT-011 (同族市场日历盲区), ReB 候选 `WATCHDOG_MARKET_BREAK_MISKILL`

## TECH_DEBT-014 Detail — 非休市时段背景零星击杀 (低频偶发阻塞)

- **范围**: 386/905 (43%) 击杀散布 0-23h; 典型 1-3 次/日; launcher 自动重启已吸收, 无停机.
- **高峰段**: 17:00Z n=33, 20:00Z n=39, 11-12:00Z n=23-29 — 需逐条关联 intent 阻塞点定性.
- **漂移单杀**: 每交易日单次击杀时刻 12:15→13:00 逐日 +5min (07-29 12:15 → 08-08 12:59 → 08-09/10 13:00 封顶), 连续 12 天精确 +5min/天 — 来源未定, 可能与某每日任务/数据刷新碰撞.
- **修复方案** (8/19 后, 低优先级): 逐条击杀时刻 × intent 日志关联, 确认是否同源 (MT5 IPC 偶发) 或独立缺陷.
- **关联**: TECH_DEBT-013 (同 watchdog 域)

## TECH_DEBT-019 Detail — TP/SL 动态追踪解耦 RR 坍缩 (The Decoupled Bracket)

- **文件**: `core/execution/position_manager.py:1707` (`compute_trail_tp`); `core/execution/trail_stop_engine.py` (TrailPolicy 字段 `tp_proximity_ratio`/`tp_min_distance_atr`/`tp_min_step`); `core/runtime/trail_dispatch.py:152` (TP 派发)
- **根因 (L3 架构缺陷)**: TP trailing (FIX-20260713-008, 2026-07-13 全盘激活, TrailPolicy 默认 0.7/1.5/0.15) 在 `atr_ratio = current_atr/entry_atr ≤ 0.80` 时把 TP 向内收窄 (`candidate = anchor ∓ trail_mult × current_atr × 1.75 × _tf_scale`), 且注释明示 "TP only moves INWARD — never widens". 但**向内缩窄的下限与 SL 距离/RR 无耦合**:
  - **TP Floor 语义方向反了**: `tp_min_distance_atr×bracket_atr` 用 `max()`(short)/`min()`(long) 选择 → 是 **upper bound 防"太激进(太远)"**, 不是 lower bound 防"太保守(太近)" — TP 可无限缩到 SL 之内
  - **Proximity Gate** (`tp_proximity_ratio=0.7`) 仅防价格走完 70% 旅程后移动, 不保 RR
  - **Bracket inversion guard** 仅在 TP 穿过 SL 时释放 TP=0, 不防 RR<1
  - **只缩不放**: ATR 恢复不复原 → 窄目标持久化
- **8/17 实证** (DQAF-20260817-001; 脚本 `scripts/_audit_xau_tp_shrink_20260817.py`):
  - ticket 4500875936 (h1_swing SHORT): TP 84.3→25.7 点 (02:35 `label="trail"`/`comment="tp"`), RR 1.73→**0.527**, SL 4451.41 全生命周期未动, 盈利曾达 R=+2.108
  - ticket 4501482790 (m15_swing SHORT): TP 34.4→13.5 点 (03:05), RR 0.98→**0.385**
  - 两策略 `min_rr_ratio=0.85` (configs/live.yaml) 均跌破; 4502364037 (05:05 开) ATR 未收缩未触发
  - 触发阈值精确吻合: h1 ATR ratio 0.791 / m15 ratio 0.790 (≤0.80)
- **历史前案**: FIX-20260709-004 "trailing-TP collapse on h1/h4 swings RR 1.66→0.08" — bracket_atr per-TF scaling 仅放大 candidate 距离, **未堵 RR 耦合缺口** → 今日复发
- **修复蓝图** (投委会 2026-08-17 行动令; **✅ 已实施 FIX-20260819-001, IC 裁决 ①②③ 全量上线**):
  1. **RR 硬底线**: `compute_rr_floor_price` (entry 参照系, 单收敛点) — `compute_trail_tp` 收紧分支内主修复 + `trail_dispatch.py` 下发前 RR Guard 兜底断言 (双保险, 自愈死区持仓)
  2. **波动率对称耦合 (Symmetric Volatility Tightening)**: `atr_ratio ≤ 0.80` 收紧 TP 的同时 `SL_Volatility_Trail` 同步收紧 SL — 利润端与风险端同比例缩小, 维持 `RR ≥ min_rr_ratio`; 鼠轮封底/max_lock 封顶/min_step 守卫/仅收紧周期触发
  3. **弹性恢复 (Elastic Expansion)**: 废除"只缩不放" — `atr_ratio ≥ 0.85` (迟滞带 0.80–0.85) 且未越过 Proximity 70% 警戒线时 TP 外向复原, 最高恢复至 `initial_tp`
- **纪律**: ~~8/19 冻结期零代码~~ → **已解除**: FIX-20260819-001 (8/19) 清偿, `tp_min_rr_ratio` 门禁 (0=disabled) 保 structural/legacy 零变化
- **状态**: ✅ **CLOSED (FIX-20260819-001, 2026-08-19)** — 三机制全量上线, 回归锁 580+111 passed
- **关联**: DQAF-20260817-001 (Sev 2), FIX-20260713-008 (激活案), FIX-20260709-004 (前案), ReB `TP_TRAIL_RR_COLLAPSE_DECOUPLED_FROM_SL`, TECH_DEBT-007 (trail 遥测同族)

## TECH_DEBT-020 Detail — training_readiness 命中空/损坏 npz → EOFError (The Empty NPZ)

- **文件**: `scripts/check_training_readiness.py:722` (`validate_stage_3_dataset_builder` → `np.load(_npz_path, allow_pickle=True)`); 触发链 `scripts/daily_ops.py:1992` (`_step_training_readiness`) → `check_training_readiness.py:1082` (`evaluate_training_readiness`)
- **现象**: `training_pipeline_xau_metafilter_v1` 的 stage-3 npz 为空/损坏 → `np.load` 抛 `EOFError: No data left in file` — **每 XAU daily_ops 运行确定性命中** (2026-08-20 12:50:44 solo 复现实证, 与 launcher 并发无关).
- **当前影响**: 被 `_step_training_readiness` 的 fail_open_guard (daily_ops.py:1994-1998) 捕获 → 管线继续 (最终 report `errors=0`, actions 正常) → **非阻断**, 属数据完整性潜伏项.
- **次生影响 (已根治)**: 该 traceback 由 `logging.exception` (logger 无 handler → last-resort handler) 例行写入 **stderr** — 曾使 FIX-20260820-003 的 launcher 成功谓词 (`"Traceback" not in stderr`) 确定性误判每条 XAU 完成运行为 FAILED → stamp 永不落盘. **FIX-20260820-004 (JSON Payload Authentication) 已根治该信号污染** (谓词转向 stdout report JSON 认证, stderr 全噪音免疫).
- **根因 (2026-08-21 实证修正, 推翻注册表推测)**: **写入侧健康** — 特征存储 XAUUSDc M5 43,580 记录无污染 (v9: 29,220). 真因 = **契约配置缺口 × validator 空文件模式 × 读取侧零容错** 三重叠加: `training_pipeline_xau_metafilter_v1.json` `stage_3_dataset_builder` **无 `builder_args` 字段** → validator (check_training_readiness.py:659-661) fallback `["--data-dir", data_dir]` → `build_btc_metafilter_v2_dataset.py:458` `symbol` 默认 `"BTCUSDc"` → `data/feature_store/records/symbol=BTCUSDc/` 不存在 → `:464-465 if not features: return` **静默早退 rc=0 不写文件** → validator 预建 `NamedTemporaryFile(suffix=".npz")` 为空 → `:722 np.load(空文件)` → `EOFError: No data left in file`. **对照组成立**: BTC v3 契约同样缺 args 但 data_btc 恰含 symbol=BTCUSDc → 默认命中不炸; swing_v3 契约含完整 builder_args → 不受影响.
- **修复方案** (2026-08-21 执行, **FIX-20260821-006, IC 三位一体防御裁决**, Phase 3 P1 首优):
  1. **契约修正 (Immediate Fix)**: `training_pipeline_xau_metafilter_v1.json` 补 `builder_script` / `builder_output_arg` / `builder_args` (`--data-dir data --symbol XAUUSDc --spread-cost-usd 0.0`) → builder 以正确 symbol 真实产出 XAU 数据集.
  2. **Builder 熔断 (Fail-Fast Generator)**: `build_btc_metafilter_v2_dataset.py` 四个静默/软早退路径 (`if not trades` / `if not features` / `if not contract_names` / `if len(X)==0`) 全部改为 `_fail()` → stderr ERROR + `sys.exit(1)`, 静默失败→硬失败, validator 能区分"builder 未运行"与"正常产出".
  3. **消费者容错 (Resilient Reader)**: `check_training_readiness.py` np.load 前加**空文件守卫** (`os.path.getsize==0` → FAIL verdict), np.load 包 `(EOFError, ValueError, OSError, pickle.UnpicklingError, zipfile.BadZipFile)` 捕获 → FAIL verdict + 明确诊断 (exception type + msg), **绝不让 daily_ops 因单个 npz 抛 traceback**.
- **状态**: ✅ **CLOSED (FIX-20260821-006, 2026-08-21, Phase 3 P1)** — 回归锁 7 (tests/scripts/test_training_readiness_xau_metafilter.py) + 全量 verify 通过. 实盘复现: XAU 契约首次真实评估成功 (builder 真实产出 1046 样本/40 维/标签 39.2%), 残余 FAIL = asof_join_rate 22.3% <80% + pnl_null 14.4% — **诚实数据质量信号**, 非崩溃伪装.
- **关联**: DQAF-20260820-004 (取证源), DQAF-20260821-020 (本清偿 docket), FIX-20260820-004 (次生信号污染已根治), ReB `EMPTY_NPZ_EOF_READINESS_HARNESS` (新) / `FAILURE_DETECTION_SIGNAL_AMBIGUITY`

## TECH_DEBT-015 Detail — launcher 停机无自动恢复 (运维空窗)

> **状态**: ✅ **已清偿 FIX-20260821-004 (2026-08-21, P9)** — 投委会战术推进令 (DevOps 收尾批) + fail-safe 孤儿策略裁决执行完毕. 以下原始裁决与事件证据完整保留供审计.

- **事件**: 2026-08-10T18:44:11.113288Z (02:44 北京) 双 launcher 收到外部 CTRL_C_EVENT (SIGINT), 优雅停机 (DQAF-20260811-001). 用户 01:44 入睡 → 排除人为; 系统事件/调度任务/launcher 自重启/内部 watchdog/仓库停机脚本 全部证伪 → 信号源未 100% 锁定 (最可能: 后台 agent/工具停机-重启循环的停机半步).
- **异常点**: 历史 ~20 次 SIGINT 停机均 0.1-0.2min 内被拉起 (重启生效节奏); 本次 **5.8h 无跟进重启** — 唯一差异 = 运维恢复链空窗. launcher 设计为子进程 supervisor, **自身无重启机制**.
- **修复方案** (8/19 后, **2026-08-21 已执行 → FIX-20260821-004**):
  1. 运维纪律: 任何 SIGINT 后 5min 内拉起双 launcher (或记录"故意停机"). — 保留为纪律约束.
  2. ~~launcher 级心跳 supervisor~~ → **hub 级心跳 supervisor** (机制同源, 监控对象精确化): `scripts/launcher_supervisor.py` — 独立 schtasks 探针 (5min) + 三态机 (HEALTHY/DEGRADED/RECOVERY) + 原子拉起, **双开防护三重防线** (① O_EXCL 锁 TTL 12min 防竞态双实例; ② 锁内重查 hub 防"已有人拉起"; ③ DEGRADED 只告警不杀不重启 — 防在活交易进程上叠 hub → 双 launcher → 双 intent → 双开, IC: 双开绝对不能容忍). 真正失活对象 = **HUB `main.py live`** (hub 自身已含 launcher 重启逻辑; 8/10 空窗 = hub 被杀, 无 restarter). 实现要点: 复用 live_launcher.py wmic 枚举 (纯 stdlib); 匹配白名单防误认 (main.py live 词边界 / 只认本 repo launcher + 本 repo config 标记, 不认 D:\cursor 拉起的); hub 重启 `subprocess.Popen` DETACHED_PROCESS + 新日志文件. TDD 40 测试 (tests/scripts/test_launcher_supervisor.py) + 实盘干跑 HEALTHY hub=16004 launcher=[15368,8776] 精确吻合.
- **关联**: DQAF-20260811-001 (Sev 2), ReB `EXTERNAL_SIGINT_NO_FOLLOWUP_RESTART`

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
- **清偿状态**: **CLOSED ✅ (2026-08-19, 清偿序列第三目标)** — FIX-20260819-004 (DQAF-20260819-004): **L3 架构级 Scope-Safe Pre-binding 4 处** (红线 8/19 到期后执行, 与 TECH_DEBT-008 同批). ① live_cycle startup_reconciliation `_positions: list[Any] | None = None` 预绑定 + `_skip_recon` 布尔守卫 (DEGRADE/MT5 超时 → 跳过 reconciliation, **known_open_tickets 保留**不丢持仓跟踪 — 修正原方案 `_positions=[]` 会清空持仓跟踪的缺陷); ② live_cycle PnL_to_equity `_eq: float = 0.0`; ③ live_intent_loop `while True` 循环体最顶层 `_EVENT_STREAM_MODE = True` (原方案入口绑定改为循环顶 — 覆盖每次循环迭代的异常跳转); ④ group_consensus CorrelationTracker:penalty `dynamic_volume = raw_volume` (系统扫描发现的第 4 处同类潜伏缺陷). 回归锁 5 测试 (1 behavioral + 4 static 顺序断言). ReB: `FTC_SCOPE_TRAP_UNBOUNDLOCAL`. 注: 修复方案中"同批清偿 TECH_DEBT-008"已完成 (FIX-20260819-003), "休市日历适配合并"归属 TECH_DEBT-013/014 另行清偿.

## TECH_DEBT-018 Detail — `META_FILTER_WIRED_STALE` 假阳性 (The Silent Monad)

- **现象**: 2026-08-12T23:19:59Z 钉钉告警 `meta_filter_state — META_FILTER_WIRED_STALE / MetaFilter wired 2808min ago (LGB=True, cal=False, micro_scaler=True, dims=40)`. 同期 `FEATURE_STORE_COLD_START` 伴随告警 (uptime 0.0 min < grace 10 min, 设计降级).
- **health check 机制** (`core/observability/health_checks.py` `check_meta_filter_state`, L394-486, FIX-20260610-007 event-interception): glob `intent_*.log` 按名称倒序取最新 2 个, **head-read 前 64KB**, 扫描 `"event": "meta_pipeline_wired"` → 若 wired_age > 360min → `META_FILTER_WIRED_STALE` WARN. Secondary fallback: `meta_filter_state.json` (已知不可靠, lazy serialization 陈旧).
- **根因链**: ① TECH_DEBT-017 崩溃-重启循环 (8/11→8/13 共 38 次) 中 intent stdout 被 launcher 捕获进 `live_launcher_20260811T003149Z.log` 的 `[intent]` 行, **不再轮换新 `intent_*.log` 文件** (8/11 08:32 后无任何新文件) → ② health check 只能读到 `intent_20260811T003150Z.log` 内 8/11 00:31:59.509Z 的陈旧 wired 事件 → ③ wired_age 计算为 2808min (8/11 00:31:59 → 8/12 23:19:59) > 360 阈值 → 假 WARN.
- **实证 MetaFilter 健康**: launcher log `[intent]` 行显示 8/12 21:11:19Z → 8/13 00:45:12Z 每次重启均成功 wired; 当前进程 PID 18052 (启动 8/13 00:45:04Z) 完成 `meta_pipeline_wired` (lgb_loaded=true, cal=false, micro_scaler_loaded=true, dims=40) → `meta_filter_long_loaded` / `meta_filter_short_loaded` → `meta_filter_gate_init` (threshold=0.4, conformal_warm=true, conformal_threshold 0.47618). MetaFilter stage2 过滤全程在位.
- **分类**: 非 MetaFilter 故障, **纯监控工具盲区** — health check 依赖 intent log 文件流, 而崩溃循环使该文件流断流. 属 TECH_DEBT-017 次生症状.
- **红线**: 8/19 前零触碰 (IC Hold Fast Order 维持全线冻结; health check 属 observability 域, 但修复动机来自崩溃循环场景 → 归入 8/19 决战收口).
- **修复方案** (8/19 后, 随 TECH_DEBT-017): ① health check 增加 launcher log 兜底 — `[intent]` 行扫描 `meta_pipeline_wired` (跨崩溃恢复, 反映真实 wired 时间); ② 或引入 `meta_pipeline_wired` 独立持久化事件文件 (SSOT, 与 intent log 生命周期解耦); ③ `FEATURE_STORE_COLD_START` 告警本身为设计行为, 无需处理.
- **清偿状态**: **RESOLVED ✅ (2026-08-21, P7, 投委会战术推进令)** — FIX-20260821-005 (DQAF 实证 + 方案② + D1 语义根治): 实证推翻"触发概率已极低"前提 — `run_data_health.py` 实盘全检当前 primary code 即 STALE (XAU intent 8/20 14:37Z 启动 ~20h 无重启 → wired_age 20h>360 → **每日假 WARN**, 非仅崩溃循环). 修复 (1) **新 SSOT** `core/observability/meta_wire_events.py` — producer (`scripts/live_intent_loop.py`) wired 成功时同步 append `{base_dir}/state/meta_pipeline_wired.jsonl` (方案②, 跨 stdout 路由持久); (2) **boot 锚定语义** `check_meta_filter_state` — 决策锚定当前 boot (SSOT 时间 ≥ 最新 intent log 文件名 boot 时刻): 当前 boot wired → PASS (MICRO_SCALER_NOT_LOADED WARN 保留); 未确认 + last wire ≤360min → mid-boot 宽限 PASS; 未确认 + >360min → 真 STALE WARN; intent-log fallback 覆盖 rollout 空窗 (每日假 WARN 即刻消除无需重启); 8/11 崩溃循环案例 (每次重启均成功 wired) 亦 PASS. `wired_age` 降级为诊断指标. 回归锁 25 测试. ReB: `BOOT_EVENT_AGED_AS_STALENESS` + `INTENT_LOG_ROUTING_COUPLED_WIRE_EVIDENCE`.
- **关联**: TECH_DEBT-017 (根因同源), TECH_DEBT-011 (同族审计工具盲区), TECH_DEBT-013 (休市窗 crash 循环来源)
