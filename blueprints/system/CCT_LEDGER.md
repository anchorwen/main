# CCT Ledger — 因果链账本

> **标准参考**: IEC 62740:2015 §6.2 "Causal Factor Charting", NTSB Form 6120.1 "Sequence of Events"
> **用途**: 记录每个 Docket 的完整因果链。症状 → 中间异常 → 根因，每环节标注证据引用和置信度。
> **格式约定**: **强制使用三级标题块格式**（禁止 Markdown 表格，因果链文本较长会被水平拉爆不可读）。

## 格式模板

```markdown
### CCT-YYYYMMDD-NNN
- **Docket ID**: DQAF-YYYYMMDD-NNN
- **日期**: YYYY-MM-DD
- **置信度**: confirmed / hypothesis / speculative
- **因果链**:
  - [Layer 1 — 症状]: 具体可观测现象 + 证据引用（日志行号/文件路径）
  - [Layer 2 — 中间异常]: 异常状态/变量 + 证据引用
  - [Layer 3 — 根因]: 根因类别（RC-XX）+ 根因陈述
- **证据引用**:
  - Source 1: [日志/Journal/Golden Master/State/Receipts] — 具体位置
  - Source 2: [独立第二源] — 具体位置
  - Source 3 (if root cause): [跨品种验证源] — 具体位置
- **是否被推翻**: 否 / 被 CCT-YYYYMMDD-NNN 取代
- **关联 ReB Pattern**: ReB-YYYYMMDD-NNN
```

## 因果链条目

---

### CCT-20260821-003
- **Docket ID**: DQAF-20260821-002
- **日期**: 2026-08-21
- **置信度**: confirmed (全层, 复刻 + builder 自证双轨逐字节吻合 + null-PnL 全量法证)
- **因果链**:
  - [Layer 1 — 症状]: XAU 训练就绪评估 `asof_join_rate 22.3% (1046/4697)` FAIL + `pnl_completeness 677/4697 null (14.4%)` FAIL → 训练数据"看起来不可用", 训练闸门假阻断 (check_training_readiness.py:846/:1031).
  - [Layer 2 — 中间异常]: readiness 分母 `_count_journal_closed` 数 `ack_status=="closed"` **原始条目 = 4697** — 混入 677 空 PnL close + 孤儿/无 ticket close + 每票重复 close; builder 的 join 宇宙是**去重含 PnL 交易 = 1262** → 分母放大 3.72× → 确定性低值假象.
  - [Layer 3 — 根因]: **L2 逻辑缺陷 (RC-06 metric-semantics)** — readiness 两个度量 (`asof_join_rate`/`pnl_completeness`) 分母语义与 builder 实际 join 口径不一致, 度量未收敛到系统唯一的"交易"定义 (position_ticket 去重 + 含 PnL). 非数据时间错位 (时区/精度双否).
- **证据引用**:
  - Source 1: scripts/build_btc_metafilter_v2_dataset.py 自身输出 `Journal: 1695 tickets, 1262 with PnL` + `ASOF join: 1046 matched, 0 no prior feature, 211 stale, 5 not-yet-known` (权威口径)
  - Source 2: scripts/_audit_asof_join_miss_20260821.py 复刻逐字节吻合 + scripts/_audit_journal_universe_20260821.py distinct 普查 (raw_closed 4697 / distinct closed 1661 / closed+pnl 1262 / manual_close 416 / orphan 7)
  - Source 3 (root cause): 跨口径对照 — builder join 宇宙 (1262) vs readiness 原始条目 (4697); 修复后实跑 82.9% (1046/1262) 转绿 + pnl_completeness 0/1238 = 0.0%
- **是否被推翻**: 否 (AR: "时区撕裂" 被匹配 gap p50=5s 推翻; "精度截断" 被秒级干净匹配推翻; "守卫过杀" 半真 — 211 STALE 是真实断供被正确拒签, 非 22.3% 成因)
- **关联 ReB Pattern**: ReB-20260821-METRIC_DENOMINATOR_SEMANTIC_SHIFT

### CCT-20260821-002
- **Docket ID**: DQAF-20260821-020
- **日期**: 2026-08-21
- **置信度**: confirmed (全层, 实证复现推翻写入侧假设)
- **因果链**:
  - [Layer 1 — 症状]: XAU 每日训练就绪评估 `_step_training_readiness` 阶段 3 `np.load` 抛 `EOFError` (check_training_readiness.py:722) → daily_ops 全管线 traceback 污染 + XAU stamp 永久阻断 (与 DQAF-20260820-004 证伪链同源). 证据: 契约 stage_3 校验崩现场 + ReB_PATTERN_INDEX EMPTY_NPZ_EOF_READINESS_HARNESS.
  - [Layer 2 — 中间异常]: builder `build_btc_metafilter_v2_dataset.py` 因 symbol 错配 (默认 BTCUSDc, data/ 仅 XAUUSDc) 走到 `if not features: return` 静默早退 rc=0, 无 npz 落盘; validator 已 `NamedTemporaryFile` 预创建空 .npz → `np.load` 空文件 EOFError.
  - [Layer 3 — 根因]: L2 逻辑缺陷 (RC-06 contract-violation) — `xau_metafilter_v1` 契约缺 `builder_args` 字段, validator 回退 `["--data-dir", data_dir]` 默认参数 → builder 在错误的 symbol 上空转. 非 L3 架构缺陷 (写入侧健康, 契约配置缺漏).
- **证据引用**:
  - Source 1: configs/contracts/training_pipeline_xau_metafilter_v1.json (缺 builder_args) + scripts/build_btc_metafilter_v2_dataset.py:458 默认 symbol=BTCUSDc + `if not features: return` 静默早退
  - Source 2: scripts/check_training_readiness.py:659-661 validator 回退 + :722 np.load EOFError; 修复后实跑: builder_execution PASS + sample_count 1046 (min 500)
  - Source 3 (root cause): 实证 feature store 健康 — `data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl` 43,580 条 → 推翻"写入侧损坏"推测; BTC v3 (data_btc 有 BTCUSDc) 对照正常 + swing_v3 契约含完整 builder_args 对照
- **是否被推翻**: 否 (AR: "调大 np.load 容错即可" 被三位一体 IC 裁决推翻 — 仅容错会把数据断供伪装成已就绪; "写入侧损坏" 被 feature store 健康计数推翻)
- **关联 ReB Pattern**: ReB-20260821-EMPTY_NPZ_EOF_READINESS_HARNESS

### CCT-20260821-001
- **Docket ID**: DQAF-20260820-005
- **日期**: 2026-08-21
- **置信度**: confirmed (全层, 实盘数据实证)
- **因果链**:
  - [Layer 1 — 症状]: 周六跑 DCI `--baseline-read` → XAU 指数 92→87 假阳性 **-5 退化 BLOCKED** (`S1_FEATURE_STALE` + `S4_GM_STALE`), 数据零损坏 (2026-08-08 实证); 休市期 `data_btc/feature_store/records/symbol=XAUUSDc` 特征以冻结收盘值重复落盘 (08-08 01:14/05:20/09:26 三条逐位一致, M5_Ret_1=0.027181/M5_Price_ZScore=-0.167581).
  - [Layer 2 — 中间异常]: 7 处停滞站点各自硬编码年龄阈值 (12h×3/24h×2/6h/30h) 无市场日历感知 → 休市静默被等价于停滞; `health_checks` POST_OUTAGE 1440min ad-hoc 独立"造钟"; 上游时钟驱动 last-value freeze 每周期重发同一根 bar → 重复行.
  - [Layer 3 — 根因]: L3 架构缺陷 (RC-12 missing-feature) — 系统无单一日历时钟, 时间语义在多个模块多点硬编码各自为政, 无收敛单点.
- **证据引用**:
  - Source 1: scripts/audit_data_chain_integrity.py 7 站点硬编码阈值 (S1 feature L279/S1 bar_sync L304/S2 L396/S4 ledger L703/S4 gm L745/S5 L847/S6 L925) + core/execution/pre_trade_guards.py:46-47 市场日历
  - Source 2: core/features/local_feature_store.py:66-73 write_records 无去重 + 08-08 实证重复行 + core/observability/health_checks.py:149-183 POST_OUTAGE 1440min
  - Source 3 (root cause): core/market/calendar.py (新网格 API, 修复后唯一时钟) — 实证: `data --now 2026-08-15T10:00:00Z` grade 🟢92 stale_faults=[]; `data_btc` grade 84 仅 S3 (dormant 保留) 零回归
- **是否被推翻**: 否 (AR: "仅调阈值"被 7 站点×多市场类型收敛需求推翻; "严格逐位含 ingested_at"被 utcnow stamp 语义推翻 — 该字段每次写 stamp, 严格对比永不触发)
- **关联 ReB Pattern**: ReB-20260821-HARDCODED_STALENESS_MULTIPLE_CLOCKS

### CCT-20260820-004
- **Docket ID**: DQAF-20260820-004
- **日期**: 2026-08-20
- **置信度**: confirmed (全层, 决定性 solo 复现)
- **因果链**:
  - [Layer 1 — 症状]: FIX-20260820-003 部署后, XAU daily_ops 每条完成运行仍被 launcher 判 `FAILED (rc=1)`, `daily_ops_state.json` stamp 永不更新 (01:04 UTC 停留, 13:30 UTC 复查未变) → 每 4h age 兜底重跑 + 假 FAILED. 证据: live_launcher_20260820T122455Z.log FAILED (rc=1) + 12:25 运行产物 mtime 12:36-12:38 UTC (管线真实推进到 param_optimization) + data/state/daily_ops_state.json 未更新.
  - [Layer 2 — 中间异常]: 谓词 `"Traceback" not in stderr` 对已完成运行求值为失败 — 因为 stderr 含 "Traceback". 决定性复现: solo `daily_ops.py --base-dir /d/future/data` (无并发/无 MT5) 完整跑通 12.2min, stdout 92,899 bytes 以完整 report JSON 结尾 (schema_version=daily_ops.v1, errors=0, actions_total=6, 31/31 steps 非 error, 按契约 rc=1), stderr 仍含 **1 个 Traceback** (EOFError). 谓词模拟: 现行→FAILED; 提案→SUCCESS.
  - [Layer 3 — 根因]: **stderr Traceback 对 daily_ops 不具崩溃特异性** — fail_open_guard 设计 (BLE001 + `logging.exception`) 将被捕获异常 traceback 例行写入 stderr (logger 无 handler → Python last-resort handler 固定落 stderr). 确定性触发点: `_step_training_readiness` (daily_ops.py:1992) → `evaluate_training_readiness` (check_training_readiness.py:1082) → `np.load(_npz_path, allow_pickle=True)` (check_training_readiness.py:722) 对空/损坏 npz (training_pipeline_xau_metafilter_v1 stage-3) 抛 `EOFError: No data left in file` → daily_ops.py:1996 `logger.exception` → stderr. L2 逻辑缺陷: 成功信号用 stderr 启发式而非 stdout 完成契约.
- **证据引用**:
  - Source 1: scripts/live_launcher.py:183 (被证伪谓词) + scripts/daily_ops.py:3589-3590 (stdout 完成标记) + scripts/daily_ops.py:3491 (schema_version 无条件键)
  - Source 2: `C:\Users\Administrator\AppData\Local\Temp\repro_daily_ops_stdout.txt` (92,899 bytes, 尾完整 report JSON) + `repro_daily_ops_stderr.txt` (10,183 bytes, 1 Traceback EOFError) + 谓词模拟
  - Source 3 (root cause): scripts/check_training_readiness.py:722 (np.load EOFError) + scripts/daily_ops.py:1994-1998 (logger.exception 捕获路径) + last-resort handler 实证
- **是否被推翻**: 否 (AR 三重反假设: 真实崩溃被 errors=0+完整 report 推翻; MT5 冲突被 solo 无 MT5 复现推翻; 12:25 并发混沌被 solo 确定性复现推翻)
- **关联 ReB Pattern**: ReB-20260820-FAILURE_DETECTION_SIGNAL_AMBIGUITY

### CCT-20260820-003
- **Docket ID**: DQAF-20260820-003
- **日期**: 2026-08-20
- **置信度**: confirmed (全层)
- **因果链**:
  - [Layer 1 — 症状]: XAU daily_ops 在新架构 (FIX-20260820-002) 下每次运行后 launcher 报 `FAILED (rc=1)`, `state/daily_ops_state.json` 永不 stamp (实证 last_daily_ops_utc 停留 01:04 UTC, 19:10 UTC 复查未变) → 4h age 兜底 (max_age 6h) 每 4h 重跑全管线 + 假 FAILED 告警. 证据: live_launcher_20260820T111100Z.log:540 FAILED (rc=1); data/state/daily_ops_state.json 未更新 vs data_btc (rc=0 对照 11:18 UTC 正常 stamp).
  - [Layer 2 — 中间异常]: 退出码契约不匹配 — daily_ops.py L3597-3602: errors>0→rc=2 / actions_total>0→rc=1 / 否则 rc=0; live_launcher `_run_daily_ops_once` L177 仅认 `rc==0` 为成功. XAU daily_ops 常态含治理/副作用动作 → 恒 rc=1 → 误判失败且不 stamp.
  - [Layer 3 — 根因]: RC-06 contract-violation (L2 逻辑缺陷) — FIX-20260820-002 将 stamp-at-completion 唯一写者迁移 launcher 时, 成功判定沿用 watchdog 时代的 rc==0 装饰性判定 (旧版 stamp 来自 intent, rc=1 仅是日志噪音被掩盖); 迁移后 rc==0 成为 stamp 唯一门禁 → 缺陷升格为阻断.
- **证据引用**:
  - Source 1: scripts/daily_ops.py:3597-3602 (退出码契约) + scripts/live_launcher.py:177 (rc==0 判定)
  - Source 2: data/logs/live_launcher_20260820T111100Z.log:540 (FAILED rc=1, stderr 前 500 字符无 Traceback) + data/state/daily_ops_state.json (stamp 缺失) + data_btc 对照 (rc=0 stamp 成功)
  - Source 3 (root cause): AR 对抗 — "崩溃撞车"假设被推翻: 崩溃必打 Traceback 至 stderr (E3 前 500 字符全为 BrainFactory 告警) + rc=1 ⟺ errors==0 (代码语义自证)
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260820-EXIT_CODE_CONTRACT_MISMATCH_IN_SSOT_STAMP

### CCT-20260820-002
- **Docket ID**: DQAF-20260820-002
- **日期**: 2026-08-20
- **置信度**: confirmed (全层)
- **因果链**:
  - [Layer 1 — 症状]: intent 心跳周期内嵌入 daily_ops 同步执行 → watchdog 300s 结构性必杀. 证据: gate_audit 击杀序列 (背景击杀同源 82/515); live_intent_loop.py watchdog 线程 300s 硬阈值; XAU launcher 子进程 600s 超时 55/55 零完成 (08-06→08-19).
  - [Layer 2 — 中间异常]: daily_ops 真实耗时 BTC~5min / XAU>10min > watchdog 300s 硬阈值, 且执行点位于 cycle-top→cycle-complete 零脉冲区 — 超时即死. stamp-at-start 语义使每次失败后 `_already_ran_today` 已置位 → 逐日 +5min 无界漂移 (08-06 12:44→08-20 01:04) + 22:00 主窗口永久抑制.
  - [Layer 3 — 根因]: RC-06 contract-violation — L3 架构缺陷: daily_ops 执行通道与心跳循环强耦合 (无独立执行者), 触发与完成戳同址 (stamp-at-start 错误语义). 修复层级 = 架构修复 (Single Executor/SSOT 时间轴重构).
- **证据引用**:
  - Source 1: gate_audit/*.jsonl 击杀序列 + scripts/live_intent_loop.py watchdog 300s 硬阈值
  - Source 2: scripts/live_launcher.py 子进程 600s 超时计数 (55/55) — 计时探针实测 daily_ops 耗时
  - Source 3 (根因): core/runtime/daily_ops_scheduler.py 旧实现同步调用链 + daily_ops_state stamp 语义
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260820-SYNC_HEAVY_COMPUTE_IN_HEARTBEAT_ZERO_PULSE

### CCT-20260819-007
- **Docket ID**: DQAF-20260819-007
- **日期**: 2026-08-19
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 触碰 `core/contracts/label_contract.py` 或 `core/ledger/` 的 git commit 被 journal_freeze_gate pre-commit 门禁**假阻断** — 覆盖率恒报 0.0% (目标 80%) → 团队长期用 `JOURNAL_FREEZE_BYPASS` env 豁免. 证据: `scripts/journal_freeze_gate.py` main() 每次 commit 输出 0.0% 阻断消息; 门禁历史豁免注释遍布 FIX_REGISTRY.
  - [Layer 2 — 中间异常]: 受保护路径覆盖率读数结构性为零 — coverage.json (pytest-cov Windows) 键用反斜杠 (`core\ledger\...`), 门禁 `_PROTECTED_PREFIXES` 用正斜杠 (`core/ledger/`), `_is_protected` 直接 `startswith` 未归一化 → 受保护文件零命中 (实证: fwd 匹配 0 / 反斜杠匹配 28).
  - [Layer 3 — 根因]: L2 逻辑缺陷 — 路径比较边界未归一化到规范形式 (消费方正斜杠前缀 vs 生成方平台原生反斜杠), 门禁读 0 数据 → 恒假阻断 → 逼出 env 免死金牌.
- **证据引用**:
  - Source 1: `scripts/journal_freeze_gate.py` `_is_protected()` / `_read_coverage_pct()` — 双输入面分隔符不一致
  - Source 2: coverage.json (git 追踪, skip-worktree) — 458 文件全反斜杠键; fwd 匹配 0 vs bs 匹配 28 (实证脚本)
  - Source 3 (root cause): 修复后实测受保护路径真实覆盖率 19.7% (28 文件, 472/1683 行) < 80% → 门禁从假阻断转为**诚实阻断**
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260819-PATH_SEPARATOR_MISMATCH_FALSE_BLOCK

### CCT-20260819-006
- **Docket ID**: DQAF-20260819-006
- **日期**: 2026-08-19
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: GitHub Actions 6/6 push 失败 — `DataIntegrityError: Shadow Veto ... adapter_name='mt5_zmq'` at `bootstrap_v9.py:191`（ci-windows.yml:72 + daily-ops.yml:52 fixture-prep 步骤）
  - [Layer 2 — 中间异常]: CI 影子回归基线生成管线（`rebuild_formal_baseline_suites` → `write_batch_regression_baselines` → `run_scenario` → `build_v9_shadow_runtime_loop`）构建影子容器时，adapter 名唯一解析自仓库生产 `configs/live.yaml` (mt5_zmq)，无显式声明通道 → 合法 stub 构建被误判"影子连真桥"
  - [Layer 3 — 根因]: L3 架构缺陷 — Shadow Veto (FIX-20260819-002) 契约缺"合法影子构建者显式声明适配器"输入面；veto 谓词正确（网络适配器一律拦），缺的是**解析来源通道**（单源读生产配置）
- **证据引用**:
  - Source 1: 用户 traceback — `ci_prepare_v9_shadow_fixtures.py:180` / `main_v9_shadow.py:1120`/`:1287`/`:390` / `bootstrap_v9.py:168`/`:191`
  - Source 2: `.github/workflows/ci-windows.yml:72` + `daily-ops.yml:52` — 双 workflow fixture-prep 步骤均跑该脚本
  - Source 3 (root cause): FIX_REGISTRY FIX-20260819-002 commit `9b41dc88` — veto 引入者（今日 push，回归时间点吻合）
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260819-VETO_NO_DECLARED_ADAPTER_CHANNEL

### CCT-20260819-005
- **Docket ID**: DQAF-20260819-005
- **日期**: 2026-08-19
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 全量 `python -m mypy core/ apps/ scripts/ tests/` (unified 模式) 报 236 类型错误 / 62 测试文件; pre-commit isolated 模式 (`follow_imports=skip`) 全绿 → 两模式结果不一致. 证据: 命令 stdout 逐文件错误列表; pre_commit_mypy.py 基线 0 新增.
  - [Layer 2 — 中间异常]: unified 模式 follow_imports 真实解析 → 暴露 isolated 模式**跳过导入**掩盖的泛型签名债 (非 Any 化容器/函数签名缺类型参数, 仅在统一解析跨模块边界时暴露); 测试域 A3 潜伏债务 (TECH_DEBT-009, 8/19 后清偿档). FIX-20260819-002 引入的 `# type: ignore[call-arg]` 在 isolated 模式触发 warn_unused_ignores → 直接 ignore 策略双向冲突.
  - [Layer 3 — 根因]: RC-A3_LATENT_TEST_DEBT — 测试代码类型标注债长期未入 unified 检查面 (隔离模式屏蔽), 结构性潜伏至 8/19 清偿序列最后一块.
- **证据引用**:
  - Source 1: `python -m mypy core/ apps/ scripts/ tests/` — 236 错误/62 文件 (唯一合法证据源)
  - Source 2: `scripts/pre_commit_mypy.py` isolated 基线 — 0 新增, 双模式对照
  - Source 3 (root cause): TECH_DEBT_REGISTRY.md TECH_DEBT-009 — A3 潜伏测试债 (mypy 236 错误/62 测试文件), 清偿序列 010✅→008✅→017✅→009✅
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260819-UNIFIED_TEST_DEBT_A3_LATENT

### CCT-20260819-002
- **Docket ID**: DQAF-20260819-002
- **日期**: 2026-08-19
- **置信度**: confirmed (全层)
- **因果链**:
  - [Layer 1 — 症状]: 影子风暴 — ZMQ rejected 风暴: 全月 281 条风暴特征记录 (symbol=XAUUSD 缺c, `message_` 前缀, magic=null, ack=rejected), 峰值时刻分布 03/09/10/13/14/15 UTC 周期性发射, 8/6 裁决后仍复发 3 次 140 条 (8/6 10:28Z 47 + 8/7 03:51Z 47 + 8/7 14:30Z 46). 跨域串台变种: BTC `modify_sltp` rejected 10 条 (btc_swing_h1_v2×6/m30×4, magic 90460/90430, `live_` 前缀) 混入**主 journal** (data/) — 与 data_btc journal 8 月 22 条全正常对照, BTC 命令错误写入 XAU 域账本. (证据: gate_audit/2026-08-0[6-7].jsonl, data/live_trade_journal.jsonl, data_btc/live_trade_journal.jsonl)
  - [Layer 2 — 中间异常]: (a) 非 `--live-dispatch` 进程 (批量/回测/阴影) 检测到 live.yaml `mt5_zmq` 时仅短路隔离不彻底 (FIX-20260806-006 缓解) → 仍可连真 ZMQ 桥; (b) service_container ZMQCommunicationAdapter 构造用默认 `tcp://127.0.0.1:5556` (XAU 桥) 兜底 — 多品种架构 BTC 域进程也能落 XAU 桥; (c) dispatch_modify_trail / close 路径只传 `mt5_terminal_path` 漏传 per-symbol `zmq_order_endpoint` → BTC 命令经默认路径落 XAU 桥 (7/20-8/4 227 条). (证据: service_container.py FIX 前默认端口; modify_trail_dispatch.py FIX 前无 endpoint 参数; scripts/_audit_btc_modify_misroute_exposure_20260819.py)
  - [Layer 3 — 根因]: (L3, RC-12 config/contract) **多品种架构无显式注入契约** — ZMQ Endpoint 用默认端口兜底而非按品种外层配置显式注入; 调用面漏传 endpoint 无 fail-fast 兜底; 阴影容器无硬断言拦截生产适配器. 同类: FIX-20260613-059c 只修了 open 路径未覆盖 modify/close 全调用面.
- **证据引用**:
  - Source 1: [Gate Audit] `gate_audit/2026-08-06.jsonl` + `2026-08-07.jsonl` — 140 条复发风暴 (message_ + XAUUSD 缺c + magic=null)
  - Source 2: [Main Journal] `data/live_trade_journal.jsonl` — 10 条 BTC modify_sltp rejected 混入 (magic 90460/90430) vs `data_btc/live_trade_journal.jsonl` 8 月 22 条全正常
  - Source 3 (跨品种/根因代码锚点): `core/deployment/service_container.py` FIX 前默认 5556 / `core/runtime/modify_trail_dispatch.py` FIX 前无 endpoint 参数 / `core/protocol/services/zmq_communication_adapter.py` FIX 前 order_endpoint 有默认 — 双裂缝锚点
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260819-CROSS_ASSET_JOURNAL_CONTAMINATION / ReB-20260819-ZMQ_DEFAULT_PORT_FALLBACK

### CCT-20260816-001
- **Docket ID**: DQAF-20260816-001
- **日期**: 2026-08-16
- **置信度**: confirmed (全层)
- **因果链**:
  - [Layer 1 — 症状]: CI shard-2 `test_flow46_alignment.py::TestCombinedInference::test_predict_is_yA_plus_r` → `ModuleNotFoundError: No module named 'lightgbm'` → `1 failed, 600 passed` exit=1 → GitHub 推送未成功 (CI 失败日志)
  - [Layer 2 — 根因]: pyproject.toml `[project].dependencies` 漏声明运行时依赖 lightgbm (同类缺口: xgboost/scikit-learn/joblib) — CI 仅 `pip install -e ".[dev]"` 从 pyproject 装依赖 → 全新环境缺包; 8/3 新增硬 import 测试首次暴露 (此前全部 import 均函数内惰性 → CI 长期全绿掩盖; FIX-20260624-107 曾以 except ImportError 掩盖同根因)
- **证据引用**:
  - Source 1: [CI Log] 用户提供 — `test_predict_is_yA_plus_r - ModuleNotFoundError: No module named 'lightgbm'` / `1 failed, 600 passed` / exit=1
  - Source 2: [pyproject.toml:6-29] dependencies/dev 均无 lightgbm; [Dockerfile:38-41] 生产运行时显式安装 lightgbm>=4.3,<5.0 等 4 包 (镜像清单与 pyproject 漂移)
  - Source 3: [core import] lightgbm_brain_adapter.py:49 / meta_signal_filter.py:218 / meta_exit_engine.py:139 / transfer_adapter.py:97,150,250 — 生产运行时 5 处 import lightgbm; [FIX_REGISTRY.md:973] FIX-20260624-107 同根因 patch 掩盖先例
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260816-MANIFEST_OMISSION

### CCT-20260807-003
- **Docket ID**: DQAF-20260807-003
- **日期**: 2026-08-07
- **置信度**: confirmed (全层)
- **因果链**:
  - [Layer 1 — 症状]: 核心记账与终结管线四重断裂 — (a) 4454299643 合法平仓 (XAUUSDc m30_swing short, 开 0.02 lot, sl_hit_first, PnL=−66.30 verified_from_mt5_deal) 被 JournalGate 判 close_without_open 隔离进 quarantine → 平仓 PnL 从 SSOT 主账本消失 → DQAF-20260807-002 审计误读"仍持仓"; (b) 已物理派发的 close 网络请求因 PnL 计算失败被标记 dispatch 失败 → _close_dispatched 恒 False → 管理循环不确认终结; (c) pending_close 盲等锁 50 分钟 — MT5 半仓成交残留 (partial fill) 不感知, 下一周期不重发 close; (d) 平仓 corpse 记 volume=0.0 (ghost — 开 0.02 平 0.0).
  - [Layer 2 — 中间异常]: (a) 多进程 _known_tickets 内存态漂移 — live_intent_loop/bridge/daily_ops 各自实例的 JournalGate 内存集合不同步 (无 IPC/无刷新), 合法 open 在 gate 进程内存缺失 → 平仓误判孤儿; (b) managed_close._close_dispatched 与 PnL 观测耦合 — dispatch 后立即计算 PnL, 失败则回滚派发结果 → 物理动作被观测过程门控; (c) management_phase 以意图 (known_open_tickets/pending lock) 推断 MT5 物理残余 — 不查 broker, partial fill 不可见 → 盲等; (d) position_registration 消费 decision.volume (reentry decay 后置覆写 0.02→0.01) 而非物理派发 DispatchResult.volume → 记账与物理偏离.
  - [Layer 3 — 根因]: (a) L3 (contract-violation RC-06) — gate 是有状态的 (init 时一次性建 _known_tickets 内存集合缓存), 而多进程共享物理 journal → 状态源与真值源分离 (SSOT 在硬盘, 状态在内存); (b) L2 (logic) — 物理动作 (派发) 与观测 (PnL) 耦合在单一布尔量; (c) L2 (logic) — 终结状态机以意图状态而非 broker 物理状态为准 (partial fill 盲区); (d) L3 (state-leak RC-03) — 记账消费可变决策字段而非不可变派发结果.
- **证据引用**:
  - Source 1: [Quarantine] `data/journal_orphan_quarantine.jsonl` L534 — settlement_verified_4454299643, pnl=−66.30, verified_from_mt5_deal, sl_hit_first, close_time 08:34:15Z, volume=0.0 (ghost fingerprint)
  - Source 2: [Main Journal] `data/live_trade_journal.jsonl` L9063 (open vol=0.02) + L9069 (close vol=0.02 pnl=−66.3 _source=zombie_reconcile_backfill) — Step 3 回填后主账本, verified_close_legs=1
  - Source 3 (跨品种/根因代码锚点): core/execution/reentry_guard.py decay ladder 后置覆写 decision.volume + core/runtime/position_registration.py (FIX 前消费 decision.volume) — 记账与物理派发偏离; core/ledger/services/journal_gate.py validate_close (FIX 前一次性 _reload, 有状态缓存) — 多进程漂移锚点
- **是否被推翻**: 否
- **关联 ReB Pattern**: STATEFUL_GATE_MULTIPROCESS_DRIFT / REGISTRATION_VS_DISPATCH_VOLUME_DIVERGENCE / DISPATCH_OBSERVATION_COUPLING / PARTIAL_FILL_BLINDNESS

### CCT-20260807-002
- **Docket ID**: DQAF-20260807-002
- **日期**: 2026-08-07
- **置信度**: confirmed (全层)
- **因果链**:
  - [Layer 1 — 症状]: 实盘系统性"震荡区最高点开多/最低点开空" (用户观察 + 审计脚本): 全历史 640 笔 long 平均 H1_z=+0.034; LONG H1_z>+1.5 桶 25.7% 胜 / −49.29 总 (最差桶); SHORT H1_z<−1.0 桶 44.3% 胜 / +99.36 (盈利).
  - [Layer 2 — 中间异常]: ML 动量特征 (H1_Price_ZScore 等) 在区间极值处峰化 → 信号置信度在顶部/底部最高 → 高置信信号恰恰出现在最差进场位置; 且 swing 家族无任何价格位置守卫, 只有方向守卫.
  - [Layer 3 — 根因]: L3 (RC-06 architecture-incomplete) — check_z_inflection (唯一价格位置闸) 被 `"statarb" in name or "ou" in name.lower()` 白名单排除 swing 家族 (trend_isolation_gates.py:205-207) → 趋势追家族空间维度结构上零保护 (方向闸再多也拦不住"顺向但位置极差"的开单).
- **证据引用**:
  - Source 1: scripts/_audit_entry_timing_20260807.py (Iron Law #11 脚本, 留工作树) — 640 笔分布: long avg H1_z=+0.034 / long z>1 bucket 25.7% −49.29 / short z<−1 bucket 44.3% +99.36
  - Source 2: core/execution/trend_isolation_gates.py:205-207 — check_z_inflection 硬限制 statarb/ou (L3 根因代码锚点); :239-384 4e 门禁实现 (FIX 锚点)
  - Source 3 (跨品种): 用户提高手数以来实盘观察 + core/execution/strategy_line.py counter-trend 动作块后 4e 接线 (spatial_zscore_gate block/degrade 事件)
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260807-TREND_CHASE_NO_POSITION_GATE

### CCT-20260807-001
- **Docket ID**: DQAF-20260807-001
- **日期**: 2026-08-07
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU 低健康开单过度放行 — 08-06 09:44Z 首单 GodsEye health=0.52 仍成交 (三单全 SHORT 追逐, "-$144 学费"); m30 ticket 4448694178 (11:20Z 开/13:09:55Z zombie 检出/09:47Z 平) 平仓后 `data/state/execution_state.json` pending_settlement_tickets 恒空 → PnL 永久不可对账。
  - [Layer 2 — 中间异常]: (GodsEye) `_gods_eye_health_vol_mult` 死区斜坡 (FIX-20260806-007 Option B2) 仅缩量不硬断 — health=0.52 时 volume×~0.36 仍 ≥0.02 Ω 终门成交; (Zombie) bridge-direct journal 写入把持仓放 position_manager 但不进 known_open_tickets → pre_mgmt_zombie_cleared 循环 `_z_open is not None` 条件为 False → 仅 `clear_position()` 无 settlement enqueue → 无 corpse。
  - [Layer 3 — 根因]: (1) L2 (contract-violation RC-06) — Cut 7 fail-open 教条 "God's Eye NEVER blocks outright" 使低健康/choppy 期无硬风控底线 (Defensive/choppy 本应拒单); (2) L2 (state-leak RC-03) — zombie-clear 的 settlement enqueue 依赖已知仓位字典, 对桥接直写逃逸的持仓结构上不可见。
- **证据引用**:
  - Source 1: `data/logs/intent_20260806T072045Z.log` 09:44Z 首单 health=0.52 (GodsEye fail-open 放行实证)
  - Source 2: `data/state/execution_state.json` pending_settlement_tickets 空 + 审计 (m30 4448694178 从未入队, zombie 逃逸实证)
  - Source 3: `core/runtime/live_cycle.py` pre_mgmt_zombie_cleared `_z_open is not None` 条件 gate (根因代码锚点)
- **是否被推翻**: 否
- **关联 ReB Pattern**: TREND_CHASE_FAILOPEN_LOW_EDGE / ZOMBIE_ESCAPE_NO_CORPSE

### CCT-20260806-003
- **Docket ID**: DQAF-20260806-003
- **日期**: 2026-08-06
- **置信度**: confirmed (全层)
- **因果链**:
  - [Layer 1 — 症状]: XAU 健康态零开单 — 每周期 `min_economic_volume_blocked: volume=0.0102-0.0175 < 0.02`，conf 最高 0.877、GodsEye health 最高 0.875 依旧 KILL。证据: `data/logs/intent_20260806T072045Z.log` L109 (07:21 0.0111) / L486 (08:05 0.0175, decisive) / L571 (08:15 0.0167) / L615。
  - [Layer 2 — 中间异常]: volume 结构性收敛到 min_economic floor (0.02): kelly raw_target 0.042-0.0536 → regime reduced ×0.65 → lot_step floor-round → trend_maturity_discount (floor 0.40) → 二次 floor-round 恒 0.02; 随后 GodsEye ×max(0.25, health) → 0.0102-0.0175。证据: intent log kelly_sizing final_stepped_volume=0.02 全周期; core/execution/strategy_line.py L1622-1635/L2027-2033; core/runtime/strategy_evaluator.py L1108-1110。
  - [Layer 3 — 根因]: (a) **L3 config-drift** — configs/live.yaml regime_map swing 家族仅 low_vol 定义, regime_gate.py:815 `gates.get(strategy_name, "reduced")` 默认 reduced (×0.65) 压低 raw target (BTC 对照 live_btc.yaml:294-304 完整, 反证 XAU 特有); (b) **L2 contract-violation** — GodsEye health 乘数 max(0.25, health) 违反自身 "normal: no modification" 契约 (strategy_evaluator.py:1103 注释), 且作用点在 Ω 终门 (L1145 `_floor=0.02`) 之前、volume 已收敛到 floor 之后 → 健康态也系统性 shave 跌破 floor (阈值共振)。
- **证据引用**:
  - Source 1: [Intent Log] `data/logs/intent_20260806T072045Z.log` — gods_eye_cycle / kelly_sizing / min_economic_volume_blocked 同毫秒链 (L104-109, L485-486, L571, L615)
  - Source 2: [代码] `core/runtime/strategy_evaluator.py:1108-1110` (health 乘数) + `:1145-1157` (Ω 终门 _floor=0.02) + `core/execution/regime_gate.py:815` (默认 reduced)
  - Source 3: [跨品种对照] `configs/live_btc.yaml:294-304` BTC regime_map 完整 (normal/trending/mild_trend=full) vs `configs/live.yaml:857-911` XAU swing 仅 low_vol — 缺口 XAU 特有
- **是否被推翻**: 否 (8/04 DQAF-20260804-004 "静候恢复" 闭案被本 docket Layer 2/3 实证推翻 — health 0.875×cm 1.10=0.963 ≫ 0.70 解锁线仍全 KILL, 证明 volume 被结构性钉死, 健康恢复无法解锁)
- **关联 ReB Pattern**: ReB-20260806-THRESHOLD_RESONANCE_VOLUME_SHAVE

### CCT-20260805-002
- **Docket ID**: DQAF-20260805-002
- **日期**: 2026-08-05
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 每次 live 重启后 ~7s 新进程生成 JOURNAL_SLA_VIOLATION CRITICAL (data_health_state.json updated_at 2026-08-05T13:49:04.777483, 重启 13:48:57 UTC 后 7s); 消息 "DUPES=123. close_price=100.0% trail=114.5% dupes=123 (eligible=1232 total=1263)" — dupes=123>10 为唯一 FAIL flag (health_checks.py:2472)。
  - [Layer 2 — 中间异常]: 123 dupes 全为 Phase 2 async retry-reentrant 架构对同一 PositionClosed 事件的重写残留 — 独立探针 (scripts/_audit_dupe_category_20260805.py): 123/123 组 = Cat A (同 position_identifier + 同 deal_id), Cat B (同仓不同 deal) = 0, Cat C (缺身份) = 0, 8/1 后零新增; 0 部分平仓证据。检查仍按原始投影行数键 (position_ticket, ack_status) 判 dup (health_checks.py:2421), 未消费 Phase 2 幂等身份 (position_events.py:53 deal_id / :88 position_identifier)。
  - [Layer 3 — 根因]: RC-06 (contract-violation) — FIX-20260611-005 声明 "Temporary patch — auto-expires 2026-07-11. After Phase 2 (PositionClosed event sourcing), these checks become structural guarantees, not runtime audits" (health_checks.py:2366-2368), 但 Phase 2 落地后补丁从未退役: `_expiry='2026-07-11'` (L2379) 仅消息字符串零退役逻辑, 检查未升级。技术债 TODO-20260711-journal-idempotency (FIX_REGISTRY.md FIX-20260613-089) 超期 25 天。
- **证据引用**:
  - Source 1: data_btc/state/data_health_state.json — updated_at 2026-08-05T13:49:04.777483 (重启后 7s), journal_completeness FAIL / JOURNAL_SLA_VIOLATION
  - Source 2: core/observability/health_checks.py:2379 (_expiry 纯字符串零退役逻辑), :2472 (dupes>10 FAIL), :2366-2368 (临时补丁自声明契约)
  - Source 3 (跨品种): data/live_trade_journal.jsonl (XAU) 同机制同型假阳性 (dupes=4, 未达阈值)
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260805-EXPIRED_TEMP_GATE_UNRETIRED

---

### CCT-20260805-003
- **Docket ID**: DQAF-20260805-003
- **日期**: 2026-08-06
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: trade_journal 健康检查每轮消息追加 "WARNING: 'trail' exit label never recorded (TRAIL_TELEMETRY_BLINDSPOT)" (health_checks.py:102), status 仍 PASS/JOURNAL_OK — 告警噪音级, 不触发升级。
  - [Layer 2 — 根因]: 探针精确键 `"trail" not in labels` (health_checks.py:101) 未随 FIX-20260612-003 label 契约演进 — reconciliation.py:193-217 将 trail-active SL 出场写为 `sl_hit_trailed` (注释 :197 "closes the TRAIL_TELEMETRY_BLINDSPOT"), 精确裸键 "trail" 永不存在 → 每轮误报。
- **证据引用**:
  - Source 1: core/observability/health_checks.py:101-102 (精确键探测 + 警告注入), core/runtime/reconciliation.py:193-217 (sl_hit_trailed 契约)
  - Source 2: data_btc/live_trade_journal.jsonl — 全量 label 分布 1263 close: sl_hit_trailed=2 (2026-06-10), sl_hit_first=274; tail-500 窗口 (7/21-7/24, 249 close): trail label = 0
- **是否被推翻**: 否 (契约对齐为最终根因; 尾窗真实缺失为诚实信号, 转 DQAF-20260806-001)
- **关联 ReB Pattern**: ReB-20260805-SEMANTIC_DRIFT_MONITOR_PROBE

---

### CCT-20260806-001
- **Docket ID**: DQAF-20260806-001
- **日期**: 2026-08-06
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: journal 自 2026-06-10 后 0 条 `sl_hit_trailed`; TRAIL_TELEMETRY_BLINDSPOT 探针契约对齐 (FIX-20260805-009) 后仍触发 (诚实信号); 近窗 (7/15+) 81 笔 sl_hit_first 高密度存在。
  - [Layer 2 — 中间异常]: 81 笔 sl_hit_first 中 **41 笔 SL 物理移动过** (position_snapshots current_sl 系列变化, 40 笔 ≥1.0R, 23 笔 ≥3 级, 最高 85.6R); **MT5 成交价反证 40/41 成交价落在『最终移动后 SL』** (broker 强制执行 trail) — 物理层健康, Chandelier trail 真实 ratchet。逻辑层: trail 出场被系统性误标 sl_hit_first。
  - [Layer 3 — 根因]: L3 (RC-06 contract-violation) — `position_close_adapter.py:439-440` 对 DEAL_REASON_SL 硬编码 `sl_hit_first`, `_build_event` 签名无 state → 结构上读不到 `position_manager.trail_advances`。时间线: 6/10 旧 MIA 路径产出 2 条 sl_hit_trailed (mia_close.py:182-185 trail-aware 参考实现) → 6/11 FIX-20260611-005 Strangler Fig #11 adapter 接管所有 close 记录 (label 硬编码) → 6/12 FIX-20260612-003 trail-aware 修复落在已被替代的 reconciliation.py (仅重启补账 live_cycle.py:1503 loop_iteration==1) → 修复与写入路径错位 (LABEL_PRODUCER_SWAP_SILENT_AMNESIA)。
- **证据引用**:
  - Source 1: `scripts/_audit_trail_mislabel_20260806.py` (Iron Law #11 脚本, 留工作树) — 81 票根: 41 SL 移动 / 40 ≥1.0R / 40/41 成交价落移动后 SL / 23 多步≥3级 / 方向 41/41 正确; 32 FLAT, 8 无快照
  - Source 2: `core/runtime/position_close_adapter.py:439-440` (硬编码 sl_hit_first), `:332` `_build_event` 签名无 state; `core/runtime/reconciliation.py:198-204` (trail-aware 打标, FIX-20260612-003), `core/runtime/live_cycle.py:1503` (仅重启补账触发), `:1756` (adapter 主写路径)
  - Source 3 (跨品种): `core/runtime/mia_close.py:89-92` + `:180-185` (trail-aware 参考实现, Strangler Fig #12 路由后被 adapter 覆盖为 sl_hit_first); XAU 同构风险 (adapter 共享代码)
- **是否被推翻**: 否 (AR 否决 4 反假设: H1 提前 GC→adapter 无 state 读不到; H2 snapshot 乐观→40/41 成交价反证; H3 仅 breakeven→23 笔多步铁证 trail; H4 BTC 无 trail→41 笔 broker 侧 SL 真实移动)
- **关联 ReB Pattern**: ReB-20260806-LABEL_PRODUCER_SWAP_SILENT_AMNESIA

---

### CCT-20260806-002
- **Docket ID**: DQAF-20260806-002
- **日期**: 2026-08-06
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 2026-08-06T05:03:23 三条 `phantom:test_fail` / `phantom:test_neg` / `phantom:counter_test` CRITICAL 推送至实盘钉钉群 (触发时间 05:03:23.285516/.270512/.397541); 审计日志 `data/logs/alert_audit.jsonl` 同毫秒三连记录 (2026-08-06T05:03:23.482559/.470558/.545573), 每条前置同毫秒 `system_online` 心跳 (LiveAlertHub 构造副作用)。
  - [Layer 2 — 中间异常]: `_alert_violation()` (core/contracts/phantom_contract.py:899-933) 对 `@phantom` 违例执行 `hub = LiveAlertHub(base_dir="data"); hub.send_critical(reason=f"phantom:{contract_id}")`; `LiveAlertHub.__init__` (live_alert_hub.py:390-397) 读机器级 env `QUANTOS_DINGTALK_WEBHOOK_URL` 接线 DingTalkAlertChannel → 真实推送。测试运行时 `@phantom` 装饰器在 `__debug__` 模式 (phantom_contract.py:763-766) 对故意失败的 predicate 调 `_alert_violation`。
  - [Layer 3 — 根因]: L2 (RC-06 contract-violation / 测试域边界缺失) — 生产代码 `_alert_violation` 对"谁在调用"零感知: 测试故意触发违例以验证契约机制时, 走了与真实违例完全相同的 `LiveAlertHub(base_dir="data")` → DingTalk 通道。test_neg/test_fail/counter_test 为测试专用 contract_id (test_phantom_contract.py:177-207/850-857), 生产 predicate id (risk_budget_non_negative 等) 永不匹配 → 100% 测试来源。FIX-20260805-006 (关键词 QuantOs) 修复送达前, 同类推送被 errcode=310000 静默拒收; 修复后首次真实触达 (05:03 = A3 验证期 pytest; 03:53/03:55 为 A2 验证期同类)。
- **证据引用**:
  - Source 1: `data/logs/alert_audit.jsonl` — 2026-08-06T05:03:23 三条 `phantom:*` CRITICAL + 三条同毫秒 `system_online` (规则名+recorded_at 逐条对应钉钉推送)
  - Source 2: `core/contracts/phantom_contract.py:899-933` (`_alert_violation` 构造 LiveAlertHub + send_critical), `:918-924` (env 接线 DingTalk), `core/observability/live_alert_hub.py:390-397` (QUANTOS_DINGTALK_WEBHOOK_URL 自动接线), shell `env` 实测 f33f64c0... 已设
  - Source 3 (测试来源): `tests/contracts/test_phantom_contract.py:177-207` (test_neg/test_fail 故意违例), `:850-857` (counter_test 直调 `_alert_violation`) — 逐字匹配推送 contract_id/message
- **是否被推翻**: 否 (AR 否决: "实盘 phantom 违例?" → 生产注册 id 列表 phantom_contract.py:551-669 无 test_* 前缀, 反假设不成立)
- **关联 ReB Pattern**: ReB-20260806-TEST_TO_PROD_ALERT_LEAK

---

### CCT-20260805-001
- **Docket ID**: DQAF-20260805-001
- **日期**: 2026-08-05
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `git status --porcelain` 对 `configs/live_btc.yaml` 持续报 ` M` 但 `git diff` 为空 — 6 轮 1s 采样全 ` M`; `git update-index --refresh` 报 needs update 仍 ` M`; 仅 `git add`(no-op, blob 不变 `76f78fcd`) 清除。内容与 index 逐字节相同 (CR=0 LF, hash-object raw + `--path` 过滤均 == index blob)。
  - [Layer 2 — 中间异常]: 启动 reconcile 经 `brain_lifecycle_manager.py:205-211` `_save_live_yaml` → `atomic_write_text` 无条件重写 live_btc.yaml — 内容与 index 等价但 mtime 刷新 (18:44:30) → git stat 缓存失步且不自愈 (幽灵 ` M`)。
  - [Layer 3 — 根因]: RC-06 (contract-violation) — hash-lock 契约应是"锁哈希(内容)而非时间戳", 但两 gate (`train.py:1311` `_enforce_hash_lock` + `daily_flow46_precheck.py:152` hash_lock, L71 "never drift" 契约) 用 stat 基 `git status --porcelain` 判定脏 → 内容等价仅 mtime 变化的文件触发假阳性。
- **证据引用**:
  - Source 1: `configs/live_btc.yaml` — blob `76f78fcd...` (raw + --path 过滤) == `git rev-parse :configs/live_btc.yaml`, CR=0, 6×1s 采样 porcelain 全 ` M`
  - Source 2: `core/deployment/brain_lifecycle_manager.py:205-211` — `_save_live_yaml` 无条件 atomic_write_text 重写触发 (mtime 18:44:30)
  - Source 3 (跨品种): `scripts/daily_flow46_precheck.py:152` — 复制版 gate 同 stat 基 (XAU live.yaml 同理), `git update-index --refresh` 失效实证
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260805-HASHLOCK_STAT_PHANTOM

---

### CCT-20260726-012
- **Docket ID**: DQAF-20260726-012
- **日期**: 2026-07-26
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 在 downtime 中于 MT5 侧平仓的仓位（SL/TP/手动）跨重启后仍出现在分析脚本的「活跃仓位」列表中。3871727437（6/10 开仓）50+ 天后仍被报告为活跃。
  - [Layer 2 — 中间异常]: execution_state.json 恢复 `known_open_tickets` → `state.known_open_tickets` 非空 → `live_cycle.py:1447` 条件 `not state.known_open_tickets` 为 False → `bootstrap_known_open_from_journal()` 被跳过 → 未播种幽灵仓位 → 启动 reconciliation 仅检查 restored state 中的票号 → 幽灵仓位永不检测。
  - [Layer 3 — 根因]: L2 — DQAF-20260710-003 的 belt-and-suspenders 设计为互斥而非互补。Journal bootstrap（suspenders）被 execution_state restore（belt）静默抑制。
- **证据引用**:
  - Source 1: `data_btc/state/execution_state.json` — 仅含 4308533605，不含 3871727437
  - Source 2: `data_btc/live_trade_journal.jsonl:327-328` — 3871727437 的 OPEN 与 3871726916 的 CLOSE（message_id 匹配但 ticket 不匹配），确认 journal bootstrap 通过 message_id 正确排除此记录
  - Source 3: `core/runtime/live_cycle.py:1447` — `not state.known_open_tickets` 守卫条件
- **是否被推翻**: 否
- **关联 ReB Pattern**: GHOST_BOOTSTRAP_RESTORE_MUTUAL_EXCLUSION

---

### CCT-20260724-001
- **Docket ID**: DQAF-20260724-001
- **日期**: 2026-07-24
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU 实盘所有 swing/trend 策略被 vol_zscore_hard_block 持续熔断（~48h）。Golden master 显示每个 cycle 所有非 micro 策略的 reason 均为 `vol_zscore_hard_block:m5_vol_zscore_-3.NN_lt_-3.0`。Feature store 39,714 条 M5_Vol_ZScore 记录（2026-05-05 至 2026-07-24）94% 非正值，仅最大值 -1.34 — 从未达到正值。
  - [Layer 2 — 中间异常]: `v9_live_computer._vol_zscore()` 算法结构正确（price_zscore 同算法 49/51 pos/neg 健康分布证明），但输入数据源 CFD tick_volume 具有 burst-decay 分布 + 连续相同值频发（→std=0→zscore=0）。`_vol_zscore` 使用 inclusive window（`volume[-lookback:]` 含当前 bar），当前 bar 加入 μ/σ 计算导致均值拖拽（Mean Drag）—— zscore 系统性地为负或零。
  - [Layer 3 — 根因]: L3 架构缺陷 — 电路断路器（circuit breaker）锚定于合成 CFD 伪指标（tick_volume）而非真实价格行为（ATR）。CFD broker 的 tick_volume 是人工合成的、充满噪声的代理变量，不应作为保护实盘资本的物理熔断器的唯一信号源。Inclusive-window z-score 的 Mean Drag 效应是次要因素——即使改用 exclusive window，CFD tick_volume 的 burst-decay 分布仍会持续产生大量负 zscore。
- **证据引用**:
  - Source 1: `data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl` — 39,714 条 Vol_ZScore 记录，94% 非正值
  - Source 2: `core/features/computers/v9_live_computer.py:116-125` — `_vol_zscore()` 算法审计（inclusive window + tick_volume 输入）
  - Source 3: `core/features/computers/v9_live_computer.py:180-189` — `_price_zscore()` 对照验证（同算法，48.9% 正值，证明算法正确）
  - Source 4: `data/golden_master.jsonl` — 实时 golden master 输出显示每 cycle vol_zscore_hard_block
  - Source 5: `core/runtime/strategy_evaluator.py:484-527` — 旧 Vol_ZScore 硬闸门代码位置
  - Source 6: `data/regime_detector_state.json` — buffer_sample (50-bar ATR buffer, 3.38-4.31 range) 用于新 ATR 比率闸门
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260724-CIRCUIT_BREAKER_ANCHORED_TO_SYNTHETIC_CFD_PSEUDO_METRIC
- **关联 FIX**: FIX-20260724-001
- **状态**: **CLOSED** — 摘除 Vol_ZScore 硬闸门，替换为 ATR 比率闸门 (atr_ratio < 0.5)；阈值 0.5 为临时热修复，历史回测校准 Deferred

### CCT-20260718-001
- **Docket ID**: DQAF-20260718-001
- **日期**: 2026-07-18
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `cleanup_orphan_opens()` 写入 `label="auto_orphan_*"` 合成 close 条目以配平无 close 的孤儿 open。当 `compact_journal()` 按 age 剪枝旧的 rejected open 时，配对的合成 close 未被级联删除 → 孤儿 close 永久残留在 journal 中。
  - [Layer 2 — 中间异常]: `compact_journal()` 单条目压缩逻辑逐行扫描 journal，仅根据单条记录自身的 age 决定保留/剪枝 — 无跨条目关联感知。
  - [Layer 3 — 根因]: L3 架构缺陷 — journal compaction 缺少级联删除语义。合成 close 通过 `open_message_id` 外键关联父 open，但 compact 无级联逻辑。
- **证据引用**:
  - Source 1: `core/ledger/services/journal_cleanup.py:compact_journal()` — 单 pass 剪枝逻辑
  - Source 2: `core/ledger/services/journal_cleanup.py:cleanup_orphan_opens()` — auto_orphan_* 合成 close 写入
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260718-ORPHAN_CASCADE_DELETE_MISSING
- **关联 FIX**: FIX-20260718-001
- **状态**: **CLOSED** — two-pass cascade: Pass 1 收集 pruned open IDs → Pass 2 级联删除匹配的 auto_orphan_* close

### CCT-20260718-002
- **Docket ID**: DQAF-20260718-002
- **日期**: 2026-07-18
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC governance 定时对账不工作。`daily_ops.py` 每夜调用 `cmd_reconcile()` 但路径全部硬编码为 XAU (`data/brains`, `live.yaml`) → BTC governance_state 漂移未被 cron 检测。
  - [Layer 2 — 中间异常]: `brain.py:cmd_reconcile()` 所有路径推导硬编码 — `brains_dir = project_root / "configs" / "brains"`, `data_path = project_root / "data"` 等 — 无资产参数化。
  - [Layer 3 — 根因]: L3 架构缺陷 — 单资产硬编码架构。`daily_ops.py` 调用 `cmd_reconcile()` 时未传递资产上下文（`base_dir` 已有资产信息但未利用）。
- **证据引用**:
  - Source 1: `scripts/brain.py:cmd_reconcile()` — hardcoded path derivation
  - Source 2: `scripts/daily_ops.py` — reconcile call site without --data-dir
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260718-SINGLE_ASSET_HARDCODED_PATHS
- **关联 FIX**: FIX-20260718-002
- **状态**: **CLOSED** — cmd_reconcile() 参数化 + daily_ops.py 从 base_dir 契约派生双资产路径

### CCT-20260718-003
- **Docket ID**: DQAF-20260718-003
- **日期**: 2026-07-18
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU statarb 策略不受 ConformalOUGate 约束。OU gate 查找不到已归档的 OU brain 配置 → 静默 passthrough，无任何日志或指标。
  - [Layer 2 — 中间异常]: XAU OU_Params_V6_Sniper 和 OU_Params_V7_M15 均已被 FIX-20260625-136 退役（统计显著亏损）。ConformalOUGate 加载时找不到任何 XAU OU 配置 → `_ou_configs_by_strategy` 为空 → `filter()` 走 passthrough 路径。
  - [Layer 3 — 根因]: L3 架构缺陷 — gate bypass 路径零可观测性。passthrough 分支无 logging、无 metrics、无 describe() 暴露 → 无法感知 statarb 信号未受门禁约束。
- **证据引用**:
  - Source 1: `core/execution/conformal_ou_gate.py:filter()` — passthrough path with no logging
  - Source 2: `configs/brains/archive_deprecated/OU_Params_V6_Sniper.json` — retired OU config
- **是否被推翻**: 否 (AR 拒绝恢复已归档配置 — 统计显著亏损)
- **关联 ReB Pattern**: ReB-20260718-SILENT_GATE_BYPASS_ZERO_OBSERVABILITY
- **关联 FIX**: FIX-20260718-003
- **状态**: **CLOSED** — 节流 WARNING + passthrough 计数器 + describe() 诊断

### CCT-20260710-001
- **Docket ID**: DQAF-20260710-001
- **日期**: 2026-07-10
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU h4_swing ticket 4108944294 (SHORT@4107.272) 从未盈利, 但 ATR 收缩 (entry_atr=4.88→current_atr≈1.6, atr_ratio=0.33) 触发 25 次 TP 收紧 (3866.73→4060.36), 每次以 `comment='tp'` modify_sltp。SL 固定 4252.08, SL:TP 比从 [(4252-4107)/(4107-3866)]≈0.60 恶化至 [(4252-4107)/(4107-4060)]≈3.09 (TP 距 entry 仅 47 点, 冒 145 博 47)。
  - [Layer 2 — 中间异常]: `compute_trail_tp()` gate `atr_ratio=current_atr/pos.entry_atr<0.80` 通过, `tp_distance=trail_mult×current_atr×1.75×tf_scale` 重算更近 TP。门禁仅检查 ATR 收缩, 无盈亏前提 — position_manager.py:1696-1712。
  - [Layer 3 — 根因]: L3 设计不对称 — SL trail (compute_trail_stop) 有 `trail_activation_atr` 盈亏水位线 (FIX-20260603-064, trail_stop_engine.py:217-223), TP trail (compute_trail_tp) 无对等保护。两个 trail 机制同出一源 (Chandelier 体系) 但保护不对称: SL 侧要求 ≥1.0×ATR 盈利才激活, TP 侧零盈亏感知。
- **证据引用**:
  - Source 1: `data/live_trade_journal.jsonl` ticket 4108944294 — 25 modify_sltp actions with comment='tp', tp 3866.73→4060.36
  - Source 2: `data/state/active_position.json` — entry_price=4107.272, cycles_held=14, breakeven_triggered=false
  - Source 3 (机制): `core/execution/position_manager.py:1696-1699` atr_ratio-only gate; `core/execution/trail_stop_engine.py:217-223` trail_activation_atr check in compute_trail_stop (对比)
  - Source 4 (golden_master): price path 4107.27→4136.30, never below entry (never profitable)
- **AR 对抗反驳**: 反假设(a)"收紧是对的 — 低 ATR 意味着原 TP 太远"→ **推翻**: 此逻辑仅对盈利持仓成立, 亏损持仓收紧 TP 让回升更难止盈; (b)"这是个例"→ **部分推翻**: XAU TP:SL=1:3.8 (总体 TP 命中率仅 7.1%), 系统性证据; (c)"SL trail 的 trail_activation_atr 已覆盖"→ **推翻**: trail_activation_atr 仅保护 SL trail, TP trail 独立运作无交叉保护。
- **是否被推翻**: 否 (存活假设; 三反假设均证伪)
- **关联 ReB Pattern**: ReB-20260710-TP_TRAIL_NO_PROFITABILITY_GATE

### CCT-20260709-003
- **Docket ID**: DQAF-20260709-003
- **日期**: 2026-07-09
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: live XAU h4_swing 4103318355 (SHORT) 的 TP 被 `comment='tp'` 逐周期从开仓 3823.46 (距 entry 232 点=3.5×H4_ATR, RR 1.66) 拉到 4044.8 (距 entry 11 点, RR 0.08, 冒 140 博 11)。影响半径: h4_swing 162/436(37.2%) + h1_swing 80/625(12.8%) 快照 RR<0.5。
  - [Layer 2 — 中间异常]: `position_manager.compute_trail_tp` gate `atr_ratio=current_atr/entry_atr`(均 M5, 3.40/4.61=0.738≤0.80) 触发, 然后 `tp_distance=trail_mult(2.0)×current_atr(M5,3.40)×1.75=11.9` 以 M5 小尺度覆盖 H4 大尺度开仓 TP (candidate=4055.844−11.9=4043.9≈实测 4044.8)。SL 全程不动 (H4 尺度 139.9)。
  - [Layer 3 — 根因]: RC-05 (boundary-error) L3 — per-TF ATR **半迁移**: FIX-20260706-027 把 per-TF ATR 注入**开仓定尺** (dynamic_sl_tp), 但 pos.entry_atr 仍存 M5 base (position_registration:198) 且管理期 current_atr 仍 M5 → 所有 bracket-relative 消费者 (compute_trail_tp / R 度量 / ratchet) 在错误尺度运算。跨 entry→management 交接的尺度边界未携带。
- **证据引用**:
  - Source 1: `scripts/verify_xau_post_restart_20260709.py` + ad-hoc RR 扫描 stdout — h4 37.2% / h1 12.8% RR<0.5 (Iron Law #11)
  - Source 2: `data/live_trade_journal.jsonl` 4103318355 open tp=3823.46 → modify tp=4044.8; `position_snapshots.jsonl` entry_atr=4.61/current_atr=3.40 (M5) 而 SL 距 139.9=2.0×H4_ATR
  - Source 3 (机制): `core/execution/position_manager.py:1691` `tp_distance=mult×current_atr×1.75`; `core/execution/dynamic_sl_tp.py:148` per-TF 定尺 vs `core/runtime/position_registration.py:198` `entry_atr=current_atr`(M5)
- **AR 对抗反驳**: 反假设(a)"SL 用 H4/TP 用 M5 开仓即异尺"→ **推翻** (open bracket 正是 1.66 RR); (b)"H4 ATR 真收缩到 3.4"→ **推翻** (若 H4 尺度 tp_distance≈210, 实测 11→反推 M5); (c)"entry_atr(4.61) 是定尺 ATR"→ **推翻** (SL 139.9≠2×4.61)。存活假设=compute_trail_tp 在 M5 尺度运算并覆盖 H4 bracket。
- **是否被推翻**: 否 (存活假设; 三反假设均证伪)
- **关联 ReB Pattern**: ReB-20260709-R_UNIT_MISMATCH_CROSS_TIMEFRAME (PER_TF_ATR_HALF_MIGRATION)
- **关联 FIX**: FIX-20260709-004
- **状态**: **CLOSED (终态)** — TP 侧 FIX-20260709-004 + 几何余项 FIX-20260709-006 (bracket_atr 换锚 breakeven/Chandelier/graduated_lock/max_lock)。激活端 (A1-A3): watermark/threshold/candidate 全切 bracket_atr with entry_atr fallback。锁定端 (L1-L2): graduated_lock_levels (3.0,1.5)/(5.0,3.5)→(1.0,0.5)/(2.0,1.0), max_lock_atr 4.0→2.0, bracket_atr 单位。Ratchet floor 故意未动 (entry_atr 稳定标尺, 双轨制)。反事实回测 96.1% XAU / 94.3% BTC breakeven death 存活。最终阈值标 SHADOW_TUNING_PENDING。proximity(Sev 4) + R 度量(observational) 仍 Deferred。

### CCT-20260708-004
- **Docket ID**: DQAF-20260708-004
- **日期**: 2026-07-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 仓位冲到 +1.4R~+6.3R MFE 后回撤, 被 signal_close 在 ~保本处市价平仓实现 ~$0, 或进一步回撤打 SL。give-back cohort (MFE≥1R 却 pnl≤0): BTC 121 / XAU 74。样本 4090084166 (short +1.44R, **0 改单**, signal_close breakeven); 样本 4067021409 (XAU long +6.30R, 27 改单 26 拒, tp@close 未置 0)。
  - [Layer 2 — 中间异常]: 唯一锁利机制=trailing SL, 但 87-89% 从未把 SL 推过 entry (SL@close 锁定 ≤0R)。三种失效: (a) `compute_trail_stop` 候选没推进就返回 None → 完全无底线; (b) 候选=extreme±mult×**current_atr**, 波动放大时 goalpost 移动, +1~1.5R 激活即锁负; (c) breakeven 楼层依赖 `breakeven_triggered`, 但 trail_dispatch.py:117 无条件置 True (意图锁, feasibility-skip/reject 也 latch)。主拒绝码 10025 NO_CHANGES (BTC 35/XAU 109)=重发同 SL。graduated_lock 首档 +3R 留 +1~3R 死区。
  - [Layer 3 — 根因]: RC-12 (missing-capability) L3 — 系统缺少一条**能抵达券商、抗改单失败、单调的硬利润棘轮**, 且模型出场(signal_close)在无底线时于保本处实现 $0。bracket 反转 (FIX-009) 仅 MODE_D 2.5-4% 尾部, **原 DQAF-004 假设误把尾部当主因, 被生命周期脚本推翻**。
- **证据引用**:
  - Source 1: `scripts/_diagnose_giveback_lifecycle.py` stdout — BTC MODE_B 105/121(86.8%) MODE_C 101/121(83.5%) MODE_D 3(2.5%); XAU MODE_B 66/74(89.2%) MODE_C 59(79.7%) MODE_D 3(4.1%); reject retcodes {10025,10006,10016}
  - Source 2: `data_btc/position_snapshots.jsonl` + `live_trade_journal.jsonl` ticket 4090084166 (+1.44R, 0 modify, SL@close=62831 在 entry 62651 之上=锁负)
  - Source 3 (对照): `data/` ticket 4067021409 (+6.30R, 27 modify/26 rej, tp@close=4186 未释放 → FIX-009 未生效)
  - Source 4 (机制): `core/execution/trail_stop_engine.py:131` compute_trail_stop (返 None 无底线) + `core/runtime/trail_dispatch.py:117` (breakeven_triggered 意图锁) + `scripts/mt5_bridge_worker.py:440` (10025 不在 _TRANSIENT_RETCODES)
- **AR 对抗反驳**: 反假设"bracket 反转 (SL 越 TP → FIX-009 释放 TP=0) 是主因"→ **被推翻**: MODE_D 仅 2.5-4%; FIX-009 本尊 ticket 4067021409 的 tp@close 仍=4186 (未置 0, FIX-009 未生效); BTC 侧主拒绝码是 10025 NO_CHANGES 非 10016 INVALID_STOPS。存活假设=trail 从未锁正底线 (MODE_B 87-89%)。
- **是否被推翻**: 否 (存活假设; 原 bracket-反转假设已被 AR 证伪并降级为 2.5-4% 尾部)
- **关联 ReB Pattern**: ReB-20260708-PROFIT_RATCHET_NEVER_REACHES_BROKER
- **关联 FIX**: FIX-20260708-004
- **状态**: **CLOSED** — Profit Ratchet Floor: peak r_max(entry_atr)≥arm_r 强制 SL 锁 ≥max(0.1R,r_max−1.0R), 折入候选即使 Chandelier 返 None 也托底, 独立于意图锁, 单调抑 NO_CHANGES。broker-bound 楼层封顶回撤 → R1 结构性吸收 MODE_C。意图锁 [[deferred_breakeven_intent_latch_20260708]] Deferred。

### CCT-20260708-003
- **Docket ID**: DQAF-20260708-003
- **日期**: 2026-07-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 用户实盘观察 BTC 到/超止盈却不平仓, 回撤后打止损; journal 却记为 breakeven ($0)。地面真相 ticket 3947528377: close 记录 close_price=63514.66 == entry_price 精确相等, pnl=0.0, reason=signal_close, label=breakeven; 而下一单 3 分钟后 @64598.99 开仓 → 真实市场 ~64599, ~+1084 点被记为 $0。
  - [Layer 2 — 中间异常]: `position_close_adapter._build_event` 取 `_new_deals = [d for d in deals if d.ticket > _cursor]` 后 `_deal = _new_deals[0]` (最早 deal)。adapter 每周期经 reconcile_and_record_closes() 新建实例 → `self._last_deal_id` 恒空 → cursor 恒 0 → `_new_deals` 含全部 deal → `[0]` = 最早 = DEAL_ENTRY_IN 入场 deal (price=入场价, profit=0, reason=3 signal) → close_price=入场价, pnl=0, label=breakeven。
  - [Layer 3 — 根因]: RC-06 (contract-violation) L3 — MT5 deal 模型知识在三处独立实现 (adapter 错取 deals[0]; reconciliation.py:118 与 mia_close.py:120 正确过滤 entry==1)。上游从未强制"一个 close 必须取自 DEAL_ENTRY_OUT 出场 deal"不变量, 允许分叉 → adapter 分支违约。同类模式 FIX-20260601-046 (label_builder 盲取 closes[0])。
- **证据引用**:
  - Source 1: `core/runtime/position_close_adapter.py` (pre-fix `_new_deals[0]` 无 entry 过滤)
  - Source 2: `data_btc/live_trade_journal.jsonl` ticket 3947528377 (close==entry==63514.66, pnl=0) + 次单 @64598.99
  - Source 3 (跨品种): `scripts/backfill_fabricated_breakeven.py` dry-run — BTC 14 (data_btc) + XAU 1 (data, ticket 4059439852) 同签名
  - Source 4 (对照正确路径): `core/runtime/reconciliation.py:118` + `core/runtime/mia_close.py:120` 均 `entry==1` 出场过滤
- **AR 对抗反驳**: 反假设"close==entry 是真实瞬时平仓(真 breakeven)"→ 被推翻: 次单 @64598.99 (相差 1084 点) 证明 3 分钟内市场已远离入场价, 若真在入场价平仓不可能在千点外重新开仓; pnl=0 与 reason=signal 是入场 deal 固有特征而非平仓结果。
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260708-BLIND_DEAL_INDEX_FABRICATES_BREAKEVEN

### CCT-20260628-062
- **Docket ID**: DQAF-20260628-062
- **日期**: 2026-06-28
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU governance_state.json 仅含 18 大脑条目，但 configs/brains/ 下存在 ~49 个 brain_registry_entry.v1 配置。PnLStore 有 49 大脑的 144K settled 记录，但 governance leaderboard 仅显示 18 大脑有指标注入。`governance_service.py:146-148` — `set_performance_metrics()` 对未注册大脑静默跳过（`_brain_states.get(brain_id)` 返回 None → 无操作，无日志，无告警）。
  - [Layer 2 — 中间异常]: Config→Governance 仅在 `_load_or_create_governance()` 首次创建时同步一次 (`daily_ops.py:117-144`)。后续新增 config 文件不会触发 governance 注册。配置状态变更 (candidate→live) 只在首次注册时写入 governance，之后 governance 独立演变 → 双轨漂移。
  - [Layer 3 — 根因]: RC-12 (missing-feature) + RC-09 (config-drift) — 缺少自动化 Config→Governance 对齐管道。FIX-20260613-076 确立 "governance owns lifecycle" 契约（正确），但未补充 "config defines existence" 的匹配机制。两者共同导致：config 定义大脑存在，governance 不知道自己需要管理它们。
- **证据引用**:
  - Source 1: `governance_service.py:146-148` — `self._brain_states.get(brain_id)` 静默跳过
  - Source 2: `daily_ops.py:117-144` — 首次创建时一次性同步，无后续对齐
  - Source 3: `daily_ops.py:3048-3056` — cmd_reconcile 已存在但仅处理 PnL ledger
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260628-CONFIG_GOVERNANCE_DUAL_TRACK_DRIFT

### CCT-20260628-063
- **Docket ID**: DQAF-20260628-063
- **日期**: 2026-06-28
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC circuit_breaker_active (management_only, consecutive_degraded=3) → 0 live brains → 全部 BTC 策略 should_trade=false → DQAF-059 ZERO LIVE brains p_win=0.40 fallback。`governance_state.json` transition_log 显示 V4 在 3 小时内被降级 3 次 (06:51/09:04/09:34)，每次 SSOT reconciliation (08:55/09:29/10:20) 恢复后下一 governance cycle 再次降级。governance state 从 3 膨胀到 16 brains（12 个幽灵重新注册）。
  - [Layer 2a — 幽灵注册]: `governance_scheduler.py:300` — `pnl_store.get_all_metrics()` 返回 PnL ledger 中全部 13 个 brain（含 12 个已归档 brain: V1/V2/V3/V5/V6/V7/V8/V9/V10/LGB_V1/V11/V12_H1_Survival）。`governance_scheduler.py:357-358` — `current_state is None → governance.register_brain(brain_id, "candidate")` 每次 cycle 将这些幽灵重新注册为 candidate。
  - [Layer 2b — 评分过严]: Quality Engine V4 评分 27.69→"degraded" tier→probation。Legacy 路径 WR=35.5% < WR_PROBATION_THRESHOLD=45%→probation。RR-adjusted 通道 (FIX-20260627-152) PF=1.15 < PF_RR_ADJUSTED_MIN=1.3→blocked。V4 有 298 trades/+42.4R/PF=1.15 但三条路径全部通向 probation。
  - [Layer 2c — Last-live guard 绕过]: FIX-20260628-162 在 `governance_rule_engine.py:201-210` 添加 last-live guard — 但实际降级走 `governance_scheduler.py:462` → `GovernanceService.transition()` 直接调用，绕过 rule engine 的 `evaluate()` 路径。Guard 从未被检查。
  - [Layer 3 — 架构根因]: RC-11 (stale-data) + RC-06 (contract-violation) — PnL ledger 无生命周期 GC 机制，已归档 brain 的历史 PnL 数据永久残留成为幽灵注册数据源。双轨降级路径（quality_engine + legacy threshold）均绕过 rule engine 的 last-live guard → 单一 live brain 无任何防护。
- **证据引用**:
  - Source 1: `governance_state.json` transition_log — V4 3 次降级 (06:51/09:04/09:34)，3 次 SSOT 恢复 (08:55/09:29/10:20)，13 次幽灵注册
  - Source 2: `governance_scheduler.py:300` — `pnl_store.get_all_metrics()` 返回 13 个 brain；`:357-358` — 无条件 `register_brain(bid, "candidate")`
  - Source 3: `governance_scheduler.py:462` — `governance.transition(brain_id, target_status)` 直接调用，绕过 rule engine
  - Source 4: `live_trade_journal.jsonl` — ticket=4006314705 V4 trade (09:40 OPEN, 10:04 TP close, +$1.38)
  - Source 5: `brain_pnl_ledger.json` — settled 表含 13 个 brain（含 12 个已归档）
  - Source 6: `brain_quality_engine.py:323-357` — V4 score=27.69, tier="degraded"
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260628-PING_PONG_DEMOTE

### CCT-20260628-061
- **Docket ID**: DQAF-20260628-061
- **日期**: 2026-06-28
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 49 XAU 大脑在 BrainPnLStore 中有指标数据 (144K+ settled 记录) 但 governance_state 中仅 18 大脑有非空 `performance_metrics`。31 大脑的指标完全不可见于 downstream (leaderboard, weighter, strategy_evaluator)。
  - [Layer 2 — 中间异常]: (A) `governance_scheduler.py:347-350` — 循环遍历 `all_metrics`（来自 PnLStore 的 49 大脑），调用 `governance.set_performance_metrics()`，但 `governance_service.py:146-148` 因大脑未注册而静默跳过。(B) `scheduler_service.py:298-317` — MT5 调度器 purge 逻辑检查 `source` 字段清除 backtest 指标，但 `daily_ops` → `governance_scheduler` 使用 `_data_source` 字段 → 字段名不匹配 → 合法 daily_ops 注入指标被作为 stale backtest 清除。(C) Journal-based metrics 因 80% XAU entries 缺少 `position_ticket` (有别于 BTC 的 `event` 字段) → `compute_journal_brain_metrics()` 跳过无 ticket 条目 → journal 无法为缺少大脑提供 fallback。
  - [Layer 3 — 根因]: RC-06 (contract-violation) + RC-09 (config-drift) — `set_performance_metrics()` 的静默跳过是合约违规：调用方期望指标被注入，实现方因未注册而吞没数据。`_data_source` vs `source` 字段名分裂是配置漂移：两个独立演进子系统约定不同的字典键名。
- **证据引用**:
  - Source 1: `governance_service.py:146-148` — 静默跳过逻辑
  - Source 2: `scheduler_service.py:298-301` — purge 仅检查 `source` 不检查 `_data_source`
  - Source 3: `governance_scheduler.py:377-379` — daily_ops 使用 `_data_source` 键名
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260628-GOVERNANCE_REGISTRATION_SILENT_SKIP

### CCT-20260626-001
- **Docket ID**: DQAF-20260626-001
- **日期**: 2026-06-26
- **置信度**: confirmed (git diff confirmed BLE001 narrowing + evidence from 6 sources)
- **因果链**:
  - [Layer 1 — 症状]: `data_btc/golden_master.jsonl` 06-20~06-24 连续 5 天零记录 (正常每天 ~150 周期)。`data_btc/regime_snapshots.jsonl` 06-21 起跟随停止 (cascade: build_regime_snapshots.py 以 GM 为主数据源)。所有下游 regime 分析工具盲化。
  - [Layer 2 — 中间异常]: (A) BLE001 Phase 3a (FIX-20260619-057) 将 live_cycle.py golden_master 异常处理从 `except Exception` 收窄为 `except (ValueError, TypeError, OSError)`。`record_cycle_inputs()` 内部 `regime_info.get()` 对非 dict 类型抛 `AttributeError` — 不在收窄元组内 → 静默逃逸。(B) `golden_master.py:170` `except OSError: pass` 为盲 catch 零日志吞没 — 即使同文件内的 I/O 错误也完全不可观测。
  - [Layer 3 — 根因]: L3 架构缺陷 — 非阻塞 telemetry 路径异常处理契约脆弱。(a) `record_cycle_inputs()` 无内部防御深度 — 依赖调用方猜对异常类型。(b) BLE001 收窄时未审计调用链完整异常剖面。(c) golden_master 失败无告警/监控/自动恢复 — 5 天盲区。(d) build_regime_snapshots 单一数据源依赖 — GM 故障直接级联。
- **证据引用**:
  - Source 1: `data_btc/golden_master.jsonl` — 06-19 cycle 153 停止, 06-20~06-24 零条, 06-25 仅 6 条
  - Source 2: `git diff 0002ea83..49e46a4c -- core/runtime/live_cycle.py` — except Exception → except (ValueError, TypeError, OSError)
  - Source 3: `core/runtime/golden_master.py:76-77` — regime_info.get() 无防御
  - Source 4: `core/runtime/golden_master.py:170` — except OSError: pass 盲 catch
  - Source 5: `scripts/build_regime_snapshots.py:27-29` — golden_master.jsonl 为主数据源
  - Source 6: XAU `data/golden_master.jsonl` — 5,890 条持续至 06-25 (同一 period 正常), 证实 BTC-only 问题
- **是否被推翻**: 否 — AR 5 条假设全被推翻 (GOLDEN_MASTER_RECORD=0 / base_dir 变化 / 系统宕机 / block_new_entries / 收窄类型足够)
- **关联 ReB Pattern**: ReB-20260626-001
- **关联 FIX**: FIX-20260626-001

### CCT-20260622-060
- **Docket ID**: DQAF-20260622-060
- **日期**: 2026-06-22
- **置信度**: confirmed (5 工程契约 × 双模 PSI × 实测验证)
- **因果链**:
  - [Layer 1 — 症状]: PSI 在 raw 特征空间 36/40 特征 Sev1 (mean_PSI=2.73, max_PSI=8.28)。归一化后降至 38/40 Sev1 (mean_PSI=3.42, max_PSI=12.43) — 不降反升确认真阳性 regime change。3 个独立 PSI 实现 (等频/等宽/合并分箱) 互不一致。
  - [Layer 2 — 中间异常]: (A) `--compute-baseline` flag 定义但从未实现 — baseline 不可复现。(B) PSI 在 raw 特征空间计算, 树模型 (`normalize: false`) 对尺度变换不敏感 — PSI 高 ≠ 模型退化。(C) 阈值 0.10/0.25 从归一化场景校准, 在 raw 空间不适配。(D) 无 model-performance correlation 验证框架 — PSI 信号不可操作。
  - [Layer 3 — 根因]: L3 架构缺陷 — PSI 监控缺乏 (1) 归一化策略 (训练 μ/σ vs 滚动 μ/σ), (2) 双模解耦 (regime vs anomaly), (3) 工程保护 (零方差/对数发散/窗口隔离/样本非对称)。`stability_monitor.compute_psi()` 使用等宽分箱 (合并数据), 而 `monitor_feature_drift._compute_psi()` 使用固定 baseline 分箱 — 两个"PSI"不可比。
- **证据引用**:
  - Source 1: `scripts/monitor_feature_drift.py:1-712` — 完整重写 (DQAF-060), 287→712 lines
  - Source 2: `core/brains/services/stability_monitor.py:31-87` — `compute_psi()` @deprecated
  - Source 3: `data/training/balanced_v1/feature_baseline_v9_normalized_20260622.json` — 新 baseline (160,138 samples, 40 features, norm μ/σ 内嵌)
  - Source 4: CLI 实测 — Mode A: mean_PSI=3.42, Mode B: mean_PSI=9.0
  - Cross-symbol: BTC confirmed regime-changed. XAU PSI pending empirical scaler.
- **是否被推翻**: 否 — AR 假设 (归一化后 PSI 应降) 被实测推翻: PSI 反升 2.73→3.42, 证伪"raw 特征导致假阳性"假设, 确认"BTC 真的 regime-changed"
- **关联 ReB Pattern**: ReB-20260622-060
- **关联 FIX**: FIX-20260622-060

### CCT-20260622-058-bis
- **Docket ID**: DQAF-20260622-058-bis
- **日期**: 2026-06-22
- **置信度**: confirmed (code audit × 3 sites verified × runtime confirmation)
- **因果链**:
  - [Layer 1 — 症状]: DQAF-058 部署后 `micro_scaler_loaded: false` 持续。健康检查 `MICRO_SCALER_NOT_LOADED` 警告未消除。MetaFilter 仍然在 raw features 上运行。
  - [Layer 2 — 中间异常]: DQAF-054 修复了 3 个 `MicrostructureFeatureAdapter` 实例化站点, DQAF-055 补齐了其余 2 个 — 但 `meta_signal_filter.py:135` 使用 `self._micro_scaler = joblib.load(micro_scaler_path)` 直接加载, 完全绕过 adapter 的 `_load_scaler_json()`。`live_intent_loop.py:1512` 和 `bootstrap_v9.py:91` 缺少 `resolve_scaler_path()` 回退。
  - [Layer 3 — 根因]: L2 逻辑缺陷 — `MetaSignalFilter` 是 `MicrostructureFeatureAdapter` 的**消费者**而非子类, 其 scaler 加载是独立实现。DQAF-054 的模式搜索 (`grep joblib.load`) 遗漏了此站点因为此处不是 adapter 实例化而是**直接消费**。
- **证据引用**:
  - Source 1: `core/execution/meta_signal_filter.py:135` — `joblib.load(micro_scaler_path)` (修复前)
  - Source 2: `scripts/live_intent_loop.py:1512-1520` — `resolve_scaler_path()` 回退 (新增)
  - Source 3: `apps/engine/bootstrap_v9.py:91-99` — `resolve_scaler_path()` 回退 (新增)
  - Cross-symbol: 仅 BTC 受影响 (XAU 尚无 MetaFilter 配置)
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260622-058-bis
- **关联 FIX**: FIX-20260622-058-bis

### CCT-20260622-058
- **Docket ID**: DQAF-20260622-058
- **日期**: 2026-06-22
- **置信度**: confirmed (6 源 ECoL + DA/AR 双轨 + 跨品种验证)
- **因果链**:
  - [Layer 1 — 症状]: BTC PSI 38/40 特征 Sev1, `micro_scaler_loaded: false` 持续 23 天。`MICRO_SCALER_NOT_LOADED` 警告从未触发（健康检查缺失此检查项）。
  - [Layer 2 — 中间异常]: (A) `MicrostructureFeatureAdapter.resolve_scaler_path()` 硬编码 `btc_micro_scaler.json` → XAU 永远找不到 scaler。(B) `require_scaler=True` + 无 scaler → `DataIntegrityError` 阻断 XAU 启动。(C) 健康检查 `check_meta_filter_state` 不提取 `micro_scaler_loaded` → 运维盲区。
  - [Layer 3 — 根因]: L3 架构缺陷 — DQAF-054 引入的 JSON scaler 加载替换了 joblib, 但部署激活是独立步骤: 需要 (1) 生成 JSON scaler 文件, (2) 配置 `micro_scaler_path`, (3) 健康检查验证。这三个步骤均缺失。冷启动路径 (无 Feature Store 的新品种/新环境) 从未被设计 — 系统要求 scaler 必须存在, 但没有"不存在时怎么办"的答案。
- **证据引用**:
  - Source 1: `core/features/adapters/microstructure_feature_adapter.py:resolve_scaler_path()` — 修复前硬编码 `btc_micro_scaler.json`
  - Source 2: `core/observability/health_checks.py:check_meta_filter_state()` — 修复前不检查 `micro_scaler_loaded`
  - Source 3: `scripts/generate_micro_scaler.py` — 新建多品种 scaler 生成脚本
  - Source 4: `data_xau/models/xau_micro_scaler.json` — XAU 冷启动 identity scaler
  - Source 5: `data_btc/models/btc_micro_scaler.json` — BTC 实证 scaler (前序 DQAF-054 产出)
  - Cross-symbol: XAU 受阻断（启动熔断）, BTC 受静默退化（raw features）
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260622-058
- **关联 FIX**: FIX-20260622-058

### CCT-20260622-057
- **Docket ID**: DQAF-20260622-057
- **日期**: 2026-06-22
- **置信度**: confirmed (Layer 1-2: confirmed by code review + ledger data; Layer 3: confirmed by DQAF-033/034 external-close evidence)
- **因果链**:
  - [Layer 1 — 症状]: Label coverage 从 85%→65% (XAU), 67%→40% (BTC)。Timestamp inversions 从 336→365 (XAU), 22→52 (BTC)。Evidence: `audit_data_exhaustive.py:216-222`, `live_labels.jsonl` per-symbol counts。
  - [Layer 2 — 中间异常]: (A) `build_trade_records()` 依赖 close_price 计算 PnL — 当 close_price 缺失时 PnL=None → label="unlabeled"。无 label_contract defense layer 可回退至 SL/TP barrier 分类。(B) `live_cycle.py:1338` 使用 `.locks` 锁目录而所有其他 writer 使用 `locks` — 跨进程 FileLock 协调失效。(C) `_merge_overflow_files` 零锁写入共享 journal。
  - [Layer 3 — 根因]: (A) DEAL_REASON_SIGNAL 外部平仓比例 66% (DQAF-033/034) → journal ingestion 盲区。(B) 多进程 journal 写入架构 + 锁命名空间碎裂 (L3 architecture defect)。(C) label pipeline 无 defense layer (L2 logic defect with L3 contributory)。
- **证据引用**:
  - Source 1: `label_builder.py:176-307` — `build_trade_records()` matching logic, `_classify_label(pnl)` vs `_classify_barrier_label()`
  - Source 2: `live_cycle.py:1338` — `.locks` lock directory (active bug)
  - Source 3: `mt5_bridge_worker.py:220` — `_merge_overflow_files` zero-lock write
  - Source 4: `daily_ops.py:2049` — `_step_label_builder` called without `contract_path`
  - Source 5: DQAF-20260621-033/034 — 66% external close evidence
  - Source 6: `audit_data_exhaustive.py:216-222` — coverage computation logic (LONG-only denominator)
  - Cross-symbol: Both XAU and BTC affected — rules out symbol-specific code asymmetry
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260622-LABEL_COVERAGE_DEGRADATION, ReB-20260613-JOURNAL_LOCK_NAMESPACE_FRAGMENTATION

### CCT-20260621-042
- **Docket ID**: DQAF-20260621-042
- **日期**: 2026-06-21
- **置信度**: confirmed (10 项发现 × 8 项坐实 × 双源交叉验证: journal + state files + code audit)
- **因果链**:
  - [Layer 1 — 症状 (视图静默损坏)]: Leaderboard 崩溃 (Sev 1) — `brain_performance` 维度全部为空, `leaderboard.json` 产出空排行榜, `daily_ops` Poison Pill 阻断整个管线。Journal vs Labels 38% 缺口 (Sev 2) — 38% 的交易在 journal 中存在但在 label 导出中缺失。治理含 3140 笔回测数据 (Sev 2) — governance_state.json 被 backtest 时代的 V12_H1 历史数据污染。校准器 p_win 退化至 0.5 (Sev 2), Alpha 数据不一致 (Sev 2), 健康报告自相矛盾 (Sev 2), golden_master 未排序 (Sev 3)。**修复被实盘进程覆盖** (Sev 1 新发现) — 人工修复的 state JSON 被 live 进程在下一 cycle 覆写回损坏版本。
    - 证据: `data_btc/reports/leaderboard.json` — brains=[]; `data_btc/reports/live_labels.jsonl` — 38% 缺口; `data_btc/governance_state.json` — V12_H1 3140 trades
  - [Layer 2 — 中间异常 (生成器数据计算残缺 + 鸭子类型无防备)]: (A) `compute_journal_brain_metrics()` 产出缺少 `sharpe_ratio` / `cumulative_pnl` 等关键字段的字典 → `BrainLeaderboard._validate_metrics()` 在缺少字段时未抛异常（使用 `dict.get(key, default)` 贴纸）, 下游静默产出空排行榜。(B) `daily_ops.py` `_step_retraining_check()` 中 `leaderboard.get("total_decisions", 0) == 0 and _gov_states` → `DataIntegrityError` → 管线 Poison Pill 阻断 — 这是正确的 Fail-Closed 行为, 但暴露了上游 generator 数据不完整的事实。(C) governance_state.json 中 `total_trades` 字段包含 backtest 时期的 3140 笔交易 — governance_service 未按 `is_live` 字段过滤历史数据。(D) 人工直接修改 state JSON → live 进程在下一 cycle 从 ledger 重建时覆盖修复 — 两套写入器 (人工 + live 进程) 对同一物化视图的竞态覆写。
    - 证据: `core/brain_leaderboard.py:_validate_metrics()` — 修复前 `dict.get(key, default)` 贴纸; `scripts/daily_ops.py:_step_retraining_check()` — Poison Pill 触发逻辑; `data_btc/governance_state.json` — V12_H1 is_live=false 但 total_trades=3140
  - [Layer 3 — 根因 (架构坍塌 — 混淆不可变账本与物化视图)]: **RC-11 (architecture-violation)** — `IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION` 架构模式被系统性违反。系统的物理设计是 Event Sourcing (ledger → generator → view), 但日常运维中人工绕过生成器直接修改物化视图——混淆了 `append-only immutable journal` 与 `regenerated ephemeral view` 两个本体论范畴。这导致: (a) 人工修复与实盘进程的互斥覆写竞态, (b) 修复无法持久 (下一 cycle 被覆盖), (c) 根因从未被触及 (因为 generator code 中的 bug 被"直接修 JSON" 的运维惯性永久掩盖)。同类根因在 DQAF-20260615-011 (退役大脑幽灵霸占排行榜 — 视图未过滤活性)、DQAF-20260615-012 (orphan 合成条目污染告警 — 视图消费者未区分合成/真实) 中反复出现。
    - 证据: `CLAUDE.md` 2. AGENT BEHAVIORAL RESTRICTIONS — 4 条 RED 禁令显式编码了正确的架构关系; `.gitignore` — 24 条 ephemeral state 模式物理隔离; `tests/test_state_reconstruction.py` — 26 契约测试强制 ledger→view 重建可复现性
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 385+ 条记录, 38% label 缺口
  - Source 2 (State Files): `data_btc/reports/leaderboard.json` — brains=[]; `data_btc/governance_state.json` — 3140 backtest trades; `data_btc/calibrator_feed_state.json` — p_win=0.5
  - Source 3 (Code Audit): `core/feedback/live_journal_metrics.py`, `core/brains/services/brain_leaderboard.py`, `scripts/daily_ops.py` — 完整 generator 链路追踪
  - Source 4 (Git History): 5 commits (7d448ae → e8fe77c5) — 四防线全生命周期
  - Source 5 (Cross-symbol): `data/live_trade_journal.jsonl` — XAU 同架构, 确认非品种特化
- **是否被推翻**: 否 — AR 反向假设 (单文件损坏, 修复 JSON 即可) 被 10 项发现中 8 项坐实推翻: 问题是架构级而非数据级
- **关联 ReB Pattern**: ReB-20260621-042 (`IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION`)
- **关联 FIX**: FIX-20260621-042

### CCT-20260621-043

- **Docket**: DQAF-20260621-043
- **Confidence**: confirmed (7 源交叉验证 × 实测复现)
- **Refutation**: 否 — AR 反向假设 (purge 已运行后被覆盖) 经 `_step_governance` 实测推翻，确认治理周期从未成功执行

**Layer 1 — 症状 (视图静默损坏)**:
  - 治理状态含 14 brains 回测数据 (BTC V12: 3140 trades, Sharpe -16.66; XAU 13 brains >1000 trades)
  - `_step_governance` 每次都返回 `{'status': 'error', 'error': "'dict' object has no attribute 'win_rate'"}`
  - 证据: `data_btc/governance_state.json` — V12_H1 total_trades=3140; `daily_ops._step_governance('data_btc', dry_run=True)` 实测 crash
  - 置信度: confirmed

**Layer 2 — 中间异常 (类型管线的隐式断裂 + 静默吞没反模式)**:
  - FIX-20260621-032 在 `governance_scheduler.py:264` 添加 `all_metrics[_bid] = _jm` — 将 journal dict 直接赋值给期望 BrainPnLMetrics dataclass 的集合
  - `compute_journal_brain_metrics()` 返回 `dict[str, dict]` — 键访问 (`.get()`)
  - `pnl_store.get_all_metrics()` 返回 `dict[str, BrainPnLMetrics]` — **dataclass 属性访问** (`.win_rate`)
  - 下游 `metrics.win_rate` (line 301) 在 dict 上触发 `AttributeError`
  - `daily_ops._step_governance` 的 `except Exception` (line 530) 静默吞没此错误 — 返回 `{"status": "error"}` 但无日志无告警
  - 证据: `governance_scheduler.py:264` — 赋值语句; `daily_ops.py:530-532` — except Exception; 实测 dry_run crash 输出
  - 置信度: confirmed

**Layer 3 — 根因 (架构缺陷: 跨模块边界缺乏类型强制)**:
  - L2 逻辑缺陷: 单一赋值语句的类型不匹配 (dict vs dataclass) 导致全治理周期静默崩溃
  - L3 架构缺陷: IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION 的数据管道缺乏端到端类型约束 —
    Journal (SSOT, dict) → governance_scheduler (期望 dataclass) → governance_state.json (projection)
    中间没有任何 Schema 校验或类型转换层
  - 反模式 (ReB-043): `BOUNDARY_TYPE_ENFORCEMENT_AND_EXPLICIT_CATCH` — 
    跨核心子系统边界禁止原生 dict 裸奔; 顶层调度器禁止无类型断言的 `except Exception`
  - 证据: 完整代码追踪 governance_scheduler.py:250-307 + daily_ops.py:493-532 + brain_pnl_ledger.py:BrainPnLMetrics dataclass
  - 置信度: confirmed

**修复验证**:
  - `_step_governance('data_btc', dry_run=True)` → `Status: ok, Brains assessed: 14` (修复前: Status: error, crash)
  - `_step_governance('data', dry_run=True)` → `Status: ok, Brains assessed: 50` (修复前: crash)
  - 3/3 合约测试 PASSED
  - purge: BTC 1 brain 修正 / XAU 13 brains 修正

**交叉引用**: ReB-20260621-043 (`BOUNDARY_TYPE_ENFORCEMENT_AND_EXPLICIT_CATCH`), FIX-20260621-043, DQAF-20260621-042 (上游)

---

### CCT-20260620-002
- **Docket ID**: DQAF-20260620-002
- **日期**: 2026-06-20
- **置信度**: confirmed (3 源确认: code audit + git history + cross-file trace)
- **因果链**:
  - [Layer 1 — 症状]: XAU budget_breached 误触发 — 单笔 -$5 亏损被计为 -500% 日 PnL，导致熔断器错误断开。budget.daily_pnl_pct 累积值远超 -3% 限制，但实际 USD 亏损仅 ~$5。断路器误触发后系统停止交易。
  - [Layer 2 — 中间异常]: 三条独立代码路径将 raw USD 值传递给 `StrategyBudget.record_trade(pnl_pct, is_win)`，该参数期望的是 decimal fraction (如 0.005 = 0.5%) 而非 USD 绝对值。(A) `live_cycle.py:2348-2358` MIA close 路径 — `_mia_pnl` 为 raw USD，直接传入 record_trade；(B) `managed_close.py:317` — `_pnl_pct = float(pnl) / 1000.0` 硬编码 divisor；(C) `position_close_adapter.py:255-260` — `_notify_budget` 回退路径未经 USD→pct 转换。唯一正确的路径是 `live_cycle.py:1617` reconciliation — `_pnl_pct = _evt.pnl / _eq`。
  - [Layer 3 — 根因]: RC-06 (contract-violation) — `pnl_pct` 参数名的语义契约仅存在于变量名中，未经类型系统强制执行。`float` 类型接受任何数值，USD 与 percentage 在类型层面不可区分。这是 L3 架构缺陷: 量纲安全依赖人工审查而非编译器闸门。同类模式已出现于 DQAF-20260615-011 (pnl_r ↔ pnl_per_unit 量纲混乱) 和 DQAF-20260607-007 (策略盈亏 USD vs R-multiple 标签错位)。
- **证据引用**:
  - Source 1: `core/execution/strategy_budget.py:record_trade()` — pnl_pct 参数 docstring 明确期望 decimal fraction
  - Source 2: `core/runtime/live_cycle.py:2348-2358` (pre-fix) — MIA 路径 raw USD 直接传入
  - Source 3: `core/execution/managed_close.py:317` (pre-fix) — 硬编码 `/1000.0` divisor
  - Source 4: `core/runtime/position_close_adapter.py:255-260` (pre-fix) — `_notify_budget` 回退路径未转换
  - Source 5: `core/runtime/live_cycle.py:1617` — reconciliation 路径正确转换 (正面控制)
- **是否被推翻**: 否 — AR 反向假设 (budget 计算正确, 实际亏损确实超限) 被 journal 逐笔去重统计推翻: 实际 PnL ≈ -$5, 远低于 -$30 daily limit
- **关联 ReB Pattern**: ReB-20260620-002 (PNL_UNIT_MIXING)
- **关联 FIX**: FIX-20260620-003

### CCT-20260615-012
- **Docket ID**: DQAF-20260615-012
- **日期**: 2026-06-15
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU 钉钉告警显示胜率 0.91% (7/767)，断路器断开。当日盈亏仅 $0.39，但窗口内"交易数"高达 767。
  - [Layer 2 — 中间异常]: 767 笔"交易"中 752 笔是 `auto_orphan_rejected` 合成 close 条目 (pnl=0, position_ticket=None)。这些条目由 `cleanup_orphan_opens()` 在启动时生成，为历史 rejected open 写 synthetic close。由于没有 ticket，绕过了告警上下文的去重逻辑 (`if _pos_tkt is not None`)。pnl=0 被计为"亏损"→752:7 的比例将真实胜率从 46.67% 稀释至 0.91%。
  - [Layer 3 — 根因]: RC-06 (contract-violation) — 告警上下文构建器未区分 `auto_orphan_*` 合成条目与真实交易。`cleanup_orphan_opens()` 生成的 synthetic close 是合法审计记录，但不应参与实时告警统计。
- **证据引用**:
  - Source 1: `data/live_trade_journal.jsonl` — 2671 orphan close, 752 today
  - Source 2: `core/ledger/services/journal_cleanup.py:275-318` — cleanup_orphan_opens() 生成逻辑
  - Source 3: `core/runtime/live_cycle.py:903-959` — 告警上下文 journal 扫描逻辑
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260615-012 (ORPHAN_ENTRY_ALERT_POLLUTION)

### CCT-20260615-011
- **Docket ID**: DQAF-20260615-011
- **日期**: 2026-06-15
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 钉钉告警显示"最差大脑"为 `BTC_Swing_V11_M15_Directional`，累积PnL(R) -1452.68，胜率 0.1579。但该大脑已归档禁用（`enabled=False`），不在 `governance_state.json` 活跃列表中。告警"最差大脑"指标与实盘日记账 $4.61/日 无法对账。
  - [Layer 2 — 中间异常]: (A) `get_all_metrics()` 返回所有大脑（含退役/归档），min(cumulative_pnl) 选中的永远是历史最长的退役大脑。(B) `load_from_stream()` 将事件流的 `pnl_r`（R-multiple, 相对值）直接赋值给 `pnl_per_unit`（美元/单位, 绝对值）→ 同一字段承载两种不可比量纲 → 累积求和无数学意义。
  - [Layer 3 — 根因]: RC-06 (contract-violation) + RC-11 (stale-data)。Event Sourcing 迁移中 `pnl_r ↔ pnl_per_unit` 的序列化契约未定义单位转换。退役大脑的历史数据未从告警评比中排除——"幸存者偏差"的逆向版本：尸体统治排行榜。
- **证据引用**:
  - Source 1: `data_btc/ledger_events.jsonl` — 1227事件(仅live+migration)中 V11 pnl_r 累积=-1452.69
  - Source 2: `configs/live_btc.yaml` — V11路径在 `archive/` 下, `enabled=False`
  - Source 3: `data_btc/governance_state.json` — V11不在 brain_states 中
  - Source 4: `brain_pnl_ledger.py:872` — `"pnl_per_unit": event.pnl_r` 单位错配
  - Source 5: `brain_pnl_ledger.py:904` — `_t.get("pnl", 0)` 读取不存在的字段名
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260615-011 (ARCHIVED_BRAIN_ALERT_POLLUTION)

### CCT-20260608-001a
- **Docket ID**: DQAF-20260608-001
- **日期**: 2026-06-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `execution_state.json` 显示 `circuit_breaker_tripped: true` 但 `consecutive_degraded: 0`。备份文件（6/6）显示 `false` → 熔断器在 6/6~6/7 间触发且从未自愈
  - [Layer 2 — 中间异常]: 熔断器有 3 种触发路径（bridge_silence/cycle_stall×3/ExecutionQueueFatalError），但自愈逻辑仅覆盖 cycle_stall 路径。bridge_silence 和 FatalError 不递增 `consecutive_degraded` → 自愈条件 `_consecutive_degraded_cycles > 0` 永久为 False
  - [Layer 3 — 根因]: RC-06 状态机非对称陷阱 (Asymmetric State Machine Trap) — 多路径触发 vs 单路径自愈的不完备状态转换表。`live_cycle.py:2771` 自愈条件与 `live_cycle.py:2634` bridge_silence 触发路径不兼容
- **证据引用**:
  - Source 1: `data_btc/state/execution_state.json:24` — `circuit_breaker_tripped: true, consecutive_degraded: 0`
  - Source 2: `data_btc/state/execution_state.json.bak:31` — 6/6 03:54 仍为 `false`
  - Source 3: `core/runtime/live_cycle.py:2634-2648` + `2771-2784` — 触发与自愈代码源
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260608-001

### CCT-20260608-001b
- **Docket ID**: DQAF-20260608-001
- **日期**: 2026-06-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `meta_filter_state.json` 所有缓冲区为空（pred_history=0, pred_buffer=0, atr_buffer=0, micro_spread_buffer=0）
  - [Layer 2 — 中间异常]: `MetaFilterGate(model_dir=f"{base_dir}/models/meta_filter_v3")` → `data_btc/models/meta_filter_v3/` 不存在 → `_mg.load()` 抛出 FileNotFoundError → `except Exception` 静默吞噬 → `_mg.is_loaded=False` → `state._meta_filter_gate` 从未赋值
  - [Layer 3 — 根因]: RC-09 config-drift — BTC 品种迁移到 `data_btc/` 时，静态模型文件留在 `data/models/`，路径构造盲目使用 `config.base_dir` 导致断裂
- **证据引用**:
  - Source 1: `core/runtime/live_cycle.py:3900` — `model_dir=f"{config.base_dir}/models/meta_filter_v3"`
  - Source 2: `data/models/meta_filter_v3/` 存在（4个文件） vs `data_btc/models/meta_filter_v3/` 不存在
  - Source 3: `meta_filter_state.json` — 全空缓冲区
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260608-002

### CCT-20260608-001c
- **Docket ID**: DQAF-20260608-001
- **日期**: 2026-06-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `calibrator_feed_state.json` 中 `"updated_utc": "35"` — 值 "35" 不是 ISO 时间戳
  - [Layer 2 — 中间异常]: `scripts/daily_ops.py:379` 代码 `"updated_utc": str(cal.describe().get("sample_count", "?"))` — 字段名期望时间戳但实际读取 `sample_count` 键（整数 35）
  - [Layer 3 — 根因]: RC-06 contract-violation — `cal.describe()` 不返回 `updated_utc` 字段，开发者使用错误键名回退到 `sample_count`
- **证据引用**:
  - Source 1: `data_btc/calibrator_feed_state.json:2` — `"updated_utc": "35"`
  - Source 2: `scripts/daily_ops.py:379` — 源码行确认为字段-值错配
  - Source 3: `core/execution/conformal_calibrator.py:337` — `describe()` 返回键名确认为 `sample_count`
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260608-003
- **Docket ID**: DQAF-20260607-005
- **日期**: 2026-06-07
- **置信度**: confirmed (3 源确认)
- **因果链**:
  - [Layer 1 — 症状]: BTC 持仓 ticket=3807675970 数小时未平仓，系统持续开新仓 (vol=0.09, 113 周期)，但 exit watchdog 未管理任何持仓。日志仅 3 个 management_phase 事件 vs 113 个 multi_strategy_eval 事件。
    - 证据: `intent_20260606T134832Z.log` — `cycle_error` 事件 (13:54:45) + `orphan_position_adopted` 事件 (13:59:44) + 仅 3 个 management 事件 vs 113 周期
  - [Layer 2 — 中间异常]: `execution_queue.py:350` 中 `_close_result` 变量未初始化即被引用 (`UnboundLocalError`)，导致 `flush()` 崩溃。调用方 `live_intent_loop.py:1902` 的 `except Exception` 仅打印日志但未熔断，系统继续新开仓循环。
    - 证据: `execution_queue.py` git diff 显示 line 194 的 `_close_result = None` 初始化是后加补丁；traceback 确认崩溃点在 `flush()` 内部
  - [Layer 3 — 根因]: **RC-07 (Fail-Open 反模式)** — 派发管道的致命异常被通用 `except Exception` 吞噬，未触发 circuit_breaker。孤儿收养逻辑仅存储 `source + adopted_at` 元数据，exit watchdog 无足够信息接管。
    - 证据: `live_cycle.py:2608-2644` 孤儿收养代码仅写入 2 字段；`live_intent_loop.py:1902-1916` 异常处理未区分 fatal vs transient
- **证据引用**:
  - Source 1: `data_btc/logs/intent_20260606T134832Z.log` — cycle_error traceback (line 350)
  - Source 2: `core/execution/execution_queue.py` git history — `_close_result` init added in 3dbeeb4
  - Source 3: `core/runtime/live_cycle.py:2608-2644` — orphan adoption minimal metadata
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260607-003

---

### CCT-20260608-002
- **Docket ID**: DQAF-20260608-002
- **日期**: 2026-06-08
- **置信度**: confirmed (双源确认)
- **因果链**:
  - [Layer 1 — 症状]: XAUUSDc LONG 平仓 (ticket=3818953854, 07:09:43, exit_watchdog:meta_exit, PnL=+0.03) 无钉钉通知。操作员仅收到开仓提醒，完全不知道仓位已平。
    - 证据: `data/live_trade_journal.jsonl` — close action at 07:09:43, PnL=0.03
    - 证据: `data/logs/alert_audit.jsonl` — 无 trade_close 条目, 最后一条 XAU 记录是 06:49:44 的 trade_open
  - [Layer 2 — 中间异常]: `dispatch_managed_close()` (managed_close.py) — 所有受管平仓的统一入口 (meta_exit/SL/TP/hesitation/time_decay/brain_flip/drawdown_kill) — 覆盖了重入守卫、ghost-volume 审计、PnL 追踪, 但**从未调用 `notify_trade()`**。
    - 证据: `core/execution/managed_close.py:298-318` — pre-fix 代码包含 `known_open_tickets.pop()`, `_pending_budget_records.append()`, `_pending_sl_records.append()`, 但没有 `notify_trade` 调用
  - [Layer 3 — 根因]: RC-06 contract-violation — FIX-20260608-002 创建了 `_emit_close_notification()` 作为平仓通知的统一 helper, 但仅接线 MIA 路径 (live_cycle.py:3807) 和执行队列 net_out 路径 (live_cycle.py:5186)。`dispatch_managed_close()` (FIX-20260530-071 从 live_cycle.py 通过 Strangler Fig 提取) 早于通知系统 (FIX-20260606-138-Phase3) 的出现, 从未被 retrofitted。本质是"事件总线缺失综合征"——横切关注点 (通知) 通过手动调用耦合到每个退出路径, 而非通过发布/订阅自动覆盖。
    - 证据: `core/runtime/live_cycle.py` git history — `_emit_close_notification` 在 88112bf 中添加, 仅 2 个调用点 (MIA + net_out)。`managed_close.py` 零调用。
- **证据引用**:
  - Source 1: `data/live_trade_journal.jsonl` — XAUUSDc LONG close at 07:09:43, ticket=3819448262, PnL=+0.03
  - Source 2: `data/logs/alert_audit.jsonl` — 无对应 trade_close 条目
  - Source 3: `core/execution/managed_close.py` — `dispatch_managed_close()` pre-fix 代码中无 `notify_trade` 调用
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260608-003

---

## 置信度标记说明

| 标记 | 定义 | 要求 |
|------|------|------|
| `confirmed` | 双源确认 | 至少 2 个独立数据源支撑 |
| `hypothesis` | 单源推断 | 仅 1 个数据源支撑，需补充验证 |
| `speculative` | 纯逻辑推理 | 无数据源支撑，仅逻辑推断 |
| `refuted` | 已证伪 | 后续证据推翻了该环节 |

---

### CCT-20260606-001
- **Docket ID**: DQAF-20260606-002
- **日期**: 2026-06-06
- **置信度**: confirmed（三层均双源确认）
- **因果链**:
  - [Layer 1 — 症状]: BTC swing 策略 Win Rate = 14.29%，PnL = -$813.49，24h 内 6 次 brain_flip_extreme_100pct 紧急出场，18 次 reentry_persistent_block 告警
    - 证据: Journal (Source 1) 6 条 brain_flip 出场记录 + Alert Audit (Source 2) 44 条 strategy_degradation + 18 条 reentry_block
  - [Layer 2 — 中间异常]: Exit Watchdog 在双脑（V4+V5）投票出现 neutral 平票时，将 `_l2_supporting=[]`（空集）传入 `evaluate_brain_exit()`，导致 flip 计算 `flipped = entry_ids - {}` = 100% 假阳性
    - 证据: live_cycle.py:1424 `_l2_supporting = []` + position_manager.py:716 `flip_ratio = len(flipped)/len(entry_ids)` = 2/2 = 1.0
  - [Layer 3 — 根因]: RC-06（contract-violation）— `_l2_supporting` 的语义在 neutral 分支（`[]` = "组内无一致方向"）与 directional 分支（`brain_ids` = "组内全部 brain"）之间存在契约断裂。`position_manager.py` 将 `[]` 错误解释为"入场 brain 全部消失"
    - 证据: contract_groups.py:385 `brain_ids=brain_ids`（全部brain）vs live_cycle.py:1424 `_l2_supporting = []`（空）
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 6 条 brain_flip_extreme_100pct
  - Source 2 (Alert Audit): `data_btc/logs/alert_audit.jsonl` — strategy_degradation PnL=-813.49 WR=14.29%
  - Source 3 (Source Code): `core/runtime/live_cycle.py:1424`, `core/execution/position_manager.py:713-746`, `core/parliament/contract_groups.py:443-458`
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260606-001
- **关联 FIX**: FIX-20260606-137

### CCT-20260606-002
- **Docket ID**: DQAF-20260606-003
- **日期**: 2026-06-06
- **置信度**: confirmed（三层均双源确认）
- **因果链**:
  - [Layer 1 — 症状]: **重启后 1 秒即在第一个周期开仓** btc_swing short（system_online 05:21:13 → open 05:21:14, conf=0.6718, p_win=0.47）。18 分钟后 brain_flip 出场，MT5 断连导致 9 次重试全部 REJECTED（retcode 10031），仓位卡死。
    - 证据: Alert Audit `05:21:13 system_online` + Journal `05:21:14 open` + Journal `05:39-05:45 9 次 rejected close`
  - [Layer 2 — 中间异常 — 门禁三重失效]: Cooldown deadline 重启时已过期（03:49:44 vs 05:21:13, >1.5h）→ Cut 1 通过。Family spacing 无冲突 → Cut 2 通过。Reentry guard 理论应拦截（前次出场=brain_flip, exit_conf=0.6889, 阈值=0.7389, 新 conf=0.6718 < 0.7389）但实际未拦截 → Cut 3 失效。
    - 证据: `execution_state.json.bak` cooldown deadline=03:49:44 + `reentry_guard.py:114-151` brain_flip 判定逻辑
  - [Layer 3 — 根因 — RC-08 (fail-open)]: `restart_state.py:107` 的原代码为 `except Exception: return`，将**整个 journal 解析逻辑**包裹在单层 try/except 中。任何非 JSON 解析异常（如 datetime.fromisoformat 失败、MAGIC_TO_STRATEGY 导入失败、ExitRecord 构造异常）均导致函数静默返回，`_reentry_states` 保持空字典。下游 `check_and_record_entry()` 发现 `last_exit = None` → 返回 `"first_entry"` → **所有重入检查被绕过**。这是 Fail-Open 反模式的标准案例：恢复失败时系统不应放行，而应进入保守状态（Fail-Closed）。
    - 证据: `restart_state.py:107` `except Exception: return` (FIX-138 修复前) + `reentry_guard.py:435-442` first_entry 分支

- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 05:21:14 open + 05:39-05:45 rejected close ×9
  - Source 2 (Alert Audit): `data_btc/logs/alert_audit.jsonl` — 05:21:13 system_online + 03:39:46 cooldown block
  - Source 3 (State): `data_btc/state/execution_state.json.bak` — cooldown deadline=03:49:44, exit_reason=brain_flip
  - Source 4 (Source Code): `core/runtime/restart_state.py:107` `except Exception: return` → `reentry_guard.py:435` `if self.last_exit is None: return True, "first_entry", 1.0`
- **是否被推翻**: 否（补充 Layer 3 根因，Layer 1-2 结论不变）
- **关联 ReB Pattern**: ReB-20260606-002 (`bootstrap_silent_fail_to_open`)
- **关联 FIX**: FIX-20260606-138

### CCT-20260606-003
- **Docket ID**: DQAF-20260606-005
- **日期**: 2026-06-06
- **置信度**: confirmed（三层均双源确认）
- **因果链**:
  - [Layer 1 — 症状]: Alert 系统报告 `rolling_win_rate=2.56%`、`daily_pnl=-$674.75`、`win_rate_collapse` 紧急告警。实际逐仓位去重后真实胜率 41.5%，真实盈亏 +$60.97。告警数据与实盘严重偏离，触发误杀级风控告警。
    - 证据: Alert audit `win_rate_collapse` + Journal 逐笔去重统计 41 笔唯一仓位
  - [Layer 2 — 中间异常 — 消费者聚合无过滤]: `_execute_alert_dispatch` 的 PnL 聚合逻辑（`live_cycle.py:770-800`）对所有 `action=="close"` 的 journal 条目无差别求和，不区分 `ack_status`（accepted/rejected/closed），不按 `position_ticket` 去重。同一仓位的 28 次 REJECTED 重试被计为 28 笔独立亏损。
    - 证据: `live_cycle.py:781` `if _e.get("action") != "close": continue` — 无 ack_status 过滤
  - [Layer 3 — 根因 — RC-10 (ontology-violation) × 消费端幂等性缺失]: Journal 作为 append-only event log 正确记录了每次尝试（包括重试），但消费者（告警聚合器）将 event log 错误地解释为 trade ledger。**Event log ≠ Trade ledger** — 前者记录所有尝试，后者只记录最终结果。这是本体论层面的范畴错误：把"发生了什么"和"结果是什么"混为一谈。
    - 证据: Journal 157 条 close 条目 (event log) vs 41 个唯一仓位 (trade ledger)
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 157 close entries, 108 retry pollution
  - Source 2 (Source Code): `core/runtime/live_cycle.py:770-800` — alert aggregation logic
  - Source 3 (Cross-check): 逐仓位去重脚本 — 41 unique positions, WR=41.5%, PnL=+$60.97
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260606-003 (`metric_pollution_via_rejected_retries`)
- **关联 FIX**: FIX-20260606-138-Phase0 / FIX-20260606-138-Phase2 / FIX-20260606-138-Phase3

### CCT-20260606-004
- **Docket ID**: DQAF-20260606-006
- **日期**: 2026-06-06
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 钉钉平仓通知永远显示 "盈亏: N/A"，且同一仓位收到多条重复通知轰炸（28 次/仓位）
  - [Layer 2 — 根因]: RC-06 (contract-violation) — `DispatchResult` 数据契约不包含 `pnl` 字段。`_net_out_close_dispatch_fn` 内部计算了 `_net_pnl` 但未通过返回值向上游传递 → `execution_queue.flush()` 无法在 `DispatchResult` 中携带 PnL → `notify_trade()` 参数链断裂 → pnl 永远为 None
- **证据引用**:
  - Source 1: `execution_queue.py:41-50` — DispatchResult 无 pnl/volume 字段
  - Source 2: `live_cycle.py:4640` (修复前) — notify_trade 调用缺失 pnl= 参数
  - Source 3: `live_alert_hub.py:317-328` — pnl_str 回退到 "N/A"
- **是否被推翻**: 否
- **关联 ReB Pattern**: `missing_pnl_in_trade_notification`
- **关联 FIX**: FIX-20260606-138-Phase3
- **Follow-up**: Phase 3 初版在 `execution_queue.py` 中引用 `_close_result` 时未初始化（变量仅在 close 分支存在），导致开仓路径 `UnboundLocalError`。已通过分支前初始化 + None 检查修复（RC-05 boundary-error）。

### CCT-20260606-005
- **Docket ID**: DQAF-20260606-004
- **日期**: 2026-06-06
- **置信度**: confirmed（三层均双源确认）
- **因果链**:
  - [Layer 1 — 症状]: 07:00 平仓后 6+ 小时零开仓。每周期产出 SHORT 信号 (conf=0.82)，GM 记录 C1-C3 全部 `should_trade=False`
    - 证据: Golden Master (Source 1) 3 周期全部 blocked + Journal (Source 2) 零 open
  - [Layer 2 — 中间异常 — 双闸门交替拦截]: C1 被 p_win=0.44 < 0.45 拦截；C2-C3 p_win 偶尔通过后被 bleed_stop_price_not_confirming 补位拦截。p_win=0.44 来自 rolling WR（含 9 次假 brain_flip 污染），在 breakeven=0.45 下方 0.01。Fail-Closed 兜底触发线 0.40 太低，留下 0.40-0.45 死锁带
    - 证据: strategy_line.py:1556-1562 Fail-Closed 逻辑 + reentry_guard.py:297-300 bleed_stop 价格确认
  - [Layer 3 — 根因 — RC-05 (boundary-error)]: FIX-137 修复了假 brain_flip，但 9 次 bug 导致的真实亏损已将 rolling WR 压低至 0.44。Fail-Closed 的触发线 0.40 是针对"系统完全盲"场景设计的，未覆盖"系统有数据但受污染"的中间态。死锁机制：p_win 略低于地板 → 不能交易 → 无新数据 → p_win 不更新 → 永久冰封
    - 证据: 去重统计 73 笔唯一仓位，真实 WR=47.7%，SHORT WR=47.5%，均高于 floor=0.45。但 rolling WR（近期窗口）因假 brain_flip 集中亏损被压低至 0.44
- **解决方案评估（三选一）**:
  - 方案一（贝叶斯收缩）: ✅ 长期稳定，但单独无法根治边界死锁
  - 方案二（卡尔曼滤波）: ❌ 问题不在噪音过滤——p_win=0.44 是真实信号
  - 方案三（UCB 弹性地板）: ✅ 精准命中死锁机制——置信度 × 不确定性溢价填平死锁带
- **证据引用**:
  - Source 1 (Golden Master): `data_btc/golden_master.jsonl` — C1-C3 全部 blocked
  - Source 2 (Journal): `data_btc/live_trade_journal.jsonl` — 去重统计 47.7% WR
  - Source 3 (Source Code): `strategy_line.py:1556-1562` Fail-Closed + `reentry_guard.py:297-300` bleed_stop
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260606-005 (`p_win_statistical_freeze_dead_zone`)
- **关联 FIX**: FIX-20260606-139

### CCT-20260607-007
- **Docket ID**: DQAF-20260607-007
- **日期**: 2026-06-07
- **置信度**: confirmed (双源确认 — journal PnL + golden master trend_direction)
- **因果链**:
  - [Layer 1 — 症状]: BTC 5/31-6/6 43笔交易中40笔为sell。架构师关注趋势衰竭和V型反转风险。
    - 证据: Source 1 (Journal) — 43笔BTC trade, 40 short, WR 44.2%, 盈亏比 2.50, PnL +$102.12
    - 证据: Source 2 (Golden Master) — 50周期全部 trend_direction="short", macro_regime="risk_off"
  - [Layer 2 — 中间异常 — 信号已计算但未消费]: Kalman velocity + Hurst 由 TrendDetector 每周期 O(1) 计算，RegimeGate.classify() 返回 dict 已包含 m5_hurst 和 h1_ema_slope，但从未接入仓位缩放或出口决策。trend/swing 策略在趋势成熟时依然等额开仓，无自适应的仓位调节机制。
    - 证据: strategy_line.py:1748 volume *= _ct_vol_mult — 仅 counter-trend 罚则，无趋势成熟折扣
    - 证据: position_manager.py:700-754 — 出口仅 consensus flip + brain flip + confidence decay，无 Kalman 一阶导信号
  - [Layer 3 — 根因 — RC-12 (missing-feature)]: 信号源→消费端的接线缺失。TrendDetector 和 RegimeGate 体系已完备，但 strategy_line 和 position_manager 的 evaluate 入口从未消费 Hurst/Kalman velocity 信号。纯架构债——不需要新信号，只需要接线。
    - 证据: strategy_line.py:510 evaluate() 签名缺少 hurst/kalman_velocity_bps 参数
    - 证据: live_cycle.py:3862-3865 仅提取 trend_direction/trend_strength/h4_trend_strength/macro_regime
- **解决方案**: 三步纯增量接线:
  - Step 1 (双因子入口折扣): `trend_maturity_discount(hurst, trend_strength, strategy_family)`:
    - 因子 A — **Hurst 持续性衰减**: H=0.60→1.00x, H=0.55→0.85x, H=0.50→0.55x, H≤0.45→0.40x (floor)。度量趋势结构是否仍在（分形市场假说）。
    - 因子 B — **Kalman 速度确信度衰减**: `trend_strength` = h1_trend_strength，来自 `KalmanTrendFilter.strength` = `sigmoid(|v|/σ)`。当 trend_strength < 0.5 时，比例折扣 `strength/0.5`。度量 Kalman 对当前趋势速度的确信程度——速度相对于不确定性的 SNR 下降时自动收缩仓位。
    - 双因子乘性叠加，floor=0.40。仅 trend_following/swing 策略族生效，statarb/mean_reversion 豁免（已有独立 sizing）。
    - **已知 Phase 2 缺口**: 当前实现使用 `trend_strength`（速度×信噪比复合分），而非纯速度比率 `|v|/EMA(|v|)`。后者能更早检测到"速度自身的历史性衰减"（加速度丧失），是更干净的领先指标。当前方案保守——仅在信噪比恶化时折扣，不会因短期速度波动误触发。EMA velocity ratio 作为 Phase 2 升级路径，需要先积累 30-50 周期的 velocity EMA 样本。
  - Step 2 (Kalman 速度翻转快速出口): `evaluate_brain_exit()` 第0层检查 — long仓位且 v < -3bps 或 short仓位且 v > +3bps → 立即退出。阈值过滤 M5 噪声。充当 PID 出口控制器的微分(D)项——在价格触及 trail stop **之前**根据动量方向提前撤退。
  - Step 3 (数据接线): live_cycle.py 从 regime_gate_result 提取 m5_hurst + h1_ema_slope → 经 _evaluate_strategy_lines → strategy.evaluate()。h1_ema_slope(h1 速度) 存入 LiveCycleState._last_kalman_velocity_bps 供下一周期的 exit management 使用（落后一个周期，对趋势级别变化可接受）。
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — 完整 PnL 统计
  - Source 2 (Golden Master): `data_btc/golden_master.jsonl` — 50 周期 trend_direction + confidence
  - Source 3 (Source Code): 6个文件的完整追踪
- **是否被推翻**: 否 — AR假设(长方向偏见)被 journal 中 3笔 long 的开仓记录推翻
- **关联 ReB Pattern**: ReB-20260607-007 (`signal_wiring_unconsumed_computed_output`)
- **关联 FIX**: FIX-20260607-143

---

### CCT-20260607-006
- **Docket ID**: DQAF-20260607-006
- **日期**: 2026-06-07
- **置信度**: confirmed (3 源确认)
- **因果链**:
  - [Layer 1 — 症状]: Ticket=3807506009 开仓后在 80 分钟内被拒绝 75 次平仓请求，journal 中产生 76 条 close 记录。同时 position_snapshots 显示 bars 3-16（13根K线/65分钟）的所有字段值完全相同（unrealized_pnl_r=-1.29, trailing_sl_distance=1269.17, current_atr=385.58），数据管道完全冻结。
    - 证据: `data_btc/live_trade_journal.jsonl` — ticket 3807506009 的 76 条 close 记录（ack=rejected × 75, ack=closed × 1）
    - 证据: `data_btc/position_snapshots.jsonl` — ticket 3807506009 的 18 条快照，bar 3-16 所有字段值完全一致
  - [Layer 2 — 中间异常]: `_mid_and_prices()` 持续从 MT5 获取价格数据，但 MT5 返回的是**相同的过期 tick**（`tick.time` 不推进）。该函数仅检查价格有效性（NaN/Inf/零值/越界/点差），无 staleness 检测。`live_cycle.py` 主循环用过期价格计算特征→开仓→管理→平仓，形成完整的"瞎子指挥"链条。同时 ExitWatchdog 在每个管理周期被重新触发（跨周期雪崩），每个 batch 5 次重试全部被 MT5 拒绝（deviation 超限——价格已偏离订单价格 $500+）。
    - 证据: `core/runtime/market_ingress.py:77-120` — `_mid_and_prices()` 返回 `(mid, bid, ask)` 无时间戳
    - 证据: `core/runtime/live_cycle.py:1542-1574` — bleed_stop 每周期触发 `_dispatch_managed_close` 但不检查之前是否已派发
    - 证据: `core/execution/exit_watchdog.py:43-49` — MAX_RETRIES=5, MAX_TOTAL_DURATION=30s，但外部管理循环不断重启新 batch
  - [Layer 3 — 根因]: **RC-07 (Fail-Open 反模式) + RC-09 (数据新鲜度契约缺失)**。MT5 Bridge 在断连/数据停滞时返回旧 tick 而非抛出异常，上层无 staleness 检测机制，系统将过期数据当作实时数据处理。同时 exit dispatch 路径缺少 pending_close 状态锁，导致 watchdog batch 被管理循环反复重新触发，形成 75 次拒绝的雪崩。
    - 证据: `core/runtime/market_ingress.py` — tick.time 字段存在但从未被提取和传播
    - 证据: `core/execution/position_manager.py` — 修复前无 `_pending_close` 锁机制
- **修复** (FIX-20260613-052: resolved placeholder):
  - (a) `_mid_and_prices()` 返回值扩展为 `(mid, bid, ask, tick_time)`
  - (b) `live_cycle.py` 主循环头部增加 staleness 检测：`data_age > 120s` → 跳过本周期；连续 3 次触发 `_circuit_breaker_tripped`
  - (c) `_dispatch_managed_close()` 增加价格年龄守卫：`tick_age > 60s` → 拒绝派发
  - (d) `ActivePositionManager` 增加 `_pending_close` 锁：同一 ticket 在 3 周期内不允许重复派发平仓
  - (e) `trail_activation_atr` 从 1.0 降为 0.3（BTC 配置）
- **证据引用**:
  - Source 1 (Journal): `data_btc/live_trade_journal.jsonl` — ticket 3807506009 完整生命周期
  - Source 2 (Snapshots): `data_btc/position_snapshots.jsonl` — 13 根 bar 的数据冻结证据
  - Source 3 (Source Code): `market_ingress.py` + `live_cycle.py` + `position_manager.py` + `exit_watchdog.py` — 4 文件完整追踪
  - Source 4 (Audit Script): `scripts/analyze_live_journal.py` — Trail SL 3.465x ATR + 83% 仓位 SL 从未收紧
- **是否被推翻**: 否 — AR 假设 (Trail 乘数计算 bug) 被代码审计推翻：乘数正确，问题在于激活水印 + staleness 导致的"Trail 从未启动"
- **关联 ReB Pattern**: ReB-20260607-008 (`stale_data_fail_open_blind_trading`)
- **关联 FIX**: FIX-20260613-052: resolved placeholder

---

### CCT-20260607-007
- **Docket ID**: DQAF-20260607-007
- **日期**: 2026-06-07
- **置信度**: confirmed (双源确认)
- **因果链**:
  - [Layer 1 — 症状]: 钉钉告警 `策略性能下降` 中显示 `策略盈亏(USD): -2105.05` 和 `策略胜率: 0.1429`，用户反馈数值不准确。实际 `当日盈亏(USD): 2.96` 与 `策略盈亏(USD): -2105.05` 差距 700 倍，引起困惑。
    - 证据: 钉钉消息截图 + `alert_audit.jsonl` — strategy_degradation 告警
  - [Layer 2 — 中间异常]: 两个独立问题叠加：(a) **标签错位**: `alert_channels.py:160` 将 `strategy_pnl` 映射为 `策略盈亏(USD)`，但 `brain_pnl_ledger.py:53` 中 `cumulative_pnl` 的注释明确写的是 `total P&L per unit`（每单位 R-multiple），不是 USD；(b) **缝合怪指标**: `live_cycle.py:886-888` 对 PnL 和 WinRate 独立取 `min()`，导致 `_worst_pnl` 来自 BTC_Swing_V4（-2105R），`_worst_wr` 来自 BTC_Swing_LGB_V1（0.1429）。告警描述的 "策略" 在物理世界中不存在——是两个不同大脑的碎片拼接。
    - 证据: `live_cycle.py:886-888` — `_worst_pnl = min(...)` 和 `_worst_wr = min(...)` 是独立循环
    - 证据: `brain_pnl_ledger.py:53` — `cumulative_pnl: float = 0.0  # total P&L per unit`
    - 证据: `alert_channels.py:160` — `"strategy_pnl": "策略盈亏(USD)"`
  - [Layer 3 — 根因]: **RC-08 (语义契约断裂)** — 数据生产者（BrainPnLStore）的 `cumulative_pnl` 明确标注为 per-unit R-multiple，但消费者（告警标签）将其错误解释为 USD。同时 "最差策略" 的构建使用了两个独立 `min()` 而非选择单一最差大脑，产生了一个无物理对应物的虚假指标。
    - 证据: `live_cycle.py:878-890` 修复前代码 vs 修复后代码
- **修复** (FIX-20260613-052: resolved placeholder):
  - (a) `live_cycle.py:886-888`: 独立 `min()` → `min(_all_m.values(), key=lambda m: m.cumulative_pnl)` 选择单一最差大脑，PnL 和 WR 同源
  - (b) `alert_channels.py:160-161`: `策略盈亏(USD)` → `最差大脑累计PnL(R)`, `策略胜率` → `最差大脑胜率`, 新增 `最差大脑ID`
  - (c) 新增 `_ctx["worst_brain_id"]` 使告警可溯源到具体大脑
- **证据引用**:
  - Source 1 (Alert Audit): `data_btc/logs/alert_audit.jsonl` — strategy_degradation 告警上下文
  - Source 2 (Governance State): `data_btc/governance_state.json` — BTC_Swing_V4 pnl_r=-2171.86 vs 告警值 -2105.05
  - Source 3 (Source Code): `live_cycle.py:878-890` + `brain_pnl_ledger.py:53` + `alert_channels.py:160`
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260607-009 (`frankenstein_metric_independent_min`)
- **关联 FIX**: FIX-20260613-052: resolved placeholder

---

### CCT-20260607-008
- **Docket ID**: DQAF-20260607-008
- **日期**: 2026-06-07
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: Phase A 焊死了价格 staleness 检测，但特征存储、Bridge 心跳、周期停顿三个组件仍处于 Fail-Open 状态——检测存在但仅发告警/记录，不阻断交易。
  - [Layer 2 — 中间异常]: 三个防线的"检测→告警"链路完整，但"告警→熔断"链路缺失。特征冻结时系统继续用过期特征推理；Bridge 断连时继续用旧价格评估。
  - [Layer 3 — 根因]: **RC-07 (Fail-Open 残余)** — 告警 ≠ 熔断 的模式在三个子系统中重复出现。
- **修复** (FIX-20260613-052: resolved placeholder Phase B):
  - (a) B1: `feature_stale_warning` → `_consecutive_stale_features`，连续 3 次 → 熔断
  - (b) B2: `_bridge_silence > 300s` → 立即熔断（无需等 3 周期）
  - (c) B3: `cycle_duration > 180s` → `_consecutive_degraded_cycles++`，连续 3 次 → 熔断
  - (d) Config 新增 `max_bridge_silence_seconds=300.0` + `cycle_stall_threshold_seconds=180.0`
- **证据引用**:
  - Source 1: `live_cycle.py:3817-3829` (修复前 feature_stale 仅 print)
  - Source 2: `live_cycle.py:777-799` (bridge_last_ack 仅用于告警上下文)
  - Source 3: `live_cycle.py:2519` (_last_cycle_start_time 已采集但未用于 stall 检测)
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260607-008 (`stale_data_fail_open_blind_trading`)
- **关联 FIX**: FIX-20260613-052: resolved placeholder

---

### CCT-20260608-003: 断路器碎片化 trip 路径死亡螺旋 (DQAF-20260608-003)

- **发现日期**: 2026-06-08
- **严重等级**: Sev 2 — 交易阻断
- **因果链**:
  - [Layer 1 — 症状] (confirmed): 断路器反复触发，系统频繁重启 (May 31: 110次)，trade_decisions=0
    - Source 1: `data_btc/logs/alert_audit.jsonl` — 123 daily_loss + 57 strategy_degradation
    - Source 2: `data_btc/state/execution_state.json` — consecutive_degraded=0 but circuit_breaker_tripped=true (矛盾)
    - Source 3: `data_btc/golden_master.jsonl` — 仅3个cycle, 全部 trade_decisions=0
  - [Layer 2 — 中间异常] (confirmed): Auto-reset (live_cycle.py L2815) 仅重置 `_consecutive_degraded_cycles=0`，未重置 `_consecutive_stale_cycles` 和 `_consecutive_stale_features`
    - Source 4: `live_cycle.py` L2817 vs L3313 — reset 只清 degraded 不清 stale
    - 后果: breaker 由 data_staleness 触发后，auto-reset 后 stale counter >= 3 仍存活，同一 cycle 内 L3296 重新 trip
  - [Layer 3 — 根因] (confirmed): 断路器架构碎片化 — 6条 trip 路径使用 3种独立计数器，auto-reset 未覆盖全部
    - Source 5: `live_cycle.py` 5条 trip 路径仅 bridge+stall+wakeup 共用 `_consecutive_degraded_cycles`
    - Source 6: `execution_state.py` save 未持久化 stale counters → 重启后 breaker=True 但计数器丢失 → "幽灵 breaker"
    - Source 7: FIX_REGISTRY — 6+次独立断路器修复均未根除
- **修复** (FIX-20260608-009):
  - (a) 新增 `_circuit_breaker_trip_reason` 字段 — 所有 5 条 trip 路径记录触发原因
  - (b) Auto-reset 统一清除全部 3 种计数器 (degraded + stale_cycles + stale_features)
  - (c) `save/restore_execution_state` 补齐全部计数器 + trip_reason 持久化
- **是否被推翻**: 否 — AR 反证确认：多次修复均为单路径打补丁
- **关联 ReB Pattern**: ReB-20260608-003 (`FRAGMENTED_BREAKER_TRIP_PATHS_WITH_STALE_COUNTER_LEAK`)
- **关联 FIX**: FIX-20260608-009

---

## CCT-20260609-001: BTC Hesitation Permanent Deadlock

- **Docket ID**: DQAF-20260609-001
- **Severity**: Sev 2
- **Date**: 2026-06-09
- **Causal Chain**:
  - **Layer 1 — 症状**: BTC btc_swing 自 2026-06-08 01:02 UTC 起零开仓。148 次连续信号评价（confidence 0.746-0.750, p_win 0.45-0.48, regime=full trending, 3/4 brains 支持 LONG）全部被 `reentry_blocked` 拦截，reason=`hesitation_confidence_not_improved`。
  - **Layer 2 — 中间异常**: 最后一笔 hesitation 退出（ticket=3808448708）通过 bootstrap 重放时 `exit_reason` 被跨记录借用到最新 close（ticket=3810297338），形成 `exit_reason="exit_watchdog:hesitation_15c_no_breakeven"` + `exit_confidence=0.7668` 的组合。`check_reentry_quality()` 的 hesitation 路径计算阈值 `max(0.7668+0.15, 0.70)=0.9168` — 此值超过 BTC 树模型 P99 输出 (~0.685) 和绝对最大值 (~0.77)，**数学上不可达**。
  - **Layer 3 — 根因 (RC-05 + RC-12)**: `reentry_guard.py` 的 `hesitation` 类别是唯一同时缺少两项保护的退出类别：(a) `_MAX_THRESHOLD=0.82` 天花板 — FIX-117 已施加于 brain_flip/sl_hit/ou_revert/unknown_close 但遗漏了 hesitation；(b) TTL 硬解锁 — FIX-127 已施加于 brain_flip+meta_exit，FIX-011 已施加于 sl_hit，但均遗漏了 hesitation。唯一的逃生通道是 24h stale exit override。
- **是否被推翻**: 否 — AR 反证确认：BTC 信号质量正常（confidence>0.74, p_win>0.45），hesitation 后 confidence 从 0.5 提升到 0.75（+50%），实为合理重入时机。死锁非市场质量导致，纯为代码边界条件缺陷。
- **关联 ReB Pattern**: ReB-20260609-001 (`HESITATION_PERMANENT_DEADLOCK`)
- **关联 FIX**: FIX-20260609-001

---

### CCT-20260609-001-B: Breakeven Floor Trail Deadlock (DQAF-20260609-001 sub-finding)

- **Docket ID**: DQAF-20260609-001
- **发现日期**: 2026-06-09
- **严重等级**: Sev 2 — 出场质量退化，保本后利润保护失效
- **因果链**:
  - **Layer 1 — 症状** (confirmed): BTC trade 3809501680 bar 16-23 共 8 根 bar，`trail_sl_candidate: null`，SL 锁死 62924。只有 TP 单向收紧。入场后价格继续涨了 +$306，仓位只能通过 TP 被命中出场，而非通过 trailing SL 逐步锁利。
    - Source 1: `management_phase_diag` 日志 — bar 16-23 全部 `trail_sl_candidate: null, trail_fired: false`
    - Source 2: `live_trade_journal.jsonl` — SL 从 bar 15 dispatch 后始终 62923.98
  - **Layer 2 — 中间异常** (confirmed): `trail_stop_engine.py:158` — `max(candidate, entry_price)` + `candidate <= current_sl + min_step` 形成双重锁定。`highest_high` 停滞在 63313，但 Chandelier 需要 `trail_mult * ATR` 的利润缓冲才能突破保本地板。trail_mult=2.5 + ATR≈185 → 需要 ~460 pts 利润。最高只到 389 pts。candidate 被地板抬到 62951, 但 62951 ≤ 62924 + 0.15 → return None。
    - Source 3: `trail_stop_engine.py` L161-173 — 完整的循环死锁代码路径
  - **Layer 3 — 根因 (RC-05)** (confirmed): trail_mult 是**静态常量** — 从入场到出场永远不变（regime-given 2.5）。没有随着利润积累而收紧的机制。保本前的大 multiplier 是为了防止水下仓位被过早止损——这是正确的。但保本后仓位已经安全，multiplier 应该变小以允许 trail 逐步锁利。静态 multiplier 无法区分"水下求生"和"水上锁利"两个阶段。
    - Source 4: `trail_stop_engine.py` L158 (旧) — `effective_mult = max(tp.min_trail_mult, pos.trail_multiplier)` — trail_multiplier 只被 regime gate 调整，永不考虑利润
- **修复** (FIX-20260609-003): 新增 `_compute_decayed_mult()` — trail_mult 随 R-max 从 base 平滑衰减到 min_trail_mult(R: 0.5→2.0)。`TrailPolicy` 新增 `decay_start_r, decay_full_r, decay_enabled`。
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260609-001-B (`BREAKEVEN_FLOOR_TRAIL_DEADLOCK`)
- **关联 FIX**: FIX-20260609-003

---

### CCT-20260609-001b
- **Docket ID**: DQAF-20260609-001
- **日期**: 2026-06-09
- **置信度**: confirmed (双源交叉验证 — 同事审计 + Agent 审计)
- **因果链**:
  - [Layer 1 — 症状 A]: `execution_state.json` 显示 `total_trades_today: 0`, `consecutive_losses: 0`，但 alert_audit 记录至少有 4 笔已关仓交易、3 笔亏损。Daily Loss Limit (-$30) 未触发。
  - [Layer 2 — 中间异常 A]: `live_cycle.py:4044` `_build_strategy_lines()` 每个 cycle 创建全新的 `StrategyBudget` 对象（计数器=0）。`live_cycle.py:4433` `restore_execution_state()` 仅在 `loop_iteration == 1` 时恢复。Cycle 2+ budget 恒为零 → 所有累计风控闸门（daily_loss_limit, max_consecutive_losses, intraday_dd）永久失效。
  - [Layer 3 — 根因 A]: RC-03 (state-leak) — `_build_strategy_lines()` 每 cycle 重建策略对象是 FIX-20260530-070 (Strangler Fig #5) 的架构残余。原设计中策略对象在循环外创建一次，提取后移入循环内但未配套恢复逻辑。
  - [Layer 1 — 症状 B]: alert_audit 显示 `hesitation_confidence_not_improved_0.746_need_0.820` 连续 150 cycles (6/8-6/9)。BTC btc_swing 重入被永久封锁。
  - [Layer 2 — 中间异常 B]: FIX-001 部署了 `_MAX_THRESHOLD=0.82` 天花板 + 2h TTL，但 `reentry_guard.py:298` 的 `exit_confidence + 0.15` 边际加法在 floor 0.70 约束下仍产生 0.82 阈值。BTC 树模型 (LightGBM/XGBoost) P99 输出 ≈ 0.685-0.75，无法达到 0.82。
  - [Layer 3 — 根因 B]: RC-05 (boundary-error) — threshold calibration 未根据目标模型的输出分布校准。+0.15 边际对 BTC tree-based 模型过大（对比 brain_flip +0.05, BTC P99≈0.685）。
- **证据引用**:
  - Source 1 (A): `data_btc/state/execution_state.json` — `total_trades_today: 0, consecutive_losses: 0` (2026-06-09 09:59 UTC)
  - Source 2 (A): `data_btc/logs/alert_audit.jsonl` — 6/9 trade_notification close events: PnL=-1.74, -1.36, -13.93, -14.01
  - Source 3 (A): `core/runtime/live_cycle.py:4044-4054` + `core/runtime/live_cycle.py:4433` — 每 cycle 重建 + 仅 cycle 1 恢复
  - Source 1 (B): `data_btc/logs/alert_audit.jsonl` — reentry_persistent_block: 150 cycles (6/8 23:29-6/9 00:44)
  - Source 2 (B): `core/execution/reentry_guard.py:298` — `min(max(exit_confidence + 0.15, 0.70), _MAX_THRESHOLD)`
  - Source 3 (B): BTC brain performance data (governance_state.json) — 4 brains all candidate, P99 confidence 0.685-0.75
- **修复** (FIX-20260609-010):
  - Sub-fix A: `live_cycle.py` 新增 per-cycle budget 恢复块 — `load_execution_state()` → `budget.load_state()` 在 `_build_strategy_lines()` 之后、pending records 之前执行
  - Sub-fix B: `reentry_guard.py:298` — margin 0.15→0.08, floor 0.70→0.65. 排序: brain_flip+0.05 < hesitation+0.08 < sl_hit+0.10
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260609-001-B (`Cap-Output Mismatch Deadlock`) + `Budget Reconstruction Amnesia`
- **关联 FIX**: FIX-20260609-001, FIX-20260609-010

---

### CCT-20260609-011
- **Docket ID**: DQAF-20260609-011
- **日期**: 2026-06-09
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC 4 个大脑全部 `candidate` 状态，0 个 `live`。系统以 0.02-0.1 lot 正常开单，今日 PnL ≈ -$30，胜率 ~31% (4关3亏)。候选大脑 profit_factor=0.72, sharpe=-30, 从未证明盈利能力。
  - [Layer 2 — 中间异常 A]: `live_startup.py:178-193` 治理过滤仅移除 `retired`/`frozen`，`candidate` 获全票权通过。更严重的是逻辑倒挂：`probation`（曾 live 后退化）被罚 vote_weight×0.5，而 `candidate`（从未证明）无任何限制。整个开单链路（strategy_evaluator, strategy_line, signal_pipeline）无 governance status check。
  - [Layer 2 — 中间异常 B]: 大脑绩效极差（profit_factor < 1.0, sharpe < -29），但治理服务（daily_ops）无法将任何大脑晋升为 `live`。系统陷入"全 candidate 死循环"——没有 live 大脑 → 开单亏损 → 绩效恶化 → 更不可能晋升 → 永远 candidate。
  - [Layer 3 — 根因]: RC-07 (missing-validation) × RC-09 (config-drift) — (A) 大脑治理状态从未作为开单前置条件，governance_state.json 在整个 live 交易链路中是"只读不用的死数据"；(B) candidate 的 vote_weight 设计意图应是 ≤ probation，但代码实现相反。
- **证据引用**:
  - Source 1: `data_btc/governance_state.json` — 4 brains all `candidate`, 0 `live`
  - Source 2: `data_btc/logs/alert_audit.jsonl` — 6/9 trade_notification: 4 closes, 3 losses, PnL ≈ -$30
  - Source 3: `core/runtime/live_startup.py:178-193` — candidate falls through to "kept" without penalty
  - Source 4: `core/runtime/strategy_evaluator.py` — zero governance status checks in entire evaluation chain
- **修复** (FIX-20260609-011):
  - (1) `live_startup.py`: candidate 加 vote_weight×0.5 惩罚
  - (2) `live_cycle.py`: 每 cycle 读取 governance_state.json → 传入 strategy_evaluator
  - (3) `strategy_evaluator.py`: Cut 4 — 无 live 大脑时 confidence<0.50→blocked, volume→0.01
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260609-011 (`GOVERNANCE_VACUUM_CADET_BRAINS`)
- **关联 FIX**: FIX-20260609-011

---

### CCT-20260609-012
- **Docket ID**: DQAF-20260609-012
- **日期**: 2026-06-09
- **置信度**: confirmed (60次数据集构建 + Walk-Forward CV)
- **因果链**:
  - [Layer 1 — 症状]: BTC_Swing_V5 test_PF=1.81 → live_PF=0.73 (训练/实盘鸿沟)。V5 训练准确率 33.96% vs 随机基线 33.3%——模型对方向几乎无预测能力。V6-V8 训练指标全部缺失（盲盒大脑）。
  - [Layer 2 — 中间异常 A]: 归一化器为 XAU 复制品（`_note`: "BTC-specific normalization not yet calibrated. DO NOT set normalize=true"），但 normalize=false 正确禁用了归一化。真正的问题是 V5 仅训练了 19 天数据（5,407 样本），且训练标签不含摩擦。
  - [Layer 2 — 中间异常 B]: 跨 4 个时间框架 × 15 组 SL/TP 网格搜索——所有 R:R ≥ 1.0 的组合 EV 为负。BTC 价格行为规律：在任何 N 小时窗口内，价格移动 X ATR 的概率 >> 移动 2X ATR 的概率 → 宽 TP 打不到、紧 SL 先被扫。
  - [Layer 3 — 根因]: RC-12 (missing-feature) × RC-05 (boundary-error) — (A) 旧大脑使用不匹配的训练数据（XAU 特征集 / 过短训练期 / 无摩擦标签）；(B) BTC 市场结构不支持传统高盈亏比 Alpha，需要"宽止损 + 高胜率"的生存策略。
- **证据引用**:
  - Source 1: `configs/brains_btc/v9_institutional_01.normalization.json` — XAU copy, normalize=false
  - Source 2: `configs/brains_btc/BTC_Swing_V5.json` — test_accuracy=33.96%, test_PF=1.81
  - Source 3: 60 次数据集构建结果（M5/M15/M30/H1 × 15 SL/TP combos）— 全部高 R:R 组合 EV 为负
  - Source 4: M15 SL=3.0/TP=2.0 — WR=92.1%, EV=+0.456R (Walk-Forward CV 验证)
- **修复** (FIX-20260609-012):
  - B1: 特征管道审计 → 归一化正确禁用，特征维度不匹配已识别
  - B2: 构建 963 行训练管线（时间衰减权重 + Walk-Forward Purged CV + 真实摩擦）
  - B3: V9 H1 (SL=3.0/TP=2.0, WR=90.0%, EV=+0.38R) + V10 M15 (SL=3.0/TP=2.0, WR=92.2%, EV=+0.46R) shadow 注册
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260609-012 (`BTC_SURVIVAL_ALPHA`)
- **关联 FIX**: FIX-20260609-012

---

### CCT-20260610-001
- **Docket ID**: DQAF-20260610-001
- **日期**: 2026-06-10
- **置信度**: confirmed (双源交叉验证: journal + snapshots)
- **因果链**:
  - [Layer 1 — 症状]: 移动止损修改后(AFTER) 16笔平仓 0%胜率 -$29.79 → 表面似trail修改导致退化
  - [Layer 2 — 中间异常 A — 数据证伪]: 亏损全部来自修改前开仓的3笔旧仓位(#3838975389/#3840851050/#3843860976, trail完全卡死 ∆=0), 修改后2笔有trail的仓位(#3853350396 ∆=+768pts, #3854799088 ∆=+74pts)均保本出场. 11/16笔为close_accepted/breakeven且PnL=None(MIA清理).
  - [Layer 2 — 中间异常 B — 微生命周期]: 修改后13笔新开仓平均持仓21分钟(4根M5 bar), 全部long方向. 逆势摸底(V9/V10 brain信号) + 宏观SHORT趋势 + trail_activation_atr 0.3-0.5激进防守 → 反弹短暂触及trail激活→衰竭被扫→保本微亏快速出场
  - [Layer 2 — 中间异常 C — 遥测盲区]: 'trail' exit label 从未在188笔闭仓中出现. 69% AFTER平仓(11/16)无PnL记录. trail行为变化只能间接通过modify_sltp和snapshot推测
  - [Layer 3 — 根因]: (A) 保本地板死锁(static trail_mult) → FIX-003衰减曲线已解除; (B) 逆势交易中激进防守的必然微生命周期→非bug,防御机制正常工作; (C) MIA管道PnL缺失→状态机同步泄漏, close_accepted/breakeven标签不记录PnL
- **证据引用**:
  - Source 1: `scripts/analyze_trail_impact.py` stdout — 21 BEFORE vs 2 AFTER SL迁移对比
  - Source 2: `data_btc/live_trade_journal.jsonl` — 385条记录, 'trail'标签count=0, close_accepted/breakeven PnL=None
  - Source 3: `data_btc/position_snapshots.jsonl` — 426条快照, SL迁移中位数BEFORE=0, AFTER=+420.9
- **是否被推翻**: 否 (AR验证通过 — 0%胜率被证伪为遥测污染而非trail退化)
- **关联 ReB Pattern**: ReB-20260610-001 (`TRAIL_TELEMETRY_BLINDSPOT`), ReB-20260610-002 (`MICRO_LIFESPAN_COUNTER_TREND`)
- **关联 FIX**: — (诊断报告, 无代码修改; IC Mandate转入MIA管道修复)

---

### CCT-20260610-002
- **Docket ID**: DQAF-20260610-002
- **日期**: 2026-06-10
- **置信度**: confirmed (code audit × git history bisect × config validation × 31 pattern tests)
- **因果链**:
  - [Layer 1 — 症状 A]: V9_H1_Survival/V10_M15_Survival training SL=3.0/TP=2.0 与 btc_swing 策略线 SL=2.0/TP=2.5 不一致
  - [Layer 2 — 中间层 A]: 非 bug——FIX-20260609-012 网格搜索确认 BTC 不支持 R:R≥1.0，特意训练生存模式(SL>TP, 90%+ WR)并注册为 shadow。但缺少 label_contract 声明其不同契约
  - [Layer 3 — 根因 A]: 训练管线未自动生成非对齐大脑的 label_contract。V6/V7/V8 有 `aligned_with: live_btc.yaml`，但 V9/V10 与任何现有策略线都不对齐——需要 `aligned_with: null` + `requires_dedicated_strategy_line: true`
  - [Layer 1 — 症状 B]: BTC_Swing_V5(retired)残留在 XAU live.yaml enabled=true
  - [Layer 2 — 中间层 B]: FIX-001 退役 V5 仅更新 BTC 配置和脑 JSON，遗漏 XAU 配置——无跨配置扫描
  - [Layer 3 — 根因 B]: 无跨配置文件一致性检查。退役操作是"点修复"模式，依赖人工同步
  - [Layer 1 — 症状 C]: 10+ 种出场原因被归为 "unknown" → 标签污染
  - [Layer 2 — 中间层 C]: `_classify_exit_reason()` 手工维护，新出场逻辑未同步更新分类规则
  - [Layer 3 — 根因 C]: 缺少"新出场原因必须注册"的强制机制
- **证据引用**:
  - Source 1: `configs/brains_btc/BTC_Swing_V9_H1_Survival.json:20-23` — training SL=3.0/TP=2.0
  - Source 2: `configs/live_btc.yaml:58-59` — strategy line SL=2.0/TP=2.5
  - Source 3: `git log bb5b386 -p -- configs/live.yaml` — V5 added to XAU Jun 6
  - Source 4: `git show 1f59e29` — V5 retired in BTC only, missed XAU config
  - Source 5: `core/execution/reentry_guard.py:21-50` — only 12 patterns before fix
- **是否被推翻**: 否
- **关联 ReB Pattern**: ReB-20260610-003 (`CONFIG_SYMMETRY_DRIFT`)
- **关联 FIX**: FIX-20260610-008

### CCT-20260612-004
- **Docket ID**: DQAF-20260612-004
- **日期**: 2026-06-12
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `resolve_p_win_from_brains()` 三条 fallback 路径全部静默返回 0.40 (`pwin_chain.py:53/62/74`)
  - [Layer 2 — 中间异常]: 调用方 `strategy_line.py:1209-1215` confidence override 掩盖了 fallback，journal 中 `p_win=0.40` 占比 0%——降级不可观测
  - [Layer 3 — 根因]: RC-06 (contract-violation): 函数接口只返回 float 不返回质量标记。FIX-20260526-031 引入 fail-closed 0.40 时只改了值未加可观测性
- **证据引用**:
  - Source 1: `core/execution/pwin_chain.py:53` — `pnl_store is None → return 0.40` 无日志
  - Source 2: `core/execution/pwin_chain.py:63` — `except Exception: pass  # noqa: BLE001` 吞一切
  - Source 3: `data_btc/live_trade_journal.jsonl` — 98 opens, p_win=0.40 count=0
  - Source 4: `data/live_trade_journal.jsonl` — 816 opens, p_win=0.40 count=0
- **是否被推翻**: 否 — AR 反向假设（不需要改，下游有安全网）被推翻：BLE001 吞一切异常是真实风险
- **关联 ReB Pattern**: ReB-20260612-001 (`SILENT_FALLBACK_ZERO_OBSERVABILITY`)
- **关联 FIX**: FIX-20260612-001

### CCT-20260612-001
- **Docket ID**: DQAF-20260612-001
- **日期**: 2026-06-12
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: data_health_overall=CRITICAL. trade_journal FAIL (PnL null 17.6%), journal_completeness FAIL (close_price 38%, dupes 89, trail 6.9%), conformal_calibrator FAIL (0 computations). 位置脆弱性 5× `.values()` 站点.
  - [Layer 2 — 中间异常]: (a) 幽灵平仓洪水: ticket 3807506009 76次平仓/80分钟, 75 rejected. (b) PnL回填断裂: bridge worker 用 mid-price 估算 PnL, receipt 不更新实际成交价. (c) MIA PnL 捕获: 单次 history_deals_get 无重试 → 23% 失败率 (10/43). (d) Trail 标签盲点: reconciliation close_reason=4 一律 sl_hit_first, 不管 trail_advances. (e) calibrator cold_started 永不 False.
  - [Layer 3 — 根因]: RC-06 (contract-violation): close-in-flight 去重缺失 + dict.values() 顺序依赖 + PnL 写入早于 fill 确认 + label taxonomy 缺失 trail 维度 + cold_started 无过渡逻辑
- **证据引用**:
  - Source 1: `data_btc/state/data_health_state.json` — overall=CRITICAL, 3 fail + 8 warn
  - Source 2: `data_btc/live_trade_journal.jsonl` — 589 lines, 36/246 PnL null, 89 dupes, 0 trail label
  - Source 3: `core/execution/position_manager.py:348` — PENDING_CLOSE_MAX_CYCLES=3 太短
  - Source 4: `scripts/mt5_bridge_worker.py:665` — pnl=msg_payload.get("pnl") 估算值, 永不更新
  - Source 5: `core/runtime/reconciliation.py:132-133` — close_reason=4 → sl_hit_first 无条件
  - Source 6: `core/execution/conformal_calibrator.py:306` — cold_started 写入后永不改为 False
- **是否被推翻**: 否 — AR 反向假设 (CRITICAL 是误报) 被推翻
- **关联 ReB Pattern**: ReB-20260612-002 (`PHANTOM_CLOSE_FLOOD`), ReB-20260612-003 (`TRAIL_LABEL_BLINDSPOT`), ReB-20260612-004 (`PNL_BACKFILL_GAP`), ReB-20260612-005 (`CALIBRATOR_COLD_STALLED`), ReB-20260612-006 (`POSITIONAL_FRAGILITY`)
- **关联 FIX**: FIX-20260612-002, FIX-003, FIX-004, FIX-005

## DQAF-20260612-002: no_live_brains 全交易阻塞

- **Label**: TRIPLE_BOOKKEEPING_RESIDUAL
- **Docket**: DQAF-20260612-002 (Sev 1)
- **Causal Chain (3 Layers)**:
  - **Layer 1 (症状)**: Golden Master 所有 12 周期标记 [degraded: no_live_brains], should_trade=True 但 decisions=0, 交易降级至 0.01 vol
  - **Layer 2 (中间异常)**: strategy_evaluator.py Cut 4 计算出 _live_count=0. strategy.brains 含 7 个 brain, 无一在 governance_state 中 status=live
  - **Layer 3 (根因)**: FIX-20260610-001 退役 BTC_Swing_V5 留下三处残留——(a) registry status=retired→strategy_builder 过滤, (b) vote_weight=0.0→投票权归零, (c) live_btc.yaml enabled=false→load_brain_entries 级别禁用. Governance 将 V5 升为 live 但上述三处均未同步.
- **Evidence**:
  - Source 1: live_btc.yaml:18 — BTC_Swing_V5 enabled: false
  - Source 2: BTC_Swing_V5.json — status=retired, vote_weight=0.0
  - Source 3: intent log — disabled_brains_filtered: before=8 after=5 (V5 被过滤)
  - Source 4: governance_state.json — BTC_Swing_V5.status=live
  - Source 5: _dqaf_probe_cut4.json — voted_ids=['V11_H1','V11_M15'] (V5 缺席)
- **Fix**: 三步同步 + Cut 4 SSOT 重构 → FIX-20260612-006
- **关联 ReB**: ReB-20260612-007 (TRIPLE_BOOKKEEPING_RESIDUAL), ReB-20260612-008 (GOVERNANCE_BRAIN_SOURCE_MISMATCH)
- **关联 FIX**: FIX-20260612-006

---

### CCT-20260621-033 (UPDATED 2026-06-21 — P0 Investigation Complete)
- **Docket ID**: DQAF-20260621-033
- **日期**: 2026-06-21
- **严重等级**: Sev 2 — 66%仓位在系统控制外平仓
- **置信度**: **confirmed** — H1 (double-journaling) FALSIFIED by temporal analysis. H2 (broker/external close) confirmed at 100%.
- **因果链 (已确认)**:
  - [Layer 1 — 症状]: 99/150 (66%) 仓位关闭不由系统 managed_close 触发。`close_accepted`(51笔) 和 `mt5_deal_reason_3`(99笔) 形成零交集（时序 asof merge ±5s: 0 matched pairs in `scripts/analyze_dqaf033_temporal_coupling.py`）。
  - [Layer 2 — 中间异常]: MT5 DEAL_REASON_SIGNAL (3) = Python API 自动化交易归类。99 笔: 91% LONG (逆势), 93% 0.01 lot, 中位持仓 40min, 24h 均匀分布。全部有 SL/TP 设置, 66/99 有 modify_sltp trail。V4 受冲击最重 (66笔), 但 MIA 交易 PnL (+26.68R) > 非 MIA (+6.50R)。
  - [Layer 3 — 根因]: **RC-08 (observability gap) + RC-04 (taxonomy drift)** — (A) bridge worker 不捕获 deal_reason, PCA 使用裸格式串, 两路径命名不一致；(B) 两条路径覆盖互斥仓位集合, 66% 出场失去可控性溯源；(C) 均未使用 `position_identifier` 作为对账主键。
- **P0 热补丁 (FIX-20260621-035)**: `mt5_bridge_worker.py` detail 新增 `deal_reason` + `position_close_adapter.py` `_DEAL_REASON_MAP.get()`
- **P1 已完成 (FIX-20260621-036)**: ✅ `PositionClosed` 合约新增 `position_identifier` 字段, PCA 从 `deal.position_id` 捕获, bridge detail + journal record 两路径注入
- **P2 待执行**: 从 MT5 终端导出原始账单核对 99 笔 MIA 的 Comment 字段

---

### CCT-20260621-034 (UPDATED 2026-06-21 — FIX-037 + FIX-038 Deployed)
- **Docket ID**: DQAF-20260621-034
- **日期**: 2026-06-21
- **严重等级**: Sev 2 — 出场质量退化，Trailing SL 功能大面积失效
- **置信度**: confirmed (Iron Law #11 脚本数据 + 代码审计 + V3 恢复路径硬编码证实三层根因 + 实盘部署后审计追加两刀)
- **因果链**:
  - [Layer 1 — 症状]: 48.7% 仓位零快照, trail 从未激活。Δ PnL = +282R (ACTIVE vs INACTIVE)
  - [Layer 2 — 中间异常 A]: V3 恢复 `current_sl=0.0` → snapshot 守卫 `_current_sl <= 0 → SKIP` → 相互死锁
  - [Layer 2 — 中间异常 B (FIX-038 追加发现)]: V3 恢复不设 strategy_name → 仓位脱离策略归属 → trail_policy 降级。entry_price 漂移 (64445.31↔64456.0) → 风险原点移动 → 保本/移动止损基准错乱
  - [Layer 3 — 根因]: RC-08 (V3 restore+snapshot guard mutual deadlock) + RC-06 (V3 序列化缺失策略归属字段) + RC-02 (可变 @dataclass entry_price 无保护)
- **Fix Summary**:
  - **FIX-037** (三刀热补丁): sync_position_from_mt5() + force_init_snapshot + fallback_unmanaged — 阻断死锁
  - **FIX-038** (两刀架构修复): V3 strategy 序列化补全 + entry_price 不可变锁 — 消除数据模型缺口
  - **FIX-039** (L3 架构收敛): 移除冗余 per-cycle sync → CRITICAL 告警替代 — recovery 失败可见
- **关联 ReB**: STATE_INITIALIZATION_DEADLOCK, SERIALIZATION_ATTRIBUTION_GAP, MUTABLE_RISK_ORIGIN, DEAD_SAFETY_NET_MASKING_RECOVERY_FAILURE
- **关联 FIX**: FIX-20260621-037, FIX-20260621-038, FIX-20260621-039
- **状态**: **CLOSED** — 架构债清偿, 全链路收敛于 recovery-once + alert-on-failure
  - H2: `register_position()` 是否同步注册 snapshot listener？→ 检查 `position_manager.py` listener attachment
  - H3: 0-snapshot 仓位是否全部为微生命周期(< 5 bar)？→ 交叉验证 snapshot count vs bars_held
- **是否被推翻**: 部分 — H1/H2 (竞态) 证伪: snapshot 在 management phase 内部执行, 与 registration 存在 happens-before。真正根因是 **STATE_INITIALIZATION_DEADLOCK**: (A) V3 恢复 `current_sl=0.0` 硬编码 → snapshot 守卫拒绝写入, (B) 31 仓位注册流水线断裂 (空白 strategy)
- **IC 裁决**: APPROVED WITH HOTFIX MANDATE — 三刀斩断 (FIX-20260621-037)
- **关联 ReB Pattern**: `STATE_INITIALIZATION_DEADLOCK` — 状态机冷启动默认值 (0.0) 与下游激活门槛 (>0) 形成逻辑互斥
- **关联 FIX**: FIX-20260621-037 (deployed)
- **部署后验证**: 下次系统重启后, 所有 V3 恢复仓位应在首个管理周期从 MT5 同步真实 SL, snapshot 不再抛弃 SL 未初始化仓位

---

### CCT-20260621-046

- **Docket ID**: DQAF-20260621-046
- **日期**: 2026-06-21
- **置信度**: confirmed (双源: probe script stdout + code audit × 16 brain configs)

**Layer 1 — 症状 (信号真空)**:
  - XAU live_shadow_ensemble 连续 45 天产出空决策文件 — 41/41 brains 返回 `neutral`/`ABSTAIN`, 0 条方向信号
  - 证据: `scripts/probe_xau_signal_generation.py` stdout — decision file history: 45 files, 0 nonempty; per-brain inference: 16/21 fallback (dim_mismatch), 5/21 real (weak), 41/41 neutral
  - 置信度: confirmed

**Layer 2 — 中间异常 (双根因)**:
  - **2a. BrainSignal API fracture**: `signal.prediction` dict 被替换为 `signal.direction: Direction` Literal + `signal.confidence: float` + `signal.raw_score: float` frozen dataclass。live_shadow_ensemble `_run_single_brain()` line ~106 仍使用旧接口 `signal.prediction.get("direction_bias", "neutral")` → 所有脑返回 neutral
  - **2b. Feature dimension mismatch**: 16 个 swing/barrier/trend brains (35-dim swing_enhanced_35 schema) 收到 40-dim institutional v9 特征 → 完全不同特征空间 → `dim_mismatch` fallback → 5 个 institution brains 信号极弱 (confidence < 0.52)
  - 证据: `scripts/live_shadow_ensemble.py:_run_single_brain()` — dict access on frozen dataclass; `core/features/schemas/swing_enhanced_schema.py` vs `v9_institutional_schema.py` — 35 vs 40 dim, different feature definitions
  - 置信度: confirmed

**Layer 3 — 根因 (L2 逻辑缺陷: 特征流水线无 schema 路由)**:
  - 特征生产层 (feature store/computers) 与特征消费层 (brain inference) 之间缺少 **schema routing contract**。brain config 中虽已有 `feature_schema_id` 字段, 但未在 feature resolution 路径中消费——特征路由器未实现 → 所有 brain 默认收到 v9 40-dim vector
  - 反模式: (1) BrainSignal 接口无向后兼容层——consumer 在不知情的情况下被破坏, (2) 特征维度无运行时校验——35-dim model 静默接收 40-dim input → model.predict() 内部 pandas/numpy 列对齐可能产生无警告截断或错误广播
  - 证据: `core/features/schemas/registry.py` — schema registry exists but not consumed; 16 brain configs — `feature_schema_id` field present but routing code missing
  - 置信度: confirmed

**证据引用**:
  - Source 1: `scripts/probe_xau_signal_generation.py` stdout — 完整诊断输出 (Iron Law #11 compliant)
  - Source 2: `scripts/live_shadow_ensemble.py` line 106 — `signal.prediction.get("direction_bias", "neutral")` 旧接口
  - Source 3: 16 brain configs `configs/brains/*.json` — `feature_schema_id: "swing_enhanced_35"`
  - Source 4 (cross-symbol): BTC ensemble 未受影响 — BTC brain 全部使用 institution schema
- **是否被推翻**: 否 — AR 反向假设 (单点配置错误) 被推翻: 16/21 brains dim_mismatch 证明是系统性 schema 路由缺失
- **关联 ReB Pattern**: `FEATURE_SCHEMA_ROUTING_AND_BRAIN_API_CONTRACT`
- **关联 FIX**: FIX-20260622-003 (XAU dual-track), FIX-20260622-001 (Plan B State Governance Protocol)
- **状态**: **CLOSED** — Dual-track feature pipeline deployed: 35-dim swing resolver (DailyFeatureComputer 24 daily + 9 micro + 2 TF) + feature router (feature_schema_id) + BrainSignal API fix (direction/confidence/raw_score). 0/21→11/21 non-neutral. Plan B 同步交付防止复发

## DQAF-20260623-066: p_win Cold-Start Triple-Break

- **Label**: COLD_EXPLORE_TRAP
- **Docket**: DQAF-20260623-066 (Sev 1)
- **Causal Chain (4 Layers)**:
  - **Layer 1 (症状)**: 30 笔交易亏损 -34.84R, 胜率 ~10% (XAU 0/6, BTC 3/24)。系统盈利能力崩溃。
  - **Layer 2 (直接原因)**: 所有获批交易使用 p_win=0.50 (cold_explore_neutral)。Kelly sizing, RR gate, volume 全部基于假数据 → 好策略和坏策略获得相同仓位规模。
  - **Layer 3 (中间异常)**: DQAF-065 MetaFilter 切除后 swing 策略永远返回 (None, None) → 触发 `_is_cold_explore=True` → p_win 硬编码为 0.50。BrainPnLStore 重启后为空 → `resolve_p_win_from_brains()` 返回 0.40 → 低于 min_p_win → 所有通过 rolling WR 路径的交易被拒。
  - **Layer 4 (根因 — L3 架构缺陷)**: p_win 计算链路三连环断裂:
    1. DQAF-065 → swing 策略唯一可行通道是 cold_explore
    2. BrainPnLStore 纯内存, 重启后为空 → 无真实统计 → fail-closed 0.40
    3. Governance `performance_metrics` 存在但未接入 p_win 解析链
- **Evidence**:
  - Source 1: `live_trade_journal.jsonl` — XAU 6 笔全部 p_win=0.50, BTC 24 笔全部 p_win=0.50
  - Source 2: `golden_master.jsonl` — XAU 1107 决策中 21 approved (1.9%), BTC 414 中 104 approved (25%)
  - Source 3: `strategy_line.py:922-930` — cold_explore 触发条件: _meta_p_win is None AND _meta_reject is None
  - Source 4: `meta_filter_routing.py:74-89` — DQAF-065: swing 策略不在 statarb 条件中 → passthrough (None, None)
  - Source 5: `pwin_chain.py:81-106` — DQAF-059 governance gate: sample_count<10 排除 → 0 valid rates → 0.40 fallback
  - Source 6: `brain_pnl_ledger.py:548-553` — BrainPnLStore.__init__() 纯内存, 无 data_dir 参数
- **是否被推翻**: 否 — AR 反向假设 (行情不利) 被推翻: BTC 在窗口中仅下跌 2%, 但 16/24 笔保本退出 (PnL=0.00) 表明是系统决策问题非行情问题
- **关联 ReB Pattern**: `COLD_EXPLORE_TRAP`
- **关联 FIX**: FIX-20260623-066 (P0-1 governance fallback, P0-2 cold_explore→governance, P0-3 ≥2 LIVE brains gate)
- **状态**: **CLOSED** — 三修复部署: `resolve_p_win_from_brains()` governance cold-start fallback + `resolve_p_win()` cold_explore governance 替代盲 0.50 + cold explore ≥2 LIVE brain 准入门禁

### CCT-20260623-070
- **Docket ID**: DQAF-20260623-070
- **日期**: 2026-06-23
- **置信度**: confirmed (code audit × grep × production log evidence)
- **因果链**:
  - [Layer 1 — 症状]: 每周期 `session_guard_error`: `'LiveCycleState' object has no attribute '_feature_buffers_warm'`。重启后 feature freshness check 从未真正执行 — 冷特征直接进入交易决策。
  - [Layer 2 — 中间异常]: `session_guards.py:148` 访问 `state._feature_buffers_warm`, 但 `LiveCycleState` dataclass (live_cycle.py:214-300) 从未定义此字段。AttributeError 被外层 `except Exception` (line 167) 捕获 → fail-open → 周期继续。
  - [Layer 3 — 根因]: L1 — Strangler Fig 重构时 `_feature_buffers_warm` 字段未被提取到 dataclass。L2 — `run_session_guards()` 的外层异常处理过宽 (`except Exception`) — 状态完整性错误与瞬时 MT5 超时被同等对待 (fail-open)。
- **证据引用**:
  - Source 1: `core/runtime/live_cycle.py:214-300` — LiveCycleState 缺少 `_feature_buffers_warm`
  - Source 2: `core/runtime/session_guards.py:148` — 直接属性访问 `state._feature_buffers_warm`
  - Source 3: `tests/runtime/test_session_guards.py:46,61,78,111,139,158,174,189` — 测试代码 mock 了此字段, 证实设计意图但从未在生产代码中实现
  - Source 4: `data_btc/logs/intent_*.log` — 每周期 `session_guard_error`
- **是否被推翻**: 否 — AR 假设 (字段在其他地方动态设置) 被全库 grep 推翻: 仅在测试中有设置, 生产代码零初始化
- **关联 ReB Pattern**: `MISSING_DATACLASS_FIELD`
- **关联 FIX**: FIX-20260623-070 (补齐字段 + MT5 bootstrap 后置 True + getattr 安全访问 + 异常处理分层)

### CCT-20260623-071
- **Docket ID**: DQAF-20260623-071
- **日期**: 2026-06-23
- **置信度**: confirmed (production log evidence × code analysis)
- **因果链**:
  - [Layer 1 — 症状]: 定期出现 "FeatureService stale cache for BTCUSDc: age=300.1s" → Tier 2 实时计算不必要触发 → MT5 IPC 负载增加。
  - [Layer 2 — 中间异常]: 特征持久化间隔 (~60s × 5 = 300s) 恰好等于新鲜度 SLA (300s)。缓存恰好在边界翻转 — 第 5 周期时 age≈300s, 第 6 周期时 age≈360s。每次翻转触发 live compute。
  - [Layer 3 — 根因]: L2 — 写入间隔与读取 SLA 相同 (两个独立参数设为同一值 300s), 无抖动余量保证写入持续领先检查线。
- **证据引用**:
  - Source 1: `core/features/feature_service.py:139` — `max_age_seconds=300.0` (修复前)
  - Source 2: `data_btc/logs/intent_*.log` — "age=300.1s (limit=300s), falling through to live compute"
- **是否被推翻**: 否 — AR 假设 (特征持久化失败) 被代码审查推翻: persist_micro_features 正常运行, 只是间隔恰好 300s
- **关联 ReB Pattern**: `CACHE_SLA_BOUNDARY`
- **关联 FIX**: FIX-20260623-071 (SLA 300→310s 负向抖动余量)

### CCT-20260623-072
- **Docket ID**: DQAF-20260623-072
- **日期**: 2026-06-23
- **置信度**: confirmed (code audit × grep × production log evidence)
- **因果链**:
  - [Layer 1 — 症状]: DQAF-059 "ZERO LIVE brains found" 警告每周期触发 + DQAF-066 governance cold-start fallback 日志从未出现 + p_win 始终退化为 `brain_confidence`。
  - [Layer 2 — 中间异常]: `strategy_line.py:538` — `governance_state.items()` 遍历顶层键 (`"brain_states"`, `"schema_version"`, `"performance_metrics"`), 而非 `governance_state["brain_states"].items()`。`_live_brain_ids` 恒为空集 `set()` (非 None)。在 `resolve_p_win_from_brains()` 中, `live_brain_ids is not None` → True, 但 `brain_id not in live_brain_ids` → True (所有 brain_id 都不在空集内) → 所有 brain 被治理门过滤 → governance fallback 也因相同检查而失败。
  - [Layer 3 — 根因]: L1 — `governance_state.items()` 应该在 `governance_state["brain_states"].items()` 上迭代。L2 — 缺少集成测试: 空 `_live_brain_ids` 从未被任何测试捕获。
- **证据引用**:
  - Source 1: `core/execution/strategy_line.py:536-540` (修复前) — `for bid, b_info in governance_state.items()`
  - Source 2: `core/execution/pwin_chain.py:99-101` — `if live_brain_ids is not None and brain_id not in live_brain_ids: continue`
  - Source 3: `data_btc/logs/intent_*.log` — `FALLBACK_PATH_3c: All 1 brain(s) filtered out by governance gate (none are LIVE)`
  - Source 4: governance_state.json 结构 — `{"brain_states": {...}, "performance_metrics": {...}}` — 顶层键不含 `status` 字段
- **是否被推翻**: 否 — BTC_Swing_V12_H1_Survival 是 LIVE (WR=51.5%, 56 trades) 但从未出现在 `_live_brain_ids` 中
- **关联 ReB Pattern**: `WRONG_DICT_LEVEL_GOVERNANCE`
- **关联 FIX**: FIX-20260623-072 (`governance_state.get("brain_states",{}).items()` 单行修复)
- **状态**: **CLOSED** — DQAF-059 治理门过滤 + DQAF-066 治理冷启动回退 + DQAF-066 cold_explore 门禁全部自愈

---

### CCT-20260625-125
- **Docket ID**: DQAF-20260625-125
- **日期**: 2026-06-25
- **置信度**: confirmed (code audit × production log evidence × file path verification)
- **因果链**:
  - [Layer 1 — 症状]: XAU `data/reports/leaderboard.json` generated_at=2026-06-22T05:44 (66h/3956min stale), BTC leaderboard 819min stale. `daily_ops_complete` 事件从未发出。
  - [Layer 2 — 中间异常]: `live_launcher` log L13632: intent loop crashed with exit code 1 (restart #12) during daily_ops execution — pipeline interrupted after feedback step, before retraining_check. Watchdog 未能自动恢复。
  - [Layer 3 — 根因]: FIX-20260622-001 (Plan B StateWriter 迁移) 遗漏 `watchdog_daily_ops.py`。三处 bug 叠加: (a) 路径 `base_dir/"daily_ops_state.json"` 与实际 `base_dir/"state"/"daily_ops_state.json"` 不匹配, (b) 字段名 `last_run_utc`/`updated_utc` 与实际 `last_daily_ops_utc` 不匹配, (c) `age_h is None` 分支未实现 auto_run。Watchdog 完全盲化 — 即使 `--auto-run` 也不执行。
- **证据引用**:
  - Source 1: `data/reports/leaderboard.json` — generated_at=2026-06-22T05:44:49 (文件 mtime 验证)
  - Source 2: `data/logs/live_launcher_20260624T114818Z.log:13632` — `intent exited with code 1`
  - Source 3: `scripts/watchdog_daily_ops.py:61` (修复前) — `state_path = base_dir / "daily_ops_state.json"`
  - Source 4: `core/state/catalog.py:321` — `path_template="state/daily_ops_state.json"` (SSOT)
- **是否被推翻**: 否 — 所有三处 bug 均已代码审计确认
- **关联 ReB Pattern**: `ORPHAN_WATCHDOG_MIGRATION_SWEEP_INCOMPLETE`
- **关联 FIX**: FIX-20260625-125 (路径修正 + 字段名修正 + never-run auto_run)
- **状态**: **CLOSED** — FIX-20260625-125 deployed. Leaderboard 手动恢复: XAU 69 brains 1192 decisions

---

### CCT-20260630-202
- **Docket ID**: DQAF-20260630-202
- **日期**: 2026-06-30
- **置信度**: confirmed (code audit × ShadowTracker evidence × governance_state.json verification)
- **因果链**:
  - [Layer 1 — 症状]: H4_V3 (456 directional signals, 100% SHORT, avg_conf=0.551) stuck at `candidate` status despite meeting all Rule 85 auto-promotion criteria. No promotion event in governance transition_log.
  - [Layer 2 — 中间异常]: (Contract 1) `v9_onnx_brain_adapter.py::get_signal()` used global hardcoded `activation_threshold=0.1` — H4_V3 raw_scores [-0.07, -0.12] fell entirely within dead-zone → 0% directional signals before 6/29 config fix. (Contract 2) `_promote_shadow_brains()` in `governance_scheduler.py:332` hard-coded `if m.long_count < 5 or m.short_count < 5: continue` — 0L/456S rejected. The same Rule 85 logic in `governance_rule_engine.py:338-349` had already been fixed with macro exemption (FIX-20260701-203), but the duplicate in `governance_scheduler.py` was not updated.
  - [Layer 3 — 根因]: **L3 Architectural — Rule 85 duplicated across two governance paths without synchronization mechanism.** The BTC path (`scheduler_service.py` → `GovernanceRuleEngine.evaluate()`) and XAU path (`daily_ops_scheduler.py` → `run_governance_cycle()` → `_promote_shadow_brains()`) independently implement the same rule. The architecture has no unified rule evaluation entry point — each path must be maintained separately. Additionally, `daily_ops_scheduler.py:200` failed to pass `base_dir=config.base_dir`, causing XAU ShadowTracker to default to `data_btc/brain_votes/` (empty for XAU).
- **证据引用**:
  - Source 1: `data/brain_votes/2026-06-28.jsonl` through `2026-07-01.jsonl` — 456 H4_V3 directional signals, 0 long, 456 short
  - Source 2: `scripts/training/governance_scheduler.py:327-334` (pre-fix) — hard-coded diversity check without macro exemption
  - Source 3: `core/governance/governance_rule_engine.py:338-349` — macro exemption already present (FIX-20260701-203) but only on BTC path
  - Source 4: `data/governance_state.json` — H4_V3 status=candidate, no promotion transition
  - Source 5: `core/runtime/daily_ops_scheduler.py:200-201` (pre-fix) — missing `base_dir=config.base_dir` pass-through
- **是否被推翻**: 否
- **关联 ReB Pattern**: `TOXIC_DIVERSITY_GATE`, `DUPLICATE_RULE_UNSYNC`
- **关联 FIX**: FIX-20260630-202, FIX-20260701-203, FIX-20260701-204

---

### CCT-20260706-003
- **Docket ID**: DQAF-20260706-003
- **日期**: 2026-07-06
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 重启后 3 笔 XAU 实盘交易由 vote_weight=0.0 的影子大脑决策 — H4 SHORT (05:08Z), M15 LONG (05:15Z), M30 LONG (06:14Z). 证据: `data/golden_master.jsonl` cycle=1 H4 SHORT `[degraded: no_live_brains]`, cycle=3 M15 LONG `[degraded: non_live_dominance]`, cycle=15 M30 LONG `[degraded: no_live_brains]` + `data/live_trade_journal.jsonl` 对应的 3 笔 open 事件
  - [Layer 2 — 中间异常]: `strategy_evaluator.py:605-611`: `_live_count` 计算仅使用 `status in ("live","probation")` 过滤，未考虑 `vote_weight`。governance_state 中 8 个 Tracer 大脑注册为 status=candidate + vote_weight=0.0 — 重启后 live 大脑尚未产出信号时，仅 candidate/shadow 大脑投票 → `_live_count=0` → 触发降级路径 (`confidence ≥ 0.50 + volume ≤ 0.01`)
  - [Layer 3 — 根因]: L3 架构缺陷: Cut 4/Cut 4-bis 降级门缺少 vote_weight 门禁。FIX-20260625-139 在信号管线层面修复了 vote_weight 传透 (BrainSignal → `_compute_weighted`)，但 `strategy_evaluator.py` 的并行治理门从未被更新。这是 strategy_evaluator.py 中「跨文件重复门逻辑」的第 4 个实例 — 前三次: FIX-20260629-174 (governance 访问路径), FIX-20260703-061 (status 维度), FIX-20260625-139 (信号管线 vote_weight — 仅修了 strategy_line.py 漏了 strategy_evaluator.py)
- **证据引用**:
  - Source 1: `core/runtime/strategy_evaluator.py:605-611` (pre-fix) — `_live_count` 仅按 status 过滤
  - Source 2: `core/runtime/strategy_evaluator.py:612-651` (pre-fix) — `_live_count==0` 降级路径无 vote_weight 检查
  - Source 3: `data/governance_state.json` — 8 个 Tracer 大脑: status=candidate, vote_weight=0.0
  - Source 4: `configs/brains/XAU_Swing_*_A.json` — IC Mandate: "Shadow, vote_weight=0.0, 7-day observation"
  - Source 5: `core/runtime/strategy_line.py` — FIX-20260625-139 已修复 signal 管线 vote_weight (证明 parallel gate 遗漏)
- **是否被推翻**: 否
- **关联 ReB Pattern**: `CROSS_FILE_DUPLICATE_GATE_LOGIC`
- **关联 FIX**: FIX-20260706-003
- **状态**: **CLOSED** — FIX-20260701-204 deployed. H4_V3 macro exemption active on both governance paths. Restart required for promotion evaluation.

---

### CCT-20260708-001
- **Docket ID**: DQAF-20260708-001
- **日期**: 2026-07-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: analyze_live_journal 报 157 BTC 孤儿平仓 (-$170.04); JournalGate 隔离区 184 条全 `close_without_open` (153 in July, ~17/day); label_builder 静默丢弃换号交易 (双轨标签损失)。证据: `scripts/analyze_live_journal.py:88/116`, `core/ledger/services/journal_gate.py:92`, `scripts/_forensic_orphan_closes.py` stdout
  - [Layer 2 — 中间异常]: MT5 在 partial-close/netting 换号 → 平仓携带新 `position_ticket`, 开仓保留原始 ticket; 所有 join 站点用可变 ticket 配对 open<->close。证据: `core/runtime/live_cycle.py:3749-3786` (换号只更新内存), `core/runtime/position_close_adapter.py:366/429` (close 从 deal.position_id 取 identifier)
  - [Layer 3 — 根因]: RC-02 type-confusion — 无单一以不可变 `position_identifier` 为键的生命周期权威; 可变 ticket 被当作稳定 join 键。~30 次历史修复全在下游打补丁; TECH_DEBT-003 命名 remedy 但记错 SSOT key。
- **证据引用**:
  - Source 1: Journal — `data_btc/live_trade_journal.jsonl` (34/35 identifier-matched-to-open, 0 identifier==ticket)
  - Source 2: State — `data_btc/journal_orphan_quarantine.jsonl` (184 close_without_open, 153 July)
  - Source 3 (cross-symbol): `data/live_trade_journal.jsonl` — XAU 53 orphan + 8009 no-ticket (异质分支: 键缺失而非键变, 3975/Jun→26/Jul 已自愈, 独立跟踪)
- **是否被推翻**: 否 (AR 证伪了 "pre-June-7 legacy" 与 "open leg lost" 两个反假设)
- **关联 ReB Pattern**: `MUTABLE_TICKET_JOIN_ON_IMMUTABLE_POSITION`
- **关联 FIX**: FIX-20260708-001
- **状态**: **CLOSED** — FIX-20260708-001 committed f139ab87. BTC 孤儿 157→119, $126 回收; journal_gate 覆盖 0%→86%。

---

### CCT-20260708-002
- **Docket ID**: DQAF-20260707-003
- **日期**: 2026-07-08
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: btc_swing_h1 (V12_H1_15) golden_master 近乎 100% 输出 LONG (记忆 diagnostics_20260628_btc_all_long_bias); 模型对多空无判别力 (Wasserstein=0.0084)。
  - [Layer 2 — 中间异常]: 补 7 个 H1 时间尺度方向特征 (H1_Ret_1/2/4, H1_Realized_Vol, H1_Ret_Accel, H1_MeanRev, H1_M5_Div) 做 48-dim 重训后, 判别力不升反降 (Wasserstein 0.0084→0.0019)。证据: `data_btc/models/btc_swing_h1_binary_48/training_summary.json` cv mean_val_wr xgb=0.5081 / lgbm=0.4895。
  - [Layer 3 — 根因]: L3 结构性 — H1 尺度方向不可从现有 M5/D1/H4 特征空间线性分离; 加特征无法拯救不可分信号 → 正确响应是退役该策略线, 而非继续调参 (反例 BTC 三连打地鼠)。
- **证据引用**:
  - Source 1: 训练 — `data_btc/models/btc_swing_h1_binary_48/training_summary.json` (val_wr ≈ 50%, 与随机不可区分)
  - Source 2: 配置 — `configs/live_btc.yaml:152/313` (V12_H1_15 + btc_swing_h1 retired, Wasserstein 记录)
  - Source 3 (机制复用): `core/brains/adapters/base_adapter.py:217-228` (quantile_gaussian 已 live 于 8 XAU brain)
- **是否被推翻**: 否 (AR 证伪了 "48-dim 模型其实可用只是没部署" — val_wr 50.8%±0.7% 与随机不可区分)
- **关联 ReB Pattern**: `FEATURE_ENGINEERING_CANNOT_RESCUE_UNSEPARABLE_SIGNAL`
- **关联 FIX**: FIX-20260708-002
- **状态**: **CLOSED (退役决策)** — FIX-20260708-002. BTC live 收敛至 V4 + B-path binary(probation); V4 confidence 采用 quantile_gaussian 校准 (T22 监控); 48-dim serving 休眠留待 Path C horizon=4。

---

### CCT-20260709-001
- **Docket ID**: DQAF-20260709-001
- **日期**: 2026-07-09
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `scripts/analyze_live_journal.py --data-dir data` (XAU) 在 Section 3 崩 `TypeError: unsupported format string passed to NoneType.__format__` (line 559 `{lbl:<55s}`), 完整 7 段审计不可得。证据: `scripts/analyze_live_journal.py:559`; 直接调用 `analyze_journal(Path("data"))` 产出 `pnl_by_label` 含 None 键 `{count:114, pnl_usd:0.68, wins:34, losses:20}`。
  - [Layer 2 — 根因]: RC-01 missing-null-check — line 135 `final_close.get("label", "?")` 对 present-but-null 失效: `dict.get(k, default)` 仅在 key **缺失**时替换 default; key 存在值为 None 时返回 None。114 条 XAU close 合法携带 `label: null` (no-ticket 孤儿分支, 早于 FIX-20260626-144 write-side 加固) → None 贯穿 realized → 成 `pnl_by_label` 字典键 → :559 `:s` 格式化崩溃。同构潜伏点 :133 side / :136 ack。上游 write-side null-label 已由 FIX-20260626-144 封堵, 但 114 条为不可变历史遗留 → 审计脚本须对合法 null 鲁棒。
- **证据引用**:
  - Source 1: 代码 — `scripts/analyze_live_journal.py:135` (`.get("label","?")`), :559 (`{lbl:<55s}`)
  - Source 2: 数据 — `data/live_trade_journal.jsonl` 114 条 label:null; `analyze_journal()` 直调产出 None 键
  - Source 3 (cross-symbol): `data_btc/live_trade_journal.jsonl` — BTC 0 null-label 不崩 (影响面隔离)
- **是否被推翻**: 否 (AR 证伪了 "None 是 pnl_usd 不是 label" — 该项 pnl_usd=0.68 有效 float, KEY 才是 None; 并证伪 "在 :559 打印处加 guard" — FIX-20260613-066 已在 Section 4 打印处 guard 却复发 → 须摄入边界根治)
- **关联 ReB Pattern**: `GET_DEFAULT_NULL_TRAP`
- **关联 FIX**: FIX-20260709-001
- **状态**: **CLOSED** — FIX-20260709-001 committed d9c147e8. `_coalesce()` 摄入边界单点规整 side/label/ack; XAU 恢复完整审计 + `(unlabeled)` 桶 114/+$0.68; 6 回归测试 (tests/scripts/test_analyze_live_journal_null_label.py)。

---

### CCT-20260709-002
- **Docket ID**: DQAF-20260709-002
- **日期**: 2026-07-09
- **置信度**: confirmed (出场/进场) + refuted-then-reclassified (持仓)
- **因果链** (三相独立根因, Iron Law #12 禁捆绑):
  - **[出场相]**
    - [Layer 1 — 症状]: XAU LONG 4098917446 (m30_swing) 开在 broker 却失管不平, 与 SHORT 4098792728 (h4_swing) 构成对冲。证据: LONG 快照停于 21:00:27; LONG 0 真实平仓 / 19 拒绝。
    - [Layer 2 — 中间异常]: 21:00 休市重启风暴 (launcher log 13 次重启 20:55–22:40); LONG 脱离 known_open_tickets → Guard 1 `position_manager_stale_cleared` ×11 (21:06–22:05) ↔ orphan 采纳 (仅 loop_iteration==1) → 乒乓; `active_position.json` 持久化 LONG 缺席 (orphan_position_adopted: active_position_tickets=[SHORT], mt5_tickets=[both])。
    - [Layer 3 — 根因]: L3 — Guard 1 (management_phase.py:947) 以 known_open_tickets 缺席推断"已平仓", **从不查 broker** (positions_get 才是 SSOT); 违反"broker 开着的仓必在管理集"不变量。
  - **[进场相]**
    - [Layer 1 — 症状]: 16:24 m30_swing LONG 对既有 (15:59) h4_swing SHORT 成形对冲。
    - [Layer 2/3 — 根因]: L3 — CrossStrategyCoordinator (block 默认) 自 P4-2 从未注入 live; strategy_evaluator.py:1071 守卫 `is not None` 恒 False → 反向持仓守卫死代码。
  - **[持仓相 — AR 推翻]**
    - [Layer 1 — 表观症状]: snapshot `unrealized_pnl_r` 达 -6.5R, SHORT SL 全程冻结 4162.674, 疑"亏损腿裸奔无保护"。
    - [Layer 2 — AR 证伪]: SL distance 127.76 = 2.0×63.88; h4_swing 配置 SL=2.0×ATR (H4 ATR≈63.9, FIX-20260706-027 per-TF ATR)。snapshot `entry_atr`=6.41 是 M5/入场 ATR, 仅供 R 度量。"-6.5R"=-6.5×M5_ATR≈-0.65×H4_ATR = 仅到 H4 止损的 ~26%。**正常 H4 swing, 非交易缺陷**。
    - [Layer 3 — 重分类]: 表观"亏损腿失护"根因**被推翻**; 真实次生问题为 R 度量 ATR 错配 (M5 vs H4) 与 bars_held 重启冻结 (可观测性/连续性, 非交易), 经 IC 裁决登记 Deferred, 不做投机交易改动 (机构级 mandate #1)。
- **证据引用**:
  - Source 1: 取证 — `scripts/forensic_xau_hedge_20260709.py` stdout (Iron Law #11); `data/position_snapshots.jsonl`; `data/live_trade_journal.jsonl` open 事件 sl=4162.674
  - Source 2: 日志 — `data/logs/live_launcher_20260708T154609Z.log` (stale_cleared ×11, orphan_position_adopted, 13 restarts, data_health current_positions=2)
  - Source 3: 代码 — `core/runtime/management_phase.py:947`, `core/runtime/strategy_evaluator.py:158/1071`, `configs/live.yaml` h4_swing SL=2.0×ATR
- **是否被推翻**: 出场/进场 否 (AR 证伪"经重启过滤/周期对账脱管"—SHORT 对照存活+LONG 0 exit deal; 证伪"PNG 已覆盖"—PNG 同周期, 对冲跨周期)。持仓 **是** (AR 证伪"亏损腿失护"—SL 按 H4 ATR 正确定尺, R 单位错配假象)。
- **关联 ReB Pattern**: `BROKER_STATE_NOT_CONSULTED_BEFORE_UNTRACK` (出场), `DORMANT_SAFETY_GUARD_NEVER_WIRED` (进场), `R_UNIT_MISMATCH_CROSS_TIMEFRAME` (持仓)
- **关联 FIX**: FIX-20260709-002 (出场), FIX-20260709-003 (进场)
- **状态**: **CLOSED** — 出场 017d726d + 进场 10e22cb2 committed+pushed; 持仓相搁置 (Deferred: R 度量 ATR 错配 + bars_held 重启冻结)。

---

## CCT Entry — DQAF-20260709-005

- **Docket ID**: DQAF-20260709-005
- **Severity**: Sev 4 (IC revised DOWN from initial Sev 1 escalation via Adversarial Review)
- **Date**: 2026-07-09
- **Layer 1 — Symptom (Extinction)**:
  `exit_watchdog.py:164` `_check_time_decay` `return unrealized_r < 0`.
  Because `position.unrealized_pnl_r` was NEVER set on ActivePosition dataclass
  (grep-confirmed: no `.unrealized_pnl_r =` / `setattr` / kwarg / SimpleNamespace),
  `getattr(position, "unrealized_pnl_r", 0)` always returns 0 → the check always
  returns False → `_check_time_decay` and `_check_price_decay` never fire.
- **Layer 2 — Intermediate (Viability)**:
  `evaluate_position` (the ONLY caller of `_check_time_decay` / `_check_price_decay`)
  has ZERO external callers repo-wide (grep-confirmed: only `def` at line 128).
  Zero `time_decay_` / `price_decay_` exits in any journal (data/ + data_btc/).
  → Not a "silently failed safety net" but a superseded, never-wired code path.
- **Layer 3 — Root Cause (Architectural)**:
  FIX-20260613-086 added `evaluate_position` as a model-independent structural
  evaluator.  Sometime later the time-decay exit role was absorbed by
  `PositionManager.should_exit_hesitation` (per-strategy `exit_hesitation_cycles`
  × `timeframe_scaling` → per-TF correct), producing the real
  `hesitation_Nc_no_breakeven` exits wired at `management_phase.py:1775`.
  The old evaluator was superseded but NEVER deleted, and its docstring
  (`exit_watchdog.py:137`) still claims "Live Cycle calls this once per open
  position per cycle" — a latent re-wiring trap.
- **AR Adversarial Review**:
  Reverse hypothesis "the attribute IS injected elsewhere" tested and REFUTED
  (grep all *.py). The initial IC Sev 1 "silent safety-net failure requiring
  immediate Hotfix" premise was overturned: the net was never deployed, not
  silently failing; fixing the attribute would resurrect an INFERIOR
  M5-hardcoded (60c) path that was deliberately superseded.
- **ECoL Evidence**:
  - Source 1: `exit_watchdog.py:161` getattr(position, "unrealized_pnl_r", 0) — never set
  - Source 2: grep `evaluate_position` repo-wide — 0 external callers
  - Source 3: journal grep `time_decay_\d+c` / `price_decay_\d+b` — 0 occurrences
  - Source 4: `position_manager.py:1826` `should_exit_hesitation` — TF-scaled wired equivalent
- **是否被推翻**: 是 — 原 Sev 1 前提被 AR 证伪; 降 Sev 4, 撤 Hotfix, 执行死代码删除
- **关联 ReB Pattern**: `SUPERSEDED_ORPHAN_CODE_WITH_STALE_DOCSTRING` (子签名: `PHANTOM_ATTR_IN_DEAD_BRANCH`)
- **关联 FIX**: FIX-20260709-005 (446ba31f)

---

## CCT-20260715-011: Counter-Trend Gate cold_explore Exemption → Systematic Counter-Trend Loss

- **Layer 1 — Symptom (Observable)**:
  BTC SHORT trades systematically lose in confirmed H4 bull trend market.
  Jul 14 cycle 9: trend_direction=long, trend_strength=0.6, yet btc_swing_h4
  opens SHORT at confidence=0.7449 (volume=0.01).  $ grep counter_trend in GM
  returns ZERO matches since 2026-06-09 — the gate has been silent for 5 weeks.
  Total SHORT loss: -$51.54 (102 trades, 39.2% WR).

- **Layer 2 — Intermediate (Mechanism)**:
  Two independent bypasses converge:
  (a) `strategy_line.py:1225`: `not _is_cold_explore` condition excludes all
      probation strategies (MetaFilter vacuum → `_is_cold_explore=True`).
      The counter-trend gate was designed to apply universally but the
      cold_explore exemption was added without architectural review.
  (b) `trend_volume_guard.py:268`: `thresholds.get(strategy_name)` uses exact
      match only.  `btc_swing_h4` does not match `btc_swing` → falls through
      to default (h4_block=0.70), which is above the current trend_strength=0.6.
  These two failures compound: even if (a) is fixed, (b) would let multi-TF
      strategies through the lenient default.  Even if (b) is fixed, (a) would
      skip the gate entirely for cold_explore strategies.

- **Layer 3 — Root Cause (Architectural Design Flaw)**:
  The design assumption that "cold exploration should be unconstrained" conflates
  TWO orthogonal dimensions: (1) model uncertainty (p_win unknown) and (2)
  structural market constraints (H4 trend gravity).  Trend alignment is NOT a
  statistical confidence problem — it is a physical market law.  A cold model
  exploring counter-trend is not "gathering data" — it is donating capital to
  the trend.  The exemption was an architectural error: cold_explore should
  reduce volume (uncertainty penalty), not bypass structural constraints.

- **AR Adversarial Review**:
  Hypothesis "trend_strength 0.6 is a false positive" → REFUTED: GM confirms
  trend_direction=long consistently across cycles 150-155.  Price action
  confirms bull trend (62k→64.5k).  The Kalman velocity signal is reliable.
  Hypothesis "cold_explore exemption is intentional for data gathering" →
  REFUTED: gathering counter-trend data during strong trend produces
  systematically negative-EV samples.  Trend-aligned exploration gathers
  equally valid data without structural penalty.

- **ECoL Evidence**:
  - E1: GM `grep counter_trend` → 124 matches, all before 2026-06-09
  - E2: Jul 14 cycle 9 GM: H4 SHORT, trend=long/0.6, should_trade=true
  - E3: `strategy_line.py:1225`: `not _is_cold_explore` condition
  - E4: `trend_volume_guard.py:268`: exact-match only for strategy_name
  - E5: Jul 15 restart cycle 4: H4 p_win=0.400 blocked by floor (Catch-22)

- **AR 是否被推翻**: 否 — AR 证伪两个反向假设, 根因确认

- **关联 ReB Pattern**: `COLD_EXPLORE_GATE_EXEMPTION` (子签名: `EXPLORATION_OVERRIDES_STRUCTURAL_CONSTRAINT`)

- **关联 FIX**: FIX-20260715-011 (a1886cfa)

- **状态**: **CLOSED** — 3 代码修复 + M15 退役 + BLE001 fail-open guard committed+pushed
- **状态**: **CLOSED** — 3 方法删除 + BLE001 noqa 标化 committed+pushed; 构造参数保留 vestigial (hot-path omega 约束, 下次 live_intent_loop.py 变更清理); 零僵尸测试

---

## CCT-20260722-002

- **Docket**: DQAF-20260722-002
- **Layer 1 (症状)**: 8/9 give-back positions (MFE≥1R, PnL≤0) labeled "loss" with zero exit signal provenance. p_win rolling_wr WR=41.2% vs brain_confidence WR=72.4%.
- **Layer 2 (传导)**: `position_close_adapter.py` label chain — watchdog→SL→TP→PnL fallthrough. For managed closes (bleed_stop, hesitation, time-based), the MT5 deal comment carries the exit reason set by dispatch_managed_close(), but the adapter ignores it and labels by PnL sign. `pwin_chain.py` resolve_p_win() rolling_wr step → resolve_p_win_from_brains() returns median win_rate regardless of aggregate sample count.
- **Layer 3 (根因)**: (P0) Adapter label assignment lacks comment-based signal preservation — PnL is not a causal signal. (P2) No sample-size degradation gate on rolling_wr source — Kelly chain receives noisy small-N estimates.
- **Fix**: FIX-20260722-002
- **Status**: **CLOSED**

## CCT-20260722-003

- **Docket**: DQAF-20260722-003
- **Layer 1 (症状)**: Ticket 4207155654 (h4_swing SHORT): +6.03R in entry_atr → SL never trailed from 4110.62 → gave back all profit → closed -$19.70. SL modification NEVER occurred throughout position lifecycle.
- **Layer 2 (传导)**: trail_dispatch.compute_and_dispatch_trail() → pm.compute_trail_stop() → TrailStopEngine.compute_trail_stop() → activation watermark check uses _resolve_geometry_atr() which returns bracket_atr (~57 for H4). At peak MFE, price move ≈30.85 cents → unrealized_r = 30.85/57 = 0.54 < trail_activation_atr=1.0 → return None (trail not activated). The ratchet floor (_ratchet_lock_r) correctly uses entry_atr and would have locked +2.0R, but it's gated behind the activation watermark.
- **Layer 3 (根因)**: Cross-TF ATR mismatch — FIX-20260709-004 (per-TF bracket_atr) correctly moved geometry distances to bracket_atr but also changed activation measurement. The activation threshold trail_activation_atr=1.0 was calibrated for entry_atr scale; using bracket_atr makes it 10× harder to reach for H1/H4.
- **Fix**: FIX-20260722-003
- **Status**: **CLOSED**

## CCT-20260730-011

- **Docket**: DQAF-20260730-011
- **Layer 1 (症状)**: BTC July 2026 journal PnL系统性偏离MT5真相。MT5经纪商报表: 426笔, PnL=+$26.86, WR=49.1%, PF=1.07。系统journal: PnL=-$140.89（偏差+$167.75）。53个票号级别PnL不匹配。`scripts/_diagnose_pnl_mismatch.py` stdout。
- **Layer 2 (传导)**: (C1) Bridge `order_send()`后立即`mt5.history_deals_get(position=ticket)`查询deal → MT5 deal清算为异步，`deal.profit`未填充 → Bridge回退到`msg_payload["pnl"]`(引擎中价估算`(mid-entry)*volume`)。`mt5_bridge_worker.py:820,1132-1136`。(C2) Bridge写入journal时无provenance标签(FIX-20260716-005前)，796/1226条(65%)`_pnl_status`缺失 → 无法区分verified vs estimated。`scripts/_diagnose_pnl_provenance.py` stdout。(C3) Engine `managed_close.py:75`注释"reconciliation corrects it later" — 但`known_open_tickets`在reconciliation运行前被清除(management_phase MIA/stale-clear路径直接`pop`)→ Reconciliation仅写9条(0.7%)vs Bridge 1217条(99.3%)。(C4) Journal dedup允许`_source=mt5_reconciliation` supersede但Reconciliation饥荒→ Bridge估算值永不被修正。
- **Layer 3 (根因)**: L3架构缺陷 — Journal PnL字段无Single Source of Truth。双写者(Bridge + Reconciliation)竞争写入同一字段，Bridge在deal.profit异步清算窗口中静默回退到中价估算并写入无provenance条目，Reconciliation修正路径因`known_open_tickets`提前清除而饥饿 → 99.3%的journal PnL值来自中价估算而非MT5权威数据。
- **Fix**: FIX-20260730-011 — Settlement Queue Isolation (委员会覆写): Bridge写`pnl=null`+`_pnl_status="pending_mt5_settlement"`; SettlementQueue三态隔离(`known_open_tickets→pending_settlement_tickets→settled`); Reconciliation消费`pending_settlement_tickets`通过`resolve_exit_deal()`轮询验证deal.profit; 四级超时上报(T1 5min→T2 1hr→T3 24hr→T4 terminal); 队列持久化+僵尸单告警; Journal dedup扩展权威来源白名单+null PnL自动被非null supersede。
- **Status**: **CLOSED**

### CCT-20260801-006
- **Docket ID**: DQAF-20260801-006
- **日期**: 2026-08-01
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 双塔每周期弹出 brain_alert `feature_dimension_mismatch expected: 37, got: 40` (live_launcher_20260801T032917Z.log, BTC_Expected_R_V4_SHORT + _LONG); btc_expected_r_m15 在 golden_master 9 条记录全 neutral, voter_count=0, confidence=0.0
  - [Layer 2 — 中间异常]: feature_assembler.py:92-97 路由条件含 swing_enhanced/daily_swing/btc_macro/btc_h1 但不含 btc_expected_r → 落入 :108-115 fallback 返回原始 40-dim V9 向量 → lightgbm_brain_adapter.py:153 维度守卫 (num_feature=37) 打回零向量 → 双塔零投票
  - [Layer 3 — 根因]: L2 逻辑缺陷 — btc_expected_r_37 仅注册于 SCHEMA_DIMENSIONS (FIX-20260731-004) + FeatureService._IMPLEMENTED_SCHEMAS (FIX-20260801-001), 漏同步 6 处分发点: feature_router SCHEMA_CONTRACTS 缺注册 + build_lake Source 7 对子集 schema 按位置 zip 41-dim (29/37 偏移) + live_cycle:4841/management_phase:478 路由条件 + management_phase:494/swing_strategy:103 btc_augment gating + swing_strategy _needs_daily
- **证据引用**:
  - Source 1: [日志] data_btc/logs/live_launcher_20260801T032917Z.log — brain_alert feature_dimension_mismatch expected=37 got=40 双塔每周期
  - Source 2: [代码路径] core/features/feature_assembler.py:92-97 路由条件 → :108-115 V9 40-dim fallback → core/brains/adapters/lightgbm_brain_adapter.py:153 维度守卫
  - Source 3: [复现脚本] scripts/_verify_expected_r_routing.py — build_lake 子集 zip 29/37 misaligned (XAUUSDc_return 取到 slot 8 而非 slot 12); 修复后 0/41
- **是否被推翻**: 否
- **关联 ReB Pattern**: SCHEMA_ROUTING_MISSING_NEW_SCHEMA

### CCT-20260801-008
- **Docket ID**: DQAF-20260801-008
- **日期**: 2026-08-01
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 重启后 live_launcher_20260801T060541Z.log 8 类告警: Freshness Contract 3 VIOLATION / exit_config_validation_warning ev_trajectory_enabled ×5 策略线 / SSOT drift M15+V4_LGB / startup_integrity missing artifact_hash(3) + artifact_hash mismatch(H1_V2,V12) / conformal_ou_gate disabled / 7 python 进程
  - [Layer 2 — 中间异常]: (a) strategy_config_validator.py:14 `_EXPECTED_EXIT_KEYS` 缺 ev_trajectory_enabled 但 management_phase.py:1853 实际读取 → 5 条 BTC 策略线 ev_trajectory_enabled:false 全误报; live_intent_loop.py:442 从 live_cycle.py:797 旧重复副本导入 (Strangler Fig #22 提取后 caller 未迁移); (b) catalog.py:508 validate_freshness_contract 将 telemetry 产物 (EXECUTION_STATE 30min/MT5_BRIDGE_HEALTH 15min/ALERT_COOLING 2h) TTL 与 daily_ops batch max_age 6h 比较 → 3 误报; (c) brain config artifact_hash 为空/过时/截断; (d) M15 config=retired 但 governance 残留 probation
  - [Layer 3 — 根因]: L2 分类学错误 + L1 副本残留 — (a) validator 白名单滞后运行时消费 key 且 strangler fig 提取后 caller 未迁移 (同逻辑双文件, Iterability 违规); (b) 实时遥测产物 TTL 强行套用批处理产物 freshness contract; (c) artifact_hash 无完整 64 位校验准入 (幽灵更新风险); (d) 治理状态未随 config 退役同步
- **证据引用**:
  - Source 1: [日志] data_btc/logs/live_launcher_20260801T060541Z.log L21/25/27/29/31/47-49/75 — 8 类告警证据包
  - Source 2: [代码路径] core/runtime/strategy_config_validator.py:14 (白名单) vs core/runtime/management_phase.py:1853 (运行时读取); core/state/catalog.py:508-552 (contract) + L411/433/452 (telemetry TTL)
  - Source 3: [配置] configs/brains_btc/*.json artifact_hash 空/过时/16字符截断 vs 磁盘模型 sha256 (H1_V2 9f7e9d6c, V12 a4f9eb8cde8a9915... 前缀截断)
- **是否被推翻**: 否
- **关联 ReB Pattern**: WHITELIST_LAGGING_RUNTIME_KEYS, STRANGLER_FIG_CALLER_NOT_MIGRATED, TELEMETRY_TTL_VS_BATCH_CONTRACT

### CCT-20260801-010
- **Docket ID**: DQAF-20260801-010
- **日期**: 2026-08-01
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC_Swing_V4 自 07-09 起 live↔probation 振荡; governance transition_log 交替出现 `throttle live→probation` 与 `pnl:stable`/config-floor 拉回; 振荡期 V4 间歇性 vote_weight 惩罚 (0.5)。Brain_performance window-100 干跑实测: V4 41W/59L PF=0.695 (见 data_btc/brain_performance.json)。
  - [Layer 2 — 中间异常]: 三个独立治理政策对同一大脑给出矛盾结论 — (1) BrainPromotionEvaluator throttle (PF=0.695 < throttle_pf=0.80, **政策正确**); (2) daily_ops governance_scheduler.py pnl:stable (all-time PnL 健康) 拉回 live; (3) Iron Law #14 config floor (V4 config status=live) 启动 reconcile 拉回 live。双轨数据源是帮凶非真凶: live_intent_loop apply_promotion_decisions (BrainPnLStore last-20, FIX-20260611-001) + daily_ops 直写 (governance_scheduler.py:664 绕过 rule engine)。
  - [Layer 3 — 根因]: L3 架构缺陷 — **政策冲突无豁免机制**: 风控 throttle 与战略观察 (IC 8/3 终审) 无仲裁层。修复: SSOT 统一 (单一写入器) + Observation Hold (观察期豁免, 机器降级在人类战略观察窗口内显式让位)。
- **证据引用**:
  - Source 1: [状态] data_btc/governance_state.json BTC_Swing_V4 transition_log (throttle 与 pnl:stable 交替)
  - Source 2: [性能] data_btc/brain_performance.json BTC_Swing_V4 window-100 (41W/59L PF=0.695) — SSOT 干跑输出 `profit_factor(0.69) < 0.80`
  - Source 3: [代码] core/brains/services/brain_promotion.py:283-284 (throttle_pf=0.80) + scripts/training/governance_scheduler.py:664 (第三轨直写) + configs/brains_btc/BTC_Swing_V4.json (config floor=live)
- **是否被推翻**: 否
- **关联 ReB Pattern**: POLICY_CONFLICT_THROTTLE_VS_CONFIG_FLOOR, DUAL_TRACK_WRITER_OSCILLATION

### CCT-20260801-011
- **Docket ID**: DQAF-20260801-011
- **日期**: 2026-08-01
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU `data/feature_store/.../timeframe=M5/features.jsonl` 冻结在 `2026-08-01T00:41:25Z` (当日仅 2 条 vs 正常周五 150+); intent_20260801T081238Z.log `bar_sync_synthetic=810/811`; `circuit_breaker_active=403`; phase_transition 仅到 `2_reconcile_positions` (811×1, 810×2), **从未到达 `4_feature_computation`**; `bridge_silence_degraded=111` + `circuit_breaker_bridge_silence_trip=37` + `circuit_breaker_reset=36` (反复跳闸/复位)。
  - [Layer 2 — 中间异常]: (a) 00:42:40 重启后 XAU MT5 tick/价格 feed 停摆 → 动态 tick 探针 (session_detector.probe, TECH_DEBT-005) 将"feed 死亡"误判为"市场关闭" → `risk_tier=off` → BAR_SESSION_OFF 全天 1405 次 (07-31 正常日仅 111, 从收盘 22:07 才出现); (b) bar sync 走合成 bar → `_last_bridge_ack_time` 不更新 (management_phase.py:1306/1312 仅在 fetch_prices 成功 / mid>0 时更新) → bridge_silence > max_bridge_silence_seconds → `_consecutive_degraded_cycles≥3` → circuit_breaker_bridge_silence_trip → management_only_mode; (c) 熔断后循环在 Phase 2 短路, 特征计算块 live_cycle.py:3330 永不执行, 且该块 `except...pass` 静默吞错 → 特征库冻结。
  - [Layer 3 — 根因]: L2 探针缺陷 + L3 架构缺陷 — (1) liveness 探针 `_last_bridge_ack_time` 是 price-ack 代理, 无法区分"市场关闭"(良性) 与 "feed 死亡"(真故障); (2) ack 更新路径在下游, 熔断跳闸后 management phase 被跳过 → ack 永远不更新 → 熔断无法复位 (循环依赖, 靠 reset 期偶发 fetch 才恢复); (3) 动态 tick 探针把 feed 停摆静默降级为 session off, 无告警分级; (4) FeatureService 兜底静默返回 last-known/zeros, 无硬失败 → 盲推理缺口。防御修复: FIX-20260801-013 StaleFeatureException 推理守卫 (3 bars 硬拒绝) + live_cycle 转 management_only。
- **证据引用**:
  - Source 1: [日志] data/logs/intent_20260801T081238Z.log (bar_sync_synthetic 810/811, circuit_breaker_active 403, phase 1/2 only) + data/logs/live_launcher_20260801T004240Z.log (00:42:40 重启, "9 other Python processes")
  - Source 2: [数据] data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl (最后 event_time 00:41:25, 48295 行) + data/reports/bar_sync_events.jsonl (BAR_SESSION_OFF 08-01 1405 vs 07-31 111)
  - Source 3: [代码] core/features/feature_service.py (兜底路径) + core/runtime/live_cycle.py:3153/3330 + core/runtime/management_phase.py:1300-1313 (ack 更新) + core/execution/session_detector.py (动态探针)
- **是否被推翻**: 否
- **关联 ReB Pattern**: FEED_STALL_MISCLASSIFIED_AS_MARKET_CLOSED, LIVENESS_ACK_CIRCULAR_DEPENDENCY

### CCT-20260802-002
- **Docket ID**: DQAF-20260802-002
- **日期**: 2026-08-02
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: btc_swing trio 每周期 p_win 锚定 0.4141 (V4_LGB 治理 WR) + 负 EV 拦截; 94 笔 ensemble 结算 WR 43.6%, PnL -$16.46。
  - [Layer 2 — 中间异常]: 治理冷启动池与 PnL 池按 live_brain_ids 过滤但不过滤 vote_weight; V4_LGB (probation, vote=0.0) 的 WR 0.4141 入中位池, 拖低 ensemble EV 至最弱脑水平。
  - [Layer 3 — 根因]: L3 边界错误 — Voting Boundary != EV Boundary。vote_weight<=0 的 mute 脑 (observation-only) 不能投票却仍贡献 EV 估计。修复: `_has_voting_rights()` fail-open 过滤双池。
- **证据引用**:
  - Source 1: [代码] core/execution/pwin_chain.py resolve_p_win_from_brains (治理池/PnL 池过滤链)
  - Source 2: [状态] data_btc/governance_state.json BTC_Swing_V4_LGB (vote_weight=0.0, WR=0.4141)
  - Source 3: [日志] live_launcher_20260802T021000Z.log (rr=0.9786 + 负 EV 拦截) + scripts/_audit_pwin_routing_20260802.py
- **是否被推翻**: 否
- **关联 ReB Pattern**: ZERO_VOTE_WR_POOL_PENETRATION

### CCT-20260802-003
- **Docket ID**: DQAF-20260802-003
- **日期**: 2026-08-02
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: btc_swing trio 每周期 rr_ratio=0.9786 (h1_v2/h4/m30 均>1.0), 交易被负 EV veto / min_economic_volume 拦截。
  - [Layer 2 — 中间异常]: 对称 SL=TP (1.5/1.5) + spread 不对称 (TP 收窄 SL 放宽) → post-spread RR=(d−2)/(d+2)<1.0 恒成立 → 盈亏平衡 WR=50.5% > 所有脑 WR (36-49%)。
  - [Layer 3 — 根因]: L3 设计致错 — 训练合约对称几何对亚 50% 精度模型 (CV 49.8%) 数学无解; builder 默认 2.0/2.5 掩盖真实 1.5/1.5 (config-taxonomy SSOT 分叉)。IC 裁决 D+C: 维持拦截 + 战略退役 + 聚焦 H1_V2/Expected R。
- **证据引用**:
  - Source 1: [配置] configs/live_btc.yaml:413-423 (sl/tp.base_atr_mult=1.5/1.5) + configs/brains_btc/BTC_Swing_V4.json label_contract (1.5/1.5 train-serve 对齐)
  - Source 2: [代码] core/execution/dynamic_sl_tp.py:254-267 (spread 不对称) + core/runtime/strategy_builder.py:730-731 (默认 2.0/2.5 分叉)
  - Source 3: [日志] live_launcher_20260802T021000Z.log (rr=0.9786 精确, 数学复现 (182.9)/(186.9))
- **是否被推翻**: 否
- **关联 ReB Pattern**: SYMMETRIC_SL_TP_PLUS_SPREAD_INEQUALITY, EXPLICIT_BETTER_THAN_IMPLICIT_CONFIG

### CCT-20260802-004
- **Docket ID**: DQAF-20260802-004
- **日期**: 2026-08-02
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: circuit_breaker_bridge_silence_trip 振荡 — BTC 每 ~35min (12:19:59/12:54:59/13:29:59/14:09:59, silence 1347-1500s) / XAU 每 ~22.5min (12:09:59→14:02:15 共 6 次, ~735s); 周五 07-31 交易时段 20 次; management_only 压制 ~10min (实盘 ~30% 时间)。
  - [Layer 2 — 中间异常]: 正常路径 ACK 冻结 — _bridge_silence = now − _last_bridge_ack_time 每周期 +300s (M5 周期) 单调增长越 600s 阈值 (max_bridge_silence_seconds) → _consecutive_degraded_cycles>=3 → 熔断 (live_cycle.py:1216-1236); 熔断分支才盖章 (L1310) → 600s 冷却复位 (circuit_breaker_reset.py:65-67) → 正常路径 ACK 再次冻结 → 无限振荡。桥全程存活 (mt5_bridge_health 心跳新鲜 + 实时价持续)。
  - [Layer 3 — 根因]: L2 接线缺陷 — _last_bridge_ack_time 被文档化为 "last successful broker.fetch_prices()" (L382), 但 4 处 fetch_prices 调用点 (live_cycle.py:2020 正常路径 + live_order_sender.py:215 + execution_queue.py:248 + management_phase.py:1305) 中仅 2 处盖章且全部门控于降级分支 (live_cycle.py:1310 熔断分支 / management_phase.py:1306/1312 持仓门控) — 健康空转路径 (0 持仓, 交易系统最常处状态) 永不刷新 → 探针实际测量"上次进降级分支的时间"而非"上次成功取价"。FIX-20260608-006 仅补熔断分支属不完整修复。修复 FIX-20260802-004: 正常路径 fetch_prices 成功 + L3 _mid_and_prices 回退成功 (mid>0) 即盖章, 镜像 management_phase 模式。
- **证据引用**:
  - Source 1: [代码] core/runtime/live_cycle.py:382/1207-1208/1216-1236/1310/2020/2027-2028/2708 (契约 + 判定 + 门控 + 未盖章点)
  - Source 2: [代码] core/runtime/management_phase.py:1305-1306/1311-1312 (持仓门控盖章基准) + core/runtime/circuit_breaker_reset.py:65-67 (复位门控)
  - Source 3: [日志] data_btc/logs/intent_20260802T115729Z.log (BTC/XAU 熔断原始事件行 + silence 值) + data_btc/state/execution_state.json (BTC tripped 14:09:59Z)
- **是否被推翻**: 否 (AR H1-H4 已全部处理: H1 桥死=推翻, H2 持仓门控=部分成立, H3 011 已修=推翻, H4 假熔断可接受=推翻)
- **关联 ReB Pattern**: LIVENESS_PROXY_STAMPED_ONLY_IN_DEGRADED_PATHS (互补 ReB-20260801-LIVENESS_ACK_CIRCULAR_DEPENDENCY)

### CCT-20260803-001
- **Docket ID**: DQAF-20260803-001
- **日期**: 2026-08-03
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 双品种防护性零开单 (08-03 00:00Z) — BTC h1_v2 volume 0.0033 / h4 0.0033, XAU m30_swing 0.0066 / h1_swing 0.0066, 全部 `volume_degraded_below_economic_minimum: X < 0.02`; BTC kelly 步进 0.01 × 健康分 1.0 = 0.01 < 0.02 → **即使健康满分也永久封杀** (结构性植物人状态)。
  - [Layer 2 — 中间异常]: GodsEye 健康分低下 (BTC 0.327 SHADOW / XAU 0.438 CAUTIOUS) → strategy_evaluator.py `volume ×= max(0.25, health)` → 成交量坍缩 → 终态闸门 `_MIN_ECONOMIC_VOLUME = 0.02` (strategy_evaluator.py:1144) 击杀。
  - [Layer 3 — 根因]: L3 架构异味 — 基于 XAU lot_step 定尺的 0.02 (注释 "For XAU: lot_step=0.01, 2× lot_step") 硬编码为**全局唯一** volume 下限, 对 BTC (base_volume 0.01) 形成结构性封杀; 单品种过度拟合残留 (Iron Law #14 同族: per-symbol 操作参数未下沉)。修复 FIX-20260803-001: `StrategyLineConfig.min_economic_volume` 字段 + `resolved_min_economic_volume` property (显式配置优先, BTC→base_volume floor 0.01, 其他→2×lot_step 0.02) + builder 20 策略线透传 + `_validate_min_economic_floors` 静态校验 + evaluator 终态闸门 per-strategy floor。**否决全局降级**: IC 拒绝 0.02→0.01 全局 hack (拆 XAU 盈亏平衡地板)。
- **证据引用**:
  - Source 1: [代码] core/execution/strategy_line.py StrategyLineConfig.resolved_min_economic_volume (per-symbol 派生 SSOT) + core/runtime/strategy_evaluator.py:1145-1148 (per-strategy floor) + core/runtime/strategy_builder.py:1295 _validate_min_economic_floors
  - Source 2: [配置] configs/live_btc.yaml 6 策略线 `min_economic_volume: 0.01` (btc_swing/m15/m30/h1_v2/h4 + btc_swing_h1) — BTC 自有合法下限
  - Source 3: [日志] data_btc/logs/intent_20260802T180823Z.log (0.0025 < 0.02) + data/logs/live_launcher_20260802T180824Z.log (0.0120/0.0150 < 0.02, defensive_confidence_floor conf=0.323)
  - Source 4: [测试] tests/execution/test_min_economic_per_symbol.py (5 用例: BTC 0.01 / XAU 0.02 / 显式覆盖 / base_volume scale / 静态校验 warning-not-raise)
- **是否被推翻**: 否 (AR 全部处理: 系统崩溃=推翻, OU 冷启动=推翻, 桥接断流=推翻)
- **关联 ReB Pattern**: XAU_CENTRIC_HARDCODED_GLOBAL_THRESHOLD, EXPLICIT_BETTER_THAN_IMPLICIT_CONFIG

---

### CCT-20260804-001
- **Docket ID**: DQAF-20260804-001
- **日期**: 2026-08-04
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: gov-eval 日志 1243 次 `transitions=['BTC_Swing_V4: live → probation (throttle)']` (live_launcher.py:263) 但 governance_state.json V4 仍 live/vote_weight=1.0 — IC D+C 退役裁决 (observation_hold 2026-08-03T23:59:59Z 过期) 从未执行。
  - [Layer 2 — 中间异常]: 60s gov-eval 循环在内存执行 V4→probation, 但每次重载磁盘 V4=live — throttle 降级决策被系统性丢弃 (data_btc/governance_state.json 永不收敛)。
  - [Layer 3 — 根因]: L3 架构缺陷 (RC-12) — `evaluate_governance_state()` 持久化顺序: save() 在 execute_transitions() 之前 (governance_evaluator.py:208-211), 之后无 save → 仅 perf 注入落盘, transitions 永不持久化。修复 FIX-20260804-001 (commit 73a17820): 移除前置 save, execute_transitions 之后单次落盘 (manual_mode 同覆盖), 双部署路径 (container scheduler_service.py:246 + launcher:261) 同享单点。
- **证据引用**:
  - Source 1: [代码] core/deployment/governance_evaluator.py (save-after-transition) + core/governance/governance_rule_engine.py:218-236 (_hold_blocked + transition)
  - Source 2: [状态] data_btc/governance_state.json (V4 live, mtime 03:49:11.742 == perf updated_at — 前置 save 落盘实况)
  - Source 3: [日志] data_btc/logs/live_launcher_20260803T070355Z.log (1243 gov-eval transitions 行)
  - Source 4: [验证] scripts/_verify_governance_evaluator.py Assert 6 (确定性持久化断言 VERIFY OK) + 175 governance pytest PASS
- **是否被推翻**: 否 (AR 处理: daily_ops 写者=推翻, last-live guard=推翻, 旧缓存=推翻)
- **关联 ReB Pattern**: PERSISTENCE_BEFORE_EXECUTOR

---

### CCT-20260804-002
- **Docket ID**: DQAF-20260804-002
- **日期**: 2026-08-04
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC 跨资产特征 slots 持续零填充 — XAUUSDc_return [12] / AUDJPYc_return [30] / BTC/XAU ratio [39-40], 日志 1600+ 次 `'numpy.void' object has no attribute 'get'` + `'FeatureService' object has no attribute 'get_latest'` → 污染持久化 v2 特征仓。
  - [Layer 2 — 中间异常]: `_compute_btc_xau_ratio`/`_compute_audjpyc_return` 对 numpy 结构化数组行 (numpy.void) 调 `.get()` → AttributeError; `_compute_xauusdc_return` 调不存在的 `get_latest()` + FeatureRecord dataclass 非 dict → 全部被 except 吞掉 → 返回 0.0。
  - [Layer 3 — 根因]: L3 边界错误 (RC-03) — 结构化数组行当 dict 用 `.get()` + 跨符号读取 API 契约缺失 + 构造点 (live_cycle.py:2558) 传 FeatureService + MagicMock 测试盲区。修复 FIX-20260804-002 (commit ca2c4db9): 单收敛点 `_coerce_feature_store()` + `_latest_cross_record()` (latest()+to_dict()) + `_bar_close()` (dict/numpy.void 统一, 负索引归一)。
- **证据引用**:
  - Source 1: [代码] core/features/computers/btc_feature_augmenter.py (_latest_cross_record/_bar_close helpers) + core/runtime/live_cycle.py:2558 (FeatureService 构造点同享 unwrap)
  - Source 2: [数据] data_btc/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl (53 条 XAU 记录存在 — 数据可用代码读不出)
  - Source 3: [日志] data_btc/logs/live_launcher_20260803T070355Z.log (numpy.void / get_latest AttributeError 行)
  - Source 4: [测试] tests/features/computers/test_btc_feature_augmenter.py::TestCrossAssetRootCauseFixes (3 根因回归锁) + 46 tests PASS
- **是否被推翻**: 否 (AR 处理: 设计如此=推翻 (53 条记录存在), MagicMock 证明设计=推翻 (测试盲区))
- **关联 ReB Pattern**: STRUCTURED_ARRAY_ROW_AS_DICT, API_CONTRACT_MAGICMOCK_BLINDSPOT

### CCT-20260804-003
- **Docket ID**: DQAF-20260804-003
- **日期**: 2026-08-04
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAUUSDc_return slot [12] 恒零 (193/193 条 _v2 特征仓记录), 而同批 AUDJPYc_return [30] + BTC/XAU ratio [39-40] 已恢复真实数据 — 三跨资产特征行为不一致。
  - [Layer 2 — 中间异常]: `_compute_xauusdc_return` 读特征仓跨符号记录, `_MAX_STALENESS_SECONDS=300` staleness guard 正确触发 — XAUUSDc 跨仓最新记录 (08-04T03:35Z) 距计算时刻 >2h, 每次调用都被 guard 归零。
  - [Layer 3 — 根因]: L3 架构不一致 (RC-03) — XAUUSDc_return 依赖稀疏跨仓 feed (4-6h/条人工 feature-update, 53 条), 而 AUDJPY/Ratio 走 MT5 直读 (连续/实时)。特征读路径本身修复正确, 但**数据源选择**使 slot [12] 结构性死亡。修复 FIX-20260804-003: `_compute_xauusdc_return` 统一 MT5 直读 (`copy_rates_from_pos("XAUUSDc",5,0,2)` + `_bar_close`), 删除特征仓读路径全部死代码 (`_latest_cross_record`/`_coerce_feature_store`/`_MAX_STALENESS_SECONDS`/`_xau_stale_count`/`feature_store` 构造参数/`_store` 状态), live_cycle 3 构造点清参。
- **证据引用**:
  - Source 1: [代码] core/features/computers/btc_feature_augmenter.py (`_compute_xauusdc_return` 特征仓读 + staleness guard, 与 `_compute_audjpyc_return`/`_compute_btc_xau_ratio` MT5 直读路径对比)
  - Source 2: [数据] data_btc/feature_store/records/symbol=BTCUSDc/timeframe=M5/features.jsonl (193 条 _v2 记录: XAU=0 / AUDJPY+Ratio 真实)
  - Source 3: [数据] data_btc/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl (53 条, 4-6h 节奏, 最后 08-04T03:35Z)
  - Source 4: [配置] configs/live_btc.yaml + 实盘日志 (无连续 XAU 跨仓 feed 任务; staleness guard 设计行为)
- **是否被推翻**: 否 (AR: "XAU 数据不可用"假设被 53 条真实 M5_Ret_1 记录推翻 — 数据可用, 是读路径数据源选择错误)
- **关联 ReB Pattern**: SPORADIC_FEED_VS_MT5_SSOT

### CCT-20260804-005
- **Docket ID**: DQAF-20260804-005
- **日期**: 2026-08-04
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: BTC_Flow46_V1_SHORT 脑 (46-dim OFI 迁移, OOS ρ=0.0721) 实盘永不投票 — shadow 注册 (FIX-20260803-007) 后无任何信号输出, 运行时求值器未接线。
  - [Layer 2 — 中间异常]: 双断链 — (a) BrainFactory 按 brain_type→BRAIN_TYPE_MAP→LightGBMBrainAdapter→load() 只加载 `artifact_path` 的 5-feature 残差 → `_num_features=5`; `SwingStrategy._run_inference` 组装 46-dim fv → `infer()` 维度守卫 46≠5 → fallback raw_score=0.0 (静默中性); (b) config 缺 `training_params.objective` → `_check_training_objective` 动态推断因 brain_type=expected_r_short 不 startswith "lightgbm"/"xgboost" 返回 None → ghost-brain ERROR → BrainConfigError 加载即拒。
  - [Layer 3 — 根因]: L3 架构不完整 (RC-06, CONTRACT_WITHOUT_RUNTIME_EVALUATOR) — freeze-and-residual 训练管线产出完整 brain config (transfer.kind=freeze_and_residual) 但运行时 adapter 层无 base+residual 组合求值器; 训练侧 `ResidualTransferLearner.predict()` 的 y=y_A+r 组合逻辑从未接入 brain adapter 链。修复 FIX-20260804-005: `TransferResidualBrainAdapter` (双 booster load + infer 切割重组, 与训练侧 bit-identical) + BrainFactory 元数据分派 (方案 A, brain_type 保留信号语义) + brain_config.py 发射 training_params/transfer + Flow46 config +3 字段。
- **证据引用**:
  - Source 1: [代码] core/brains/adapters/lightgbm_brain_adapter.py:152-165 (infer 维度守卫) + core/brains/adapters/__init__.py:43 (expected_r_short→lightgbm_txt)
  - Source 2: [配置] configs/brains_btc/BTC_Flow46_V1_SHORT_20260803_120909.json (缺 training_params.objective; artifact_path=5-feature 残差)
  - Source 3: [代码] core/training/transfer_adapter.py:214-224 (ResidualTransferLearner.predict 组合逻辑 — 零运行时调用者) + core/brains/services/brain_factory.py (分派前无 transfer 元数据分支)
  - Source 4: [代码] core/deployment/brain_config_validator.py:230-262 (_check_training_objective ghost-brain 路径)
- **是否被推翻**: 否
- **关联 ReB Pattern**: CONTRACT_WITHOUT_RUNTIME_EVALUATOR

---

### CCT-20260804-006
- **Docket ID**: DQAF-20260804-006
- **日期**: 2026-08-04
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 今日 XAU 3 单全 SHORT 全在局部底部/回撤进场, 2 SL + 1 深套 (上涨日 4047→4082 逆势做空)。证据: `data/live_trade_journal.jsonl` ticket 4419403411 (m15 SHORT 4048.7 SL@4063.8) / 4419404324 (m30 SHORT 4049.1) / 4421972593 (h1 SHORT 4063.8 持仓深套)。
  - [Layer 2 — 中间异常]: 全 swing 脑方向锁死 SHORT — H1_Exec_A 155/155 SHORT, M30_V5 154/155 SHORT (99.4%), 信号 886 SHORT:3 LONG; M15_V7_binary 恒定 conf 0.783 (退化签名)。证据: `data/brain_votes/2026-08-04.jsonl` + scripts/audits/_audit_xau_votes_today.py。
  - [Layer 3 — 根因]: L3 毒井 (RC-09) — `live_intent_loop.py:759` H1 硬编码 `d1_csv="data/raw/xauusdc_d1_merged.csv"` → BTC 进程 LiveDailyProvider `_sync_csv()` (core/features/computers/live_daily_provider.py:112-185) 按 `self._symbol=BTCUSDc` 拉 BTC D1 bar 追加进 XAU 文件 → `data/raw/xauusdc_d1_merged.csv` 行 2510-2532 (2026-07-04→08-04) 全 BTC 价 63,000-64,700 → 全 swing 脑 D1_* 特征投毒 → 模型 out-of-distribution → 输出退化 SHORT 锁死 (MODEL_OUTPUT_DEGENERACY_SHORT_COLLAPSE, 同签名 FIX-20260629-184)。
- **证据引用**:
  - Source 1: `data/raw/xauusdc_d1_merged.csv` — 行 2510/2512/2514/2516/2528/2530/2532 = 2026-07-04→08-04 BTC 价 (63,093.3→63,466.89)
  - Source 2: `data/brain_votes/2026-08-04.jsonl` — H1_Exec_A 155 SHORT / M30_V5 154 SHORT (独立于 journal 的投票流)
  - Source 3 (根因): `core/features/computers/live_daily_provider.py:129-158` `_sync_csv()` 按 `self._symbol` 抓取 + 追加 `self._d1_csv` (跨资产路径无符号守卫) — 跨品种验证源
- **是否被推翻**: 否
- **关联 ReB Pattern**: D1_WELL_CROSS_ASSET_POISONING / MODEL_OUTPUT_DEGENERACY_SHORT_COLLAPSE / MONITOR_TRIPLE_BLIND_SPOT

### CCT-20260804-007
- **Docket ID**: DQAF-20260804-007
- **日期**: 2026-08-04
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `data/raw/xauusdc_d1_merged.csv` 尾部 12 行 (2026-07-04→08-04) 全 BTC 价 63,093-64,794; 与 XAU 干净行 (行 2508-2509 = 07-26/27, close 4093-4094) 交错 → date-keyed dedup 使 XAU 正确 bar 无法回填。
  - [Layer 2 — 中间异常]: BTC 进程 `LiveDailyProvider(symbol=BTCUSDc, d1_csv="data/raw/xauusdc_d1_merged.csv")` — 同构实例, 硬编码 XAU 路径; `_sync_csv()` 按 `self._symbol` 拉 BTC D1 bar 无条件追加进 XAU 文件 (写前无符号守卫)。
  - [Layer 3 — 根因]: L3 (RC-03) — `core/runtime/live_bootstrap.py:129-135` init_feature_services 硬编码 `d1_csv/h4_csv="data/raw/xauusdc_*"`, 跨资产共享单一路径, 无 symbol↔filename 契约守卫 → BTC 每新 D1 bar 跨写 XAU 文件。
- **证据引用**:
  - Source 1: `data/raw/xauusdc_d1_merged.csv` 行 2510-2521 (BTC 价) + 行 2508-2509 (XAU 价) 交错
  - Source 2: `core/runtime/live_bootstrap.py:133-134` 硬编码 XAU 路径 (BTC 进程同路径)
  - Source 3: `core/features/computers/live_daily_provider.py:112-185` `_sync_csv()` 无符号守卫跨写
- **是否被推翻**: 否
- **关联 ReB Pattern**: D1_WELL_CROSS_ASSET_POISONING

### CCT-20260804-008
- **Docket ID**: DQAF-20260804-008
- **日期**: 2026-08-04
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: XAU 99.7% SHORT 退化 (886 SHORT:3 LONG) 整月无任何风控告警; 今日 3 单烂进场事后审计才发现。
  - [Layer 2 — 中间异常]: `direction_concentration_monitor` 对 XAU 恒输出 INSUFFICIENT DATA (exit 0 静默) — 修复后实测 24h 577 信号 (486 SHORT: 91 LONG 方向分布, XAU) 证明数据存在但监控器读不到。
  - [Layer 3 — 根因]: L2 (MONITOR_TRIPLE_BLIND_SPOT) — 三重失聪: (1) `_scheduled_monitor` 硬编码 `data_dir="data_btc"` → XAU 永不检查; (2) golden_master 方向字段路径错 (读顶层 `direction` 恒 None vs 实际 `outputs.<strategy>.direction`) → 恒 0 信号; (3) 大小写错配 (gm 存小写 short/long vs 匹配大写 SHORT)。叠加时间戳字段错配 (读 `timestamp`/`recorded_at` vs 实际 `timestamp_utc`) → 时间过滤 0 行。
- **证据引用**:
  - Source 1: `scripts/_monitor_direction_concentration.py:214` `data_dir="data_btc"` 硬编码
  - Source 2: 修复前实测 `data/golden_master.jsonl` 时间过滤 0 行 (timestamp_utc 280 行), 方向提取 0 命中
  - Source 3: 修复后 `python scripts/_monitor_direction_concentration.py --data-dirs data data_btc` → XAU CRITICAL (86% SHORT / 100% trades)
- **是否被推翻**: 否
- **关联 ReB Pattern**: MONITOR_TRIPLE_BLIND_SPOT / D1_WELL_CROSS_ASSET_POISONING

### CCT-20260817-001
- **Docket ID**: DQAF-20260817-001
- **日期**: 2026-08-17
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 8/17 XAU 订单 TP 持仓中大幅缩窄 (4500875936: 84.3→25.7 点 / 4501482790: 34.4→13.5 点), SL 全程不动, 运行中 RR 崩到 0.527/0.385 (min_rr_ratio=0.85 均跌破), 盈利曾达 R=+2.108 仍被收窄目标侵蚀.
  - [Layer 2 — 中间异常]: compute_trail_tp (core/execution/position_manager.py:1707, FIX-20260713-008) atr_ratio=current_atr/entry_atr ≤0.80 触发 → TP candidate = anchor ∓ trail_mult×current_atr×1.75×_tf_scale 向内收窄, "TP only moves INWARD — never widens"; SL 由独立 Chandelier 引擎管理 (trail_activation_atr 未达 → SL 不动), 两引擎零耦合.
  - [Layer 3 — 根因]: L3 架构缺陷 — TP 缩窄下限与 SL 距离/RR 无耦合: TP Floor (tp_min_distance_atr×bracket_atr, max()/min() 语义) 是 upper bound 防激进非 lower bound 保 RR; Proximity Gate 仅防末程移动; 只缩不放 (ATR 恢复不复原) → 窄目标持久化负期望.
- **证据引用**:
  - Source 1: `core/execution/position_manager.py:1707` compute_trail_tp (触发条件 atr_ratio≤0.80, TP Floor max()/min() 语义)
  - Source 2: journal 事件 4500875936 02:35 modify_sltp label="trail" comment="tp" (TP 84.3→25.7)
  - Source 3: `scripts/_audit_xau_tp_shrink_20260817.py` stdout (RR 轨迹 1.73→0.527 / 0.98→0.385, 触发 ATR ratio 0.791/0.790)
- **是否被推翻**: 否
- **关联 ReB Pattern**: TP_TRAIL_RR_COLLAPSE_DECOUPLED_FROM_SL

### CCT-20260819-003
- **Docket ID**: DQAF-20260819-003
- **日期**: 2026-08-19
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 5 红线文件 8 处 mypy 错误被 RED_LINE_FROZEN_ALLOWANCE 冻结 (TECH_DEBT-008), 其中 live_intent_loop.py zombie-fuse 告警块在引擎死循环熔断时**从不送达** — 熔断信号全静默, 运维侧只能读到本地 watchdog_kill.log, 无人收到钉钉/告警.
  - [Layer 2 — 中间异常]: zombie-fuse 块三重错配全触发: `_ah = getattr(state, "_alert_hub", None)` 恒 None (全模块零 `_alert_hub=` 赋值) → fallback 构造 `LiveAlertHub(log_dir=..., ding_webhook_url=...)` — 两参数在现行签名 (base_dir/symbol/dingtalk_url/dingtalk_secret) 中不存在 → TypeError; `.fire(...)` 方法不存在 (现行 `send_critical`) → AttributeError; 整块被 `except (RuntimeError, ValueError, KeyError, TypeError, OSError): pass` (BLE001:FOG) 吞掉 → 熔断信号从未送达.
  - [Layer 3 — 根因]: L2 (RC-06 contract-violation) — LiveAlertHub 接口演进后 zombie-fuse 调用点未迁移 (构造参数 + 方法 + state 字段三错配), 叠加 BLE001 吞异常 → 告警全静默. 其余 7 处 L1 — 类型声明与实现不一致 (market_ingress `_compute_atr_from_rates`), 调用处未传必参 (live_cycle DataHealthService/feature_vector_sample), 不必要的 str() 转换 (shadow_ensemble), 变量名复用 (governance_scheduler `_jm`). 冻结清单 = 掩蔽层, 真实危险是行为级静默.
- **证据引用**:
  - Source 1: `scripts/live_intent_loop.py:2413-2431` zombie-fuse 告警块 (修复前: `getattr(state, "_alert_hub", None)` + `LiveAlertHub(log_dir=..., ding_webhook_url=...)` + `.fire(...)`)
  - Source 2: `scripts/_mypy_scope.py` RED_LINE_FROZEN_ALLOWANCE (修复前 5 文件 8 错)
  - Source 3: `core/observability/live_alert_hub.py` 现行契约 `__init__(base_dir, *, symbol, dingtalk_url, dingtalk_secret)` + `send_critical(reason, detail)` (无 log_dir/ding_webhook_url/fire)
  - Source 4: 修复后 `python -m mypy --follow-imports=normal` 5 文件 EXIT=0 + 针对性回归 296 passed + 回归锁 3 passed
- **是否被推翻**: 否 (AR: "type: ignore 压制即可" 被推翻 — 告警 bug 是运行时异常非类型问题, 压制让静默永久化; "state._alert_hub 恒 None 是设计" 被推翻 — 全模块零赋值)
- **关联 ReB Pattern**: FROZEN_DEBT_MASKING_LIVE_BUG / LIVE_ALERT_HUB_SIG_DRIFT

### CCT-20260819-004
- **Docket ID**: DQAF-20260819-004
- **日期**: 2026-08-19
- **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 8/11→8/13 休市期 intent_loop 每天 11 连崩共 38 次 — 每个休市周期引擎在 5s→30s respawn 循环中反复死亡, 无完整周期存活, 恢复瞬间状态重置 (known_open_tickets/governance 快照扰动).
  - [Layer 2 — 中间异常]: 降级路径抛 UnboundLocalError (继承 NameError): live_cycle.py startup_reconciliation `_positions` 在 mt5_call_with_timeout 抛 RuntimeError (MT5 IPC 未初始化/休市/离线) 后永不绑定 (Python 作用域语义 — 块内 RHS 抛出则变量名不在局部命名空间) → 块外 `set(state.known_open_tickets.keys()) - _open_tickets` 崩溃; live_intent_loop.py `_EVENT_STREAM_MODE` 原赋值点在循环体中部, 异常跳转路径 (DEGRADE/except) 跳过赋值 → L2743 引用崩. UnboundLocalError 不在 except 元组 (RuntimeError, ValueError, KeyError, TypeError, OSError) → 穿透传播 → 进程 exit 1 → launcher 无限 respawn.
  - [Layer 3 — 根因]: L3 (architecture-incomplete) — FTC(level∈{DEGRADE/LOG/IGNORE}) 契约"吞异常继续执行" × Python 作用域语义冲突; fault_handler.py docstring L141-158 官方自述陷阱 ("Always pre-initialise variables before the `with` block") 但调用点未系统性预绑定 → 每个 DEGRADE 块都是潜在 UnboundLocalError 地雷. 系统扫描全库 20+ FTC 块确认: 缺陷点 4 (live_cycle L1509/1843, group_consensus L123, live_intent_loop 循环顶) vs 安全点均已有预绑定 (live_cycle L2039/3520/4717/4921, management_phase L1060/1372/1517/527, group_consensus L64).
- **证据引用**:
  - Source 1: `blueprints/system/TECH_DEBT_REGISTRY.md:212-227` — 38 次崩溃 traceback 完整记录 (两处 UnboundLocalError)
  - Source 2: `core/runtime/fault_handler.py:141-158` — 官方自述作用域陷阱 docstring; `:221-239` DEGRADE 吞异常语义 (return True, caller checks ctx.exception)
  - Source 3: `core/runtime/live_cycle.py:1509-1596` — 修复后 pre-binding + `_skip_recon` 守卫 + known_open_tickets 保留
  - Source 4: `core/runtime/live_cycle.py:1854-1865` — `_eq: float = 0.0` 预绑定
  - Source 5: `scripts/live_intent_loop.py:2305-2311` — `while True` 循环顶部 `_EVENT_STREAM_MODE = True`
  - Source 6: `core/parliament/group_consensus.py:122-135` — `dynamic_volume = raw_volume` 预绑定
  - Source 7: 回归锁 `tests/runtime/test_tech_debt_017_scope_safety.py` 5 passed
- **是否被推翻**: 否 (AR: "休市崩溃可忽略" 被推翻 — 38 次/11 连崩 = 引擎无法存活完整休市周期; "加 UnboundLocalError 进 except" 被推翻 — 需显式降级语义)
- **关联 ReB Pattern**: FTC_SCOPE_TRAP_UNBOUNDLOCAL

---

### CCT-20260820-001 — TECH_DEBT-013 watchdog 休市误杀 (MARKET_CLOSED_BLOCK_MISCLASSIFIED_AS_DEADLOCK)
- **日期**: 2026-08-20 | **Severity**: Sev 2 | **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: `watchdog_kill.log` 8/19 21:00:06→21:54:47 **十一连杀**, 每杀 elapsed≈307.6s; kill 时间戳精确对齐 XAU 每日休市窗 (21:00-22:00 UTC); 休市结束进程恢复, 无持久故障. [confirmed]
  - [Layer 2 — 中间异常]: bar_sync 等待 (`live_intent_loop.py:2615-2661`) 在 execute_live_cycle() **之外** — FIX-20260725-002 的 `_is_daily_close_window()` (live_cycle.py:2496) 只守卫 cycle Phase 0, 拦不住 inter-cycle bar_sync 等待; 该等待阻塞 ≥300s 且 **零 heartbeat 刷新** → 内部 daemon 线程 (live_intent_loop.py:2229-2249, 每 10s 检查 last_heartbeat) 判定 stall → `os._exit(1)`. [confirmed]
  - [Layer 3 — 根因]: **L3 双层架构缺陷** — (a) 等待时长 vs 守护阈值同量级: degraded wakeup 310s (bar_period+10s) / bar_sync timeout 360s 均 > watchdog 300s, 且等待期不刷新心跳 → 合法休市等待结构性等价于死锁; (b) 语义 gate 错配: bar_sync session gate 仅放行 risk_tier=="off", 而休市窗 (21-22 UTC) 返回 "caution" → gate 结构性失败. **M5 悖论**: M5 bar period (300s) == watchdog 阈值 (300s), 纯超时压缩会在真实 bar 形成前提前 degraded 破坏正常交易 → 心跳穿透是唯一正确解. [confirmed]
- **证据引用**:
  - Source 1: `data_btc/watchdog_kill.log` — 8/19 十一连杀 (elapsed≈307.6s)
  - Source 2: `scripts/live_intent_loop.py:2229-2249` — daemon 线程 stall 判定 → os._exit(1); `:2615-2661` bar_sync 等待点
  - Source 3: `core/protocol/event_bar_sync.py` — degraded deadline 逻辑 (310s) + timeout (360s)
  - Source 4: `core/runtime/live_cycle.py:2496` — FIX-20260725-002 守卫覆盖边界 (cycle 内, 不含 bar_sync)
  - Source 5: `core/execution/pre_trade_guards.py:190-215` — 休市窗 → "caution"; `:73-79` BTC crypto_24_7 → "normal"
  - Source 6: `core/protocol/event_bar_sync.py` — FIX-20260820-001 heartbeat_refresh 注入 4 点 + degraded deadline 结构化 (有 pulse=310s / 无 pulse=270s 硬帽)
  - Source 7: 回归锁 `tests/unit/test_event_bar_sync_heartbeat.py` 8 passed (含 BTC 对照)
- **是否被推翻**: 否 (AR: "MT5 假死需重启" 被推翻 — kill 精确对齐休市窗, BTC 24/7 零误杀; "超时压缩 <300s" 被推翻 — M5 悖论破坏正常交易)
- **关联 ReB Pattern**: MARKET_CLOSED_BLOCK_MISCLASSIFIED_AS_DEADLOCK

### CCT-20260821-001 — TECH_DEBT-007 close label 五生产者分叉 (CLOSE_LABEL_MULTI_PRODUCER_DIVERGENCE)
- **Docket ID**: DQAF-20260821-001
- **日期**: 2026-08-21 | **Severity**: Sev 2 | **置信度**: confirmed
- **因果链**:
  - [Layer 1 — 症状]: 同一 deal (reason=4/comment="exit_watchdog:hesitation_18c_no_breakeven"/trail_active=True) 在四路 get 不同 label: adapter=watchdog:hesitation_18c, bridge=watchdog:hesitation_18c_no (3 段漂移); reason=None 的孤儿平仓 reconciliation 标 broker:client_close (谎标客户手动); settlement_queue 对带 trail_advances 的 SL 出场硬编码 sl_hit_first (DQAF-20260806-001 trail 盲点复活). 审计取证: XAU div-A 176 + div-B 8; BTC div-A 199 + div-B 17 (scripts/_audit_close_label_divergence_20260821.py). [confirmed]
  - [Layer 2 — 中间异常]: 出场归因/策略评估/p_win 校准/训练标签全污染 — SL 实为 trail 出场 (物理层 health, 逻辑层失忆) 被误标 first; managed/broker 因果信号在 mia_close 被 PnL label 丢弃 (reason 0-3/6/7 无特殊分支); 孤儿平仓被谎标客户手动. [confirmed]
  - [Layer 3 — 根因]: L3 架构缺陷 (RC-06 contract-violation) — 无单一 close-label 决策点: 五生产者 (adapter/reconciliation/mia_close/settlement_queue/bridge) 各持独立 label 逻辑, DQAF-20260806-001 Option C (三路单源统一) 记 Deferred 未清偿 → 每次新生产者接入/迁移都再抄一份分叉逻辑. [confirmed]
- **证据引用**:
  - Source 1: `core/runtime/settlement_queue.py` — FIX-20260730-011 硬编码 sl_hit_first (无 trail), `_source=mt5_reconciliation` 覆写桥标签 (pre-P6)
  - Source 2: `scripts/mt5_bridge_worker.py:1054-1059` — watchdog 3 段提取 `split("_",3)[:3]` (pre-P6)
  - Source 3: `core/runtime/reconciliation.py` — None-reason 伪造 `broker:client_close` (pre-P6)
  - Source 4: `scripts/_audit_close_label_divergence_20260821.py` — div-A/B 取证 (XAU 176+8 / BTC 199+17)
  - Source 5: `core/runtime/close_label.py` — SSOT leaf (P0-P6)
  - Source 6: `tests/runtime/test_close_label_convergence.py` — 9 行参数化矩阵 × 4 生产者 byte-identical
  - Source 7: 回归锁 46 新测试 + runtime 456 + 全量 5225 passed
- **是否被推翻**: 否 (AR: "label 只是遥测不影响训练" 被推翻 — training label_contract 消费 close label, 污染传播; "bridge 标签会被 dedup 覆写无需修" 被推翻 — 未覆写的孤儿桥条目保留错误格式)
- **关联 ReB Pattern**: CLOSE_LABEL_MULTI_PRODUCER_DIVERGENCE
