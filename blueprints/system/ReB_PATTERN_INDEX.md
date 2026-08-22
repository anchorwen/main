# ReB Pattern Index — 修复知识库

> **标准参考**: SRE "Postmortem Culture" (Google SRE Book Chapter 15), ISO 30401:2018 "Knowledge Management Systems"
> **用途**: 记录可复用的 Bug 模式签名，使历史模式可被程序化搜索，防止同类 Bug 重复修复（FIX-022 型问题）。
> **格式约定**: **强制使用三级标题块格式**（禁止 Markdown 表格，模式描述和预防策略文本较长会被水平拉爆不可读）。

## 格式模板

```markdown
### ReB-YYYYMMDD-NNN
- **Pattern Signature**: 简短机器可读标识（如 `hardcoded_feature_list_in_assembler`）
- **描述**: 该模式的本质特征（2-3 句）
- **关联 FIX IDs**: FIX-YYYYMMDD-NNN, ...
- **关联 Docket IDs**: DQAF-YYYYMMDD-NNN, ...
- **预防策略**: 如何从类型系统/架构层面防止复发
- **检测方法**: 自动化检测手段（mypy rule / ruff rule / 专项测试）
```

## 模式索引

---

### ReB-20260822-METRIC_SATURATION_SESSION_BIAS
- **Pattern Signature**: `METRIC_SATURATION_SESSION_BIAS`
- **Date Cataloged**: 2026-08-22
- **Source Docket**: DQAF-20260822-001
- **Related**: FIX-20260822-001 (RESOLVED); 同族: FIX-20260821-007 (METRIC_DENOMINATOR_SEMANTIC_SHIFT — "传感器读数≠市场真相"度量族), FIX-20260731-004 (MISSING_TF_AS_FLAT_DEGRADATION)

**定义**: 监控/评分指标若用会话累积计数器 (单调递增、永不衰减/复位) 计算"当前状态"而非滚动窗口速率, 则指标随进程存活时长单调饱和 → 永久锁死惩罚因子 → 健康/质量分成为 uptime 代理而非市场真相. 长会话 (如零交易期) 使缺陷显性化, 短会话 (频繁重启) 掩盖之; 跨品种共享组件同步污染. 关键签名: (1) 得分分子 = 累积计数/固定窗口, 分子无界; (2) 指标会话内单调非降, 0 回落; (3) 饱和后恒等 floor×其他因子 (health≡alignment×0.1); (4) 跨会话 Pearson(周期数/会话时长, 指标)<0 (指标反比 uptime); (5) 同组件双品种同偏置 (BTC f_chop 56.1% / XAU 68.0%).
- **预防** (IMPLEMENTED, FIX-20260822-001): ① **滚动窗口 SSOT** — 得分分子必须来自 `deque(maxlen=N)` 内相邻 label 差异计数 (有界), 任何会话累积状态禁止参与"当前状态"计算 (accumulator 语义只可服务于"累积事件"型输出, 不得用于"当前速率/状态"); ② **单调性回归锁** — 长稳定期后指标必须回落: `test_rolling_window_recovers_after_prolonged_stability` (26 交替 → chop_detected; 再 30 单调稳定 → chop_score==0.0 非 1.0, 旧累加器饱和场景被物理锁); ③ **uptime 相关性哨兵** — 审计脚本输出 Pearson(cycles/day, health) 与单调回落计数, 双品种 (data_btc + data) 同法跑, 指标↔uptime 强负相关即红旗; ④ **魔法数字冻结** — 阈值 (0.55 硬否决线/_chop_window=24/_chop_threshold=6) 视为红线, 修复只改来源不改标尺.
- **检测**: 回归锁 `tests/test_gods_eye.py::TestChopDetection::test_rolling_window_recovers_after_prolonged_stability`. 通用法: 任何监控指标先检查是否会话累积计数参与分子; grep `min(1.0, <counter> / <window>)` 形态得分; 跨会话 Pearson(uptime↔score) < 0 即红旗.

### ReB-20260821-CROSS_ASSET_DEFAULT_SILENT_MISPAIR
- **Pattern Signature**: `CROSS_ASSET_DEFAULT_SILENT_MISPAIR`
- **Date Cataloged**: 2026-08-21
- **Source Docket**: DQAF-20260821-003
- **Related**: FIX-20260821-008 (RESOLVED), CROSS_ASSET_CONTAMINATION_AUDIT H2, FIX-20260821-006 (EMPTY_NPZ_EOF_READINESS_HARNESS — 同族"默认值空转"跨品种坑)

**定义**: 多品种训练/服务脚本若把跨品种默认输入路径硬编码进 argparse (snapshots 默认 XAU 而 journal 默认 BTC), 且加载后无品种一致性断言, 则数据不足/质量门禁拒签被确定性伪造 — "insufficient samples" 成为掩盖物理路由缺陷的可信业务借口, 训练/部署在静默中假性受阻. 关键签名: (1) 默认值跨品种错配 (两个默认路径属于不同资产); (2) 数据不足假象 (同源快照在正确期刊 86.6% 保留 vs 错误期刊 8.6%); (3) 门禁诚实拒签但根因被错误归因于数据量 (7-wins < 15 → "insufficient samples"); (4) 运行时消费方同构硬编码单品种模型路径 (BTC 进程加载 XAU 退出模型).
- **预防** (IMPLEMENTED, FIX-20260821-008): ① **per-asset path SSOT** — `_SYMBOL_PATHS` 单点定义 (snapshots/journal/output), `--symbol` 动态派生, 取消跨品种硬编码默认 (无 symbol/无显式路径 → 硬错); ② **Join-Retention 硬断言** — 加载后 snapshot/journal ticket 交集保留率 < 阈值 (默认 50%) → `sys.exit(1)` 拒绝训练 (负对照实证: 8.6% → CONSISTENCY GUARD HALT); ③ **运行时品种感知** — `core/deployment/path_defaults.py` per-asset 模型常量 + `live_intent_loop.py` 按 `args.base_dir` 分派; ④ **回归锁** — tests/scripts/test_train_exit_metamodel_guard.py 9 测试 (路径派生 xau/btc / 显式覆盖 / 无 symbol 硬错 / 保留率通过 / 跨品种 HALT / 空宇宙 HALT / 精确阈值).
- **检测**: 回归锁 `tests/scripts/test_train_exit_metamodel_guard.py`. 通用法: 任何新增多品种训练/服务脚本先 grep 默认路径是否跨品种; 任何"数据不足"拒签先跑双期刊配对对照 (同一快照归属普查); 任何 per-asset 模型加载先确认品种感知分派 (base_dir 而非硬编码路径).

### ReB-20260821-METRIC_DENOMINATOR_SEMANTIC_SHIFT
- **Pattern Signature**: `METRIC_DENOMINATOR_SEMANTIC_SHIFT`
- **Date Cataloged**: 2026-08-21
- **Source Docket**: DQAF-20260821-002
- **Related**: FIX-20260821-007 (RESOLVED), TECH_DEBT-021 (CLOSED), TECH_DEBT-022 (The Silent Feature Drought — 同源断供残留)

**定义**: 监控/就绪度量若用与生成器/消费者**不同语义的基数** (原始事件条目 vs 去重业务实体) 做分母, 会产生确定性比例失真 — 把"达标 (82.9%)"伪报成"灾难 (22.3%)"或反之, 掩盖真实状态并触发假阻断. 关键签名: (1) 度量分母用 ledger 原始行数 (`ack_status=="closed"` 条目 4697) 而非业务实体 (position_ticket 去重 + PnL 非空 = 1262); (2) 生成器自身有权威口径 (builder 实配 1046/1262) 但只印在 stdout, 未被监控侧消费; (3) 运维性质事件 (manual_close/orphan) 混入业务计数.
- **预防** (IMPLEMENTED, FIX-20260821-007): ① **生成器自报口径 SSOT** — builder 落 `*.report.json` 边车 (valid_trades_count / real_closed_trades_count / manual_close / orphan + asof 分类), 度量侧直接读权威分母, 禁止独立重数; ② **度量分母 = 业务实体** — 任何"交易计数"必须 position_ticket 去重 + 关键字段 (PnL) 非空, 运维事件显式排除; ③ **本地回退显式标注** — report 缺失时回退本地 distinct 计数并标注 fallback, 杜绝静默口径漂移; ④ **回归锁** — 新测试锁定 SSOT 读 / fallback / manual_close 排除三分支.
- **检测**: 回归锁 `tests/scripts/test_training_readiness_xau_metafilter.py` (+3: test_stage2_pnl_completeness_uses_real_closed_denominator / test_stage3_asof_rate_reads_report_denominator / test_stage3_asof_rate_fallback_to_distinct_pnl). 通用法: 任何新"率类度量"必须先声明分母定义 = 业务实体 vs 原始事件; 若生产者存在, 度量分母必须由生产者自报 (report/json), 禁止消费方独立重数.

### ReB-20260821-HARDCODED_STALENESS_MULTIPLE_CLOCKS
- **Pattern Signature**: `HARDCODED_STALENESS_MULTIPLE_CLOCKS`
- **Date Cataloged**: 2026-08-21
- **Source Docket**: DQAF-20260820-005
- **Related**: FIX-20260821-001 (RESOLVED), TECH_DEBT-011/012 (CLOSED), FIX-20260820-001 (MARKET_CLOSED_BLOCK_MISCLASSIFIED_AS_DEADLOCK — 同族休市盲区)

**定义**: 系统"数据何时算停滞/何时该判新"的时间语义若在多个模块各自硬编码 (年龄阈值 + 独立停机启发式), 则休市/闭市静默期被结构性误分类为停滞 → 审计/监控工具周末假阳性 (监控失信), 且未来修改时间语义需改 8+ 站点 (Iterability 债). 关键签名: (1) 同一 staleness 概念在 N 处硬编码 N 种阈值; (2) 无市场日历感知 (forex_24_5 休市 vs crypto_24_7 全天); (3) 造钟逻辑与消费方解耦 (健康检查独立 POST_OUTAGE 启发式); (4) 上游时钟驱动 last-value freeze 在写入侧产生逐位重复行 (次生 `LAST_VALUE_FREEZE_DUPLICATE_WRITE`).
- **预防** (IMPLEMENTED, FIX-20260821-001): ① **单一日历时钟** — `core/market/calendar.py` 纯 stdlib leaf 网格 API, `staleness_anchor(now_utc, market_type, base)` 为全局唯一停滞基准 (open→now−base / closed→last_close−base); ② **离线/观测反向依赖安全** — leaf 包零反向依赖 (core/__init__/core/market/__init__ docstring-only), audit 脚本与 health checks 直接 import; ③ **market_type 派生收敛** — data_dir/symbol → forex_24_5/crypto_24_7 单函数; ④ **写入侧幂等** — 尾行指纹去重 (排除写时元数据 ingested_at), compact 失效尾缓存防数据丢失; ⑤ **回归锁** — 58 测试 (周末冻结不误报/周一重开仍抓/BTC 不放松/闭市冻结 PASS).
- **检测**: 回归锁 `tests/market/test_calendar.py` (TestCalendarGrid 16) + `tests/runtime/test_dci_calendar.py` + `tests/observability/test_health_checks_feature_store.py` + `tests/engine/test_feature_store.py` (TestWriteDedupTailFingerprint 7). 通用法: 新增停滞/年龄判断前先 grep `staleness_anchor`; 任何新时间语义必须收敛到 calendar.py 单点, 严禁再造第二个"钟表".

### ReB-20260821-EMPTY_NPZ_EOF_READINESS_HARNESS
- **Pattern Signature**: `EMPTY_NPZ_EOF_READINESS_HARNESS`
- **Date Cataloged**: 2026-08-21
- **Source Docket**: DQAF-20260821-020
- **Related**: FIX-20260821-006 (RESOLVED), TECH_DEBT-020 (CLOSED), FIX-20260820-004 (FAILURE_DETECTION_SIGNAL_AMBIGUITY — 次生信号污染已根治)

**定义**: 训练就绪/契约评估链路若 (1) 用**默认值 (契约结构化字段缺失 → builder 默认 symbol 空转)** 实例化通用生成器, 且 (2) 生成器对"数据不足"**静默早退 rc=0 不写产出**, 且 (3) 校验器**预建空临时文件**并对其 `np.load` **零容错**, 则确定性触发 `EOFError` — 评估工具报崩溃而非诚实的数据缺口, 使训练就绪评估结构性失效 (XAU 此前从未被真实验证). 关键签名: (1) 契约结构化字段缺失但 description 文本记载正确调用 (结构化/叙事分叉); (2) `if not <data>: return` 静默 rc=0 (成功/失败信号无歧义性); (3) 消费方预建空文件 + `np.load` 无异常捕获; (4) 控制组掩盖 — 恰命中默认值的品种 (BTC v3 / data_btc BTCUSDc) 不炸, 缺陷被对照组"正常"遮蔽.
- **预防** (IMPLEMENTED, FIX-20260821-006): ① **契约字段补全** — `builder_script`/`builder_output_arg`/`builder_args` 结构化声明, 禁止依赖默认值空转 (全契约族检漏: 凡 builder_script 默认命中但缺 builder_args 一律显式声明); ② **Fail-Fast Generator** — 生成器无数据路径 stderr ERROR + 非零退出码, 静默失败→硬失败 (下游可区分"未运行"与"正常产出"); ③ **Resilient Reader** — `np.load` 前空文件守卫 (`os.path.getsize==0` → FAIL verdict), load 捕获 `(EOFError, ValueError, OSError, pickle.UnpicklingError, zipfile.BadZipFile)` → FAIL + 明确诊断, 绝不让整条管线因单个 npz 抛 traceback; ④ **回归锁** — 契约字段断言 + valid/empty/corrupt/builder_fail/legacy 矩阵.
- **检测**: 回归锁 `tests/scripts/test_training_readiness_xau_metafilter.py` (7 测试). 通用法: 新增 stage-3 类评估前 grep `builder_args` + `np.load`; 任何"数据不足则 return"路径必须非零退出; 任何 `np.load` 必须包损坏异常捕获.

### ReB-20260820-FAILURE_DETECTION_SIGNAL_AMBIGUITY
- **Pattern Signature**: `FAILURE_DETECTION_SIGNAL_AMBIGUITY`
- **Date Cataloged**: 2026-08-20
- **Source Docket**: DQAF-20260820-004
- **Related**: FIX-20260820-004 (RESOLVED), FIX-20260820-003 (前案), TECH_DEBT-020 (次生数据债)

**定义**: 进程成功/失败判定若采用**对自身日志语义不具特异性的启发式信号**, 会被例行写入该通道的"噪音"确定性误导 → 正常完成被误判为失败 → 下游状态机 (stamp/重试) 结构性阻断. 关键签名: (1) 被判定进程有 fail-open/fail_open_guard 设计 — 用 `logging.exception` 将被捕获异常 traceback 例行写入 stderr (Python last-resort handler 无 handler 配置时固定落 stderr); (2) 判定方用 `"Traceback" not in stderr` 作为崩溃判别 (混淆"未捕获崩溃"与"被捕获异常日志"); (3) 确定性触发点 (空/损坏 npz → EOFError) 使误判每轮必现, 非偶发; (4) 被判定进程**有权威完成契约信号却未使用** (daily_ops 正常返回前必然打印的 report JSON).
- **预防** (IMPLEMENTED, FIX-20260820-004): ① **认证 stdout 完成契约而非 stderr 启发式** — 成功谓词 `returncode <= 1 AND stdout 尾部认证出完整 report JSON (schema_version 标识)`, 崩溃走不到打印点 → report 缺失 = 未完成; ② **stderr 全噪音免疫** — Fail-Open 吞噬职责内错误, traceback/warning 不参与崩溃判别; ③ **完成标记单点锚定** — daily_ops.py L3589-3590 (正常返回前最后输出) + L3491 (schema_version 无条件键); ④ **回归锁** — 7 分支含 IC 指定 solo 复现桩 (stderr EOFError Traceback + stdout report → SUCCESS) + 截断 report → FAILED.
- **检测**: 回归锁 `tests/runtime/test_live_launcher_daily_ops.py` (7 分支 JSON Payload Authentication). 通用法: 判定子进程成功时, 列出该进程"正常完成必然产生的 stdout 信号"与"崩溃必缺失的信号", 优先认证前者.
- **关联次生债**: TECH_DEBT-020 — 空/损坏 npz 是噪音源头 (数据完整性, 独立建档).

### ReB-20260820-EXIT_CODE_CONTRACT_MISMATCH_IN_SSOT_STAMP
- **Pattern Signature**: `EXIT_CODE_CONTRACT_MISMATCH_IN_SSOT_STAMP`
- **Date Cataloged**: 2026-08-20
- **Source Docket**: DQAF-20260820-003
- **Related**: FIX-20260820-003 (RESOLVED, Traceback 判别部分已被 FIX-20260820-004 替换), FIX-20260820-004

**定义**: 当"完成状态"的唯一事实源从进程 A 迁移到进程 B 时, 进程 B 若沿用旧的退出码判定语义 (而非读取被调用方的新退出码契约), 则正常完成被误判为失败 → SSOT 时间戳永不写入 → age 兜底无限重跑. 关键签名: (1) 子进程有非零成功退出码 (如 rc=1=完成且应用动作); (2) 父进程用 `rc==0` 硬判定成功 (watchdog 时代装饰性判定被掩盖); (3) stamp-at-completion 迁移使该判定升格为门禁; (4) 崩溃与动作完成共用 rc=1, 需 Traceback 判别 (未捕获异常必打 Traceback 至 stderr).
- **预防** (IMPLEMENTED, 修正): ① **成功谓词对齐被调用方契约** — 原 `returncode <= 1 and "Traceback" not in (stderr or "")` 的 **rc∈{0,1}=成功 / rc=2=失败 对齐部分保留**; ② **⚠️ 防弹背心已证伪并替换** — stderr Traceback 判别被 DQAF-20260820-004 证伪 (fail_open_guard 将捕获异常 traceback 例行写 stderr, 不具崩溃特异性), 现由 FIX-20260820-004 **JSON Payload Authentication** (stdout report JSON 认证) 取代; ③ **不破坏被调用方契约** — daily_ops.py 退出码语义保留 (CLI 监控依赖 rc=1 判断有动作); ④ **回归锁升级** — 7 分支 (rc=0/1+report→stamp, rc=2→fail, rc=1+无report→fail, **rc=1+EOFError Traceback+report→stamp (solo 复现桩)**, 截断 report→fail).
- **检测**: 回归锁 `tests/runtime/test_live_launcher_daily_ops.py` (7 分支) + `tests/runtime/test_daily_ops_state.py` (trigger/stamp 契约) + `tests/runtime/test_daily_ops_scheduler.py` (信号触发零重负载).

### ReB-20260820-SYNC_HEAVY_COMPUTE_IN_HEARTBEAT_ZERO_PULSE
- **Pattern Signature**: `SYNC_HEAVY_COMPUTE_IN_HEARTBEAT_ZERO_PULSE`
- **Date Cataloged**: 2026-08-20
- **Source Docket**: DQAF-20260820-002
- **Related**: FIX-20260820-002 (RESOLVED), TECH_DEBT-014

**定义**: 心跳驱动进程 (intent loop) 在周期内零脉冲区同步执行重负载计算 (daily_ops 5-10min+), 远超 watchdog 硬阈值 → 结构性必杀. 关键签名: (1) 重负载与心跳同线程同进程; (2) 执行点位于必须保持存活的守护周期内 (cycle-top→cycle-complete); (3) 完成戳打在开始 (stamp-at-start) 而非成功回执 → 失败后逐日漂移 + 主窗口抑制, 且漂移被 age 兜底掩盖为"非故障".
- **预防** (IMPLEMENTED): ① **Single Executor/SSOT** — 重负载计算唯一执行者 = 独立子进程 (launcher), 心跳进程降级为纯信号触发器 (瞬时信标 + best-effort 写零异常冒泡); ② **stamp-at-completion** — 完成时间戳唯一事实源 = 子进程成功回执, 废除 stamp-at-start (消除漂移与主窗口抑制); ③ **副作用归属迁移** — 所有重负载副作用 (feature compaction / label prune / governance) 集中到子进程管线; ④ **回归锁** — 心跳进程零重负载同步调用断言 + 幂等无异常触发契约.
- **检测**: 回归锁 `tests/runtime/test_daily_ops_scheduler.py` (信号触发零重负载/不写 state/幂等无异常/写失败不冒泡) + `tests/runtime/test_daily_ops_state.py` (trigger/stamp 契约) + `tests/engine/test_daily_ops.py` (label_prune + P12 gate).

### ReB-20260819-PATH_SEPARATOR_MISMATCH_FALSE_BLOCK
- **Pattern Signature**: `PATH_SEPARATOR_MISMATCH_FALSE_BLOCK`
- **Date Cataloged**: 2026-08-19
- **Source Docket**: DQAF-20260819-007
- **Related**: FIX-20260819-007 (RESOLVED), FIX-20260604-087 (freeze gate 源起)

**定义**: 读取外部报告 (coverage.json / manifest) 时, 生成器用平台原生路径分隔符 (Windows 反斜杠), 消费方断言用规范正斜杠前缀, 直接字符串 `startswith` 匹配**未归一化** → 合法文件零命中 → 门禁读 0 数据 → 假阳性阻断 (或假阴性放行). 关键签名: (1) 平台相关生成器 (pytest-cov Windows) vs 硬编码规范前缀; (2) 匹配边界无归一化; (3) 长期存在被 env 免死金牌掩盖, 门禁形同虚设.
- **预防** (IMPLEMENTED): ① **匹配边界单收敛点归一化** — `_is_protected` 入口 `filepath.replace("\\", "/")` 后前缀匹配, 同时覆盖双输入面 (git staged 路径正斜杠 + 覆盖报告反斜杠); ② **退役 env 免死金牌** — 门禁诚实读真实覆盖率, 紧急例外走文档化 `--no-verify` (Iron Law #0-bis) + 注明理由; ③ 回归锁双分隔符 + 加权计算 + 绕过移除断言.
- **检测**: 回归锁 `tests/scripts/test_journal_freeze_gate.py` — TestIsProtected (双分隔符命中/相邻未保护不命中) + TestReadCoveragePct (反斜杠键加权 45.0/65.0/0.0) + TestNoEnvBypass (env 不豁免 / 绕过常量已移除).

### ReB-20260819-VETO_NO_DECLARED_ADAPTER_CHANNEL
- **Pattern Signature**: `VETO_NO_DECLARED_ADAPTER_CHANNEL`
- **Date Cataloged**: 2026-08-19
- **Source Docket**: DQAF-20260819-006
- **Related**: FIX-20260819-006 (RESOLVED), FIX-20260819-002 (Shadow Veto 引入者), FIX-20260521-009 (stub adapter 契约)

**定义**: 硬断言防御门 (fail-closed veto) 的判定输入**单源解析自生产配置**, 无"合法调用者显式声明"通道 → 合法确定性场景 (CI 影子回归基线生成 / 批量重放) 被误伤. 关键签名: (1) veto 谓词本身正确 (网络适配器在影子模式 = 非法); (2) adapter 名唯一来自生产 live.yaml; (3) 存在合法影子构建者 (CI stub 场景) 却无声明途径 → 全 push CI 阻断.
- **预防** (IMPLEMENTED): ① 防御门判定输入支持**显式声明通道** — `QUANTOS_SHADOW_ADAPTER` env 显式声明优先于生产配置 (但**任何来源**解析到被禁止值仍一律拦, 谓词零削弱); ② 合法场景入口 (CI fixture prep) 在**模块级**声明其 stub-only 本质 (`os.environ.setdefault`); ③ 回归锁双方向 — 声明合法值放行 / 声明被禁止值照拦.
- **检测**: 回归锁 `test_tech_debt_010_death_of_defaults.py` (test_shadow_veto_accepts_explicit_stub_env_declaration / test_shadow_veto_env_network_adapter_still_blocked); CI 推送即校验.

### ReB-20260819-UNIFIED_TEST_DEBT_A3_LATENT
- **Pattern Signature**: `UNIFIED_TEST_DEBT_A3_LATENT`
- **Date Cataloged**: 2026-08-19
- **Source Docket**: DQAF-20260819-005
- **Related**: FIX-20260819-005 (RESOLVED), TECH_DEBT-009, DQAF-20260816-001 (MANIFEST_OMISSION — CI 面同类潜伏)

**定义**: 类型债在 isolated mypy 模式 (`follow_imports=skip`, pre-commit/baseline 使用) 下被**跳过导入**结构性掩盖, 仅在 unified 模式 (`python -m mypy core/ apps/ scripts/ tests/`, follow_imports 真实解析) 暴露 — 全量 236 错误/62 测试文件, pre-commit isolated 基线却 0 新增, 双模式结果不一致 = 潜伏债务真实存在但常规门禁不可见. 关键签名: (1) 仅统一解析跨模块边界时出现的泛型签名债 (非 Any 化容器/函数签名); (2) isolated 模式全绿 + unified 模式报错并存; (3) 修复 `# type: ignore` 在 isolated 触发 warn_unused_ignores → 双模式冲突.
- **预防** (IMPLEMENTED): ① **cast-not-ignore 策略** — 仅 unified 可见的错误禁用裸 `# type: ignore` (isolated 触发 unused-ignore 阻塞 commit), 一律 `cast(...)` 纯类型层 no-op 双模式干净; 高重构成本且零安全风险的孤立警告允许 `# type: ignore` + 明确注释; ② **零运行时行为改变纪律** — 全批纯类型层修改, 97 文件业务逻辑分支零触碰; ③ unified 面纳入清偿序列标准验证 (`python -m mypy core/ apps/ scripts/ tests/` = 0 为完成定义).
- **检测**: `python -m mypy core/ apps/ scripts/ tests/` (unified); 双模式对照 pre_commit_mypy.py (isolated 基线) + 全量 unified 应一致.

### ReB-20260819-FTC_SCOPE_TRAP_UNBOUNDLOCAL
- **Pattern Signature**: `FTC_SCOPE_TRAP_UNBOUNDLOCAL`
- **Date Cataloged**: 2026-08-19
- **Source Docket**: DQAF-20260819-004
- **Related**: FIX-20260819-004 (RESOLVED), TECH_DEBT-017, DQAF-20260616-101 (timeout-wrapped MT5 IPC 引入 DEGRADE 触发面)

**定义**: FaultTolerantContext 吞异常级别 (DEGRADE/LOG/IGNORE) 的块内, 若唯一赋值语句的 RHS 抛异常, Python 作用域语义使该变量名**永不绑定**到局部命名空间 — 块外引用即 UnboundLocalError (继承 NameError, 不在调用方宽 except 元组内 → 穿透崩溃). 关键签名: (1) `with FaultTolerantContext(level=DEGRADE/LOG/IGNORE)`; (2) 块内首次绑定变量仅通过调用式赋值 (RHS 可为外部调用); (3) 块外有引用; (4) 块前无预绑定. 与 CRASH 级别无关 (异常 re-raise 传播, 块后不可达). fault_handler.py docstring 官方自述陷阱, 但**无机制强制调用点遵守** → 每次新增 DEGRADE 块都是潜在地雷.
- **预防** (IMPLEMENTED): ① **Scope-Safe Pre-binding 契约** — 每个 FTC(DEGRADE/LOG/IGNORE) 块内首次绑定变量必须在 `with` 块前最顶层预绑定安全默认值 (镜像 fault_handler.py docstring: "Always pre-initialise variables before the with block"); ② 新代码审查标准 — 新增 FTC 块必须双检: 块内变量是否块外引用 + 是否预绑定; ③ 回归锁静态断言 — 预绑定语句文本位置必须先于 FTC component 标记 (test_tech_debt_017_scope_safety.py 4 处 static 顺序断言, 未来删除/后移预绑定即 FAIL).
- **检测**: 回归锁 test_tech_debt_017_scope_safety.py (static 顺序断言); 代码审查: grep `FaultTolerantContext` + `level=FaultLevel.(DEGRADE|LOG|IGNORE)` 块, 检查块前预绑定; AST 工具可扩展: 扫描 FTC 块内赋值变量 × 块外引用 × 无预绑定.

### ReB-20260819-CROSS_ASSET_JOURNAL_CONTAMINATION
- **Pattern Signature**: `CROSS_ASSET_JOURNAL_CONTAMINATION`
- **Date Cataloged**: 2026-08-19
- **Source Docket**: DQAF-20260819-002
- **Related**: FIX-20260819-002 (RESOLVED — Blueprint B Journal Firewall), TECH_DEBT-010, DQAF-20260804-007 (D1_WELL_CROSS_ASSET_POISONING — 同族跨资产写)

**定义**: 命令/事件因 per-symbol 路由裂缝错误写入**其他品种域**的 SSOT 账本 — 多品种架构 (XAU data/ + BTC data_btc/) 下, 任一命令未携带 per-symbol 域身份 (endpoint/journal-domain) 即落入默认域 → BTC 命令混入 XAU 账本 (实证: 10 条 BTC modify_sltp rejected, magic 90460/90430 混入主 journal data/, 而 data_btc journal 全正常). 关键签名: (1) 单一默认值兜底 (默认端口/默认路径/默认账本); (2) 写盘 chokepoint 无域校验; (3) 对账时数据方向与命令方向不一致.
- **预防** (IMPLEMENTED): ① 唯一写盘 chokepoint (`_append_journal`) 前插**域 Firewall** — XAU 账本仅收 XAUUSD(c), BTC 仅收 BTCUSDc, 跨域记录写 `cross_domain_warnings.jsonl` 绝不进 SSOT; 域由 `--journal-domain` 显式 / `--default-symbol` 前缀推导, 启动打印 firewall_armed/disarmed 状态; ② 命令链路 per-symbol endpoint 显式注入 (禁默认端口兜底, 见 ZMQ_DEFAULT_PORT_FALLBACK).
- **检测**: 回归锁 test_append_journal_blocks_cross_domain / test_append_journal_btc_domain_blocks_xau (跨域记录 → cross_domain_warnings 存在 + 主账本零写入); 审计 `cross_domain_warnings.jsonl` 增长; 对账 journal symbol 域 vs 命令源方向.

### ReB-20260819-ZMQ_DEFAULT_PORT_FALLBACK
- **Pattern Signature**: `ZMQ_DEFAULT_PORT_FALLBACK`
- **Date Cataloged**: 2026-08-19
- **Source Docket**: DQAF-20260819-002
- **Related**: FIX-20260819-002 (RESOLVED — Blueprint C Death of Defaults), TECH_DEBT-010, DQAF-20260804-007 (同族"跨资产默认值")

**定义**: 多品种架构中 ZMQ Endpoint 用默认端口兜底 (service_container 默认 `tcp://127.0.0.1:5556` = XAU 桥), 导致非 XAU 域进程/命令静默落到错误品种桥 (BTC 命令落 XAU 桥 → 227 条 rejected + 10 条 journal 污染). 关键签名: (1) 构造器参数有默认端口值; (2) 调用面漏传 endpoint 不报错 (静默回退); (3) 多品种共享单默认值.
- **预防** (IMPLEMENTED): ① `ZMQCommunicationAdapter.__init__` `order_endpoint` 改 **required 无默认** (TypeError 级拦截); ② service_container/live_launcher 无显式 endpoint → `DataIntegrityError`/`RuntimeError` **fail-fast 崩溃** (无 Endpoint 想发单 → 死, 不静默串台); ③ 全 dispatch 调用面 (open + modify + 7 close 点) 显式注入 per-symbol endpoint (XAU 5556 / BTC 5558); ④ 配置解析单收敛点断言双品种端口互异.
- **检测**: 回归锁 test_zmq_adapter_constructor_requires_order_endpoint / test_mt5_zmq_without_endpoint_raises_data_integrity_error / test_mt5_zmq_with_endpoint_binds_correct_symbol_port; grep 构造器 ZMQ endpoint 默认值 (禁 `= "tcp://...`).

### ReB-20260816-MANIFEST_OMISSION
- **Pattern Signature**: `MANIFEST_OMISSION`
- **Date Cataloged**: 2026-08-16
- **Source Docket**: DQAF-20260816-001
- **Related**: FIX-20260816-001 (RESOLVED — L2 根治), FIX-20260624-107 (同根因 patch 掩盖先例 — except ImportError 遮缺包)

**定义**: 运行时依赖被一个部署清单声明 (Dockerfile `pip install <explicit list>`) 却从包清单 (pyproject.toml `[project].dependencies`) 遗漏 → CI / `pip install .` 全新环境缺包。若所有 import 均为函数内惰性执行, CI 测试套件可长期全绿掩盖缺口, 直到新增一个硬 import 的测试首次暴露 (实证: FIX-20260803-007 `test_predict_is_yA_plus_r` L133 硬 import lightgbm)。关键签名: (1) 双清单并存 (Dockerfile + pyproject) 且内容漂移; (2) 生产核心模块惰性 import 缺失包 (不 import 即不崩); (3) 存在 except ImportError 掩盖路径 (FIX-20260624-107)。
- **预防** (IMPLEMENTED): 单清单真源 — pyproject `[project].dependencies` 必须为所有运行时依赖唯一权威声明, 与 Dockerfile 显式 install 列表逐项对齐; 禁止用 except ImportError 掩盖缺包 (应让缺失在测试/启动即暴露); 硬 import 契约测试须在清单中声明其依赖。
- **检测**: 脚本对比 Dockerfile pip install 显式列表 vs pyproject `[project].dependencies` 求差; grep `except.*ImportError` 于缺包敏感路径 (meta_signal_filter/adapters); 新测试硬 import 第三方包时 lint 提示清单声明。

### ReB-20260807-STATEFUL_GATE_MULTIPROCESS_DRIFT
- **Pattern Signature**: `STATEFUL_GATE_MULTIPROCESS_DRIFT`
- **Date Cataloged**: 2026-08-07
- **Source Docket**: DQAF-20260807-003
- **Related**: FIX-20260807-003 (RESOLVED — Boundary 1 Stateless Gate), DQAF-20260726-012 (GHOST_BOOTSTRAP_RESTORE_MUTUAL_EXCLUSION — 同族跨重启状态), DQAF-20260708-001 (MUTABLE_TICKET_JOIN_ON_IMMUTABLE_POSITION — 同族 identity 漂移)

**定义**: 一个守卫 append-only 账本的验证闸门在 __init__ 时一次性把"已知合法记录"缓存进内存集合, 而真值源是多个 OS 进程 (live_intent_loop / mt5_bridge_worker / daily_ops) 共享写入的物理 journal — 各进程实例内存态各自漂移, 无 IPC/无刷新 → 合法记录在 gate 进程内存缺失 → 被误拒 (实证: 4454299643 合法平仓被判 close_without_open 隔离, PnL 从 SSOT 消失). 关键签名: (1) gate 持 `_known_tickets` 等内存缓存; (2) 多进程各自实例; (3) 物理 journal 是不可变共享真值, 但 gate 读的是过时内存.
- **预防** (IMPLEMENTED, IC Boundary 1): 守卫共享账本的验证闸门必须**无状态** — 每次 validate 调用重新扫描物理 journal (或受限 tail), "SSOT 在硬盘里, 不在内存里". 任何其正确性依赖跨进程缓存一致性的 gate 都是架构设计错误 (RC-06). 性能顾虑用 bounded tail / mtime hint 解决, 绝不用无界内存集合.
- **检测**: grep 多进程上下文中的 in-memory ticket/state 缓存 gate; 回归锁 test_stateless_gate_sees_open_written_by_other_process (模拟"他进程写 open, 本进程 validate close") + test_stateless_gate_still_quarantines_genuine_orphan.

### ReB-20260807-REGISTRATION_VS_DISPATCH_VOLUME_DIVERGENCE
- **Pattern Signature**: `REGISTRATION_VS_DISPATCH_VOLUME_DIVERGENCE`
- **Date Cataloged**: 2026-08-07
- **Source Docket**: DQAF-20260807-003
- **Related**: FIX-20260807-003 (RESOLVED — 体积单源化 合并入执行令 2a), DQAF-20260708-003 (close_price 伪造 — 同族 "记账非物理真值")

**定义**: 记账层从决策对象 (decision.volume) 读持仓体积, 而物理派发已发出不同体积 → 账本与券商偏离 (ghost volume). 关键签名: (1) 下游变更 (reentry decay) 在 portfolio_risk snapshot **之后**覆写 decision.volume (0.02→0.01); (2) 派发发 adjusted_volume (0.02); (3) 记账读 decision.volume (0.01) → 开仓腿体积错; (4) 平仓 corpse 更记 0.0 → PnL 无法按体积对账 (实证: 开 0.02 平 0.0, PnL −66.30 与物理体积自证 $2/pt×32.807pts≈66).
- **预防** (IMPLEMENTED, IC 执行令 2a 合并): 记账必须消费**物理派发结果** (DispatchResult.volume) 为唯一真值 — 绝不可消费可能被其他阶段覆写的可变决策字段. 记账是对物理现实的观测, 不是从意图重新推导. 退化 DispatchResult 无 volume 才回退 decision.volume (镜像派发侧表达式).
- **检测**: 每次开仓记录 volume 与对应派发回执/journal 条目 volume 交叉核对 (Iron Law #11 脚本); 回归锁 test_registration_consumes_dispatch_volume (decision.volume≠DispatchResult.volume 时记账取后者).

### ReB-20260807-DISPATCH_OBSERVATION_COUPLING
- **Pattern Signature**: `DISPATCH_OBSERVATION_COUPLING`
- **Date Cataloged**: 2026-08-07
- **Source Docket**: DQAF-20260807-003
- **Related**: FIX-20260807-003 (RESOLVED — 执行令 2a The Dispatch Truth), DQAF-20260708-003 (close_price 伪造 — 同族观测污染物理判定)

**定义**: 一个物理动作的结果布尔量 (order dispatched) 与一个观测量 (PnL 计算) 耦合在单一变量 — dispatch 成功后立即计算 PnL, PnL 失败则把"已发出的网络请求"标记为未派发 → 管理循环不确认终结, 下游重发/失管. 关键签名: (1) `_close_dispatched` 赋值依赖 PnL 观测路径无异常; (2) 派发 (物理) 与观测 (PnL) 不同生命周期 (派发立即成功, PnL 需 MT5 deal 异步到达); (3) 观测失败反噬物理结果.
- **预防** (IMPLEMENTED): 物理动作结果与观测结果必须解耦 — "Dispatched" 的定义只能是"订单已成功投递到 Bridge 网络 socket" (物理定义); PnL 是独立观测, 不得门控派发结果. 状态机语义分层: dispatch (物理动作) → settlement (观测), 中间态不得混用单一布尔量.
- **检测**: 搜索赋值 _dispatched/dispatched 的地方, 确认其不在同一 try 块内依赖后续观测计算; 回归锁 test_close_dispatched_on_dispatch_success_while_pnl_fails (PnL 抛错仍 _close_dispatched=True).

### ReB-20260807-PARTIAL_FILL_BLINDNESS
- **Pattern Signature**: `PARTIAL_FILL_BLINDNESS`
- **Date Cataloged**: 2026-08-07
- **Source Docket**: DQAF-20260807-003
- **Related**: FIX-20260807-003 (RESOLVED — 执行令 2b Partial Fill State Machine), DQAF-20260709-002 (BROKER_STATE_NOT_CONSULTED_BEFORE_UNTRACK — 同族 broker 状态不查)

**定义**: 终结状态机以**意图** (pending_close 锁 / known_open_tickets) 而非 **broker 物理状态** (MT5 残余成交量) 推断仓位是否已平 — 平仓指令部分成交 (residual>0) 后, 盲等锁不感知, 下一周期不重发 close → 半仓裸露 50 分钟 (实证: 旧 pending_close 盲等逻辑). 关键签名: (1) 终结判定读意图状态而非 broker; (2) MT5 residual 与 intent 期望成交量有差异时无追平路径; (3) 锁 (pending lock) 无 partial-fill 感知.
- **预防** (IMPLEMENTED): (1) 终结状态机必须感知 partial fill — `_probe_mt5_residual` + `_is_partial_fill` (residual < expected_remaining_volume) → 下一周期立即重发 close; (2) sync_position_volume 只降 pos.volume, 保持 expected_remaining_volume 为 full-close 目标 (target 不动, 只修正已成交部分); (3) pending lock 在残余>0 时保持激活, 绝不在残余未清时放行下一状态.
- **检测**: 平仓 dispatch 后下周期核对 MT5 residual 与 intent 目标; 回归锁 tests/runtime/test_partial_fill_state_machine.py (13 测试: residual 探测/partial-fill 判定/重发 close/残余保留).

### ReB-20260807-TREND_CHASE_NO_POSITION_GATE
- **Pattern Signature**: `TREND_CHASE_NO_POSITION_GATE`
- **Date Cataloged**: 2026-08-07
- **Source Docket**: DQAF-20260807-002
- **Related**: FIX-20260807-002 (RESOLVED — 4e Spatial Z-Score Gate, Option A+C), trend_isolation_gates.py (4aa/4b/4c/4d), FIX-20260701-206 (4c ADX config thresholds), DQAF-20260630-198 (4c thermal fuse origin)

**定义**: 一个趋势追家族只有方向性闸门 (ADX 强度、多TF 一致、counter-trend、z 拐点), 却没有任何价格位置闸门 — 当 ML 动量特征在区间极值处峰化时, 结构上"在震荡区最高点开多、最低点开空". 关键签名: (1) 唯一的 z 价格位置校验 (check_z_inflection) 被策略白名单硬限制在 statarb/ou, 排除整个 swing 家族; (2) 全历史数据呈现非对称证据 — LONG H1_z>+1.5 桶 25.7% 胜 / −49.29 (最差), SHORT H1_z<−1.0 桶 44.3% 胜 / +99.36 (盈利) → 多头顶部接盘是主要出血点, 空头低位追空反而可行.
- **预防** (IMPLEMENTED, IC 雷霆裁决 Option A+C, 2026-08-07): 空间位置门禁必须与方向门禁正交并存. (1) **非对称设计** — Long 高位 (H1_z>+1.5) → 硬否决 (绝不接盘), Short 低位 (H1_z<−1.5) → 仅 volume 降额 (×0.5, 数据证明 sell-low 仍盈利); (2) **ranging 耦合** — detected_regime∈(ranging/chop) 时阈值收紧 ±1.5→±1.0 (区间极值最危险处最严格); (3) **数据接入用 schema registry 动态索引** (v9_institutional_40 H1_Price_ZScore), 禁止硬编码索引位; (4) **fail-open 契约** — 缺失/非有限 z-score → 放行 (数据缺口不得制造新阻塞), 仅在有效数据时收紧. 红线: 8/19 前禁止 Option B 类大周期回撤进场 (改变模型行为, 污染积累测试数据).
- **检测**: 每笔实盘开单校验其 H1_Price_ZScore 相对方向阈值契约 (LONG 必 z≤+1.5, SHORT 必 z≥−1.5, ranging 更严 ±1.0); 审计脚本 scripts/_audit_entry_timing_20260807.py 全历史桶分析 (long z>1 桶胜率/总盈亏); intent log `spatial_zscore_gate` 事件: `spatial_zscore_long_blocked` (硬断) / `spatial_zscore_short_degraded` (降额) 计数; 任何新策略加入趋势追家族必须同时评估空间位置闸 (Iron Law #5 同模式搜索).

### ReB-20260807-TREND_CHASE_FAILOPEN_LOW_EDGE
- **Pattern Signature**: `TREND_CHASE_FAILOPEN_LOW_EDGE`
- **Date Cataloged**: 2026-08-07
- **Source Docket**: DQAF-20260807-001
- **Related**: FIX-20260807-001 (RESOLVED — Option A Chop Filter), DQAF-20260806-003 (deadband ramp origin), TECH_DEBT-010 (Option C cold-explore, DEFERRED 8/19 后)

**定义**: 一个带健康度/模式信号的入场闸门以 "NEVER blocks outright (fail-open)" 为教条, 对 Defensive/choppy 状态只缩量不硬断 → 低健康期仍过度开单 (08-06 09:44Z health=0.52 首单成交, 三单全 SHORT "-$144 学费"). 与 BTC 侧的区别: 乘数斜坡 (`_gods_eye_health_vol_mult`) 只让 volume 收敛到 floor 上, 从不把 decision 打成零 → 风控底线在闸门层缺失, 依赖 Ω 终门兜底 (但终门只在 volume 跌破 floor 时拦, 健康态恰在 floor 之上).
- **预防**: (1) 对 Defensive/choppy 状态必须存在硬否决路径 (`should_trade=False + volume=0`), 显式状态 BLOCKED_BY_GODSEYE 向下游传递, 不得仅依赖缩放; (2) 闸门语义分层: fail-closed (拒单) 用于风险状态, fail-open (放行) 仅用于数据不足/可观测性缺口, 绝不可用于已确认的风险状态; (3) 修复层级 ≥ 根因层级 — 闸门契约 (L2) 必须改契约本身, 不能靠下游 Ω 门兜底.
- **检测**: `data/logs/intent_*.log` 中 `gods_eye_blocked_entry` 事件计数 + `gods_eye_health`/`chop_detected` 字段审计; 对每笔实盘开单校验其 `gods_eye_health ≥ 0.55 AND chop=False` 契约; 定期扫描 XAU/BTC 首单 health 分布 (低健康放行 = 红旗).

### ReB-20260807-ZOMBIE_ESCAPE_NO_CORPSE
- **Pattern Signature**: `ZOMBIE_ESCAPE_NO_CORPSE`
- **Date Cataloged**: 2026-08-07
- **Source Docket**: DQAF-20260807-001
- **Related**: FIX-20260807-001 (RESOLVED — unconditional enqueue + field fallback), DQAF-20260726-012 (GHOST_BOOTSTRAP_RESTORE_MUTUAL_EXCLUSION — 同族幽灵仓位), DQAF-20260709-002 (BROKER_STATE_NOT_CONSULTED_BEFORE_UNTRACK)

**定义**: 一个仓位状态机有两条并行写入路径 — 主路径 (known_open_tickets → settlement → PnL corpse) 与桥接直写路径 (bridge-direct journal → position_manager 但不进 known_open_tickets). 当老化清理 (pre_mgmt_zombie_cleared) 的 settlement enqueue 以 `_z_open is not None` 为 gate 时, 桥接直写逃逸的持仓在 `_z_open=None` 分支下只 `clear_position()` 不 enqueue → 仓位从 MT5 消失却永远无 PnL 尸体 → 资金对账永久缺口 (实证 m30 4448694178, settlement queue 空).
- **预防**: (1) "任何退出 MT5 的持仓必须留下 PnL 尸体" 设为不变量 — settlement enqueue 只 guard 队列存在性, 不 guard 仓位字典 (enqueue 字段可空, 尸体仍可写); (2) 多写入路径必须收敛到单一注册点 (known_open_tickets), 桥接直写也必须注册 — 至少用快照字段 (position_manager) 回退重建 settlement 入队; (3) zombie 清理路径的每一条 clear_position 都必须有对偶 settlement 动作 (corpse 强制写).
- **检测**: `execution_state.json` pending_settlement_tickets 与 position_manager 快照 + journal 最近平仓票号三方对账; 出现 "已平仓票号不在 settlement 队列也不在 journal corpse" 即逃逸红旗 (Iron Law #11 脚本: position_ticket 严格去重对账).

### ReB-20260806-THRESHOLD_RESONANCE_VOLUME_SHAVE
- **Pattern Signature**: `THRESHOLD_RESONANCE_VOLUME_SHAVE`
- **Date Cataloged**: 2026-08-06
- **Source Docket**: DQAF-20260806-003
- **Related**: FIX-20260806-007 (RESOLVED — Option A regime_map + Option B2 deadband ramp), FIX-20260730-010 (Ω Final Settlement Gate origin), DQAF-20260804-004 (partial capture, "静候恢复" premise later falsified), TECH_DEBT-010 (Option C cold-explore chain, DEFERRED 8/19 后)

**Definition**: A downstream multiplier sits on top of a volume that has already converged to a hard floor (min_economic 0.02), and its default/reduced branch multiplies that floor-bound value below the floor → the final Ω gate then KILLs the order even when every upstream signal is healthy. Signature: (1) an upstream chain structurally floors volume (regime reduced ×0.65 → lot_step floor-round → trend_maturity_discount floor 0.40 → 2nd floor-round = pinned at 0.02); (2) a contract-violating multiplier (`max(0.25, health)` at strategy_evaluator.py:1108-1110) applies AFTER the floor-convergence and BEFORE the Ω gate, shaving 0.02 → 0.0102-0.0175; (3) the gate default (`gates.get(strategy_name, "reduced")` at regime_gate.py:815) silently mis-tags unmapped strategies as reduced. The 8/04 "wait for health recovery" premise (health×cm>0.70 unlocks) was falsified by E2b: health=0.875 × cm=1.10 = 0.963 ≫ 0.70 yet still KILL — proof the volume was structurally pinned, not health-gated.

**Prevention** (IMPLEMENTED, IC Approved 2026-08-06): (1) **Option A** — regime_map completeness: every strategy must have an explicit mapping in live.yaml for all regimes (mirror live_btc.yaml), eliminating the silent default-reduced fallback. (2) **Option B2** — Deadband + Proportional Control transfer function replaces `max(0.25, health)`: health ≥ 0.70 → 1.0 (zero intervention in the healthy band), 0.25-0.70 → continuous linear ramp, ≤ 0.25 → clamp 0.25 (worst-case floor preserved). B1 (mode-conditional flat 1.0) rejected — kills the continuous-micro-adjustment contract; B3 (multiplier-floor) rejected — floor-on-floor code smell violating FIX-20260730-010 single-gate philosophy. Rule: a floor-converged volume must not pass through a shaving multiplier before the terminal gate; if it must, the multiplier needs an explicit deadband.

**Detection**: (1) Grep for `gates.get(` with a default in regime gate code — every strategy must be explicitly keyed. (2) For any volume multiplier applied before the Ω floor gate, verify a deadband ≥ the multiplier's no-intervention threshold exists. (3) Regression lock `tests/runtime/test_strategy_evaluator.py::TestGodsEyeHealthDeadbandRamp` — 4 pure-function tests (deadband/ramp/clamp) + 2 E2E (healthy 0.875 survives Ω gate at 0.02; degraded 0.30 still KILLs). (4) Monitor: `min_economic_volume_blocked` rate in intent log should drop to ~0 at healthy GodsEye states post-deploy.

---

### ReB-20260805-EXPIRED_TEMP_GATE_UNRETIRED
- **Pattern Signature**: `EXPIRED_TEMP_GATE_UNRETIRED`
- **Date Cataloged**: 2026-08-05
- **Source Docket**: DQAF-20260805-002
- **Related**: FIX-20260805-008 (RESOLVED), FIX-20260611-005 (temp patch origin), FIX-20260613-089 (threshold desensitization), DQAF-20260619-002 (detection-defect fixes)

**Definition**: A temporary patch/gate declares a contract with a future state ("auto-expires YYYY-MM-DD; after Phase N lands these become structural guarantees, not runtime audits") but the retirement is encoded ONLY as a message string with zero retirement logic. When the future state lands, the check keeps running on its old premise — counting the new architecture's expected behavior as a violation → false CRITICAL on every re-fire trigger (here: every restart). The signature: an expiry date that exists only in a formatted string / docstring, with no code path that retires the check or upgrades its semantics when its declared precondition is met. The FIX-20260611-005 temp patch expired 2026-07-11 (25 days overdue at diagnosis) yet kept FAILing on 123 retry-residue dupes using the pre-Phase-2 (ticket, ack_status) key.

**Prevention** (IMPLEMENTED): Temp-gate retirement must be LOGIC, not text. When the declared precondition is satisfied, upgrade the check to consume the new architecture's identity contract — here, the Phase 2 idempotent event key `(position_identifier, deal_id)`. Expected-by-design residue (retry re-writes of the same event) becomes a metric, never a FAIL; genuine divergence (≥2 distinct non-zero deal_ids for one position) becomes the FAIL signal. Hard-deadline tech debt (`TODO-YYYYMMDD-*`) must be settled or re-scoped AT the deadline, never silently left as a stale string. Retained structural gates (close_price fill rate, trail coverage) keep their FAIL thresholds.

**Detection**: Grep for `[EXPIRES ` / `auto-expire` / `TODO-\d{8}-` inside check/alert code with no associated retirement branch. Regression lock: `tests/observability/test_journal_completeness_phase2.py` asserts the expired framing (`EXPIRES`/`expires`) is absent and the new idempotent-key semantics hold (residue→PASS, ambiguity→FAIL, deal-0 rejected-then-confirmed→not ambiguous, orphan exclusion, unidentifiable metric).

---

### ReB-20260806-LABEL_PRODUCER_SWAP_SILENT_AMNESIA
- **Pattern Signature**: `LABEL_PRODUCER_SWAP_SILENT_AMNESIA`
- **Date Cataloged**: 2026-08-06
- **Source Docket**: DQAF-20260806-001
- **Related**: FIX-20260806-001 (RESOLVED, Option A), FIX-20260611-005 (Strangler Fig #11 producer swap), FIX-20260612-003 (trail-aware label on deprecated path), mia_close.py (trail-aware reference), DQAF-20260805-003 (probe follow-up chain)

**Definition**: A Strangler Fig replacement takes over a write path (here: PositionCloseAdapter became the sole close-event producer on 6/11 via FIX-20260611-005) but the DOMAIN-AWARE label contract of the old producer (trail-aware `sl_hit_trailed`, which landed one day later in FIX-20260612-003) is applied to the DEPRECATED path (reconciliation.py, restart-only `loop_iteration==1`) while the NEW producer silently hardcodes a coarse label (`sl_hit_first` at position_close_adapter.py:439-440, signature without `state`). The fix and the writer diverge in the same release window → telemetry goes dark (0× `sl_hit_trailed` for 8 weeks) with zero error. Distinguishing signature vs SEMANTIC_DRIFT_MONITOR_PROBE: the probe fix was about the OBSERVER; this is the PRODUCER swallowing domain signal at the write boundary. ECoL proof of the physical layer: broker-side SL trail executed perfectly (41/81 SL moved, 40/41 fills landed on the trailed SL) — only the observation label collapsed.

**Prevention** (IMPLEMENTED, Option A surgical): The active producer must read the same runtime state the domain contract keys on. `state.position_manager` → `get_position(ticket).trail_advances` threaded into `_build_event`/`detect_and_build` (mirror of reconciliation.py:198-204) + MIA `trail_contribution` fallback restoring mia_close.py:180-185 semantics lost in the swap. When a Strangler Fig replaces a writer, AUDIT the label-decision logic itself (not just the write API) and migrate the label contract in the SAME change — a swapped producer carrying a hardcoded label is a silent telemetry kill. **Deferred (Option C, 8/19 后)**: unify the three divergent label sources (adapter/reconciliation/mia_close) into one function — kills the 3-way semantic fork at the root.

**Detection**: (1) Grep the ACTIVE close-writer for `sl_hit_first` literal; verify it reads trail_advances or has no state param. (2) Regression lock `tests/runtime/test_position_close_adapter.py::TestTrailAwareSLLabel` — 6 tests weld the contract: trailed SL → sl_hit_trailed; no-trail → sl_hit_first; state=None → sl_hit_first (back-compat); watchdog comment priority; MIA trail_contribution fallback; detect_and_build end-to-end. (3) Monitor: journal `sl_hit_trailed` count should now rise from 0 as trailed SL exits occur; `TRAIL_TELEMETRY_BLINDSPOT` probe (FIX-20260805-009, inclusive match) stays honest.

---

### ReB-20260805-SEMANTIC_DRIFT_MONITOR_PROBE
- **Pattern Signature**: `SEMANTIC_DRIFT_MONITOR_PROBE`
- **Date Cataloged**: 2026-08-06
- **Source Docket**: DQAF-20260805-003
- **Related**: FIX-20260805-009 (RESOLVED), FIX-20260612-003 (label contract origin), DQAF-20260806-001 (follow-up: trail exit telemetry gap)

**Definition**: A monitoring/alerting probe keys on an EXACT literal value (here: the bare label key `"trail"`) that the underlying data contract later replaced with a versioned synonym (`sl_hit_trailed`, written by FIX-20260612-003). The probe's dictionary never catches up → the warning fires on every cycle even though the telemetry it claims is missing is actually healthy. Signature: a monitor condition that must be kept in sync with a producer-side label/token vocabulary, checked as exact-equality instead of contract-inclusive membership. A deeper trap surfaced during this fix: the probe scans a WINDOW (tail-500) while the "evidence" of health (2× `sl_hit_trailed`) lived outside that window — so after the vocabulary fix the warning persisted as an HONEST signal pointing to a genuine recent-window absence, which must be evaluated on its own (DQAF-20260806-001), not silenced.

**Prevention** (IMPLEMENTED): Monitor probes must match the producer's label CONTRACT inclusively (`any("trail" in k for k in labels)`), not an exact historical token. When a producer changes label vocabulary, grep the consumers/probes for the old exact token and update them in the same change. When verifying "telemetry is healthy", measure on the SAME window the probe scans (tail-500), never the full corpus — a full-corpus positive that is outside the probe window leaves the warning legitimately firing.

---

### ReB-20260806-TEST_TO_PROD_ALERT_LEAK
- **Pattern Signature**: `TEST_TO_PROD_ALERT_LEAK`
- **Date Cataloged**: 2026-08-06
- **Source Docket**: DQAF-20260806-002
- **Related**: FIX-20260806-005 (RESOLVED, Option C), FIX-20260805-006 (DingTalk keyword unblock — the trigger that surfaced this leak), DQAF-20260806-001 (same day, unrelated)

**Definition**: A production alerting path (here: `_alert_violation()` → `LiveAlertHub(base_dir="data").send_critical()` in core/contracts/phantom_contract.py) is invoked by TEST code that deliberately triggers the guarded condition to exercise the mechanism. Because the alert path has zero awareness of its caller domain, the test violations traverse the FULL production channel (env-wired DingTalk webhook) and push real CRITICAL alerts into the live ops group on every test run. The leak is masked while delivery is broken (errcode=310000 keyword rejection) and becomes VISIBLE the moment delivery is fixed — fixing the "sewer" (keyword) reveals the test water already leaking into it. Signature: a hardcoded live `base_dir="data"` in a library-adjacent alert helper + auto-wired webhook from process env + tests that intentionally violate the contract. Auditable proof: `phantom:test_*` contract_ids in the alert stream match test-fixture ids verbatim, never production predicate ids.

**Prevention** (IMPLEMENTED, Option C — test-domain isolation, zero production change): Disarm at the TEST boundary, never pollute production with test-awareness. (1) Module autouse fixture makes `LiveAlertHub.__init__` raise `ImportError` so `_alert_violation` falls into its existing stderr branch (counter semantics preserved, hub/thread/audit-log side effects eliminated). (2) `tests/conftest.py` global autouse `delenv` of `QUANTOS_DINGTALK_WEBHOOK_URL`/`QUANTOS_DINGTALK_SECRET`/`QUANTOS_SLACK_WEBHOOK_URL`/`SLACK_WEBHOOK_URL` physically blinds the whole test domain (belt-and-suspenders). Rejected Option A (production `if "pytest" in sys.modules`) as Test-Induced Design Damage — production code must never know it is being tested.

**Detection**: (1) Grep alert-injection helpers for hardcoded `base_dir="data"` + env-wired channels — each is a leak candidate. (2) Check alert_audit.jsonl for `phantom:*` or `test_*` rule_names whose contract_ids don't exist in the production predicate registry. (3) After any alert-DELIVERY fix (keyword/webhook), audit the audit log for previously-silent test-origin alerts. (4) Regression lock: `tests/contracts/test_phantom_contract.py` autouse fixture + `tests/conftest.py` global delenv (audit-log line count must not grow when the phantom suite runs).

**Detection**: Grep check/alert code for exact-string membership tests (`X not in dict`) against label/token vocabularies produced elsewhere; flag when the producer writes a compound/variant token. Regression lock: `tests/observability/test_trade_journal_trail_probe.py` asserts inclusive-match semantics (sl_hit_trailed present → no warning; genuinely absent → warning retained; below close-count threshold → no warning).

---

### ReB-20260805-HASHLOCK_STAT_PHANTOM
- **Pattern Signature**: `HASHLOCK_STAT_PHANTOM`
- **Date Cataloged**: 2026-08-05
- **Source Docket**: DQAF-20260805-001
- **Related**: FIX-20260805-007 (RESOLVED), FIX-20260805-005 (LF contract double-insurance)

**Definition**: A process rewriting a tracked file with byte-identical content (mtime bump only) desynchronizes git's stat cache → `git status --porcelain` reports a persistent phantom ` M` that never self-heals (`git update-index --refresh` fails; only `git add` clears it), even though the content hash == index blob. Any stat-based gate (`git status --porcelain` + extension filter) then false-positives on content-equivalent files. The signature: a "dirty tree" check built on METADATA (stat) instead of CONTENT (blob diff).

**Prevention** (IMPLEMENTED): Hash-lock gates must compare WORKTREE to HEAD by CONTENT — `git diff HEAD --name-only` (immune to stat phantoms AND CRLF pseudo-diffs, which git's clean filter normalizes away) + `git ls-files --others --exclude-standard` for untracked source (blocking, minus `_audit_*.py` forensic probes). Three sites synchronized under the "never drift" contract: `_enforce_hash_lock` (canonical train_btc_expected_r_institutional), train.py inline copy, daily_flow46_precheck hash_lock. The write-side LF contract (FIX-20260805-005, `atomic_write_text newline="\n"`) removes the CRLF pseudo-diff source entirely — belt and suspenders.

**Detection**: Automated regression — `tests/training/test_hash_lock_content_gate.py` (throwaway git repo: content-identical rewrite + mtime bump must NOT block; real semantic change must block; `_audit_*.py` untracked probe must NOT block; non-probe untracked source must block; gitignored data/ must never block). Grep for `git status --porcelain` used as a dirty predicate in automation.

---

### ReB-20260803-XAU_CENTRIC_HARDCODED_GLOBAL_THRESHOLD
- **Pattern Signature**: `XAU_CENTRIC_HARDCODED_GLOBAL_THRESHOLD`
- **Date Cataloged**: 2026-08-03
- **Source Docket**: DQAF-20260803-001
- **Related**: TECH_DEBT-006, FIX-20260803-001 (RESOLVED)
- **✅ RESOLVED 2026-08-03**: FIX-20260803-001 — per-symbol floor implemented. `StrategyLineConfig.min_economic_volume` field + `resolved_min_economic_volume` property (explicit config wins; BTC→base_volume floor 0.01, others→2×lot_step 0.02); strategy_builder `_cfg` passthrough (20 lines) + `_validate_min_economic_floors()` static cross-symbol validation (warning-only, RuleEngine dict-config skipped); strategy_evaluator final settlement gate reads per-strategy floor (RuleEngine dict-config guard → XAU fallback 0.02). live_btc.yaml 6 strategy lines explicitly declare `min_economic_volume: 0.01`. **Global downgrade vetoed by IC** (0.02→0.01 would strip XAU's breakeven floor).

**Definition**: A per-symbol operational threshold (MIN_ECONOMIC_VOLUME=0.02, derived from XAU lot_step) hardcoded as the global-only volume floor in the pipeline, structurally barring another symbol (BTC, lot_step 0.01, base_volume 0.01) from trading even at full health. The signature: a global constant whose derivation comment names one specific symbol ("For XAU: lot_step=0.01, 2× lot_step"), yet is applied unconditionally to all assets. The affected symbol's standard trade size sits below the floor by design, so any degradation factor (GodsEye health `max(0.25, health)`, session multiplier) pushes volume under the floor → protective zero-open (08-03: BTC 0.0033 / XAU 0.0066 both killed at the final settlement gate).

**Prevention** (IMPLEMENTED): Symbol- and strategy-specific operational thresholds must be config-driven (live.yaml strategy_configs), defaulting from the symbol's own lot_step/base_volume — never hardcoded globally. Single resolution point: `StrategyLineConfig.resolved_min_economic_volume` (SSOT). Static cross-symbol validation: `strategy_builder._validate_min_economic_floors()` flags any strategy whose `base_volume < min_economic` (warning-only — floor may be intentional/documented per IC ruling).

**Detection**: Grep for global constants whose derivation comment names a single symbol. Static audit: for every enabled strategy line, flag any asset where `config.base_volume < _MIN_ECONOMIC_VOLUME` — that asset is structurally un-tradeable at standard size regardless of health. Now automated by `_validate_min_economic_floors()`.

---

### ReB-20260726-GHOST_BOOTSTRAP_RESTORE_MUTUAL_EXCLUSION
- **Pattern Signature**: `GHOST_BOOTSTRAP_RESTORE_MUTUAL_EXCLUSION`
- **Date Cataloged**: 2026-07-26
- **Source Docket**: DQAF-20260726-012
- **Related**: FIX-20260726-012 (supersedes DQAF-20260710-003's conditional guard)

**Definition**: A startup safety mechanism with redundant paths (belt-and-suspenders) where the two paths are mutually exclusive rather than complementary. When path A (execution_state restore) succeeds, path B (journal bootstrap) is unconditionally skipped via a guard condition. If path A has incomplete data (e.g. doesn't contain a position that was cleaned up on a previous restart), the gap is never detected because path B never runs to fill it. The signature: `if condition_A_succeeded: skip condition_B` where B should instead merge with A's results.

**Prevention**: Defensive startup guards should use MERGE semantics (additive union) rather than REPLACE semantics (either-or). Each protective path should contribute its findings to the shared state without suppressing other paths. Specifically: `if should_run_bootstrap(): merge_bootstrap_results_into_existing_state()` — never `if state_is_empty: run_bootstrap()`.

**Detection**: Grep for startup guards with `not state.*` conditions that gate protective scans. The pattern `if loop_iteration == 1 and not state.X:` where X is populated by a parallel restore path is a candidate.

---

### ReB-20260724-CIRCUIT_BREAKER_ANCHORED_TO_SYNTHETIC_CFD_PSEUDO_METRIC
- **Pattern Signature**: `CIRCUIT_BREAKER_ANCHORED_TO_SYNTHETIC_CFD_PSEUDO_METRIC`
- **Date Cataloged**: 2026-07-24
- **Source Docket**: DQAF-20260724-001
- **Related**: FIX-20260724-001 (supersedes FIX-20260719-001 item 1)

**Definition**: A safety-critical circuit breaker (hard block on all swing/trend entries) is anchored to a CFD broker's synthetic `tick_volume` metric. CFD tick volume is an artificial construct with burst-decay distribution and frequent identical consecutive values — it does not measure real market activity. When combined with an inclusive-window z-score computation (current bar included in baseline μ/σ), every cycle produces a negative z-score → the circuit breaker becomes a permanent trade blockade. The signature: a gate metric that is structurally unable to produce positive values (94% non-positive over 39,714 records spanning ~80 days), yet is the sole signal for an all-strategies hard block.

**Prevention Strategy**:
1. All circuit breakers that block real capital must be anchored to **price-action-derived metrics** (ATR, Bollinger Band Width, high-low range) — never to volume proxies from CFD brokers.
2. When adding a new hard gate, require **distributional validation**: sample ≥10,000 historical values and verify the gate metric produces both positive and negative values in rough proportion (|skew| < 2.0).
3. Z-score computations should use **exclusive windows** (`window = data[-lookback-1:-1]`) to avoid Mean Drag on the current observation — but this alone does not fix a structurally broken data source.

**Detection Method**: 
- Automated: `scripts/audit_gate_metrics.py` — for each gate metric, compute positivity rate over trailing 10,000 cycles. Alert if positivity rate < 10% or > 90%.
- Manual: Check golden_master.jsonl for gate block reason frequency. If a single reason accounts for >80% of blocks, investigate the gate metric's distribution.

### ReB-20260718-ORPHAN_CASCADE_DELETE_MISSING
- **Pattern Signature**: `ORPHAN_CASCADE_DELETE_MISSING`
- **Date Cataloged**: 2026-07-18
- **Source Docket**: DQAF-20260718-001
- **Related**: FIX-20260718-001

**Definition**: A journal compaction/pruning operation removes parent records (orphan opens) but does not cascade-delete child records (synthetic closes). The synthetic closes, created by an earlier cleanup step (`cleanup_orphan_opens()`), reference the parent open via a foreign key (`open_message_id`). When compaction prunes the parent open by age, the child close survives — leaving orphaned synthetic entries that pollute the journal indefinitely. The signature: synthetic entries with `label` starting with `auto_orphan_*` whose `open_message_id` no longer exists in the journal.

**Prevention**: Any compaction operation that removes records must identify and cascade-delete child records in a second pass. The child records reference their parent via a foreign key field (`open_message_id`). The first pass collects pruned parent IDs; the second pass filters out children matching those IDs.

**Detection**: Count `auto_orphan_*` labeled entries whose `open_message_id` is not found in the journal — these are orphaned synthetic closes. The `compact_journal()` function now returns `cascade_removed` in its result dict for monitoring.

### ReB-20260718-SINGLE_ASSET_HARDCODED_PATHS
- **Pattern Signature**: `SINGLE_ASSET_HARDCODED_PATHS`
- **Date Cataloged**: 2026-07-18
- **Source Docket**: DQAF-20260718-002
- **Related**: FIX-20260718-002, Iron Law #14 (Brain Status SSOT Priority)

**Definition**: A cross-asset operation (reconciliation, governance, health checks) hardcodes paths for a single asset, silently excluding other assets. The caller (timed cron job) invokes the operation without passing asset context, even though the caller already has the asset context in scope (`base_dir` contract). The result: the operation works for one asset and silently no-ops for others — no error, no log, just silent exclusion.

**Prevention**: Any operation that touches asset-specific data paths must be parameterized by asset context (`data_dir`, `brains_subdir`, `live_config_name`). Callers MUST derive these from the existing `base_dir` contract rather than hardcoding. The `base_dir` naming convention (`data` → XAU, `data_btc` → BTC) is the SSOT for asset routing.

**Detection**: Verify that `daily_ops.py` SSOT reconciliation call site passes asset-derived parameters. Cross-check: run `brain.py reconcile --data-dir data_btc --brains-subdir brains_btc --live-config live_btc.yaml` and confirm BTC governance states are reconciled.

### ReB-20260718-SILENT_GATE_BYPASS_ZERO_OBSERVABILITY
- **Pattern Signature**: `SILENT_GATE_BYPASS_ZERO_OBSERVABILITY`
- **Date Cataloged**: 2026-07-18
- **Source Docket**: DQAF-20260718-003
- **Related**: FIX-20260718-003, FIX-20260625-136 (OU config archival)

**Definition**: A quality gate has a bypass/passthrough path that is taken when its configuration source is absent (e.g., all brain configs for the gate type are archived). The passthrough path has zero logging, zero metrics, and zero diagnostic exposure — making it impossible to detect that a class of signals is ungoverned. The telltale: a gate's `describe()` method reports empty configuration arrays, but no warning is emitted at runtime.

**Prevention**: Every gate bypass path must include: (1) throttled WARNING logging with the reason for bypass, (2) a counter tracking bypass events by reason, and (3) a `describe()` or status method that exposes bypass statistics for monitoring integration. The logging must be throttled (e.g., every N cycles) to avoid log flooding.

**Detection**: Check gate `describe()` output for `passthrough_diagnostics` section. If `total_cycles > 0` but no WARNING log lines appear in the application log, the bypass is unobservable. Monitor `_passthrough_count` by reason to detect configuration gaps.

---

### ReB-20260710-TP_TRAIL_NO_PROFITABILITY_GATE
- **Pattern Signature**: `TP_TRAIL_NO_PROFITABILITY_GATE`
- **Date Cataloged**: 2026-07-10
- **Source Docket**: DQAF-20260710-001
- **Related**: FIX-20260710-002, FIX-20260603-064 (trail_activation_atr — the proven SL-side pattern)

**Definition**: A trailing mechanism operates on a pure volatility signal (ATR contraction) without checking whether the position has ever seen favourable price movement. The sibling mechanism (SL trail via `compute_trail_stop`) correctly enforces a `trail_activation_atr` profitability watermark, but the TP trail (`compute_trail_tp`) lacks an equivalent guard. The asymmetry means: on a losing position, ATR contraction (market calming down while you are wrong) triggers TP tightening — bringing the take-profit target closer to entry, making it HARDER to recover if the market reverses. The telltale: repeated `modify_sltp` with `comment='tp'` on a position that has never been profitable, verified by `lowest_low >= entry_price` (SHORT) or `highest_high <= entry_price` (LONG).

**Prevention**: Any trailing mechanism that tightens a bracket toward entry MUST gate on profitability before activating. The minimal gate mirrors the `trail_activation_atr` pattern: `highest_high <= entry_price` → suppress (LONG), `lowest_low >= entry_price` → suppress (SHORT). The fields `highest_high`/`lowest_low` are already maintained per-cycle by `_update_single_position()`. When adding a new trail mechanism, audit that BOTH SL and TP sides have symmetric profitability gates — asymmetry is the signature of this defect class.

**Detection**: Check `live_trade_journal.jsonl` for modify_sltp actions with `comment='tp'` on positions where `breakeven_triggered=false` AND `cycles_held > hesitation_cycles`. Also: positions with a high modify_sltp count (>10) but zero trail_sl modifications (SL unchanged) indicate TP-only trailing — a red flag if the position closed as a loss. Unit test pattern: `test_compute_trail_tp_suppressed_when_never_profitable` (LONG and SHORT variants).

**Cross-References**: FIX_REGISTRY.md FIX-20260710-002, DQAF_DOCKET_REGISTRY.md DQAF-20260710-001, CCT_LEDGER.md CCT-20260710-001

### ReB-20260708-PROFIT_RATCHET_NEVER_REACHES_BROKER
- **Pattern Signature**: `PROFIT_RATCHET_NEVER_REACHES_BROKER`
- **Date Cataloged**: 2026-07-08
- **Source Docket**: DQAF-20260708-004
- **Related**: FIX-20260708-004, FIX-20260707-009 (bracket inversion — the 2.5-4% tail, NOT the main cause), FIX-20260603-064 (activation watermark), DQAF-20260609-001 (nonlinear decay)

**Definition**: A profitable position reaches a meaningful peak (+1.4R..+6.3R) but the trailing stop never places a POSITIVE floor at the broker, so a model exit (signal_close) realises ~$0 at breakeven, or a retracement hits the original SL. The give-back is dominated (87-89% of the cohort) by "trail never locked", NOT by bracket inversion (which was only 2.5-4%). The trailing calculator has three coupled holes: (1) it returns `None` when the raw candidate does not advance — so on a cycle where the trail cannot tighten, NO floor is applied at all; (2) its candidate is priced off `current_atr`, which balloons in volatile moves and pushes the goalpost away from a positive lock in the +1R..+1.5R band; (3) its breakeven floor depends on an intent-latch (`breakeven_triggered`) that is set even when the modify was feasibility-skipped or broker-rejected, so once latched the floor logic silently disengages. A telltale is a flood of `NO_CHANGES` (retcode 10025) modify rejections — the engine re-sends an SL the broker already holds because local state is stale. The trap: a prior fix (bracket inversion) addressed a small tail and looked like "the" fix, masking that the dominant cohort never had a floor at all.

**Prevention**: A monotonic profit ratchet inside the trail calculator that is (a) measured against a STABLE goalpost (`entry_atr`, not `current_atr`), (b) armed off the monotonic peak (`highest_high`/`lowest_low`) so it can only ratchet up, (c) applied via `max()/min()` into the candidate so it FIRES EVEN WHEN the raw trail returned `None`, and (d) INDEPENDENT of any intent-latch. Because the lock is monotonic, the existing `min_step` guard suppresses `NO_CHANGES` resends. A broker-bound positive floor physically caps how far price can retrace before the stop catches it, so a downstream model exit is structurally forced to close at a protected level — no separate "give-back guard" is needed (avoid duplicating a give-back mechanism that already exists as governed shadow infra, e.g. V6 RatchetRisk). Diagnostic rule: when auditing a give-back, reconstruct the per-ticket lifecycle (snapshots + opens + modifies + closes) and attribute the DOMINANT failure mode before fixing — do not let a small, already-fixed tail (bracket inversion) masquerade as the root cause.

**检测方法**: `scripts/_diagnose_giveback_lifecycle.py` — per-ticket give-back attribution (MODE_A modify_rejected / MODE_B trail_never_locked / MODE_C model_exit_at_be / MODE_D tp_released). In production, watch `management_phase_diag.ratchet_floor_r` — it should become non-zero once a position's peak arms the ratchet; a give-back cohort with `ratchet_floor_r == 0` at high MFE means the ratchet did not arm. Watch modify-reject retcode histograms for a 10025 (NO_CHANGES) spike = trail re-sending an unchanged SL.

**Cross-References**: FIX_REGISTRY.md FIX-20260708-004, DQAF_DOCKET_REGISTRY.md DQAF-20260708-004, CCT_LEDGER.md CCT-20260708-004

### ReB-20260708-BLIND_DEAL_INDEX_FABRICATES_BREAKEVEN
- **Pattern Signature**: `BLIND_DEAL_INDEX_FABRICATES_BREAKEVEN`
- **Date Cataloged**: 2026-07-08
- **Source Docket**: DQAF-20260708-003
- **Related**: FIX-20260708-003, FIX-20260601-046 (label_builder 盲取 closes[0] 同类), FIX-20260612-004 (bridge deal.profit capture)

**Definition**: Code that resolves a position's close from an MT5 `history_deals_get()` list by positional index (`deals[0]` / `_new_deals[0]`) instead of filtering by the deal role flag (`entry == 1` = DEAL_ENTRY_OUT). The earliest deal is the DEAL_ENTRY_IN **opening** deal, which carries `price == entry_fill` and `profit == 0`. Selecting it fabricates a break-even close AT THE ENTRY PRICE — `close_price == entry_price`, `pnl == 0`, `label == breakeven` — for every full close. The fabrication is silent: it corrupts governance WR/PF and the calibrator, and hides real wins/losses (a give-back that hit SL is recorded as $0). Aggravating factor: the same MT5-deal knowledge was implemented three times (adapter wrong; reconciliation and mia_close correct), with no upstream invariant forcing all paths to agree — so one copy could silently diverge.

**Prevention**: A single deal-selection SSOT (`core/runtime/deal_selection.py::resolve_exit_deal()`) that is the ONE place encoding the MT5 deal model. It filters `entry == 1`, prefers SL/TP-reason deals, aggregates exit-deal profit, and — critically — returns a `no_exit_deal` provenance with `close_price == None` when no exit deal exists, so callers can NEVER fall back to the entry deal's price. All close-detection paths (adapter/reconciliation/mia_close) call it. Every close carries `close_price_source` / `pnl_status` provenance, making any future fabrication self-declaring in the journal. New MT5-deal consumers must call the SSOT, never index a raw deal list.

**Detection**: `scripts/backfill_fabricated_breakeven.py` — fingerprint `label==breakeven ∧ pnl∈{0,None} ∧ close_price==entry_price ∧ _close_price_source≠mt5_exit_deal`. Runs read-only (Iron Law #11) per symbol. Ongoing: any close whose `_close_price_source == "no_exit_deal"` is an anomaly to alert on. Unit guard: `tests/runtime/test_deal_selection.py` locks "never resolve close from the opening deal".

### ReB-20260628-GOVERNANCE_REGISTRATION_SILENT_SKIP
- **Pattern Signature**: `GOVERNANCE_REGISTRATION_SILENT_SKIP`
- **Date Cataloged**: 2026-06-28
- **Source Docket**: DQAF-20260628-061
- **Related**: ReB-20260628-CONFIG_GOVERNANCE_DUAL_TRACK_DRIFT, FIX-20260529-035 (`P0.1 State Injection`)

**Definition**: A data injection method (`set_performance_metrics()`) accepts data for any brain_id but silently discards it when the brain doesn't exist in `_brain_states`. The caller (PnLStore/metrics pipeline) has no way to know the injection failed — no return value, no exception, no log. Combined with a second factor (field name mismatch `source` vs `_data_source` causing trusted metrics to be purged), this creates a "silent disconnect" where rich data exists in the data layer but never reaches the governance layer.

**Prevention**: Auto-registration gate before injection. Every `set_performance_metrics()` call site must be preceded by `if not governance.get_brain_state(brain_id): governance.register_brain(brain_id, "candidate")`. Purge logic must check ALL source markers (both `source` and `_data_source`). Field name contracts must be validated at integration boundaries.

**Detection**: `bootstrap_registered` count in daily_ops report. If > 0 after initial run, previous cycles were blind. Automated check: `assert len(pnl_store.brain_ids - governance.brain_ids) == 0` in reconcile step.

### ReB-20260628-CONFIG_GOVERNANCE_DUAL_TRACK_DRIFT
- **Pattern Signature**: `CONFIG_GOVERNANCE_DUAL_TRACK_DRIFT`
- **Date Cataloged**: 2026-06-28
- **Source Docket**: DQAF-20260628-062
- **Related**: FIX-20260613-076 (`Governance Owns Lifecycle`), FIX-20260529-034 (`SSOT Reconciliation`)

**Definition**: Two systems (Config files + Governance state) separately define "which brains exist." Config (brain_registry_entry.v1) is the SSOT for existence — governance is the SSOT for lifecycle status. But the sync between them only runs at governance creation time (`_load_or_create_governance`), not on every cycle. Adding a new brain config → new brain registered only if governance JSON doesn't exist yet. Config status changes (e.g. candidate→live in config) create "drift" because governance independently tracks its own status per the governance-owns-lifecycle contract. This is a correct architectural separation BUT requires an explicit reconciliation gate to prevent the two tracks from diverging permanently.

**Prevention**: Per-cycle reconciliation gate (`_step_config_gov_reconcile()`) that: (1) registers config-present/governance-missing brains as "candidate", (2) detects and logs status drift WITHOUT overriding governance (governance owns lifecycle), (3) reports `bootstrap_registered` and `drifts_detected` counts in daily_ops output. Config defines existence; governance manages lifecycle; reconciliation gate bridges the two.

**Detection**: `drifts_detected > 0` in daily_ops report → config-governance status mismatch. `bootstrap_registered > 0` after initial cycle → previous cycles had registration gaps. Automated check in `cmd_reconcile()`.

### ReB-20260628-PING_PONG_DEMOTE
- **Pattern Signature**: `PING_PONG_DEMOTE`
- **Date Cataloged**: 2026-06-28
- **Source Docket**: DQAF-20260628-063
- **Related**: FIX-20260628-161 (SSOT reconciliation), FIX-20260628-162 (last-live guard in rule engine), FIX-20260628-163 (config-floor reconcile), FIX-20260628-168 (3-lock fix), ReB-20260628-CONFIG_GOVERNANCE_DUAL_TRACK_DRIFT

**Definition**: Automated governance cycle repeatedly demotes a brain that is the SSOT-designated live brain, while a separate reconciliation process (SSOT reconciliation) repeatedly restores it. The cycle repeats indefinitely because: (a) stale data from a source without lifecycle cleanup (PnL ledger → ghost re-registration), (b) scoring system with rigid thresholds incompatible with the strategy's natural metrics (trend-following WR~35% blocked by 45% floor), (c) a safety guard (last-live) exists but is placed in a code path the actual demotion bypasses (rule engine vs direct transition()). The system oscillates between "0 live brains → trading blocked" and "1 live brain → trading resumes" every governance cycle.

**Prevention**: Three-layer defense: (1) Registration gate validates data sources against config SSOT — no registration without config on disk. (2) Last-live guard at the actual transition call site (not just the rule engine) — the guard must be on the code path that executes the demotion. (3) Scoring thresholds must have escape hatches for profitable strategies that fall outside normal ranges (RR-adjusted channel, manual override).

**Detection**: `GHOST REGISTRATION BLOCKED` log pattern → ghost brain prevented. `LAST-LIVE GUARD TRIGGERED` log pattern → demotion intercepted. `transition_log` shows ping-pong pattern (same brain live↔probation within hours). `governance_state.json` brain count oscillates (3↔16).

---

### ReB-20260626-001
- **Pattern Signature**: `BLE001_NARROW_CATCH_CASCADE`
- **Date Cataloged**: 2026-06-26
- **Source Docket**: DQAF-20260626-001
- **Related**: ReB-20260622-058 (`SCALER_DEPLOYMENT_ACTIVATION_GAP`)

**Definition**: BLE001 Phase 3a 将 `except Exception` 收窄为显式异常类型元组, 但在非阻塞 telemetry 路径 (golden_master) 上收窄过度 — 调用链中存在未被类型系统保护的隐式数据契约 (regime_info 类型为 dict|None 但调用方可传入非 dict 对象), 其抛出的 AttributeError 不在收窄元组内 → 功能静默丧失 → 无告警/无日志/无自愈 → 5 天盲区 → 下游级联 (regime_snapshots→label_builder→所有 regime 分析)。第二因子: golden_master.py 自身 `except OSError: pass` 盲 catch 零日志吞没 I/O 错误 — 即使同文件内错误也完全不可观测。

**Prevention Strategy**:
1. 非阻塞 telemetry 路径使用 `fail_open_guard()` 而非显式异常类型元组 — broad catch + 结构化日志是 telemetry 的正确模式
2. BLE001 收窄前必须审计调用链完整异常剖面 (包括隐式数据契约的类型安全边界)
3. 关键数据管道 (golden_master/regime_snapshots) 增加独立监控告警 (gap > N cycles → 主动推送)
4. 数据管道避免单一源依赖 — build_regime_snapshots 应有独立 Feature Store 回退 (已有, 但 GM 优先 — 应自动回退)
5. `dict.get()` 调用前加防御: try/except AttributeError 或 isinstance check

**Detection Method**: `python scripts/data_integrity_check.py --data-dir data_btc` Section 10 (Golden Master cycles) — gap > 50 cycles → CRITICAL. Alternatively: `grep -c "timestamp_utc" data_btc/golden_master.jsonl | awk -F: '{print "daily avg:", $2/30}'`

### ReB-20260622-060
- **Pattern Signature**: `PSI_RAW_FEATURE_FALSE_POSITIVE_AND_DUAL_MODE_DECOUPLING`
- **Date Cataloged**: 2026-06-22
- **Source Docket**: DQAF-20260622-060
- **Related**: ReB-20260622-058 (`SCALER_DEPLOYMENT_ACTIVATION_GAP`)

**Definition**: PSI (Population Stability Index) 在 raw 特征空间计算时产生大量假阳性, 因为 tree-based 模型 (`normalize: false`) 对线性尺度变换不敏感。当 market regime 改变 (如 BTC ATR 4→34~95), raw-feature PSI 飙升但模型预测能力未退化 — 警报不可操作。此外, 系统中 3 个独立 PSI 实现使用不同分箱策略 (等频/等宽/合并数据), 同一数据产生不同 PSI 值 — 无 SSOT。修复: (1) 归一化空间 PSI (z-score with training μ/σ for regime, rolling μ/σ for anomaly), (2) 双模解耦 (Mode A: long-term regime detection → retrain trigger; Mode B: short-term anomaly detection → data bug alert), (3) 5 工程契约 (零方差熔断/对数发散保护/Mode B 自归一化/排他窗口隔离/样本非对称缓解), (4) 统一 SSOT PSI 实现 (deprecate `stability_monitor.compute_psi()`).

**Prevention Strategy**:
1. 任何监控指标必须先确认其计算空间与下游模型的特征空间一致
2. 树模型用 raw features → PSI 应在归一化空间计算 (消除位置+尺度变化, 仅保留形状变化)
3. 多个实现必须收敛为单一 SSOT — `@deprecated` 标注旧实现
4. 所有分母必须 epsilon-floor 保护; 所有对数必须 epsilon-pad
5. 滚动窗口 baseline 与 actual 必须排他隔离 (无数据泄漏)

**Detection Method**: `python scripts/monitor_feature_drift.py --data-dir data_btc --mode both --normalize` — Mode A PSI > 0.25 确认 regime change, Mode B PSI > 0.25 确认 pipeline anomaly.

### ReB-20260622-058-bis
- **Pattern Signature**: `INCOMPLETE_PATTERN_SEARCH_CONSUMER_VS_SUBCLASS`
- **Date Cataloged**: 2026-06-22
- **Source Docket**: DQAF-20260622-058-bis
- **Related**: ReB-20260622-058 (`SCALER_DEPLOYMENT_ACTIVATION_GAP`)

**Definition**: 代码模式搜索 (`grep joblib.load`) 找到 N 个匹配项并全部修复, 但遗漏第 N+1 个站点 — 因为它使用相同的 API 但是作为**消费者**而非**子类**实例化。`meta_signal_filter.py:135` 的 `self._micro_scaler = joblib.load(path)` 与 `MicrostructureFeatureAdapter` 的 `joblib.load()` 是相同的反模式但在不同调用上下文 — grep 匹配但修复时因"不在 adapter 实例化路径中"被跳过。本质是模式搜索后缺少"相同 API 但不同角色"的二次审查。

**Prevention Strategy**:
1. 模式搜索后必须交叉验证: 对每个 `grep` 命中, 检查其**调用上下文**是否与修复范围重叠
2. `joblib.load` → `json.load` 迁移应全局搜索, 不限于特定类/模块
3. 新增 `resolve_scaler_path()` 后必须检查所有 scaler 加载点是否使用 (包括 config-driven 路径)

**Detection Method**: `grep -rn "joblib.load\|pickle.load" core/ scripts/ apps/` — 应为 0 结果。

### ReB-20260622-058
- **Pattern Signature**: `SCALER_DEPLOYMENT_ACTIVATION_GAP`
- **Date Cataloged**: 2026-06-22
- **Source Docket**: DQAF-20260622-058
- **Related**: ReB-20260622-060 (`PSI_RAW_FEATURE_FALSE_POSITIVE_AND_DUAL_MODE_DECOUPLING`)

**Definition**: 代码修复 (DQAF-054/055: joblib→JSON scaler loader) 完成后, 部署激活是独立的三个步骤: (1) 生成 JSON scaler 文件, (2) 配置 `micro_scaler_path` 指向该文件, (3) 健康检查验证 `micro_scaler_loaded: true`。这三个步骤均缺失 — 修复在代码层面完成但从未在运行时激活。23 天后才通过 PSI 监控发现 `micro_scaler_loaded: false`。冷启动路径 (新环境/新品种, 无 Feature Store) 从未被设计 — `require_scaler=True` 要求 scaler 必须存在, 但没有 `generate_cold_start_scaler()` 兜底。

**Prevention Strategy**:
1. 任何依赖外部 artifact 的代码修复必须包含: (a) artifact 生成脚本, (b) 冷启动/缺失时的兜底路径, (c) 健康检查验证 artifact 已加载
2. 部署后必须运行 `verify.py` + 健康检查确认新功能已激活 (不仅是通过)
3. 品种迁移 (BTC→data_btc) 时必须审计所有 `models/` 路径引用

**Detection Method**: `python scripts/verify.py --quick` + 检查 `meta_pipeline_wired` 事件中 `micro_scaler_loaded: true`。

### ReB-20260622-LABEL_COVERAGE_DEGRADATION

- **Pattern Signature**: `EXTERNAL_CLOSE_PRICE_MISSING × LABEL_PIPELINE_NO_DEFENSE_LAYER`
- **Date Cataloged**: 2026-06-22
- **Source Docket**: DQAF-20260622-057
- **Related**: ReB-20260621-033 (`EXTERNAL_CLOSE_DEAL_REASON_SIGNAL`), ReB-20260613-JOURNAL_LOCK_NAMESPACE_FRAGMENTATION

**Definition**: Label coverage degradation driven by two interacting failures: (1) positions closed outside system control (DEAL_REASON_SIGNAL) produce journal close records missing `close_price`, so PnL-based label classification returns `unlabeled`; (2) the label pipeline has no defense layer — `_step_label_builder` is called without `contract_path`, so barrier-based SL/TP classification (`_classify_barrier_label()`) never activates as fallback.

**Prevention Strategy**:
1. **Defense Layer**: Always pass `contract_path` to `_step_label_builder` — barrier-based classification serves as fallback when PnL is uncomputable.
2. **Journal Completeness**: Backfill `close_price` from MT5 deal history for positions closed via DEAL_REASON_SIGNAL (Phase 2).
3. **Monitoring**: Add `label_coverage_pct` metric to `daily_ops` output with <80% alert threshold.
4. **Gate**: `verify.py` should WARN when `contract_path` is None for `_step_label_builder` calls in production paths.

**Detection Method**: `audit_data_exhaustive.py` Section 3b label coverage check. Run `python scripts/audit_data_exhaustive.py` and verify XAU coverage >80%, BTC coverage >60%.

### ReB-20260621-043

- **Pattern Signature**: `BOUNDARY_TYPE_ENFORCEMENT_AND_EXPLICIT_CATCH`
- **Date Cataloged**: 2026-06-21
- **Source Docket**: DQAF-20260621-043
- **Related**: ReB-20260621-042 (`IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION`), ReB-20260612-004 (`SILENT_FALLBACK_ZERO_OBSERVABILITY`)

**Definition**:
跨核心子系统的数据交接中，生产者返回裸 `dict`（鸭子类型），消费者期望强类型 dataclass（属性访问）。类型不匹配在赋值时不报错，在下游消费时发生 `AttributeError` 延时崩溃。崩溃被顶层 `except Exception` 静默吞没 → 子系统多日无输出 → 状态文件数据持续腐烂。

**Recurrence Indicators**:
1. 代码审查: 数据管线中 `all_metrics[key] = raw_dict` 但集合元素类型声明为 dataclass
2. 运行时: `except Exception` 不记录 `type(exc).__name__` — 无法区分 AttributeError vs 临时文件锁错误
3. 测试: 缺少端到端治理周期合约测试 (输入 journal dict → 输出 governance metrics)

**Prevention Strategies**:
1. **边界类型洗脱**: 所有跨子系统数据交接必须通过显式转换函数 (如 `_dict_to_pnl_metrics()`)，禁止裸 dict 赋值给类型化集合
2. **边界后类型断言**: 增强循环后必须遍历验证 `isinstance(v, BrainPnLMetrics)` — 任何 dict 漏过立即修复+日志错误
3. **显式异常捕获**: 顶层调度器的 `except Exception` 必须记录 `type(exc).__name__` + 堆栈 — 区分"预期的运行时文件锁错误"和"意外的类型错误"
4. **合约测试**: 每个数据管线的关键转换点需要单元测试 (dict→dataclass 转换 + 缺字段默认值 + 完整周期不崩溃)
5. **数据溯源标签**: 所有注入 governance 的 metrics 必须携带 `_data_source` 字段 (`"live_journal"` / `"pnl_store"` / `"backtest"`)

**Detection Methods**:
1. `_step_governance` 返回 `{"status": "error"}` 时应触发告警 (当前静默)
2. `grep "except Exception"` 检查是否有 `type(exc).__name__` 日志
3. Pytest 合约测试: governance cycle with journal-augmented metrics
4. 检查 `governance_state.json` 中各 brain 的 `_data_source` 标签

**Known Instances**:
| Docket | Date | Module | Severity |
|--------|------|--------|----------|
| DQAF-20260621-043 | 2026-06-21 | governance_scheduler | Sev 1 (6+ 天静默崩溃) |

**Cross-References**: FIX-20260621-043, CCT-20260621-043, ReB-20260621-042

---

### ReB-20260621-042
- **Pattern Signature**: `IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION` (不可变账本与物化视图架构模式)
- **描述**: 系统物理设计遵循 Event Sourcing 架构——append-only journal (ledger_events.jsonl / live_trade_journal.jsonl / position_snapshots.jsonl / golden_master.jsonl) 是不可变的唯一真理源 (SSOT)；所有 `.json` state 文件 (leaderboard.json / governance_state.json / execution_state.json 等) 是物化视图 (Materialized View)，由 generator code (daily_ops.py / brain_leaderboard.py / live_journal_metrics.py) 从 ledger 动态重建。当运维人员直接修改 state JSON 时，live 进程在下一 cycle 从 ledger 重新生成覆盖修复——人工修复与实盘进程形成互斥覆写竞态。本质是混淆了 "append-only immutable journal" 与 "regenerated ephemeral view" 两个本体论范畴。必须配套 **Poison Pill (DataIntegrityError)** 阻断机制——当 generator 产出损坏投影时，系统 Fail-Closed (停止产出) 而非 Fail-Open (产出坏数据并静默传播至下游)。
- **关联 FIX IDs**: FIX-20260621-042
- **关联 Docket IDs**: DQAF-20260621-042
- **关联 CCT**: CCT-20260621-042
- **预防策略**:
  1. **物理隔离**: `.gitignore` 阻挡所有 ephemeral state `.json` 进入版本控制——Git 中只存在不可变账本和生成器代码
  2. **AI Agent 禁区**: CLAUDE.md 顶部编码 4 条 RED 绝对禁令——NEVER 编辑 state JSON / NEVER git-add state JSON / NEVER dict.get() 贴纸 / ONLY 修 generator code
  3. **契约测试**: `test_state_reconstruction.py` — Mock journal → 删除所有 state → 运行 generator → 断言精确输出。只有 ledger → view 可复现重建，系统才真正具备自我修复能力
  4. **Poison Pill 强制 Fail-Closed**: `DataIntegrityError` 在 generator 产出不完整/损坏数据时阻断管线——宁可无输出，不可产出坏数据
  5. **新 Feedback Loop 注册时**: 必须满足 (a) 写入 append-only journal, (b) generator 从 journal 重建投影, (c) 投影文件在 .gitignore 中, (d) 契约测试覆盖重建路径
- **检测方法**:
  1. `git status` — 不应出现 ephemeral state `.json` 文件在 staging area
  2. `python -m pytest tests/test_state_reconstruction.py -q` — 26/26 通过
  3. `grep -rn "dict\.get(" core/brains/services/brain_leaderboard.py` — 不应有静默 fallback
  4. 新增 state 文件路径 → CI 检查 `.gitignore` 中是否存在对应 pattern

### ReB-20260620-002
- **Pattern Signature**: `PNL_UNIT_MIXING` (PnL 量纲混合 — USD vs Decimal Percentage)
- **描述**: `StrategyBudget.record_trade(pnl_pct: float, is_win: bool)` 的 `pnl_pct` 参数期望 decimal fraction (0.005 = 0.5% profit)，但多个调用点传入 raw USD 值 (-5.0 = -500% daily PnL)。`float` 类型在编译期不可区分 USD 与 percentage——量纲契约仅存在于变量名中。三条错误路径 + 一条正确路径表明: 没有类型级保护时，多调用点必然出现量纲漂移。本模式与 DQAF-20260615-011 (pnl_r ↔ pnl_per_unit 量纲混乱) 和 DQAF-20260607-007 (USD vs R-multiple 标签错位) 同属量纲安全缺失的家族缺陷。
- **关联 FIX IDs**: FIX-20260620-003
- **关联 Docket IDs**: DQAF-20260620-002
- **预防策略**:
  1. **类型级量纲标记 (P3 北极星)**: 引入 `USD(float)` / `Percentage(float)` / `RMultiple(float)` 的 NewType 封装，使 mypy 在编译期捕获跨量纲赋值
  2. **当前务实的闸门**: `StrategyBudget.record_trade()` 入口添加运行时断言 `abs(pnl_pct) <= 1.0` (合理的 percentage 范围)，超出则 WARNING + 裁剪
  3. **所有 `_notify_budget` / `record_trade` 调用点必须显式声明量纲转换**: 代码注释标注 `# USD → pct: pnl / equity`
  4. **Code review 规则**: 任何向 `record_trade` 传参的新调用点，必须验证 `pnl / equity` 转换是否存在
- **检测方法**: `grep -rn "record_trade\|_notify_budget\|_pending_budget_records" core/` — 逐一验证每个调用点的 pnl 参数是否经过 `/ equity` 转换

### ReB-20260615-012
- **Pattern Signature**: `ORPHAN_ENTRY_ALERT_POLLUTION`
- **描述**: 启动管道生成的合成 orphan close 条目 (label=`auto_orphan_*`, pnl=0, position_ticket=None) 涌入告警上下文的滚动窗口计算。由于无 ticket 绕过去重、pnl=0 计为亏损，真实胜率被稀释至灾难级 (0.91%)。修复: 告警上下文构建器中按 label 过滤 `auto_orphan_` 前缀——纯展示层修复，0 行动及实盘逻辑。
- **关联 FIX IDs**: FIX-20260615-012
- **关联 Docket IDs**: DQAF-20260615-012
- **预防策略**:
  1. 任何向 journal 写入 synthetic 条目的函数必须使用可识别的 label 前缀 (如 `auto_orphan_`)
  2. 告警/统计模块在遍历 journal 时应显式定义包含/排除的 label 集合
  3. CI 检查: 新 synthetic label 出现时自动注册到排除列表
- **检测方法**: `grep auto_orphan_ data/live_trade_journal.jsonl | wc -l` > 100 → 触发本 Pattern

### ReB-20260615-011
- **Pattern Signature**: `ARCHIVED_BRAIN_ALERT_POLLUTION`
- **描述**: 退役/归档大脑的历史 counterfactual PnL 数据永久占据告警"最差大脑"评比位置。因为 `get_all_metrics()` 返回所有大脑（含已禁用/归档），而累积 PnL 随历史长度单调增长 → 退役大脑(最长的历史、最极端的累积值)永远"赢"过活跃大脑。告警面板对活跃大脑的实时退化完全失聪——这是"幸存者偏差"的逆向版本：尸体统治排行榜。
- **关联 FIX IDs**: FIX-20260615-011
- **关联 Docket IDs**: DQAF-20260615-011
- **预防策略**:
  1. 任何跨大脑排名/评比必须先过滤治理活性状态 → 仅评估 operational (非 terminal) 大脑
  2. `get_all_metrics()` 应接受可选的 `active_brain_ids: set[str] | None` 参数
  3. 每次大脑退役时，CI 检查是否有告警/排行榜仍然引用退役大脑
- **检测方法**: 对比 `governance_state.json` 活跃大脑列表与告警"最差大脑"输出 → 出现非活跃大脑 → 触发本 Pattern

### ReB-20260608-001
- **Pattern Signature**: `CIRCUIT_BREAKER_RESET_ASYM`
- **描述**: 熔断器有 N 个触发路径（bridge_silence / cycle_stall×3 / ExecutionQueueFatalError / staleness），但自愈逻辑仅覆盖其中一部分（只检查 `consecutive_degraded_cycles > 0`）。未被自愈逻辑覆盖的触发路径导致熔断器永久卡死。本质是状态机转换表不完备——触发边与自愈边不是 N:N 映射。
- **关联 FIX IDs**: FIX-20260608-003
- **关联 Docket IDs**: DQAF-20260608-001
- **预防策略**: 
  1. 熔断器必须有统一的状态转换表文档（触发边 × 自愈边矩阵），代码审查时对照检查
  2. 自愈逻辑应基于超时冷却（cooldown）而非依赖特定计数器——冷却制是通用自愈路径，覆盖所有触发源
  3. 每个 `circuit_breaker_tripped = True` 赋值点必须同步记录 `tripped_at` 时间戳
- **检测方法**: 搜索 `_circuit_breaker_tripped = True` 的所有赋值点 → 逐一检查是否存在对应的自愈路径 → 缺失则告警

### ReB-20260608-002
- **Pattern Signature**: `ORPHAN_SUBSYSTEM_DETECTION`
- **描述**: 子系统代码完整存在（core/alpha/, MetaFilterGate），状态文件存在但永远处于初始/空值。根因是子系统从未被主循环接线（Alpha）或接线因路径断裂静默失败（MetaFilter）。表面看状态文件"正常"（schema 正确、无损坏），但数据量为零暴露了"未接线"的事实。
- **关联 FIX IDs**: FIX-20260608-003
- **关联 Docket IDs**: DQAF-20260608-001
- **预防策略**:
  1. 每个子系统在模块蓝图中标记为 {wired | standalone | deferred} 三态
  2. 每周定时扫描：状态文件大小 < 阈值 → 告警
  3. 新子系统集成必须在 live_cycle.py 中有显式调用点，CI 检查调用链完整性
- **检测方法**: `python scripts/audit_data_health.py` 已包含状态文件 size=0 检测 → 扩展为检测初始值模式（如 `alpha_count: 0`, `pred_history: []`）

### ReB-20260608-003
- **Pattern Signature**: `WEAKLY_TYPED_DICT_KEY_MISMATCH`
- **描述**: 使用弱类型字典 `.get(key, fallback)` 时，上游返回的字典不包含预期的 key，导致代码静默回退到错误的 fallback 值。本质是 Dict[str, Any] 类型在调用处和返回处之间的契约断裂——没有编译期检查保证键名一致。
- **关联 FIX IDs**: FIX-20260608-003
- **关联 Docket IDs**: DQAF-20260608-001
- **预防策略**:
  1. 状态描述方法（如 `describe()`）应返回 dataclass 而非 `Dict[str, Any]`
  2. 如果必须用 dict，调用处应显式检查期望的 key 是否存在，不存在时记录 WARNING
  3. 字段名应自文档化——`updated_utc` 应被命名为 `updated_utc_iso` 以明确其格式
- **检测方法**: mypy 的 `TypedDict` 可以捕获键名拼写错误 → 将状态文件 schema 声明为 TypedDict 而非 plain dict

---

### ReB-20260606-001
- **Pattern Signature**: `neutral_deadlock_misinterpreted_as_total_flip`
- **描述**: 当多脑策略的群组投票出现 neutral 平票时，调用方将 `current_supporting` 设为空列表 `[]`，导致下游 flip 计算将空集误解为"100% 入场 brain 已翻转"，触发假阳性 brain_flip_extreme 紧急出场。本质是 neutral 状态与 flip 判定之间的语义契约断裂。
- **关联 FIX IDs**: FIX-20260606-137
- **关联 Docket IDs**: DQAF-20260606-002
- **预防策略**: 
  1. 在 `evaluate_brain_exit()` 中添加防御性检查：若 `current_supporting` 为空但 `entry_ids` 非空，应记录 WARNING 而非执行 flip 判定
  2. 类型系统层面：`current_supporting` 参数应有明确的 None vs `[]` 语义区分（None="未计算"，[]="确实无支持 brain"）
- **检测方法**:
  1. 单元测试：模拟双脑 neutral 平票场景，验证 `evaluate_brain_exit()` 不产生 brain_flip
  2. 运行时监控：若 `brain_flip_extreme_100pct` 在 1h 内触发超过 2 次，触发 DQAF 诊断流程

---

### ReB-20260608-003
- **Pattern Signature**: `MISSING_NOTIFY_IN_MANAGED_CLOSE`
- **描述**: 受管平仓的统一入口函数 (`dispatch_managed_close`) 完成了所有业务逻辑（重入守卫、Budget、SL 追踪、仓位清理）但遗漏了 横切关注点——钉钉通知。每个新的退出路径 (meta_exit/SL/TP/hesitation 等) 都通过此函数, 却全部静默。本质是"事件总线缺失综合征" (Missing Event Bus Syndrome): 通信 (通知) 通过手动调用耦合到每个 action site, 而非通过发布/订阅机制自动覆盖。任何新增退出路径都可能遗漏相同关注点。
- **关联 FIX IDs**: FIX-20260608-005, FIX-20260608-002 (MIA 路径的先发修复, 同根同源)
- **关联 Docket IDs**: DQAF-20260608-002
- **预防策略**:
  1. **架构北极星**: Event Bus (Pub-Sub) 模式 — 每笔平仓成功后发布 `TRADE_CLOSED_EVENT`, 钉钉通知器订阅该事件。底层订单模块无需知道通知的存在。
  2. **当前务实的闸门**: `dispatch_managed_close()` 现在在函数尾部 (所有业务逻辑完成后的统一收口处) 调用 `notify_trade`。任何新增的退出路径通过此函数自动获得通知覆盖。
  3. **代码审查清单**: 任何新增"平仓"代码路径 (action="close") 必须包含 `notify_trade` 调用或复用 `dispatch_managed_close`。
- **检测方法**:
  1. `verify.py --quick` 中的蓝图合规检查 — 若 managed_close.py 有修改但 FIX_REGISTRY 无对应条目 → 阻断
  2. 运行时审计: 对比 `live_trade_journal.jsonl` 中的 close action 数量与 `alert_audit.jsonl` 中的 trade_close 数量, 差距 > 0 → 触发 DQAF

---

### ReB-20260607-003
- **Pattern Signature**: `dispatch_crash_fail_open_orphan_spiral`
- **描述**: 执行队列 (ExecutionQueue) 内部发生未预期异常时，通用 `except Exception` 仅打印日志而不触发 circuit_breaker。系统主循环继续运行，大脑持续开新仓但派发管道已断，持仓沦为孤儿。孤儿收养逻辑缺乏完整 MT5 数据富化，exit watchdog 无法接管管理。本质是 **三层 Fail-Open**: (1) dispatch 内部异常未熔断, (2) 调用方未区分 fatal vs transient 异常, (3) 孤儿收养缺乏强制看门狗接管回调。
- **关联 FIX IDs**: FIX-20260607-140, FIX-20260607-141, FIX-20260607-142
- **关联 Docket IDs**: DQAF-20260607-005
- **预防策略**:
  1. 所有执行层方法必须 Fail-Closed: 内部异常 → 抛出特定 FatalError → 调用方 trip circuit_breaker
  2. 孤儿收养必须从 MT5 读取完整 position 数据 (SL/TP/entry/方向/手数)
  3. circuit_breaker 触发 → 直接市场市价清仓 (绕过大脑和队列)
- **检测方法**:
  1. 预提交 hook 检查: 任何 `except Exception` 在 dispatch 路径中必须包含 circuit_breaker trip 逻辑
  2. 运行时监控: `cycle_error` 后 3 个周期内若无 management_phase 事件 → 触发 DQAF Sev 1 告警

### ReB-20260606-002
- **Pattern Signature**: `bootstrap_silent_fail_to_open`
- **描述**: 重启状态恢复（restart_state bootstrap）中的异常被静默吞噬（`except Exception: return`），导致 `_reentry_states` 保持空字典。下游 reentry guard 的 `check_and_record_entry()` 遇到 `last_exit = None` 时按"首次入场"放行（`return True, "first_entry"`），使所有重入防护被绕过。本质是 **Fail-Open 反模式**：恢复失败时系统不应放行，而应进入保守状态（Fail-Closed）阻塞所有交易直到人工确认。这是 RC-03（state_leak_across_restart）的最致命子类。
- **关联 FIX IDs**: FIX-20260606-138
- **关联 Docket IDs**: DQAF-20260606-003
- **预防策略**:
  1. **严禁空 except 捕获**: 所有 error-handling 路径必须使用结构化日志（WARNING/ERROR 级别）并打印完整 traceback
  2. **引导失败必须 Fail-Closed**: 状态恢复失败时设置 `_bootstrap_degraded` 标志，下游 gate evaluator 检查此标志并阻塞所有交易
  3. **代码审查规则**: CI 中禁止 `except Exception: return` 和 `except Exception: pass` 模式（ruff 自定义规则或 pre-commit grep 检查）
- **检测方法**:
  1. 单元测试：模拟 journal 解析异常，验证 `_bootstrap_degraded = True` 且所有策略被阻塞
  2. 静态检查：grep `except Exception:\s*(return|pass)` 并标记为阻断
  3. 运行时告警：若 `system_online` 后 60s 内出现 `open` 记录，触发 DQAF 诊断流程

---

### ReB-20260606-003
- **Pattern Signature**: `metric_pollution_via_rejected_retries`
- **描述**: Append-only event log（记录所有尝试）被消费者错误地解释为 trade ledger（只记录最终结果）。当 MT5 断连导致 exit_watchdog 的重试在 journal 中产生大量 `ack_status="rejected"` 的重复条目时，告警聚合器无条件求和所有 `action=="close"` 的 `pnl` 字段，将同一仓位的 N 次重试计算为 N 笔独立亏损。本质是 **ontology-violation (RC-10)**：event log 与 trade ledger 是不同本体论范畴，消费者混淆了二者。
- **关联 FIX IDs**: FIX-20260606-138-Phase0, FIX-20260606-138-Phase2
- **关联 Docket IDs**: DQAF-20260606-005
- **预防策略**:
  1. **消费端幂等性聚合**: 告警聚合器必须按 `ack_status IN ("accepted","closed")` 过滤 + 按 `position_ticket` 去重（反向扫描取首条 = 终态）
  2. **Schema 语义标注**: journal 条目应区分 "attempt"（尝试）与 "settlement"（结算），可选 `is_durable: bool` 字段
  3. **跨周期冷却**: 连续被拒 ≥3 次的仓位进入 10 周期冷却池，从源头掐断重试风暴
- **检测方法**:
  1. 告警系统自检：对比 `COUNT(*)` vs `COUNT(DISTINCT position_ticket) WHERE ack_status IN ('accepted','closed')` — 差异 >20% 触发指标污染告警
  2. 单元测试：注入 5 条同仓位 rejected + 1 条 accepted 的 journal → 验证聚合结果仅计 1 笔
  3. 运行时监控：`exit_cooldown_activated` 事件计数，>0 时触发 bridge health 检查

---

### ReB-20260606-004
- **Pattern Signature**: `missing_pnl_in_trade_notification`
- **描述**: Dispatch 返回值契约不包含估算 PnL，导致下游通知服务无法获取盈亏数据。`_net_out_close_dispatch_fn` 内部已计算理论 PnL，但返回的 dict 未携带 → `execution_queue.flush()` 构造 `DispatchResult` 时无 PnL 来源 → `notify_trade(pnl=None)` → 钉钉永远显示 "N/A"。本质是数据契约在调用链中的逐层断裂。
- **关联 FIX IDs**: FIX-20260606-138-Phase3
- **关联 Docket IDs**: DQAF-20260606-006
- **预防策略**:
  1. `DispatchResult` 应作为通用 dispatch 结果承载所有通知所需字段（pnl, volume, price）
  2. 回调函数返回值契约应显式声明可选字段，避免"隐式丢弃"
- **检测方法**: 单元测试：构造带 PnL 的 close dispatch → 验证 DispatchResult.pnl 非空 → 验证 notify_trade 收到 pnl

---

---

### ReB-20260606-005
- **Pattern Signature**: `p_win_statistical_freeze_dead_zone`
- **描述**: 当历史 bug 导致的真实亏损将 rolling WR 压低至盈亏平衡地板附近（如 0.44 vs 0.45）时，Fail-Closed 兜底因触发线太低（0.40）无法介入，而 p_win 闸门硬阻断所有交易。无新交易 → 无新数据 → rolling WR 不更新 → 永久冰封。本质是边界值死锁：p_win 在 0.40 和 breakeven 之间的"死锁带"无逃生机制。
- **关联 FIX IDs**: FIX-20260606-139
- **关联 Docket IDs**: DQAF-20260606-004
- **预防策略**: UCB 弹性地板——当 p_win 落入死锁带（0.40 < p_win < min_p_win）且置信度高时，用置信度推导弹性 p_win 解锁。Kelly 自动将仓位缩减至微仓级别，风险可控。
- **检测方法**: 监控 `p_win_source == "ucb_elastic_floor"` 触发频率——若连续 >10 周期触发，说明弹性地板在持续兜底，需人工检查脑健康。若连续 >50 周期触发，触发 DQAF 诊断。

---

### ReB-20260709-SUPERSEDED_ORPHAN_CODE_WITH_STALE_DOCSTRING

- **Pattern Signature**: `SUPERSEDED_ORPHAN_CODE_WITH_STALE_DOCSTRING`
- **Sub-signature**: `PHANTOM_ATTR_IN_DEAD_BRANCH`
- **Date Cataloged**: 2026-07-09
- **Source Docket**: DQAF-20260709-005 (AR revised from Sev 1 to Sev 4)
- **关联 FIX IDs**: FIX-20260709-005 (446ba31f)
- **关联 Docket IDs**: DQAF-20260709-005

**Definition**: A structural evaluator is replaced by a superior mechanism (TF-scaled, better criteria) but the OLD implementation is LEFT in-place with a misleading docstring claiming it is still wired. The code is dead (zero callers repo-wide), reads phantom attributes never set (always-0 getattrs), and produces zero execution-path outputs in journals — yet its docstring asserts "Live Cycle calls this once per open position per cycle". When first discovered it can masquerade as a "silent safety-net failure" (Sev 1) but AR reveals it is Sev 4 dead-code cleanup because the role is already covered and the dead code was never wired.

**Detection**: (1) grep method name repo-wide — if only `def` matches, suspect orphan. (2) Check attributes read via getattr — if no assignment exists, the getattrs are phantom. (3) Check journals for the expected exit reason — zero occurrences confirms dead.

**Prevention (Proactive Amputation)**: When a structural evaluator is superseded by a new mechanism, DELETE the old implementation in the SAME commit that wires the new one. DEPRECATED marks are invisible to grep-callers and do not prevent future re-wiring. Docstring alone is insufficient — code MUST be removed to prevent maintenance tax.

**Notable Case: ExitWatchdog FIX-20260613-086 evaluator**: `evaluate_position` + `_check_time_decay` + `_check_price_decay` remained after `should_exit_hesitation` (per-strategy TF-scaled, wired at `management_phase.py:1775`) took over. Phantom `unrealized_pnl_r` attribute (only written to snapshot dict in `trail_dispatch.py:229`, never set on ActivePosition) made `_check_time_decay` always return False. IC initially Sev 1 Hotfix — AR overturned to Sev 4 dead-code removal.

**Cross-References**: DQAF_DOCKET_REGISTRY.md DQAF-20260709-005, CCT_LEDGER.md CCT-20260709-005; Related: [[deferred_r_unit_mismatch_cross_tf_20260709]]

---

以下模式来自 FIX_REGISTRY.md 中反复出现的 Bug 类型，作为初始化参考：

### PATTERN-PLACEHOLDER-001
- **Pattern Signature**: `hardcoded_feature_dimension_mismatch`
- **描述**: 特征装配点硬编码了特定品种/周期的维度，导致训练-推理特征错位。8+ 历史 FIX 条目（FIX-022, FIX-025, FIX-026, FIX-028, FIX-076, FIX-080, FIX-081, FIX-133）
- **关联 FIX IDs**: FIX-20260525-026, FIX-20260526-028, FIX-20260526-037, FIX-20260528-017, FIX-20260529-028, FIX-20260531-022, FIX-20260601-039
- **关联 Docket IDs**: 待回填
- **预防策略**: 集中式 Schema Registry（`core/features/schemas/registry.py`）作为 SSOT，FeatureAssembler 严格按 Schema 名动态组装，禁止硬编码维度
- **检测方法**: `BrainConfigValidator` 启动时校验训练维度=推理维度；`verify_all_brains.py` 全量脑加载测试

### PATTERN-PLACEHOLDER-002
- **Pattern Signature**: `cross_symbol_parameter_leak`
- **描述**: 一个品种的参数/配置/硬编码路径静默泄漏到另一品种（如 BTC 使用 XAU 的 contract_size / MetaFilter 路径 / MT5 worker symbol_select）
- **关联 FIX IDs**: FIX-20260530-088, FIX-20260531-014, FIX-20260601-031, FIX-20260601-037, FIX-20260601-038
- **关联 Docket IDs**: 待回填
- **预防策略**: `validate_artifacts.py` 跨文件跨品种参数漂移检测；双品种 Golden Master 重放对比
- **检测方法**: `audit_btc_cross_validate.py` 跨品种交叉验证；启动时验证所有 config 路径同时存在于 XAU 和 BTC 数据目录

### PATTERN-PLACEHOLDER-003
- **Pattern Signature**: `state_leak_across_restart`
- **描述**: 系统重启后内存状态（冷却/预算/跟踪器）被重置为默认值而非从持久化存储恢复，导致"重启即开单"的反复出现
- **关联 FIX IDs**: FIX-20260602-050, FIX-20260603-072, FIX-20260603-073, FIX-20260603-074, FIX-20260604-077
- **关联 Docket IDs**: DQAF-20260606-003
- **预防策略**: `execution_state.json` 作为 SSOT 持久化所有门禁状态，启动时强制水合（hydration），不可跳过
- **检测方法**: `state_hydration_test.py` 启动水合完整性检查；`reentry_guard.py` TTL 持久化验证

### ReB-20260607-007
- **Pattern Signature**: `signal_wiring_unconsumed_computed_output`
- **描述**: 信号源已通过 O(1) 算法计算完成，包含在下游函数的返回 dict 中，但决策层从未消费。表现为：数据存在（regime_gate_result["m5_hurst"]），下游函数（evaluate/exit）的参数签名中缺失对应字段。本质是数据路径的最后一公里未接通——信号发射器与信号消费器之间的 glue code 缺失。
- **关联 FIX IDs**: FIX-20260607-143
- **关联 Docket IDs**: DQAF-20260607-007
- **预防策略**: 对任何新增的 RegimeGate 特征字段，在 classify() 返回 dict 中添加后，应同步检查两个消费点：(1) evaluate() 入口是否需要该信号，(2) exit management 是否需要。可选: 在架构审计 checklist 中增加"信号消费审计"专项。
- **检测方法**: 用 grep 搜索 `regime_gate_result.get("` 找出所有被提取的字段，对比下游函数签名中被实际使用的字段。gap = extracted - consumed。自动化脚本 `check_unconsumed_regime_signals.py` 考虑加入 pre-commit。

---

### ReB-20260607-008
- **Pattern Signature**: `stale_data_fail_open_blind_trading`
- **描述**: 数据源（MT5 Bridge）在断连或数据停滞时返回过期 tick 而非抛出异常，数据获取层（market_ingress）未提取并传播 tick 时间戳，决策层（live_cycle）无 staleness 检查，系统在数据管道冰封时继续用过期价格做特征计算、开仓、平仓决策。同时平仓派发路径缺少 pending 状态锁，watchdog batch 被管理循环反复重新触发形成百次级重试拒绝雪崩。本质是**两道 Fail-Open**：(1) 数据层——过期数据被当作实时数据处理，(2) 执行层——已派发的平仓指令可被后续周期无脑重建。
- **关联 FIX IDs**: FIX-20260613-052: resolved placeholder (Staleness Contract + Pending Close Lock)
- **关联 Docket IDs**: DQAF-20260607-006
- **预防策略**:
  1. **Staleness Contract (数据新鲜度契约)**: 所有价格获取函数必须返回时间戳，调用方在每次决策前验证 `time.time() - tick_time < max_age`。连续超限 → circuit_breaker 熔断
  2. **Pending Close Lock (派发锁)**: 对已派发平仓的 ticket，管理循环在 N 周期内禁止重建新的 watchdog batch。锁在 `clear_position()` 时自动释放，超时后自动过期
  3. **价格年龄守卫**: 在平仓派发前验证用于构建订单的价格不超过 60 秒。过期价格必然导致 deviation 拒绝，不如让 MT5 服务端 SL/TP 执行
  4. **Circuit Breaker**: 连续 3 周期 staleness → `circuit_breaker_tripped = True` → 下一周期绕过所有决策层，直接 `mt5_worker.order_send()` 平掉所有持仓
- **检测方法**:
  1. 启动时健康检查：验证最近一次 tick 的年龄 < 30s
  2. 运行时监控：`data_stale` 事件计数 > 5/小时 → DQAF 诊断
  3. `analyze_live_journal.py` 脚本检测：ticket 的 close_attempts > 10 → 告警
  4. 单元测试：模拟 stale tick → 验证 circuit_breaker 触发 + close dispatch 被拒

---

### ReB-20260607-009
- **Pattern Signature**: `frankenstein_metric_independent_min`
- **描述**: 当需要报告"最差策略"的性能指标时，对多个子组件的 PnL 和 WinRate **独立取 min()**，导致最终报告的两个指标可能来自**不同的大脑/策略**。告警描述的"策略"在物理世界中不存在——是多个实体的碎片拼接（缝合怪）。本质是聚合语义错误：`min()` 应该作用于**整个实体**（选择最差的那个），而非作用于**各个字段**（拼接各字段的最差值）。
- **关联 FIX IDs**: FIX-20260613-052: resolved placeholder
- **关联 Docket IDs**: DQAF-20260607-007
- **预防策略**:
  1. 对多实体聚合场景，始终使用 `min(items, key=lambda x: x.field)` 选择单一实体，而非对各字段独立 `min()`
  2. 告警标签必须匹配数据的物理量纲——`per-unit R-multiple` ≠ `USD`
  3. 告警上下文中的"策略级"指标应标注来源实体 ID（如 `worst_brain_id`），使运维可溯源
- **检测方法**:
  1. Code review 规则：搜索 `min(acc, x.field1)` + `min(acc2, x.field2)` 在同一循环中的模式
  2. 告警审计：若 `strategy_pnl` 和 `strategy_win_rate` 在同一告警中出现，验证它们来自同一实体

---

### ReB-20260608-003: `FRAGMENTED_BREAKER_TRIP_PATHS_WITH_STALE_COUNTER_LEAK`

- **发现日期**: 2026-06-08
- **发现环境**: BTCUSDc 实盘 — 熔断器反复触发，系统 110 次/日重启 (May 31)
- **模式描述**: 断路器有多条独立 trip 路径（bridge_silence, cycle_stall, data_staleness, feature_staleness, degraded_wakeup），各自使用独立的连续计数器。Auto-reset 仅重置其中一种计数器（`_consecutive_degraded_cycles`），其余计数器（`_consecutive_stale_cycles`, `_consecutive_stale_features`）在 reset 后仍然存活。若 breaker 由未重置的计数器触发，auto-reset 后的同一 cycle 内立即被重新 trip → 形成"reset → same-cycle re-trip → reset → ..."的死亡螺旋。重启后 breaker 状态从磁盘恢复（`circuit_breaker_tripped=true`），但触发它的 stale counter 丢失（未持久化）→ breaker 无原因存活（"幽灵 breaker"），必须等待 cooldown 超时才能恢复。
- **关联 FIX IDs**: FIX-20260608-009 (root cause), FIX-20260608-006 (circular dependency), FIX-20260608-003 (asymmetric reset), FIX-20260605-120 (persistence), FIX-20260522-019 (initial implementation)
- **关联 Docket IDs**: DQAF-20260608-003
- **预防策略**:
  1. **断路器 trip 路径必须使用统一计数器** — 任何新的 trip 条件必须 increment 同一 `_consecutive_degraded_cycles` 计数器
  2. **Auto-reset 必须清除所有计数器** — 添加新计数器时必须同步更新 reset 逻辑；使用 `_ALL_DEGRADATION_COUNTERS` 元组强制编译时检查
  3. **持久化与恢复必须对称** — `save` 存什么，`restore` 就恢复什么；保存时记录 `trip_reason` 使运维可溯源
  4. **单路径打补丁是反模式** — 如果同一个子系统被修复 ≥ 3 次，必须从架构层面审查整体设计
- **检测方法**:
  1. Code review 规则：搜索 `_circuit_breaker_tripped = True` 的所有赋值点，验证是否存在独立计数器未在 auto-reset 中清除
  2. 运行时监控：`circuit_breaker_trip_reason` 字段值的变化频率——同一 reason 短时间内重复出现 = 死亡螺旋
  3. 启动诊断：若 `circuit_breaker_tripped=true` 但所有 counter=0，判定为"幽灵 breaker"，发出 `ghost_breaker_detected` 告警

---

## ReB-20260609-001: Hesitation Permanent Deadlock

- **发现日期**: 2026-06-09
- **来源 Docket**: DQAF-20260609-001
- **分类**: 边界条件死锁 (Boundary Deadlock) / 代码骨架不完整 (Incomplete Code Skeleton)
- **模式签名**: 重入守卫某退出类别同时缺少 `_MAX_THRESHOLD` 天花板和 TTL 硬解锁，正边际加法 (`exit_confidence + margin`) 产生的阈值超过模型输出范围形成数学死锁。签名关键词: `category=hesitation AND exit_confidence + 0.15 > model_P99 AND no_TTL AND no_MAX_THRESHOLD`.
- **典型症状**:
  1. 某策略线连续数小时至数天零开仓
  2. intent log 中出现大量连续同一 `reentry_blocked` 事件，reason 包含同一退出类别
  3. 被拦截信号的置信度明显正常（非极低值），但始终无法达到阈值
  4. 计算 `exit_confidence + margin` 若超过 0.82 (树模型输出天花板)，即为本模式
- **根因机制**: 多个 FIX 向 reentry guard 添加保护（`_MAX_THRESHOLD` / TTL 硬解锁）时，每次仅针对特定类别施加，遗漏了 hesitation 类别。每次遗漏都是因为"此 FIX 针对 X 类别"的范围限定，没有系统性验证"所有类别是否都需要此保护"。结果: hesitation 在 FIX-117 (ceiling)、FIX-127 (TTL)、FIX-011 (TTL) 三次广谱加固中均被遗漏，成为唯一裸奔的类别。
- **修复模板**:
  1. 对该类别的正边际阈值施加 `_MAX_THRESHOLD` 包裹: `min(max(exit_conf + margin, floor), _MAX_THRESHOLD)`
  2. 添加 TTL 硬解锁: 超时后降级为基础置信度检查 (confidence > 0.50)
  3. 增强 rejection reason 包含阈值数值，便于未来诊断
- **预防措施**:
  1. 编写 `reentry_guard_category_compliance` 测试：验证每一个退出类别同时具备 (a) `_MAX_THRESHOLD` 包裹（若存在正边际加法）(b) TTL 硬解锁（若存在正边际加法 + price confirmation）
  2. Code review 规则：新增/修改退出类别处理时，必须显式说明是否施加了上述两项保护
  3. 架构审计：每季度运行全类别保护扫描，确保无遗漏
- **关联 CCT**: CCT-20260609-001
- **关联 FIX**: FIX-20260609-001

---

### ReB-20260609-001-B: `BREAKEVEN_FLOOR_TRAIL_DEADLOCK`

- **发现日期**: 2026-06-09
- **发现环境**: BTCUSDc 实盘 — trade 3809501680，保本后 SL 锁死 23 根 bar
- **模式描述**: 保本止损触发后，trail_stop_engine 的 Chandelier 公式要求 `highest_high - trail_mult × ATR > entry_price` 才能让 SL 突破保本地板。当 trail_mult 是静态常量（如 regime-given 2.5）且 ATR 较高时，需要的利润缓冲可能超过仓位实际能达到的最高点。此时 `max(candidate, entry_price)` 将 candidate 锁定在 entry_price，`candidate ≤ current_sl + min_step` 返回 None — 数学死锁形成。SL 永远不动，只有 TP 单向收紧。
- **关联 FIX IDs**: FIX-20260609-003
- **关联 Docket IDs**: DQAF-20260609-001
- **预防策略**:
  1. **trail_mult 必须随利润动态衰减** — 水下求生用大乘数（2.5x），水上锁利用小乘数（1.2x）。线性插值连接两者
  2. **所有"地板"逻辑必须配套"突破"机制** — breakeven floor 保护本金，但必须有路径让 trail 在利润积累后突破地板
  3. **静态参数 + 阈值 = 死锁风险** — `static_mult > profit / ATR` 是死锁的充要条件，必须有一个动态衰减变量打破不等式
- **检测方法**:
  1. 运行时监控：`management_phase_diag` 中 `trail_sl_candidate: null` 连续 ≥ 10 bars → 告警
  2. 回测检查：搜索 `breakeven_triggered=true + trail_fired=false` 持续超过半衰期的仓位
  3. 单元测试：验证 R=1.0, 1.5, 2.0, 3.0 时 trail 均有非 null candidate

---

### ReB-20260609-001-B
- **Pattern Signature**: `CAP_OUTPUT_MISMATCH_DEADLOCK` (Cap-Output Mismatch Deadlock)
- **描述**: Reentry guard 的置信度阈值公式（如 `max(exit_confidence + margin, floor)` + `_MAX_THRESHOLD` 天花板）产生的阈值超过目标模型的 P99 输出范围。当模型是 tree-based (XGBoost/LightGBM) 时，天然输出上限约 0.75-0.82，而 `_MAX_THRESHOLD=0.82` 在天花板有保护的情况下仍因 floor/margin 组合产生不可达阈值。BTC 观测：150+ 连续周期封锁（12.5h）。历史先例：FIX-127/130 (brain_flip, floor 0.70→0.65), FIX-117 (新增 `_MAX_THRESHOLD`), FIX-001 (hesitation TTL+ceiling), FIX-010 (hesitation margin+floor)。
- **关联 FIX IDs**: FIX-20260609-001, FIX-20260609-010, FIX-20260606-127, FIX-20260606-130, FIX-20260605-117
- **关联 Docket IDs**: DQAF-20260609-001
- **预防策略**:
  1. 任何涉及 `exit_confidence + X` 边际加法的阈值公式，必须在代码注释中标注目标模型的 P99 输出范围
  2. 新增 reentry 类别时必须附带模型输出分布分析（histogram + P50/P90/P99 percentiles）
  3. CI 中增加 `_MAX_THRESHOLD` 合规检查：任何 exit_category 的阈值公式必须包含 `_MAX_THRESHOLD` 天花板 且 floor 不超过目标模型 P90
- **检测方法**: 搜索 alert_audit 中 `reentry_persistent_block` 连续 ≥ 50 cycles → 触发 `_MAX_THRESHOLD` 审查

### ReB-20260609-001-C
- **Pattern Signature**: `BUDGET_RECONSTRUCTION_AMNESIA` (Budget Reconstruction Amnesia)
- **描述**: 策略对象（含 StrategyBudget）在每个 cycle 被重建（`_build_strategy_lines()`），但持久化状态仅在 cycle 1 恢复（`restore_execution_state()`）。Cycle 2+ 的 budget 计数器恒为零 → 所有累计风控闸门（daily_loss_limit, max_consecutive_losses, intraday_dd, consecutive_degraded）永久失效。本质是对象生命周期管理（recreate-on-every-cycle）与状态生命周期管理（restore-once）之间的契约断裂。关联模式：FIX-20260603-072 引入了 `restore_execution_state()` 但未预见 `_build_strategy_lines()` 会移至循环内（FIX-20260530-070 Strangler Fig #5）。
- **关联 FIX IDs**: FIX-20260609-010, FIX-20260603-072, FIX-20260530-070
- **关联 Docket IDs**: DQAF-20260609-001
- **预防策略**:
  1. 任何 `_build_*` / `_create_*` 在循环内被调用时，必须配套 `_restore_*` / `_hydrate_*` 在同一循环迭代中
  2. CI 中添加 "destructive-rebuild-in-loop" 检测：扫描循环内的 `_build_*` 调用 → 检查是否紧跟 `restore`/`hydrate` 调用 → 缺失则告警
  3. 架构原则：状态对象应在循环外创建一次（构造即持久），而非每 cycle 重建
- **检测方法**:
  1. 启动后（loop_iteration ≥ 3）检查 `execution_state.json` 中 `total_trades_today` 是否 > 0 — 若为 0 但 trade_journal 中当日有记录 → 告警
  2. 运行时断言：每个 cycle 开始时 budget counters ≥ 上一 cycle 结束时 counters（单调不减，除日切外）

---

### ReB-20260609-011
- **Pattern Signature**: `GOVERNANCE_VACUUM_CADET_BRAINS` (治理真空——未成年模型驾驶重型机甲)
- **描述**: 所有活跃大脑均处于 `candidate`（从未证明盈利）状态，无 `live` 大脑。治理状态在整个开单链路中完全是"死数据"——governance_state.json 被 daily_ops 写入但没有任何交易门禁消费它。逻辑倒挂：`probation`（已退化）被罚 vote_weight×0.5，而 `candidate`（未证明）获全票权。结果是 profit_factor=0.72、sharpe=-30 的候选大脑以 0.1 lot 实盘裸奔。
- **关联 FIX IDs**: FIX-20260609-011
- **关联 Docket IDs**: DQAF-20260609-011
- **预防策略**:
  1. governance_state.json 必须被至少一个交易门禁作为 BLOCKING 条件消费——不能仅仅是"记录"
  2. 新增大脑状态时必须在 `live_startup.py` 的 `filter_brains_by_governance()` 中显式处理，不允许 fall-through
  3. daily_ops 中增加 "全 candidate 超时告警"：如果连续 N 天无大脑晋升 live → 触发人工审核
- **检测方法**:
  1. 每个 cycle 检查 governance_state 中 `status=="live"` 的大脑数量 → 0 且 strategy 在开单 → 告警
  2. 搜索 `filter_brains_by_governance` 中未被显式处理的状态 → CI lint 检查

---

### ReB-20260609-012
- **Pattern Signature**: `BTC_SURVIVAL_ALPHA` (BTC 生存策略即 Alpha)
- **描述**: BTC 市场结构不支持传统高盈亏比 Alpha（R:R ≥ 1.0）。跨 4 个时间框架 × 15 组参数的网格搜索证明：所有高 R:R 组合 EV 为负。BTC 的 Alpha 形态是"宽止损 + 紧止盈 + 极高胜率"的生存策略——M15 SL=3.0/TP=2.0 以 EV=+0.456R 位居全场最佳。这不是模型的缺陷，而是 BTC 价格行为物理规律（趋势性强、回调浅）的结构性结果。
- **关联 FIX IDs**: FIX-20260609-012
- **关联 Docket IDs**: DQAF-20260609-012
- **预防策略**:
  1. 任何新资产的大脑训练必须首先执行 SL/TP 网格搜索以确定该资产的正 EV 区域
  2. 不要假设高 R:R = 高 Alpha —— 先在数据上验证该资产是否支持
  3. 训练管线必须包含时间衰减权重 + Walk-Forward Purged CV + 真实摩擦，缺一不可
- **检测方法**:
  1. `python scripts/training/train_btc_swing_v9.py --build-only` 可复现全部网格搜索
  2. CI 中检测 brain config 的 SL/TP 参数是否落入该资产的已知正 EV 区域

---

### ReB-20260610-001
- **Pattern Signature**: `TRAIL_TELEMETRY_BLINDSPOT`
- **描述**: 移动止损(Chandelier Trail)通过 modify_sltp 持续调整 SL 水平，但平仓时的 exit label 永远不包含 'trail' 标签——无论 SL 被 trail 推了多少个 ATR，最终平仓一律标记为 `sl_hit_first` 或 `loss`。这导致 trail 的利润锁定贡献完全不可测量：无法区分"原始 SL 被命中"(trail 未生效) vs "已收紧的 SL 被命中"(trail 保护了部分利润)。整个 trail 子系统的运维只能间接通过 modify_sltp 记录和 snapshot 推测，形同盲飞。
- **关联 FIX IDs**: —
- **关联 Docket IDs**: DQAF-20260610-001
- **预防策略**:
  1. 平仓 dispatch 时比较 final_sl 与 initial_sl——如果不同，label 应为 `trail_sl_hit` 而非 `sl_hit_first`
  2. 在 live_trade_journal 的 exit label 字段增加 trail 相关的子标签（如 `sl_hit_trailed`, `sl_hit_original`）
  3. TrailStopEngine 输出 trail 贡献指标（sl_advance_count, final_sl_delta_from_entry）供遥测
- **检测方法**: 搜索 live_trade_journal 中 `label=="trail"` 的计数 → 应为非零。当前 counter=0。

---

### ReB-20260610-002
- **Pattern Signature**: `MICRO_LIFESPAN_COUNTER_TREND`
- **描述**: 当大脑信号方向与宏观趋势相反时（急跌中生成 LONG 信号做反弹），配合激进防守参数（trail_activation_atr 0.3-0.5, breakeven 激活早），仓位呈现"微型生命周期"——平均持仓 21 分钟（4 根 M5 bar）。价格短暂反弹触发 trail 收紧 → 趋势重力重新压回 → 迅速击穿已收紧的 SL/breakeven → 保本微亏快速出场。这不是系统缺陷，而是逆势交易中防御机制正常工作的表现——系统用高换手率保护了本金，而非被单边碾压。
- **关联 FIX IDs**: —
- **关联 Docket IDs**: DQAF-20260610-001
- **预防策略**:
  1. 趋势隔离门禁(trend isolation gate)应作为第一道防线——逆势信号在 gate 层就应降权或拦截，而非依赖 trail 做后发补救
  2. 当检测到仓位平均持仓时间 < N 个 bar 且全为单一方向时，触发"逆势微仓模式"告警
  3. 大脑训练时应在标签中包含趋势方向信息，使模型学会"顺大势、逆小势"的区别
- **检测方法**: `python scripts/analyze_trail_impact.py` 已包含持仓时间分析。定期运行监控 avg_hold_mins 和方向集中度。

---

### ReB-20260610-003
- **Pattern Signature**: `CONFIG_SYMMETRY_DRIFT`
- **描述**: 双品种部署架构中，对共享大脑的配置修改只应用到单一品种配置文件(live_btc.yaml)，未同步到另一品种(live.yaml)。多见于退役/禁用/参数调整操作。典型场景: 大脑在 commit A 被添加到两个品种的配置中(如 Phase 5b 批量注册)，在 commit B 退役时只更新了主品种配置——因为退役决策基于主品种的实盘表现，次品种的引用被遗忘。
- **关联 FIX IDs**: FIX-20260610-008
- **关联 Docket IDs**: DQAF-20260610-002
- **预防策略**:
  1. `_check_config_consistency()` in verify.py — 静态扫描所有 `live*.yaml`，检测 `status: retired/frozen` 但 `enabled: true` 的大脑
  2. 退役流程标准化: 退役大脑时必须(1)更新脑 JSON(status+vote_weight),(2)在所有引用该脑的配置文件中设 enabled=false,(3)运行 verify.py 确认
  3. 未来: governance_service 自动退役时同步更新所有配置文件引用
- **检测方法**: `python scripts/verify.py --quick` 自动检测并报错。也可手动: `grep -r "BTC_Swing_V5" configs/live*.yaml`

### ReB-20260612-001
- **Pattern Signature**: `SILENT_FALLBACK_ZERO_OBSERVABILITY`
- **描述**: 纯函数在降级路径上返回安全默认值 (0.40)，但不发出任何信号表明降级发生。下游消费方无法区分"真实统计值"与"兜底默认值"，导致系统在降级模式下裸奔而运维无感知。根本原因：返回值设计为裸 float，缺少 quality/source 元数据；fallback 路径无日志。本次实例：`resolve_p_win_from_brains()` 三条静默路径全部返回 0.40。
- **关联 FIX IDs**: FIX-20260612-001
- **关联 Docket IDs**: DQAF-20260612-004
- **预防策略**:
  1. 所有返回统计估计值的函数必须记录降级日志（含降级原因和影响范围）
  2. 调用链透传 `source` + `degraded` 标记至 journal 供事后审计
  3. Iron Law #10: BLE001 替换为 `fail_open_guard()` 确保异常至少被记录
- **检测方法**: `grep -n "return 0\.40\|return 0\.5[0]*$" core/execution/pwin_chain.py` 检查是否仍有未日志化 fallback；`grep "FALLBACK_PATH" data_btc/logs/` 监控降级频率

---

### ReB-20260612-002
- **Pattern Signature**: `PHANTOM_CLOSE_FLOOD`
- **描述**: 退出看门狗每次周期重新评估仓位是否需要平仓——若无 close-in-flight 状态追踪，已发送但未确认的平仓请求会在一段时间后重复发送。每次重试创建新 journal entry（不同 message_id），形成幽灵洪水。典型案例：ticket 3807506009 在 80 分钟内产生 76 条平仓记录（75 rejected + 1 closed）。根因：`PENDING_CLOSE_MAX_CYCLES=3` 太短 + 无 attempt counter 上限。
- **关联 FIX IDs**: FIX-20260612-003
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. `PositionManager` 追踪 `_close_attempt_count`，超过 `PENDING_CLOSE_FLOOD_THRESHOLD=3` 永久锁定
  2. `PENDING_CLOSE_MAX_CYCLES` 延长至 10（50 分钟）给 MT5 充足处理时间
  3. `clear_position()` 一次性清理 counter + lock
- **检测方法**: `python -c "import json; from collections import Counter; ..."` 统计每 ticket 的 close entry 数 — 超过 5 条触发告警

---

### ReB-20260612-003
- **Pattern Signature**: `TRAIL_LABEL_BLINDSPOT`
- **描述**: 移动止损（Chandelier Trail）通过 247 条 `modify_sltp` 记录持续收紧 SL，但所有 246 条平仓记录中 `label='trail'` 计数为 0。Reconciliation 路径遇到 `close_reason=4 (SL)` 无条件分配 `sl_hit_first`，不检查 `trail_advances`。Bridge worker 按 PnL 符号分配 `loss`/`win`，忽略 `trail_contribution`。仅 MIA enrichment 路径正确分配 `sl_hit_trailed`（FIX-20260610-006 已修）。
- **关联 FIX IDs**: FIX-20260612-003
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. Reconciliation: `close_reason==4` → 检查 `state.position_manager.get_position(ticket).trail_advances > 0` → `sl_hit_trailed`
  2. Bridge worker: 检查 payload 的 `trail_contribution.trail_advances > 0` → 调整 label
  3. 所有平仓 label 路径统一检查 trail 历史
- **检测方法**: `python -c "..."` 统计 journal 中 label='sl_hit_trailed' 计数 → 应随实盘交易增长

---

### ReB-20260612-004
- **Pattern Signature**: `PNL_BACKFILL_GAP`
- **描述**: 平仓 PnL 在两个独立路径中无法捕获：(a) Bridge worker 使用 dispatch 时的 mid-price 估算 PnL，journal 写入后永不更新实际成交价/利润；(b) MIA 检测调用 `history_deals_get()` 单次无重试——MT5 成交数据延迟 1-3 秒时 PnL 为 null（23% 失败率）。这两个缺口合计导致 17.6% PnL null rate (JOURNAL_PNL_NULL_RATE_HIGH)。
- **关联 FIX IDs**: FIX-20260612-004
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. Bridge worker: 平仓成功后立即查询 `history_deals_get(position=ticket)` 获取 `deal.price` + `deal.profit` → journal 使用实际成交 PnL
  2. MIA enrichment: `history_deals_get()` 包装 3 次重试 + 1 秒延迟（对齐 PositionCloseAdapter 模式）
  3. `detail.close_price` + `detail.profit` + `detail.fill_volume` 填入 journal 供审计
- **检测方法**: `python scripts/analyze_live_journal.py --data-dir data_btc` → PnL null rate < 5%

---

### ReB-20260612-005
- **Pattern Signature**: `CALIBRATOR_COLD_STALLED`
- **描述**: ConformalCalibrator 的 `cold_started` 标志被 `cold_start_from_journal()` 设为 True 后永不改为 False——即使已积累 51+ 条历史记录（超过 warmup_samples=50）。`total_computations` 计数器因实盘无交易（无 brain proposal → gate filter 不触发 → `compute_threshold()` 从不被调用）保持为 0。两个指标叠加导致 `CONFORMAL_COLD_STALLED` 误报。Calibrator 实际运作正常（有足够历史数据计算 Q10 分位数），只是状态标志不反映真实就绪度。
- **关联 FIX IDs**: FIX-20260612-005
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. `_save_state()`: `history >= _warmup_samples` → `cold_started = False`
  2. `_load_state()`: 对旧状态文件反向补填过渡（历史 ≥ 50 → 非 cold）
  3. 就绪度判断基于历史计数而非计算计数——历史是 Q10 分位数的实际数据源
- **检测方法**: 检查 `conformal_calibrator_state.json` → `cold_started` 应在 history≥50 后变为 false

---

### ReB-20260612-006
- **Pattern Signature**: `POSITIONAL_FRAGILITY`
- **描述**: 特征字典通过 `list(feature_source.values())` 转换为模型输入数组时，特征顺序依赖 Python dict 插入顺序（Python 3.7+ 稳定但不同代码路径构建顺序可能不同）。若上游字典键顺序错乱或多插特征，模型静默使用错误特征位置——MACD 值被当成 RSI 权重，产生垃圾预测。影响范围：3 个 adapter + 1 个 feedback hook 共 5 个 `.values()` 站点。LightGBM adapter 已通过命名查找修复（FIX-20260516-004），其余站点为遗留回退路径。
- **关联 FIX IDs**: FIX-20260612-002
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. 所有 adapter 使用 `brain_entry["features"]` 命名投影 → `[feature_source[n] for n in feature_names]`
  2. BrainFactory 加载时验证 `features` 列表 ≡ `.meta.json feature_names`（已有）
  3. XGBoost adapter 48h 影子校验（新旧数组比对 + mismatch 告警）
  4. 禁止在特征组装路径使用 `dict.values()` — ruff 自定义规则检测
- **检测方法**: `grep -rn "\.values()" core/brains/ core/feedback/` → 应为 0 结果（5 站点全部替换后）

## ReB-20260612-007: TRIPLE_BOOKKEEPING_RESIDUAL

- **Docket**: DQAF-20260612-002
- **Pattern**: 退役大脑时在多处独立配置位置留下残留（registry status, vote_weight, live.yaml enabled），后续重新激活时任何一处未同步都会静默阻止大脑投票
- **Signature**: 三处独立配置点中任一为 'retired/disabled/zero' 即可形成合力阻断——无任何一处是 SSOT
- **Detection**: governance 有 live brain 但 voted_brain_ids 中缺失 + disabled_brains_filtered 日志 + strategy.brains 不包含该 brain_id
- **Prevention**: 大脑退役/重激活应通过单一原子操作执行，或至少包含一致性检查（governance live ↔ registry status ↔ yaml enabled ↔ vote_weight）。参考 FIX-20260612-006。

## ReB-20260623-066: COLD_EXPLORE_TRAP

- **发现日期**: 2026-06-23
- **关联 DQAF**: DQAF-20260623-066
- **严重等级**: Sev 1
- **模式签名**: `cold_explore_neutral` 成为策略的唯一可行路径 → 所有获批交易使用固定 p_win=0.50 → Kelly sizing 和 RR 评估基于假数据 → 系统无方向偏差抵抗力
- **传导机制**:
  1. MetaFilter 切除 (DQAF-065) → swing 策略永远返回 (None, None)
  2. (None, None) 触发 `_is_cold_explore=True` → p_win=0.50
  3. BrainPnLStore 重启后为空 → `resolve_p_win_from_brains()` 返回 0.40 (fail-closed)
  4. PnL store 空 → 冷启动豁免条件过严 → amnesty 可能被阻断
  5. 结果: 好策略和坏策略获得相同的 p_win=0.50, 真实 alpha 被淹没
- **影响品种**: XAU (-15.70R), BTC (-19.14R)
- **修复**: FIX-20260623-066: (1) governance `performance_metrics` 冷启动回退, (2) cold_explore 使用 governance 替代盲 0.50, (3) ≥2 LIVE brain 准入门禁
- **预防**: 任何切除 MetaFilter 的策略必须确保有替代的 p_win 数据源; 冷启动后验证至少 2 个 LIVE brain 有有效 win_rate
- **检测**: 监控 `cold_explore_neutral` 占总批准决策的比例; 告警阈值 >50%

## ReB-20260612-008: GOVERNANCE_BRAIN_SOURCE_MISMATCH

- **Docket**: DQAF-20260612-002
- **Pattern**: 两套大脑状态源（brain registry JSON + governance_state.json）各自独立维护，状态变更未双向同步
- **Signature**: governance 标记 brain 为 live，但 registry 仍为 retired/frozen，strategy_builder 使用 registry 状态过滤→governance 的 live 标记无效
- **Detection**: 检查 governance_state live brains ∩ brain registry entries → 交集为空时告警
- **Prevention**: strategy_builder 过滤时应同时检查 governance_state（如在 governance 中为 live，覆盖 registry retired）。参考 FIX-20260610-001 → FIX-20260612-006 根因链。

---

### ReB-20260621-046

- **Pattern Signature**: `FEATURE_SCHEMA_ROUTING_AND_BRAIN_API_CONTRACT`
- **Date Cataloged**: 2026-06-21
- **Source Docket**: DQAF-20260621-046
- **Related**: ReB-20260612-004 (`SILENT_FALLBACK_ZERO_OBSERVABILITY`)

**Definition**:
特征生产层 (feature store/computers) 与特征消费层 (brain inference) 之间缺少 schema routing contract。Brain config 中存在 `feature_schema_id` 字段但未被 routing code 消费 → 所有 brain 默认接收同一特征格式 → 维度不匹配时静默 fallback 到 neutral。BrainSignal 接口变更 (dict→frozen dataclass) 无向后兼容层 → consumer 代码 `signal.prediction.get()` 静默返回 None → neutral fallback。双重静默: (1) 特征维度不匹配被 `dim_mismatch` fallback 吞没, (2) API fracture 被 `.get()` 默认值吞没。

**Recurrence Indicators**:
1. 代码审查: brain config 中的 `feature_schema_id` 字段存在但未被任何 router 消费
2. 运行时: ensemble report 中 `dim_mismatch` 计数 > 0 但无对应的告警
3. 监控: 品种信号产出连续 N 天为 0 (freshness guard 应触发)
4. 编译期: `signal.prediction.get()` — dict 方法调用在 frozen dataclass 上应在 mypy 中报错

**Prevention Strategies**:
1. **Schema Router**: 特征解析路径必须根据 `feature_schema_id` 路由到正确的 assembler
2. **BrainSignal 向后兼容层**: 接口变更时保留 `signal.prediction` 属性作为 deprecated wrapper
3. **Mypy 类型检查**: 禁止在 dataclass 实例上调用 `dict.get()` — mypy `check_untyped_defs` 可捕获
4. **Freshness Guard**: TTL 监控信号文件 — 24h 无产出立即告警 (Plan B Phase 4)
5. **特征维度运行时校验**: model.predict() 前断言 `len(features) == model.n_features_in_`

**Detection Methods**:
1. `grep "signal.prediction.get"` — 检查是否有 dict 方法调用在 dataclass 上
2. `grep "dim_mismatch"` — 非零计数必须触发告警而非静默 fallback
3. Freshness Guard (Plan B) TTL 检查 — 45 天空文件不可再次发生
4. Brain config audit: 验证 `feature_schema_id` 对应的 schema 确实存在于 registry 中

**Known Instances**:
| Docket | Date | Module | Severity |
|--------|------|--------|----------|
| DQAF-20260621-046 | 2026-06-21 | live_shadow_ensemble, brain configs | Sev 2 (45 天信号真空) |

**Cross-References**: FIX-20260622-003, CCT-20260621-046, ReB-20260621-046

---

### ReB-20260622-001

- **Pattern Signature**: `WILD_STATE_WRITE_POISONING` (野生状态写入中毒)
- **Date Cataloged**: 2026-06-22
- **Source**: Plan B — State Governance Protocol (Phase 1-4)
- **Related**: ReB-20260621-042 (`IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION`), ReB-20260612-004 (`SILENT_FALLBACK_ZERO_OBSERVABILITY`)

**Definition**:
系统中 16 个独立的 `json.dump()` / `write_text()` 调用点各自直接写入状态文件，无统一 Schema 校验、无原子性保证、无跨品种污染检测、无新鲜度监控。每次写入是独立的不受监管行为 — 任何一个调用点的数据缺陷都会产生脏文件，下游静默消费脏数据，发现时已传导数层。

**Recurrence Indicators**:
1. 代码审查: grep `json.dump` 或 `write_text(json.dumps` 写入 `.json` 路径
2. 运行时: 状态文件 0 字节 (原子写入失败), schema 关键字段缺失, 跨品种 ID 泄漏
3. 监控: 状态文件 mtime 超过 TTL 无更新 (freshness guard)

**Prevention Strategies**:
1. **Write Gate**: 所有状态写入必须通过单一闸门 (StateWriter) — 4 道检查 (required fields + schema + cross-symbol + atomic)
2. **Data Catalog**: 每个状态文件必须注册为 StateArtifact — 声明 TTL + validator + generator + cross_symbol_guard
3. **Freshness Guard**: 定时扫描所有 artifact 的 mtime — 超过 TTL 立即 CRITICAL 告警
4. **CI Enforcement**: 新增 `json.dump` 到 `.json` 路径必须触发 blueprint compliance gate
5. **物理隔离**: ephemeral state files 全部在 `.gitignore` — 只有 generator code + ledger 进入版本控制

**Detection Methods**:
1. `grep -rn "json\.dump\|\.write_text.*json" core/ scripts/ | grep -v test_` — 检测野生写入
2. `python core/state/freshness_guard.py` — 新鲜度扫描
3. `python scripts/audit_state_of_system.py` — 跨品种污染检测
4. CI: 新状态文件未注册到 CATALOG → build failure

**Known Instances**:
| Docket | Date | Module | Severity |
|--------|------|--------|----------|
| DQAF-20260621-046 | 2026-06-22 | 7 modules, 16 write sites | Sev 2 (系统性架构缺陷) |

**Cross-References**: FIX-20260622-001, CCT-20260621-046

### ReB-20260623-070
- **Pattern Signature**: `MISSING_DATACLASS_FIELD`
- **Date Cataloged**: 2026-06-23
- **Source Docket**: DQAF-20260623-070
- **Related**: ReB-20260623-072 (`WRONG_DICT_LEVEL_GOVERNANCE`)

**Definition**: 重构提取 (Strangler Fig / Extract Method) 时, 新模块引用的字段未在新 dataclass 中定义。原代码中字段可能通过 `__dict__` 动态注入或在调用方初始化, 提取到类后未同步。当访问代码使用 `obj.field` (直接属性访问) 而非 `getattr(obj, 'field', default)`, 外层的宽泛 `except Exception` 会吞没 `AttributeError`, 导致 fail-open — 保护性门禁静默失效。修复: (1) 在 dataclass 补齐字段 + 正确默认值, (2) 门禁代码使用 `getattr` safe access, (3) 区分状态完整性错误 (AttributeError/TypeError → fail-closed) 与瞬时错误 (timeout/network → fail-open)。

**Prevention Strategy**:
1. 重构提取后运行 `grep -r "state\._" --include="*.py" | sort -u` 对比 dataclass 字段列表
2. 门禁类 Guard 代码必须使用 `getattr(obj, field, safe_default)` 而非直接属性访问
3. Guard 的异常处理必须分层: 状态完整性错误→fail-closed, 数据质量错误→fail-open
4. CI: 新增 dataclass 字段对比检查 (extract 前 → extract 后 diff 确保无遗漏)

**Detection Method**: `grep "session_guard_error" data_btc/logs/intent_*.log | grep AttributeError` → 任何 AttributeError in guard 代码 = 本模式触发。

**Known Instances**:
| Docket | Date | Module | Severity |
|--------|------|--------|----------|
| DQAF-20260623-070 | 2026-06-23 | runtime_live | Sev 2 |

**Cross-References**: FIX-20260623-070, CCT-20260623-070

### ReB-20260623-071
- **Pattern Signature**: `CACHE_SLA_BOUNDARY`
- **Date Cataloged**: 2026-06-23
- **Source Docket**: DQAF-20260623-071
- **Related**: None

**Definition**: 缓存写入间隔与读取 SLA (Service Level Agreement) 被设为相同值 → 缓存恰好在边界翻转 (fresh ↔ stale)。当 writetime ≈ SLA, 每个周期都在边界上竞争 → 不必要触发 fallback 路径 (实时计算/重计算) → 增加下游负载。修复: 引入负向抖动余量 (SLA > 写入间隔) — 例如写入每 60s 发生, SLA 设为 310s (而非 300s) 给予 10s 缓冲。

**Prevention Strategy**:
1. 任何 freshness SLA 必须 > 最大写入间隔 + 安全余量 (至少 10%)
2. SLA 值不应与任何系统周期 (M5 bar × N) 存在整数倍关系
3. 使用质数或分数值 (如 307s 而非 300s) 打破与整周期的共振
4. 健康检查应区分 "warn" (接近 SLA) 和 "critical" (超过 SLA)

**Detection Method**: 日志中 age ≈ SLA ± 10s 反复出现 (非单调增长) → 边界竞争。真正的管线冻结会产生 age >> SLA (如 600s+)。

**Known Instances**:
| Docket | Date | Module | Severity |
|--------|------|--------|----------|
| DQAF-20260623-071 | 2026-06-23 | features | Sev 3 |

**Cross-References**: FIX-20260623-071, CCT-20260623-071

### ReB-20260623-072
- **Pattern Signature**: `WRONG_DICT_LEVEL_GOVERNANCE`
- **Date Cataloged**: 2026-06-23
- **Source Docket**: DQAF-20260623-072
- **Related**: ReB-20260623-070 (`MISSING_DATACLASS_FIELD`)

**Definition**: 遍历嵌套字典时错误地迭代顶层键而非嵌套子字典 → 下游消费者基于空/错误数据静默失效。本例中 `governance_state.items()` (顶层: `brain_states`, `schema_version`) 被误用替代 `governance_state["brain_states"].items()` (嵌套: `BTC_Swing_V12: {status: live, ...}`)。`_live_brain_ids` 恒为空集 `set()` (非 None) → 所有 downstream gate 的 `if live_brain_ids is not None` 分支激活但 `brain_id in live_brain_ids` 永远 False → 全部 brain 被过滤。最危险的是: 两个独立部署的防护层 (DQAF-059 + DQAF-066) 被同一个 bug 同时静默击穿 — 红线多重覆盖产生虚假安全感。

**Prevention Strategy**:
1. 嵌套字典访问必须显式写 `.get("sub_key", {})` 链, CR 中禁止裸 `.items()` 在非叶子节点
2. 集成测试: 验证 `_live_brain_ids` 非空且包含已知 LIVE brain_id
3. Bootstrap 自检: 系统启动时如果 `_live_brain_ids` 为空但 governance_state 存在且 brain_states 含有 LIVE entry → 立即 CRITICAL 告警
4. 两级防御必须使用异构检查路径 (不同代码路径/不同字典 key) — 相同 bug 不应能击穿两层

**Detection Method**: `grep "ZERO LIVE brains found"` 日志 + 验证 governance brain_states 包含 `status: live` → 不匹配 = 本模式触发。

**Known Instances**:
| Docket | Date | Module | Severity |
|--------|------|--------|----------|
| DQAF-20260623-072 | 2026-06-23 | execution_strategy_line | Sev 1 |

**Cross-References**: FIX-20260623-072, CCT-20260623-072

---

### ReB-20260625-125
- **Pattern Signature**: `ORPHAN_WATCHDOG_MIGRATION_SWEEP_INCOMPLETE`
- **Date Cataloged**: 2026-06-25
- **Source Docket**: DQAF-20260625-125
- **Related**: ReB-20260622-001 (`WILD_STATE_WRITE_POISONING`), ReB-20260622-047 (`ORPHAN_GENERATOR_NOT_WIRED_TO_PIPELINE`)

**Definition**: 基础设施级迁移 (如 StateWriter gate rollout) 跨越多个模块时, 某个消费者/探测器脚本未被纳入迁移范围 → 该脚本继续使用旧路径/旧字段名/旧数据格式 → 静默失效。本例中 Plan B StateWriter 迁移 (FIX-20260622-001) 将 `daily_ops_scheduler.py` 的写入路径从 `daily_ops_state.json` 变更为 `state/daily_ops_state.json`, 但 `watchdog_daily_ops.py` (读取同一状态文件的消费者) 未被更新 → 三连盲 (路径/字段名/auto_run) → watchdog 完全失效。

**Prevention Strategy**:
1. 基础设施迁移必须包含 `grep` 完整消费者审计 — 搜索旧路径/字段名的所有引用, 在迁移 PR 中全部更新
2. 迁移 checklist: 迁移后运行 `python scripts/commander_guardrails_arch.py` 检查 stale state files + orphan consumers
3. Watchdog 自检: `watchdog_daily_ops.py` 增加启动时路径可达性检查 — 如果 state_path 不存在且 `--auto-run` 启用, 立即 CRITICAL 告警
4. 消费者-生产者契约: 状态文件路径和字段名应定义在 `core/state/catalog.py` 中 (SSOT), 消费者使用 `catalog.lookup()` 而非硬编码

**Detection Method**: `grep "daily_ops_state.json"` 全库搜索 → 验证所有引用点使用相同路径 + 字段名。`scripts/system_trust_report.py` staleness 检查可捕获 >24h 未生成 leaderboard 的情况。

**Known Instances**:
| Docket | Date | Module | Severity |
|--------|------|--------|----------|
| DQAF-20260625-125 | 2026-06-25 | runtime, scripts | Sev 2 |

---

### ReB-20260701-TOXIC_DIVERSITY_GATE
- **Pattern Signature**: `TOXIC_DIVERSITY_GATE`
- **Date Cataloged**: 2026-06-30
- **Source Docket**: DQAF-20260630-202
- **Related**: DUPLICATE_RULE_UNSYNC, FIX-20260630-202, FIX-20260701-203, FIX-20260701-204

**Definition**: A mandatory directional diversity requirement (long≥N AND short≥N) that structurally excludes macro-trend-following models. Genuine H4/D1 trend followers in a sustained regime produce directionally-monopolistic signals by design — a correct model SHOULD output 100% SHORT in a multi-week downtrend. Historical backtests establish this is normal behavior, not signal degradation. The diversity gate conflates "model failure" (all NEUTRAL) with "regime alignment" (all SHORT).

**Prevention**: (1) Macro timeframes (H4+, D1+) exempted from bidirectional diversity via BrainRegistry contract_group/timeframe probe. (2) `is_macro` flag logged in promotion reason for audit. (3) Fail-open: on BrainRegistry probe failure, falls through to legacy diversity check (conservative).

**Detection**: Governance audit script: for each candidate brain with ≥50 directional signals and 0 count in one direction, check BrainRegistry for macro timeframe — flag as false negative if macro + not promoted.

### ReB-20260701-DUPLICATE_RULE_UNSYNC
- **Pattern Signature**: `DUPLICATE_RULE_UNSYNC`
- **Date Cataloged**: 2026-07-01
- **Source Docket**: DQAF-20260630-202
- **Related**: TOXIC_DIVERSITY_GATE, FIX-20260701-204

**Definition**: The same business rule (Rule 85: shadow-to-probation auto-promotion) is implemented in two separate files serving different execution paths (cloud `scheduler_service.py` → `GovernanceRuleEngine`, local `daily_ops_scheduler.py` → `_promote_shadow_brains()`). A fix applied to one path is invisible to the other — the second path continues executing the old logic, producing divergent behavior for the same input. This is a silent architectural defect: no error, no warning, just different outcomes depending on which code path processes the brain.

**Prevention**: (1) Deferred: unify both paths under a single `GovernanceRuleEngine.evaluate()` callable from both scheduler_service and daily_ops. (2) Until unified: cross-reference comments (`# MIRROR: governance_rule_engine.py::_shadow_to_probation_condition`) in both files to signal co-modification requirement. (3) Pre-commit lint rule: detect semantically-identical threshold values duplicated across files, flag for review.

**Detection**: Compare promotion decisions between cloud scheduler_service path and local daily_ops path for same brain_id — divergent decisions = unsynced logic.

**Known Instances**:
| Docket | Date | Module | Severity |
|--------|------|--------|----------|
| DQAF-20260630-202 | 2026-06-30 | governance | Sev 2 |
| DQAF-20260706-003 | 2026-07-06 | runtime | Sev 1 |

---

### ReB-20260706-CROSS_FILE_DUPLICATE_GATE_LOGIC
- **Pattern Signature**: `CROSS_FILE_DUPLICATE_GATE_LOGIC`
- **Date Cataloged**: 2026-07-06
- **Source Docket**: DQAF-20260706-003
- **Related**: `GOVERNANCE_DEGRADATION_VOTE_WEIGHT_BLIND`, FIX-20260706-003, FIX-20260629-174, FIX-20260703-061, FIX-20260625-139

**Definition**: A governance/safety gate (e.g., minimum live brain count, vote_weight enforcement, governance state access) is implemented in TWO independent code paths — typically one in the signal pipeline (`strategy_line.py`) and one in the strategy evaluator (`strategy_evaluator.py`). When a fix is applied to only ONE of the two paths, the unfixed path silently continues executing the old (broken) logic. This is a silent architectural defect: no error, no warning, no code sharing — just two independent implementations of the same rule that drift apart over successive fixes.

**Known instances in strategy_evaluator.py (chronological)**:
1. FIX-20260629-174: governance_state access path (`get(bid)` → `get("brain_states",{}).get(bid)`) — fixed in strategy_line.py, missed in strategy_evaluator.py L307+L548
2. FIX-20260703-061: status dimension (`status=="live"` → `status in ("live","probation")`) — fixed in strategy_line.py, missed in strategy_evaluator.py Cut 4
3. FIX-20260625-139: vote_weight dimension (BrainSignal vote_weight contract) — fixed in strategy_line.py signal pipeline, missed in strategy_evaluator.py Cut 4/Cut 4-bis
4. FIX-20260706-003: this fix — closes the vote_weight blind spot in Cut 4/Cut 4-bis, completing the three-dimensional coverage (governance access, status filter, vote_weight)

**Prevention**: (1) Consolidate governance gate logic into a single shared module (`governance_gates.py`) imported by both strategy_line and strategy_evaluator — eliminate the duplicate code path entirely. (2) Until consolidation: pre-commit lint rule that flags semantically-identical threshold values / filter conditions replicated across files. (3) When fixing a gate bug, grep for ALL occurrences of the key identifier (e.g., `_live_count`, `vote_weight`, `brain_states`) across the entire codebase — don't assume the bug is single-site.

**Detection**: For each governance gate dimension (status, vote_weight, access path), grep both strategy_line.py and strategy_evaluator.py for the gate condition — flag if they differ.

**Cross-References**: FIX-20260625-125, CCT-20260625-125

---

### ReB-20260708-MUTABLE_TICKET_JOIN_ON_IMMUTABLE_POSITION
- **Pattern Signature**: `MUTABLE_TICKET_JOIN_ON_IMMUTABLE_POSITION`
- **Date Cataloged**: 2026-07-08
- **Source Docket**: DQAF-20260708-001
- **关联 FIX IDs**: FIX-20260708-001
- **关联 Docket IDs**: DQAF-20260708-001
- **Related**: TECH_DEBT-003 (PositionStateMachine — SSOT key corrected ticket→identifier), FIX-20260626-144 (prior orphan patch), `CONFIG_GOVERNANCE_DUAL_TRACK_DRIFT`

**Definition**: A position lifecycle keys its open<->close join — and every derived consumer (orphan detection, journal-gate admission, PnL reconciliation, training-label pairing) — on `position_ticket`, which MT5 MUTATES on partial-close/netting re-ticketing. The close then carries a NEW ticket while the open keeps the original, so ticket-equality join structurally manufactures a false "orphan close" on every re-ticket: real closes get quarantined (invisible to PnL/labels) and re-ticketed trades silently produce no training label. The immutable `position_identifier` (== the original opening ticket, sourced from MT5 `deal.position_id`) already rides on the close leg, but no consumer joins on it. Distinct sibling failure: `position_identifier`/`position_ticket` BOTH absent on a close (key-missing, not key-changed) — resolve_identity cannot repair those; they need write-side stamping.

**预防策略**: (1) One immutable-identity resolver (`core/data/ticket_resolver.py::resolve_identity()`) is the ONLY join-key authority; every open<->close pairing consumer imports it; broker-facing sites (real MT5 close/modify order requests) keep the mutable `resolve()` ticket. (2) Write BOTH legs with the immutable anchor (`PositionOpened`/`PositionClosed.to_journal_entry` emit `position_identifier`) so the join never depends on the mutable field. (3) Never key a persistent lifecycle join on a broker-mutable identifier — separate "broker handle" from "lifecycle identity".

**检测方法**: grep for `by_ticket` / `open_by_ticket` / `\.get\("position_ticket"\)` join/pairing patterns that should route through `resolve_identity()`; regression tests pinning that a re-ticketed close (new ticket, same identifier) pairs with its open and is admitted (`tests/data/test_ticket_resolver_identity.py`, `tests/ledger/test_journal_gate.py::test_reticketed_close_admitted_by_identity`).

**Cross-References**: TECH_DEBT_REGISTRY.md TECH_DEBT-003, FIX_REGISTRY.md FIX-20260708-001

---

### ReB-20260707-FEATURE_ENGINEERING_CANNOT_RESCUE_UNSEPARABLE_SIGNAL
- **Pattern Signature**: `FEATURE_ENGINEERING_CANNOT_RESCUE_UNSEPARABLE_SIGNAL`
- **Date Cataloged**: 2026-07-08
- **Source Docket**: DQAF-20260707-003
- **关联 FIX IDs**: FIX-20260708-002
- **关联 Docket IDs**: DQAF-20260707-003
- **Related**: FIX-20260705-064 (V12_H1_15 kill-switch precedent), BTC 三连打地鼠 family (spread→max_spread→min_sl), Iron Law #12 (架构优先修复 — 禁止补丁累积)

**Definition**: When a model exhibits a directional pathology (e.g. ~100% LONG output) and diagnostics show near-zero class separability (Wasserstein ≈ 0), the reflex is to ADD features to inject the missing signal. This pattern names the failure mode where the target signal is STRUCTURALLY inseparable from the available feature space — adding engineered features does not raise separability. Here 7 H1-scale momentum features moved Wasserstein 0.0084→0.0019 (WORSE) and cv val_wr stayed at coin-flip (xgb 50.8% / lgbm 49.0%). Continuing to add features is whack-a-mole that burns retrain cycles without changing the outcome. The institutionally-correct response is to RETIRE the strategy line (architecturally admit the signal is dead at this timescale), not to iterate feature engineering.

**预防策略**: (1) Before a feature-augmentation retrain, run a cheap separability probe (Wasserstein / AUC on holdout) on the CANDIDATE features — GO only if it materially raises discrimination. (2) Gate feature-rescue attempts: if N consecutive augmentations fail to raise separability, retire the strategy line rather than patch again (Iron Law #12). (3) Preserve the failed experiment's serving wiring as dormant infra (not deleted) so a genuinely different signal source (order-flow / Path C) can reuse it without re-plumbing.

**检测方法**: Compare pre/post-augmentation separability in `training_summary.json` (`cv_summary.*.mean_val_wr` near 0.50 = no edge); a retrain whose val_wr stays ≈ coin-flip is a rescue failure. Watch FIX_REGISTRY for repeated same-strategy retrains with escalating feature counts and flat WR.

**Cross-References**: FIX_REGISTRY.md FIX-20260708-002, DQAF_DOCKET_REGISTRY.md DQAF-20260707-003, CCT_LEDGER.md CCT-20260708-002

---

### ReB-20260709-GET_DEFAULT_NULL_TRAP
- **Pattern Signature**: `GET_DEFAULT_NULL_TRAP`
- **Date Cataloged**: 2026-07-09
- **Source Docket**: DQAF-20260709-001
- **关联 FIX IDs**: FIX-20260709-001
- **关联 Docket IDs**: DQAF-20260709-001
- **Related**: FIX-20260613-066 (same script, same None-format class, patched at the PRINT site — the recurrence this boundary fix supersedes), FIX-20260626-144 (write-side null-label seal), Iron Law #12 (禁止补丁累积), CLAUDE.md #4 (`dict.get(key, default)` paper-over 反模式)

**Definition**: `dict.get(key, default)` substitutes `default` ONLY when the key is ABSENT; a key PRESENT with a `None` value returns `None`, not `default`. Code that uses `.get(k, sentinel)` to "guarantee a non-null value" is therefore wrong whenever the data legitimately carries `key: null`. The `None` flows past the very boundary the guard was meant to protect and detonates downstream — here it became a `pnl_by_label` dict KEY and crashed a `format(None, ':s')` call. Distinct from a missing-key bug: the key exists, so presence/schema checks pass; only the VALUE is null. A print-site guard (`or "?"`) patches ONE consumer while leaving the boundary leaking, so the same class recurs at the next unguarded consumer (FIX-20260613-066 print-site guard → this docket's Section-3 crash).

**预防策略**: (1) Normalise present-but-null at the INGESTION boundary once (`_coalesce(mapping, key, default)` = get, then `None -> default`), not at each print/consumer site. (2) For categorical fields later formatted or used as dict keys, prefer an explicit `is None` coalesce over `.get(k, default)` — use `(mapping.get(k) or default)` ONLY when falsy-but-valid values (`0`, `""`) are not meaningful for that field. (3) When a None-format crash is fixed, immediately fix the SIBLING fields built at the same construction (side/ack) in that one place — do not wait for each to crash in turn.

**检测方法**: grep `\.get\([^,]+,\s*["']` (get-with-string-default) whose result feeds a `:s`/`:d`/`:f` format or a dict key; `TypeError: unsupported format string passed to NoneType.__format__` names this class (but NOT which field — every `format(None, spec)` raises the same message, so enumerate present-but-null candidates). Regression: feed a record with `field: null` and assert the aggregation key is a `str`, never `None` (`tests/scripts/test_analyze_live_journal_null_label.py`).

**Cross-References**: FIX_REGISTRY.md FIX-20260709-001, DQAF_DOCKET_REGISTRY.md DQAF-20260709-001, CCT_LEDGER.md CCT-20260709-001

---

### ReB-20260709-BROKER_STATE_NOT_CONSULTED_BEFORE_UNTRACK
- **Pattern Signature**: `BROKER_STATE_NOT_CONSULTED_BEFORE_UNTRACK`
- **Date Cataloged**: 2026-07-09
- **Source Docket**: DQAF-20260709-002 (出场相)
- **关联 FIX IDs**: FIX-20260709-002
- **关联 Docket IDs**: DQAF-20260709-002
- **Related**: FIX-20260603-074 (_active_open_mids restart reconciliation), FIX-20260525-024 (MIA preflight — the sibling Guard 2 that DOES consult the broker), FIX-20260708-001 (immutable position_identifier join), Iron Law #12

**Definition**: A management/tracking path removes a position from its managed set (position_manager / known_open_tickets) using a LOCAL absence signal — "not in the local tracker" or "positions_get returned empty during a transient window" — and treats that absence as a confirmed close, WITHOUT consulting the broker's authoritative `positions_get` for that specific ticket. The broker is the SSOT for "is this position still open?"; the local tracker can transiently lose a still-open position (e.g. a market-closed restart where orphan re-adoption runs only at loop_iteration==1). When the false removal is also persisted to the state file (active_position.json), it desyncs permanently and the only self-healing net (restart-only orphan adoption) creates a stale-clear ↔ re-adopt PING-PONG: the position is open at the broker but never stably managed, so it never exits. Here XAU LONG 4098917446 got `position_manager_stale_cleared` ×11 while `mt5_tickets=[SHORT,LONG]` proved both open.

**预防策略**: (1) Any tracking-removal decision must be broker-authoritative: probe `positions_get(ticket)` before clearing; if still open, RE-ADOPT (self-heal) instead of clearing. (2) An inconclusive probe (MT5 timeout) is NEVER "closed" — retain and retry. (3) Extract the removal decision into a pure, unit-tested function (tracked/readopt/retain/clear) so the branch table is verifiable without the full management phase. (4) Prefer confirming a close via a broker EXIT DEAL (DEAL_ENTRY_OUT) — the same SSOT invariant PositionCloseAdapter enforces.

**检测方法**: grep for `clear_position` / `known_open_tickets.pop` whose guard is "not in known_open_tickets" or "not positions_get" with NO per-ticket broker re-probe. Runtime: `position_manager_stale_cleared` repeating for the SAME ticket across cycles + `orphan_position_adopted` for that ticket = a ping-pong. A ticket in `mt5_tickets` but absent from `active_position_tickets` is a persisted desync.

**Cross-References**: FIX_REGISTRY.md FIX-20260709-002, DQAF_DOCKET_REGISTRY.md DQAF-20260709-002, CCT_LEDGER.md CCT-20260709-002

---

### ReB-20260709-DORMANT_SAFETY_GUARD_NEVER_WIRED
- **Pattern Signature**: `DORMANT_SAFETY_GUARD_NEVER_WIRED`
- **Date Cataloged**: 2026-07-09
- **Source Docket**: DQAF-20260709-002 (进场相)
- **关联 FIX IDs**: FIX-20260709-003
- **关联 Docket IDs**: DQAF-20260709-002
- **Related**: FIX-20260705-064 (PortfolioNettingGate — the sibling guard that IS wired; order-level vs this position-level), Iron Law #12

**Definition**: A safety component is fully implemented with an active-by-default mode, but its injection point in the production assembly path defaults to `None` and no caller ever passes an instance. The guard that consumes it (`if component is not None:`) is therefore permanent dead code — the safety never runs in production despite existing, tested, and documented. Here CrossStrategyCoordinator (mode="block") shipped at P4-2 but strategy_evaluator's `cross_strategy_coordinator` param defaulted None and live_cycle never injected one, so opposing same-symbol positions were never blocked → an XAU LONG hedged an existing SHORT.

**预防策略**: (1) An optional `X | None = None` safety param is a smell — audit whether ANY production caller injects it; if none, the guard is dormant. (2) Prefer wiring safety components as non-optional dependencies, or add a startup assertion/telemetry that logs whether each safety guard is ACTIVE. (3) When adding a guard, add the injection in the SAME change as the guard, and a test that the production assembly passes a non-None instance (or lock the config default).

**检测方法**: grep for `: SomeGuard | None = None` params; then grep the production call sites for whether that kwarg is ever passed. A guard whose only reference is its own `is not None` check is dormant. Lock with a test asserting the config/default activates it (e.g. `LiveCycleConfig.cross_strategy_mode == "block"`).

**Cross-References**: FIX_REGISTRY.md FIX-20260709-003, DQAF_DOCKET_REGISTRY.md DQAF-20260709-002, CCT_LEDGER.md CCT-20260709-002

---

### ReB-20260709-R_UNIT_MISMATCH_CROSS_TIMEFRAME
- **Pattern Signature**: `R_UNIT_MISMATCH_CROSS_TIMEFRAME`
- **Date Cataloged**: 2026-07-09
- **Source Docket**: DQAF-20260709-002 (持仓相 — AR 推翻 + Deferred); DQAF-20260709-003 (止盈坍缩 — trail-TP 表现已修)
- **关联 FIX IDs**: FIX-20260709-004 (trail-TP 表现: bracket_atr 原语 + compute_trail_tp TF-scaling) + FIX-20260709-006 (几何余项: breakeven + Chandelier + graduated_lock + max_lock 全部换 bracket_atr, 阈值折叠, ratchet 不动); proximity(Sev 4 inert) + R 度量(observational) 仍 Deferred
- **关联 Docket IDs**: DQAF-20260709-002, DQAF-20260709-003
- **Related**: FIX-20260706-027 (per-timeframe ATR injection — the source of the two ATR scales), Iron Law #9 (AR 对抗反驳), 机构级 mandate #1 (禁投机修改)

**Sub-signature — PER_TF_ATR_HALF_MIGRATION** (DQAF-20260709-003): a scale-carrying parameter (ATR) is migrated to per-timeframe at the ENTRY sizing path but the migration is NOT propagated to the stored anchor (`pos.entry_atr` still M5) nor to in-flight consumers (trail/R/ratchet still M5). The bracket is thus sized in one timeframe (H4) but managed in another (M5): `compute_trail_tp`'s `tp_distance = mult × current_atr(M5) × 1.75` OVERWRITES the H4-scale TP whenever `atr_ratio ≤ 0.80`, collapsing RR 1.66 → 0.08 on h1/h4 swings (37 %/13 % of snapshots). Fix (FIX-20260709-004): carry the per-TF sizing ATR on the position (`bracket_atr`) and scale the trail-TP distance by `bracket_atr / entry_atr` — the contraction GATE stays scale-invariant, `entry_atr` stays the M5 reference for R/ratchet/MetaExit (changing it needs a backtest). The R-metric + Chandelier + breakeven + proximity-exit consumers share the same root and remain Deferred.

**Definition**: A risk/telemetry metric expressed in "R" (risk multiples) is computed with a DIFFERENT ATR than the one that sized the position's SL/TP. For a multi-timeframe strategy (h4_swing), SL = 2.0×H4_ATR (≈63.9) but the snapshot's `unrealized_pnl_r` divides PnL by the M5/entry `entry_atr` (≈6.41), inflating the reading ~10×. A position at −0.65 H4-ATR (≈26 % to its SL — a normal swing drawdown) is reported as "−6.5R", which reads as a catastrophic un-protected loss. This is a MEASUREMENT ARTIFACT, not a trading defect — acting on it (adding "losing-leg SL protection") would be a speculative behavior change grounded in a mislabelled metric. The AR step must convert the apparent R into the SL's own ATR units before concluding distress.

**预防策略**: (1) Compute "R" against the SAME ATR that sized the SL for that position/timeframe; carry `sl_atr` on the snapshot so R is self-consistent. (2) Before any exit/ratchet logic consumes `unrealized_pnl_r` as a threshold, verify the R unit matches the SL unit — a cross-TF mismatch silently mis-triggers arm/floor thresholds. (3) In diagnosis, always normalise apparent R by `SL_distance / SL_mult` (the true risk-unit) before calling a position "distressed".

**检测方法**: For any strategy, check `SL_distance / config_SL_mult` vs the `entry_atr` used in the R metric; a ~N× gap (N = TF-ATR ratio) flags the mismatch. Symptom: multi-TF (h1/h4) positions showing alarming R (−5R..−9R) while price is well inside the SL. Audit consumers of `unrealized_pnl_r` / `highest_r` in trail/ratchet/time_decay for TF-unit assumptions.

---

## Pattern: `COLD_EXPLORE_GATE_EXEMPTION`

**Sub-signature — `EXPLORATION_OVERRIDES_STRUCTURAL_CONSTRAINT`** (DQAF-20260715-011): A gate that enforces a structural market constraint (trend alignment, minimum RR, maximum drawdown) is conditioned on `not _is_cold_explore`, allowing probation/cold-start strategies to bypass it. The design assumption is that "exploration should be unconstrained to gather data," but this conflates model uncertainty (unknown p_win) with market physics (H4 trend gravity). The exemption is never architecturally justified — the gate protects against a constraint that is independent of model confidence.

**Definition**: A safety or quality gate in the decision pipeline includes `not _is_cold_explore` (or equivalent exploration-state exemption) in its activation condition. When the strategy enters cold_explore mode (MetaFilter vacuum, new probation brain, governance data insufficient), the gate silently deactivates. The strategy then trades without that constraint, accumulating losses that are misattributed to "exploration variance" rather than the missing gate.

**Known occurrence sites** (gate chain audit):
1. Counter-trend gate (`strategy_line.py:1225`): `not _is_cold_explore` — FIXED (FIX-20260715-011)
2. p_win floor gate (`strategy_line.py:1403`): `not _is_cold_explore` — DEFERRED (governance_guided escape exists)
3. RR/breakeven gate (`strategy_line.py:1485`): `not _is_cold_explore` — DEFERRED (negative EV exploration wastes capital)
4. Cold explore default volume (`strategy_line.py:1197`): `_ct_vol_mult = 0.5` — DEFERRED (half volume is reasonable, but should stack with other penalties)

**Causal mechanism**: MetaFilter vacuum (no MetaFilter routing for swing strategies post DQAF-065) → `_is_cold_explore=True` → gate condition false → gate skipped → counter-trend/bad-RR trade passes → systematic loss accumulation. The cold_explore state can persist for weeks/months if governance data never accumulates (Catch-22: need trades to get p_win, need p_win to pass gates).

**预防策略**: (1) **Never exempt exploration state from structural constraints.** Trend alignment, minimum RR, and maximum risk are market physics — independent of model confidence. Cold exploration should reduce VOLUME (uncertainty penalty), not bypass constraints. (2) When adding a new gate, include the question: "Does this gate protect against model uncertainty or market physics?" If market physics → no cold_explore exemption. (3) Audit all `not _is_cold_explore` conditions in gate chain — each one is a candidate for removal. (4) Multiplicative penalty stacking: cold volume reduction (0.5) × gate penalty (0.70) = 0.35, not 0.70 overriding 0.50.

**检测方法**: `grep "not _is_cold_explore" core/execution/strategy_line.py` — every hit is a gate that silently deactivates during cold exploration. For each hit, ask: "Would I want my capital protected from this even if the model is unproven?" If yes → remove the exemption. Also: `grep "counter_trend" data_btc/golden_master.jsonl` — zero matches over a multi-week window while counter-trend trades are executing → gate is bypassed.

**Cross-References**: DQAF_DOCKET_REGISTRY.md DQAF-20260715-011, CCT_LEDGER.md CCT-20260715-011; Deferred: p_win floor exemption + RR breakeven exemption audit

**Cross-References**: DQAF_DOCKET_REGISTRY.md DQAF-20260709-002, CCT_LEDGER.md CCT-20260709-002; Deferred: R-metric ATR consistency + bars_held restart continuity

---

## ReB-20260722-002: PnL-Based Label Poisoning (Telemetry Black Hole)

- **Pattern**: Exit label is derived from PnL sign rather than the causal exit signal carried in the MT5 deal comment. The deal comment is populated by dispatch_managed_close(reason=...) but the adapter ignores it when reason ≠ "exit_watchdog:".
- **Signature**: grep 'label.*\"(win|loss)\"' in close-detection code → any hit that doesn't first check deal_comment/deal_reason is a black hole.
- **Impact**: 100% of managed closes without watchdog prefix are labeled "win"/"loss" → downstream p_win calibration receives no signal provenance → Bayes update becomes PnL-fit noise.
- **Detection**: `grep -n 'label.*=.*"loss"\|label.*=.*"win"' core/runtime/` — zero remaining after FIX-20260722-002.
- **Status**: **CLOSED** (FIX-20260722-002)

## ReB-20260722-003: Cross-TF ATR Activation Mismatch (Trail Silence)

- **Pattern**: Risk exit activation watermark uses a different ATR scale than the R-measurement scale. When bracket_atr >> entry_atr (H1/H4 strategies), trail_activation_atr threshold is effectively unreachable.
- **Signature**: `_resolve_geometry_atr()` returns bracket_atr; activation check divides price_move by bracket_atr; ratchet divides price_move by entry_atr. The two scales diverge by ~10× for H4.
- **Impact**: Higher-TF positions have no trailing stop protection. The Chandelier trail, breakeven, graduated lock, and ratchet floor are all gated behind an unreachable activation threshold.
- **Detection**: Check position_snapshots.jsonl for positions with MFE≥2R but current_sl==initial_sl throughout → trail never fired.
- **Status**: **CLOSED** (FIX-20260722-003)

## ReB-20260730-011: Journal PnL Dual-Writer Race with Starved Correction Path

- **Pattern**: `JOURNAL_PNL_DUAL_WRITER_RACE_WITH_STARVED_CORRECTION_PATH`
- **Date Cataloged**: 2026-07-30
- **Source Docket**: DQAF-20260730-011
- **Related FIX**: FIX-20260730-011

**Definition**: Two independent writers (Bridge + Reconciliation) compete to write the same journal field (`pnl`) with different data quality. Writer A (Bridge) writes immediately after execution with potentially estimated/incorrect data because the ground truth hasn't settled yet (async clearance). Writer B (Reconciliation) has access to authoritative ground truth but its correction path is starved because the tracking state (`known_open_tickets`) is cleared before Writer B runs. Writer A produces 99%+ of entries; Writer B produces <1%. The journal field's quality degrades to Writer A's estimate quality, and the `_pnl_status` provenance tag is absent from >60% of historical entries (added later by FIX-20260716-005).

**Root mechanism**: (1) Async settlement window — MT5 deal.profit is populated asynchronously after order_send() returns. (2) Silent fallback — Bridge uses engine's mid-price estimate when deal.profit unavailable, writes without provenance tag. (3) Correction starvation — management phase clears known_open_tickets after dispatch before reconciliation can detect the close and write corrected PnL. (4) Committee architectural mandate: Broker state ("清算状态") and trading state ("交易状态") must never share the same memory pool.

**Prevention**: Settlement Queue Isolation — three-state lifecycle with physical separation: `known_open_tickets` (active positions, engine-managed) → `pending_settlement_tickets` (awaiting settlement, engine MUST NOT touch) → settled (verified PnL written, removed from queue). Bridge writes `pnl=null` + `_pnl_status="pending_mt5_settlement"` as default; Reconciliation is SOLE authority for writing non-null PnL. Zombie protection: 4-tier timeout escalation with degraded writes and terminal alerts.

**Detection**: Check `_pnl_status` distribution — if >5% of recent close entries have `pending_mt5_settlement` for >1hr, settlement queue is stalled. Monitor `pending_settlement_tickets` queue size — >10 pending = CRITICAL (MT5 connectivity likely lost). Cross-validate journal PnL vs MT5 broker report monthly.

---

### ReB-20260801-SCHEMA_ROUTING_MISSING_NEW_SCHEMA
- **Pattern Signature**: `SCHEMA_ROUTING_MISSING_NEW_SCHEMA`
- **Date Cataloged**: 2026-08-01
- **Source Docket**: DQAF-20260801-006
- **Related**: FIX-20260801-007, FIX-20260731-004 (schema registration), FIX-20260801-001 (whitelist), FIX-20260610-009 (3 dispatch site unification), FIX-20260528-022 (swing_enhanced_35 capability)

**Definition**: A new feature schema is registered in the SSOT registry (`SCHEMA_DIMENSIONS`) and possibly the FeatureService whitelist, but NOT wired into the runtime dispatch pipeline. Because schema routing is implemented as scattered string-matching conditions (`"btc_macro" in schema_id`) at multiple call sites, a schema name that matches none of the keywords silently falls back — for BTC brains to the raw 40-dim V9 vector, producing a dimension mismatch every cycle (model rejects → zero signals / brain goes blind) or, worse, silently-wrong-dimension vectors. The positional zip in `build_lake` (zipping subset schema names against the full 41-dim augmenter output) adds a second silent corruption mode: 29/37 features shifted by the number of deleted placeholder slots.

**Root mechanism**: (1) Schema routing is N hardcoded string conditions across feature_assembler + live_cycle + management_phase + swing_strategy — adding a schema requires touching all sites or the new name falls through. (2) The feature router's lake is built by position-zipping the requested schema's name list against the canonical full-dim vector; subset schemas (37 = 41 minus 4 placeholders) misalign after the deletion point. (3) The btc_augment pass-through gates (`"btc_macro" in schema`) duplicate the routing conditions and miss the new schema independently. (4) No automated test asserts that every registered schema is reachable through the full dispatch chain.

**Prevention**: (1) build_lake is now canonical — ALWAYS binds the full 41-dim canonical names → values; subset schemas extract BY NAME at dispatch (position-independent lake). (2) Schema dispatch conditions centralized: every new schema must be added to SCHEMA_CONTRACTS + the routing condition (documented in registry.py "Adding a new schema" checklist). (3) Regression guards: `scripts/_verify_expected_r_routing.py` (17 asserts: lake alignment, dispatch order vs training extraction, legacy shim/v2 bit-identical) + `_verify_expected_r_e2e.py` (real adapter inference, no dimension mismatch) — run after any schema change.

**Detection**: Grep for `feature_dimension_mismatch` in live logs (expected=37 got=40 → schema fell to V9 fallback). Assert `len(SCHEMA_CONTRACTS) == count of active brain schemas`. Run the two verify scripts after any schema/routing change.

### ReB-20260801-WHITELIST_LAGGING_RUNTIME_KEYS
- **Pattern Signature**: `WHITELIST_LAGGING_RUNTIME_KEYS`
- **Date Cataloged**: 2026-08-01
- **Source Docket**: DQAF-20260801-008
- **Related**: FIX-20260801-008

**Definition**: A static validation whitelist (allowed keys/values) drifts out of sync with the keys the runtime actually consumes. Runtime code adds a new config key (`ev_trajectory_enabled` gating `should_exit_time_based()`), the runtime reads it, but the validator's `_EXPECTED_EXIT_KEYS` is never extended → every strategy line setting that key triggers a spurious `unknown keys` startup warning (or worse, a spurious reject). Symptom: `strategy_lines.<name>.exit: unknown keys ['ev_trajectory_enabled']` while `management_phase.py` demonstrably reads the same key. The whitelist is a contract that must be kept in lockstep with runtime consumption — the failure mode is a validator that penalizes valid configuration.

**Root mechanism**: Whitelists are manually maintained literal sets. Any runtime feature addition that introduces a config knob is a 2-site change (runtime read + whitelist) — if the second site is missed, the system reports healthy config as invalid.

**Prevention**: (1) When adding a runtime config key, grep `_EXPECTED_EXIT_KEYS` and update it in the same change (the validator even tells you the missing key — the warning string is the TODO list). (2) A unit test that asserts every `_exit_cfg.get(...)` key in runtime has a corresponding whitelist entry. (3) Run `validate_strategy_exit_configs(live.yaml strategy_lines)` after any exit-feature change — it must be empty.

**Detection**: Grep `_EXPECTED_EXIT_KEYS` vs `_exit_cfg.get(`/`exit_cfg.get(` across core/runtime — the diff is the missing set. Or simply run the validator against live yaml.

### ReB-20260801-STRANGLER_FIG_CALLER_NOT_MIGRATED
- **Pattern Signature**: `STRANGLER_FIG_CALLER_NOT_MIGRATED`
- **Date Cataloged**: 2026-08-01
- **Source Docket**: DQAF-20260801-008
- **Related**: FIX-20260801-008, FIX-20260619-022 (Strangler Fig #22 extraction)

**Definition**: A Strangler Fig refactor extracts a function/module to a canonical home, but the actual runtime call sites still import the OLD location, where a duplicate definition lingers. The extraction created `strategy_config_validator.py` with the improved whitelist, but `live_intent_loop.py:442` kept importing `validate_strategy_exit_configs` from `core.runtime.live_cycle` — which retained its own stale `_EXPECTED_EXIT_KEYS` and old function body. Fixing only the canonical module fixes nothing at runtime; the divergence is invisible until someone edits the canonical copy and the behavior doesn't change. It is the Iterability anti-pattern (同一逻辑分散多文件) materialized.

**Root mechanism**: Strangler Fig "keep the old entry working" creates a re-export or leaves the old definition; if the migration step (pointing callers at the new home) is deferred, the codebase permanently carries two copies that drift apart.

**Prevention**: (1) Strangler Fig completion checklist MUST include migrating all callers to the new module and deleting the old definition — verified by `grep -r "from <old_module> import <symbol>"`. (2) After extraction, grep the symbol name and confirm exactly one definition site. (3) A static check that the canonical module's public symbol is imported by callers, not the duplicate.

**Detection**: `grep -rn "from core.runtime.live_cycle import.*validate"` → should be empty (callers import from `strategy_config_validator`). Duplicate definitions of the same function name in two modules is the trigger.

### ReB-20260801-TELEMETRY_TTL_VS_BATCH_CONTRACT
- **Pattern Signature**: `TELEMETRY_TTL_VS_BATCH_CONTRACT`
- **Date Cataloged**: 2026-08-01
- **Source Docket**: DQAF-20260801-008
- **Related**: FIX-20260801-008, FIX-20260628-156 (freshness contract), FIX-20260622-057a (TTL tightening)

**Definition**: A freshness contract intended for batch-produced artifacts (TTL ≥ scheduler max_age + buffer) is applied uniformly to ALL catalog artifacts, including real-time telemetry produced every live cycle (`EXECUTION_STATE` 30min, `MT5_BRIDGE_HEALTH` 15min, `ALERT_COOLING` 2h). Telemetry TTLs are deliberately SHORT (tightened by DQAF-057 for fast staleness detection) and their producers run far more often than the batch scheduler — so the batch contract's premise ("artifact can legally age past TTL before next producer run") is false for them. Result: 3 false-positive `Freshness Contract VIOLATION` warnings at every startup, permanently, until the namespace is split.

**Root mechanism**: A single producer model (one daily_ops scheduler governs all) was assumed for all artifacts. Real-time artifacts have per-cycle producers with different cadence — they need their own freshness namespace (TTL vs own producer interval), not the batch max_age contract.

**Prevention**: (1) `StateArtifact.producer_class` field: `batch` (subject to `TTL ≥ scheduler_max_age + buffer`) vs `telemetry` (excluded from batch contract; freshness enforced at runtime by `freshness_guard` against own TTL). (2) Any new real-time artifact must be declared `producer_class="telemetry"`. (3) A catalog test asserting every artifact with TTL < batch max_age is telemetry (or has a documented producer).

**Detection**: `validate_freshness_contract(scheduler_max_age)` must return 0 violations — if it flags EXECUTION_STATE/MT5_BRIDGE_HEALTH/ALERT_COOLING, the telemetry/batch namespace split was lost.

### ReB-20260801-POLICY_CONFLICT_THROTTLE_VS_CONFIG_FLOOR
- **Pattern Signature**: `POLICY_CONFLICT_THROTTLE_VS_CONFIG_FLOOR`
- **Date Cataloged**: 2026-08-01
- **Source Docket**: DQAF-20260801-010
- **Related**: FIX-20260801-011, FIX-20260801-012, FIX-20260628-162, FIX-20260611-001

**Definition**: Two independent governance policies give contradictory verdicts on the same brain: a risk-control throttle (e.g. `profit_factor < throttle_pf=0.80` on window-100) correctly wants demotion, while Iron Law #14 config floor (human SSOT says live) + daily_ops all-time-healthy pull-back keep restoring live. Neither is "wrong" — the system lacks an arbitration/exemption layer. BTC_Swing_V4 oscillated live↔probation since 07-09; a dry-run proved even a SINGLE SSOT writer throttles V4 policy-correctly, disproving the "dual-track data source is the root cause" theory.

**Root mechanism**: Risk machinery (short-window PF/WR gates) and strategic intent (IC observation window, config floor) are separate policy planes with no explicit priority rule. A single correct demotion engine firing on the right data still conflicts with a human-imposed observation hold.

**Prevention**: (1) Express IC strategic observation windows as explicit `observation_hold_until` config contracts (L1 SSOT), honored at the SOLE WRITER choke point (`GovernanceRuleEngine.execute_transitions`). (2) Never "fix" a policy conflict by removing/weakening the risk rule — add an explicit exemption layer instead. (3) Before assuming a dual-writer race, dry-run the SSOT evaluator on real data to check whether the single writer would already make the disputed decision (Iron Law #11 script-first).

**Detection**: `_verify_governance_evaluator.py` — asserts a held brain's throttle is refused at the sole writer; a recurring live↔probation oscillation in governance transition_log for one brain is the trigger symptom.

### ReB-20260801-DUAL_TRACK_WRITER_OSCILLATION
- **Pattern Signature**: `DUAL_TRACK_WRITER_OSCILLATION`
- **Date Cataloged**: 2026-08-01
- **Source Docket**: DQAF-20260801-010
- **Related**: FIX-20260801-011, FIX-20260611-001, FIX-20260517-017, FIX-20260628-168

**Definition**: Multiple independent governance writers (a patched direct-write in the runtime loop, a daily_ops scheduler, a startup reconciler) each evaluate the same brain on different data windows (last-20 BrainPnLStore vs window-100 brain_performance vs all-time PnL) and each writes `GovernanceService.transition()` directly — bypassing the rule engine. Contradictory transitions applied alternately → the brain oscillates between statuses.

**Root mechanism**: A bridge patch (FIX-20260611-001) wired a direct writer into the launcher path because GovernanceRuleEngine wasn't available there; later the same pattern was duplicated (daily_ops third rail). Each writer is "correct" for its own data source; the conflict is structural (no single executor).

**Prevention**: (1) ONE Auditor (brain_performance SSOT) + ONE Executor (`GovernanceRuleEngine.execute_transitions`) — Iron Law #14. (2) All deployment paths (container + bare-metal launcher + daily_ops) delegate to the shared `governance_evaluator.evaluate_governance_state()`. (3) Direct `GovernanceService.transition()` from runtime/scheduler code is forbidden except via the executor; grep `governance.transition(` outside governance_evaluator/rule_engine to catch stragglers.

**Detection**: `grep -rn "\.transition(" core/ scripts/ --include="*.py"` filtered to non-rule-engine callers; governance transition_log showing rapid alternation for a single brain is the runtime signature.

### ReB-20260801-FEED_STALL_MISCLASSIFIED_AS_MARKET_CLOSED
- **Pattern Signature**: `FEED_STALL_MISCLASSIFIED_AS_MARKET_CLOSED`
- **Date Cataloged**: 2026-08-01
- **Source Docket**: DQAF-20260801-011
- **Related**: FIX-20260801-013, FIX-20260629-172, FIX-20260601-042

**Definition**: A dynamic market-open detector (tick-timestamp probe) classifies a mid-session data-feed stall as "market closed" because both present as zero fresh ticks. The system then degrades silently (synthetic bars, session-off gate, all-zero feature guard) instead of alerting on the real fault. XAU 2026-08-01: MT5 feed died at 00:41:25Z during an open trading day; the probe returned risk_tier=off for 20 hours; bar sync ran 810/811 synthetic; feature store froze; bridge_silence tripped the circuit breaker into management_only for the whole session.

**Root mechanism**: The probe measures *physical tick activity* but collapses two distinct states — "market legitimately closed" (benign, weekend/scheduled close) and "feed dead mid-session" (fault) — into the single `risk_tier=off` signal, with no cross-check against wall-clock market schedule.

**Prevention**: (1) Distinguish the two states: session-off should be cross-validated against the calendar schedule (weekday+hour) before classifying as closed; a stall during scheduled-open hours must raise a distinct FEED_STALL alert. (2) Feature freshness must be a hard inference gate (StaleFeatureException, 3-bar threshold) rather than silent last-known fallback. (3) Do not let the feature-write exception handler swallow errors (`except...pass` at live_cycle.py:3330) — log and alert.

**Detection**: `bar_sync_synthetic` ratio → 1.0 during scheduled market-open hours; `BAR_SESSION_OFF` events during weekday open hours; feature store last event_time freezing while intent loop cycles continue.

### ReB-20260801-LIVENESS_ACK_CIRCULAR_DEPENDENCY
- **Pattern Signature**: `LIVENESS_ACK_CIRCULAR_DEPENDENCY`
- **Date Cataloged**: 2026-08-01
- **Source Docket**: DQAF-20260801-011
- **Related**: FIX-20260801-013, FIX-20260608-008

**Definition**: A liveness/heartbeat timestamp is updated inside the very phase the circuit breaker it feeds is allowed to skip. `_last_bridge_ack_time` is only refreshed by `management_phase` (fetch_prices success / mid>0); when bridge_silence trips the breaker, management_phase is bypassed → the ack never refreshes → the breaker cannot reset → the system is stuck in management_only until an unrelated path happens to refresh the ack. Observed as 37 trips + 36 resets across the 08-01 XAU session.

**Root mechanism**: The ack producer and the ack consumer share a single downstream control-flow gate, so the failure the consumer guards against also suppresses the producer — a self-sustaining trip state.

**Prevention**: (1) Update the liveness ack from a path that is NOT gated by the circuit breaker (e.g., the bridge/fetch layer itself, independent of phase sequencing). (2) Session-aware breaker: skip silence-trip during scheduled market close (weekend/daily break) — the ack staleness is expected there. (3) A stuck breaker should auto-expire (max cooldown) rather than require ack refresh.

**Detection**: `circuit_breaker_bridge_silence_trip` followed by `circuit_breaker_reset` cycling >3× in one session while `_last_bridge_ack_time` stays frozen; management_only_mode persisting through scheduled-open hours.

### ReB-20260802-ZERO_VOTE_WR_POOL_PENETRATION
- **Pattern Signature**: `ZERO_VOTE_WR_POOL_PENETRATION`
- **Date Cataloged**: 2026-08-02
- **Source Docket**: DQAF-20260802-002
- **Related**: FIX-20260802-001

**Definition**: A brain stripped of voting rights (governance `vote_weight<=0` — muted / observation-only) still contributes its historical win rate to the strategy-line's EV/win-rate pool, anchoring the ensemble EV estimate to the weakest link ("短板穿透"). A brain that cannot issue buy/sell orders must not drag the EV estimate of the brains that can.

**Root mechanism**: The p_win pool filter used `live_brain_ids` (status-based) but not the voting-weight contract. Vote weight and EV contribution were treated as independent axes when they are the same boundary: a muted brain is observation-only, so its historical WR is not a valid EV source.

**Prevention**: (1) EV pools (rolling WR + governance cold-start + sample-significance) MUST filter by `vote_weight > 0` via a single shared helper (fail-open on missing/malformed weight, mirroring `brain_gates.count_valid_voters`). (2) Any new brain-aggregation layer (ensemble EV, consensus, sample counting) applies the same voting-boundary filter. (3) Diagnostic log on exclusion count for observability.

**Detection**: cold_explore/rolling_wr p_win resolves exactly to a muted brain's governance WR; governance_state has `brain_states[*].vote_weight <= 0` while that brain's WR appears in the pool.

### ReB-20260802-SYMMETRIC_SL_TP_PLUS_SPREAD_INEQUALITY
- **Pattern Signature**: `SYMMETRIC_SL_TP_PLUS_SPREAD_INEQUALITY`
- **Date Cataloged**: 2026-08-02
- **Source Docket**: DQAF-20260802-003
- **Related**: FIX-20260802-002, DQAF-20260709-003 (XAU TP collapse — same family, different mechanism)

**Definition**: A symmetric SL=TP exit geometry (`base_sl_atr_mult == base_tp_atr_mult`) combined with spread asymmetry (TP narrowed, SL widened by spread_cost) makes the post-spread RR = (d−spread)/(d+spread) < 1.0 mathematically unavoidable. Breakeven WR = 1/(1+RR) rises above any realistic model accuracy, making the strategy line structurally unprofitable regardless of signal quality.

**Root mechanism**: Training/serving labels are generated against symmetric barriers, so the model's achievable accuracy is calibrated to RR≈1.0. Spread then makes the effective payoff worse than the model was trained on. Unlike the XAU TP collapse (per-TF ATR mismatch, DQAF-20260709-003), this is a config/geometry design defect, not a computation bug.

**Prevention**: (1) Any symmetric SL=TP contract must be audited for post-spread RR<1.0 (breakeven check: model accuracy must exceed 1/(1+RR)). (2) builder defaults MUST equal the serving yaml SSOT (`EXPLICIT_BETTER_THAN_IMPLICIT_CONFIG`) so the real geometry is visible. (3) For sub-50% accuracy models, prefer asymmetric geometry (TP>SL) or strategic retirement over re-training with symmetric barriers.

**Detection**: runtime `rr_ratio` resolves to a value < 1.0 whose decimal matches (d−spread)/(d+spread) for the configured SL/TP distances; breakeven WR 1/(1+rr) exceeds all constituent brains' win rates.

### ReB-20260802-LIVENESS_PROXY_STAMPED_ONLY_IN_DEGRADED_PATHS
- **Pattern Signature**: `LIVENESS_PROXY_STAMPED_ONLY_IN_DEGRADED_PATHS`
- **Date Cataloged**: 2026-08-02
- **Source Docket**: DQAF-20260802-004
- **Related**: FIX-20260802-004, FIX-20260608-006, ReB-20260801-LIVENESS_ACK_CIRCULAR_DEPENDENCY (base half-ring — fixing this docket also resolves 011's self-sustaining loop)

**Definition**: A liveness/heartbeat timestamp that is documented as "last successful <probe>" (live_cycle.py:382: `_last_bridge_ack_time` = "Unix ts of last successful broker.fetch_prices()") is only ever stamped inside degraded or management-gated branches (live_cycle.py:1310 tripped-branch, management_phase.py:1306/1312 position-gated). The healthy idle normal path — the state a live system occupies most often (0 open positions) — calls the probe every cycle and succeeds, but never refreshes the ACK. The silence counter grows monotonically (~300s/M5-cycle) until it crosses `max_bridge_silence_seconds` (600s) → false `bridge_silence` trip → management_only → the tripped branch finally stamps → cooldown reset → normal path freezes again → infinite oscillation (BTC every ~35min / XAU every ~22.5min; 20 trips in Friday trading hours).

**Root mechanism**: The probe's write sites are placed at the *failure* endpoints rather than the *success* path of the probe's own contract. The documented semantics ("last successful fetch") and the actual semantics ("last time a degraded branch ran") diverge, so the probe measures degraded-branch activity, not liveness. This is the complement of ReB-20260801-LIVENESS_ACK_CIRCULAR_DEPENDENCY (which catalogs the stuck-in-trip half of the same field's failure).

**Prevention**: (1) Stamp the liveness ACK at the probe's success path itself (the normal-path `broker.fetch_prices()` success and the L3 `_mid_and_prices()` fallback success), mirroring the management_phase pattern — success-only, exception paths leave the ACK stale so true bridge-death detection is preserved. (2) After wiring any heartbeat/ack field, audit ALL call sites of the probe it documents against the field's stated contract — a probe with write sites gated behind the very condition it monitors is a smell. (3) Session-aware silence-trip exemption for scheduled market close (deferred).

**Detection**: `circuit_breaker_bridge_silence_trip` cycling with near-constant cadence (~35min for M5 cycles) while `mt5_bridge_health.json` heartbeat stays fresh and live prices keep flowing; trip count spikes during trading hours with 0 positions open.

### ReB-20260804-PERSISTENCE_BEFORE_EXECUTOR
- **Pattern Signature**: `PERSISTENCE_BEFORE_EXECUTOR`
- **Date Cataloged**: 2026-08-04
- **Source Docket**: DQAF-20260804-001
- **Related**: FIX-20260804-001, FIX-20260801-011, FIX-20260801-012

**Definition**: A state-persistence `save()` call is placed BEFORE the executor that applies in-memory side effects (`execute_transitions`), with NO save after. Every cycle the "before" snapshot is written (perf metrics injected), then the executor mutates the in-memory object graph, then the cycle ends — the next reload reads the pre-executor snapshot and discards the transitions. The system *computes* the correct outcome every cycle but never *commits* it: governance_state.json never converges, and a human reading the file concludes the automation is broken when in fact the automation ran 1243 times. DQAF-20260804-001 evidence: 1243× `BTC_Swing_V4: live → probation (throttle)` in logs vs V4=live in state.

**Root mechanism**: The save site was placed at the *auditor output* stage (after metrics injection) rather than the *executor completion* stage. The cycle's durable boundary was chosen before the mutation boundary. Any orchestrator that (1) loads state, (2) injects derived data, (3) saves, (4) executes transitions, (5) returns — without a save after (4) — reproduces it.

**Prevention**: (1) In any audit→executor cycle, the save must come AFTER the executor, covering BOTH the injected data AND the transitions (a single post-transition save subsumes the pre-save). (2) When closing a loop that "runs but never converges", diff the file's mtime/contents against the executor's log — a live log of transitions with an unchanged state file is the signature. (3) State-mutation loops need an assertion/verification that the written file reflects the executed outcome (a reload-and-check), not just that a save was called.

**Detection**: A governance/state log shows transitions firing every cycle while the corresponding state file's status field never changes; file mtime equals the pre-executor save time (perf-injection window), not the post-executor time.

### ReB-20260804-STRUCTURED_ARRAY_ROW_AS_DICT
- **Pattern Signature**: `STRUCTURED_ARRAY_ROW_AS_DICT`
- **Date Cataloged**: 2026-08-04
- **Source Docket**: DQAF-20260804-002
- **Related**: FIX-20260804-002, FIX-20260625-137

**Definition**: Code calls `.get("close", default)` on the row of a library-returned structured array (numpy dtype), treating `rates[i]` as a dict. `rates[i]` is a `numpy.void` scalar which has no `.get()` → AttributeError, caught by a broad `except` → silent zero-fill. Because the failure is swallowed and the code "works" (returns 0.0), the defect runs for thousands of cycles while persisted feature-store records accumulate zeroed slots. DQAF-20260804-002 evidence: 1600+ `'numpy.void' object has no attribute 'get'` on BTC/XAU ratio + AUDJPYc paths.

**Root mechanism**: The row-return shape of the data source (numpy structured array) diverges from the dict-shaped assumption baked into the accessor. The `except Exception` around it converts a loud crash into a silent wrong value — the degradation path hides the bug's existence.

**Prevention**: (1) Access structured-array rows by field name (`row["close"]`), not `.get()`; write a single `_bar_close(rates, idx)` helper that normalizes dict-row AND numpy.void-row access (with negative-index normalization) — one convergent point for all cross-asset rate reads. (2) Add a regression lock that feeds the ACTUAL production shape (numpy structured array) through the exact code path — the mock must reflect the real source's return type. (3) Never let a degradation path silently swallow the first N occurrences: the root-cause class (attribute mismatch) deserves a distinct log/alert so it surfaces early.

**Detection**: A log line like `failed to fetch <SYMBOL>: 'numpy.void' object has no attribute 'get'` repeating thousands of times while the corresponding feature slots are persistently 0.0 in persisted records.

### ReB-20260804-API_CONTRACT_MAGICMOCK_BLINDSPOT
- **Pattern Signature**: `API_CONTRACT_MAGICMOCK_BLINDSPOT`
- **Date Cataloged**: 2026-08-04
- **Source Docket**: DQAF-20260804-002
- **Related**: FIX-20260804-002

**Definition**: Unit tests mock a dependency with bare `MagicMock()` and assert behavior through `.get_latest(...)` — a method that does not exist on the real dependency. `MagicMock` absorbs ANY method call and returns a child mock, so the test passes while production fails with `AttributeError: 'FeatureService' object has no attribute 'get_latest'`. The mock encodes the *buggy* API contract, creating a test blind spot: the suite is green, the pipeline is broken, and the divergence survives for 1600+ cycles.

**Root mechanism**: The mock's API shape is invented by the test author rather than derived from the real type. MagicMock's permissive interface never validates that the mocked method exists on the real object.

**Prevention**: (1) Mock with `spec=<RealType>` so only genuine attributes are allowed (`MagicMock(spec=LocalFeatureStore)`) — the mock fails fast when the test calls a nonexistent API. (2) Test helpers must return the REAL data shapes (FeatureRecord dataclass, numpy structured arrays), not dict look-alikes. (3) When a method "can't be found" on the real type, fix the code or the mock — never both silently diverge.

**Detection**: A production `AttributeError`/`TypeError` for an API that unit tests call freely without error — the test suite's pass/fail never exercised the real interface.

### ReB-20260804-SPORADIC_FEED_VS_MT5_SSOT
- **Pattern Signature**: `SPORADIC_FEED_VS_MT5_SSOT`
- **Date Cataloged**: 2026-08-04
- **Source Docket**: DQAF-20260804-003
- **Related**: FIX-20260804-003

**Definition**: A feature that semantically needs the CURRENT price of a cross-symbol asset reads a feature store that is only fed sporadically (hours apart), while its sibling cross-asset features read the live MT5 terminal directly. The staleness guard (correctly) zero-fills the feature ~100% of the time — the slot is structurally dead not because the fix is wrong, but because the DATA SOURCE was chosen wrong: a sparse feed and a freshness guard are mutually exclusive by design. MT5 is the single source of truth for live prices; a periodic cross-symbol feed is distributed-systems over-engineering for a one-feature need.

**Root mechanism**: Cross-asset features inside one compute unit drifted onto two different data paths (feature store vs MT5 direct) with no architectural consistency gate. The store path looks "safer" (cached, local) but has a cadence the guard can't tolerate.

**Prevention**: (1) All cross-asset live prices in a compute unit must read the SAME source — MT5 direct (`copy_rates_from_pos` + a shared row-read helper). (2) Before adding a "staleness guard" to a store read, verify the store's feed cadence is ≤ the guard tolerance; if not, the source is wrong, not the guard. (3) A guard firing 100% of the time is a bug report about the FEED, never "working as intended".

**Detection**: A feature slot恒零 while sibling slots from the same logical group are live and continuous; a debounced staleness/zero-fill warning that never stops firing; persisted feature-store records where one cross-asset column is all-zeros across the whole file.

### ReB-20260804-CONTRACT_WITHOUT_RUNTIME_EVALUATOR
- **Pattern Signature**: `CONTRACT_WITHOUT_RUNTIME_EVALUATOR`
- **Date Cataloged**: 2026-08-04
- **Source Docket**: DQAF-20260804-005
- **Related**: FIX-20260804-005, FIX-20260803-007

**Definition**: A training pipeline emits a complete brain config for a novel model architecture (here: freeze-and-residual, base+residual composition) but the runtime adapter layer has no evaluator for that physical structure. The brain registers as shadow with full lineage (Phase 5), yet the generic adapter route (BRAIN_TYPE_MAP → LightGBMBrainAdapter) loads only one of the two model files → dimension guard fallback (silent neutral, raw_score=0.0), OR the config fails the validator (ghost-brain ERROR from missing training_params.objective). The system produces a model it cannot execute; the "dead object" survives indefinitely because nothing crashes loudly — it just never votes. DQAF-20260804-005 evidence: Flow46 brain registered 2026-08-03 with OOS ρ=0.0721, never emitted a single signal; validator `_infer_objective_from_artifact` returns None for brain_type=expected_r_short (not startswith "lightgbm") → BrainConfigError at load.

**Root mechanism**: brain_type encodes SIGNAL semantics (expected_r_short → Path 5 voting) and routes to a generic adapter that can load a single artifact. The config's PHYSICAL structure (transfer.kind=freeze_and_residual, two boosters) needs a bespoke composition adapter. Without a dispatch seam keyed on physical structure (not signal type), the model is inert.

**Prevention**: (1) Any config carrying a `transfer` block describing multi-artifact composition MUST have a matching adapter dispatch — BrainFactory reads `transfer.kind` and instantiates the composition adapter (Method A, IC 2026-08-04 ruling: brain_type = signal semantics, transfer = physical structure). (2) The registration gate should verify adapter resolvability at registration time, not defer to load time. (3) A shadow brain that produces zero signals across its whole observation window is a bug report about missing runtime wiring, never "model is quiet".

**Detection**: A brain registered with full lineage but `describe()` showing `backend` never reaching a transfer-aware string; `_num_features` = the single-artifact dimension while the schema is larger; validator ghost-brain ERROR (missing training_params.objective) on a config that should be valid.

---

### ReB-20260804-MODEL_OUTPUT_DEGENERACY_SHORT_COLLAPSE
- **Pattern Signature**: `MODEL_OUTPUT_DEGENERACY_SHORT_COLLAPSE`
- **Date Cataloged**: 2026-08-04
- **Source Docket**: DQAF-20260804-006
- **Related**: FIX-20260629-184 (DQAF-20260629-P03), FIX-20260804-006
- **定义**: 回归/分类模型输出坍缩为近恒定方向与置信度 — 全部信号同向 (SHORT), 置信度唯一值极少 (uniq=1~7) 或 accuracy≈随机。XAU 案例: H1_Exec_A 155/155 SHORT, M15_V7_binary 恒定 0.783 (uniq=1), M30_V5 test-acc 0.3396≈随机却 live。第三次历史实例 (前: BTC Swing_V10_H1_Directional 100% SHORT → 冻结)。
- **上游触发**: 输入特征 out-of-distribution (本例: D1 特征毒井喂 BTC 价) 或标签偏置 (asymmetric SL/TP 合约 61.5% SHORT label)。
- **预防**: (1) 方向浓度监控器 per-asset 正常报警 (≥90% 同向 → DingTalk); (2) 模型注册/晋升门禁加输出多样性检查 (方向熵 + conf 唯一值下限); (3) accuracy≈随机 (<0.4) 禁止 live。
- **检测**: `scripts/audits/_audit_xau_votes_today.py` 置信度唯一值 ≤3 或单方向 ≥95% → 告警; audit_xau_directional_bias.py 长期方向比。

---

### ReB-20260804-D1_WELL_CROSS_ASSET_POISONING
- **Pattern Signature**: `D1_WELL_CROSS_ASSET_POISONING`
- **Date Cataloged**: 2026-08-04
- **Source Docket**: DQAF-20260804-006
- **Related**: FIX-20260804-006, CROSS_ASSET_CONTAMINATION_AUDIT.md (DQAF-20260615-006 H1)
- **定义**: 双进程共享代码, 一方硬编码另一方的 CSV 路径 (`live_intent_loop.py:759` d1_csv 恒指 XAU) → BTC 进程的 `LiveDailyProvider._sync_csv()` 按 BTC symbol 抓取 bar 追加进 XAU 文件 → date-keyed dedup 使 XAU 正确 bar 无法回填 → XAU 日线尾部被 BTC 价格毒化 → 所有消费 D1 特征的模型 out-of-distribution。污染行数 = 07-04 起全月。
- **预防**: (1) 数据文件路径必须由 base_dir/symbol 派生, 禁止跨资产硬编码 (CROSS_ASSET_CONTAMINATION_AUDIT H1 修复); (2) LiveDailyProvider `_sync_csv` 写前校验 `self._symbol` 与 CSV 文件名符号一致, 不符 → 拒绝写入 + SEVERE 告警 (FIX-20260804-007 `_assert_d1_symbol_contract` 已实现 — DataIntegrityError 熔断); (3) 数据质量守卫: 日线 close 超出该资产合理量级 (XAU 4-5k vs BTC 60k+) → fail-fast。
- **检测**: 全仓数据审计脚本 — 对每资产 D1 CSV 校验价格量级域 (XAU 1k-10k, BTC 10k-200k); reconcile 脚本比对 CSV 尾部日期与对应 symbol 的 MT5 直读 bar; direction_concentration_monitor (FIX-20260804-008 后全资产监控)。

### ReB-20260804-MONITOR_TRIPLE_BLIND_SPOT
- **Pattern Signature**: `MONITOR_TRIPLE_BLIND_SPOT`
- **Date Cataloged**: 2026-08-04
- **Source Docket**: DQAF-20260804-008
- **Related**: FIX-20260804-008, DQAF-20260804-006, ReB-20260804-D1_WELL_CROSS_ASSET_POISONING
- **定义**: 风控/监控器三重结构性失聪致长期静默: (1) 调度入口硬编码单一资产 data_dir (`_scheduled_monitor` data_btc) → 另一资产永不检查; (2) 读错数据字段路径 (读顶层 `direction`, 实际嵌套 `outputs.<strategy>.direction`) → 恒 0 信号; (3) 大小写/词形错配 (存小写 short/long vs 匹配大写 SHORT) → 恒 0 命中。叠加时间戳字段错配 (`timestamp_utc` vs `timestamp`/`recorded_at`) → 永远 INSUFFICIENT DATA 分支, 静默返回 exit 0。风控警报器失效比模型失效更不可饶恕 (IC 语)。
- **预防**: (1) 监控器必须参数化全资产目录, 禁止硬编码单资产; (2) 读取字段前先实证数据结构 (golden_master 结构探查), 禁止凭假设读字段; (3) 方向/枚举值必须归一化 (uppercase + 别名映射 BUY/SELL) 后匹配; (4) 时间过滤字段与数据 schema 对齐; (5) 监控器须有"数据不足≠正常"区分 — INSUFFICIENT 分支也要告警而非静默 exit 0。
- **检测**: `scripts/_monitor_direction_concentration.py --data-dirs data data_btc` (FIX-20260804-008 后实证 XAU CRITICAL 86% SHORT); 定期核对监控器读入信号数 vs 实际 golden_master 行数 (信号数=0 即盲区红旗)。

### ReB-20260805-TEST_LIVE_LEDGER_POLLUTION
- **Pattern Signature**: `TEST_LIVE_LEDGER_POLLUTION`
- **Date Cataloged**: 2026-08-05
- **Source Docket**: FIX-20260804-010 (FIX-20260804-007 部署验证发现)
- **Related**: FIX-20260804-010, DQAF-20260804-007
- **定义**: 测试代码把运行时 base_dir 指向实盘目录 (`StrategyLineConfig(base_dir="data"/"data_btc")`), 而 `record_brain_votes()` 每 evaluate() cycle 无条件追加 `{base_dir}/brain_votes/{date}.jsonl` → 全量 pytest 每次运行把 test_brain_01/b1/b2 测试投票 (brain_status=unknown, strategy=test_line/barrier_12bar/micro_3bar 等) 写入实盘投票台账。测试与实盘共享写入路径, 无隔离边界 → 台账被测试数据毒化, 审计脚本读到幻影脑。污染时间戳与 pytest 运行窗口精确重合 (22:51Z 启动即 14:51Z 首行)。
- **预防**: (1) 测试构造的任何运行时 config 的 base_dir 必须指向隔离目录 (pytest tmp 或 OS temp), 严禁字面 `"data"`/`"data_btc"` (FIX-20260804-010 `config_factory.TEST_BASE_DIR` 单收敛点); (2) 新增 `record_brain_votes` 调用链的测试必须显式注入 base_dir; (3) 台账污染检测: 对 `data/brain_votes/*.jsonl` 定期审计 brain_id 是否在已知生产脑清单内, 出现 test_* 即红旗; (4) 全量 pytest 结束后核对台账行数变化, 新增行数应≈0 (测试必须零写入实盘)。
- **检测**: `python scripts/audits/_audit_xau_votes_today.py --date YYYY-MM-DD --data-dir data` 输出中出现 test_brain_01/b1/b2 即污染; `data/brain_votes/{date}.jsonl` 中出现 strategy=test_line 行即污染。

### ReB-20260817-TP_TRAIL_RR_COLLAPSE_DECOUPLED_FROM_SL
- **Pattern Signature**: `TP_TRAIL_RR_COLLAPSE_DECOUPLED_FROM_SL`
- **Date Cataloged**: 2026-08-17
- **Source Docket**: DQAF-20260817-001
- **Related**: FIX-20260713-008 (TP trailing 激活), FIX-20260709-004 (前案 RR 1.66→0.08), TECH_DEBT-019
- **定义**: TP 动态追踪 (ATR 收缩时向内收窄) 与 SL 距离/RR **零耦合** — TP Floor 用 max()/min() 语义只防"太激进(TP 太远)", 不保"RR≥1"; Proximity Gate 仅防末程; TP 只缩不放 → 波动率收缩后运行中 RR 坍缩 (0.527/0.385), 止盈空间<止损空间负期望, 且 SL 引擎独立 (Chandelier) 全程不动. FIX-20260709-004 曾修同族 (只放大 candidate 距离未堵 RR 耦合缺口) → 复发.
- **预防**: (1) trail_dispatch 下发前注入 RR 耦合断言 `TP_floor_dist = max(Dynamic_TP_dist, Current_SL_dist × min_rr_ratio)`; (2) 波动率对称耦合 — ATR 收缩收紧 TP 时 SL 同步同比例收紧, 维持 RR≥1.0 数学期望; (3) 弹性恢复 — Proximity 70% 警戒线内 ATR 恢复时 TP 复放至开仓初始距离 (废除只缩不放).
- **检测**: 对在仓单快照 (position_snapshots.jsonl) 周期计算运行中 RR = TP 距离/SL 距离, 跌破策略 min_rr_ratio 即告警; `scripts/_audit_xau_tp_shrink_20260817.py` 可复现. 8/19 决战冻结期零代码, 清偿随 TECH_DEBT-019.

### ReB-20260819-FROZEN_DEBT_MASKING_LIVE_BUG
- **Pattern Signature**: `FROZEN_DEBT_MASKING_LIVE_BUG`
- **Date Cataloged**: 2026-08-19
- **Source Docket**: DQAF-20260819-003
- **Related**: FIX-20260819-003, TECH_DEBT-008, ReB-20260819-LIVE_ALERT_HUB_SIG_DRIFT
- **定义**: 静态类型债冻结清单 (RED_LINE_FROZEN_ALLOWANCE / mypy-baseline) 把"会说话的类型错误"当作纯静态问题冻结推迟, 而**同一代码行上的运行时异常被 BLE001 宽 except 吞掉** → 冻结掩盖了行为级静默缺陷. 表现: 8 处类型错中 7 处确为 L1 声明/调用点 (冻结无害), 但第 8 处 zombie-fuse 告警块是 L2 行为缺陷 — 类型错误只是它"被冻结点名"的方式, 真实故障 (熔断告警永不送达) 一直在线下运行. 冻结 = 把修复信号当噪音归档.
- **预防**: (1) 冻结任何 mypy 错误前, 逐行检查该错误所在代码块是否有运行时异常路径被宽 except 吞没 — 类型错误点往往就是静默逻辑缺陷点; (2) 冻结条目必须附"该错误是纯类型还是行为"定性, 行为级一律立即修, 禁止推迟; (3) 清偿冻结债务时按"根因分层"逐处归类 (L1/L2/L3), 不能全部当 L1 类型债一起刷; (4) BLE001 宽 except 后接 logging 会掩盖信号 — 关键告警路径 (熔断/健康/风控) 禁止吞异常, 吞之前先问"这个异常被吞会怎样".
- **检测**: 清偿冻结清单时逐条 `grep` 错误行所在 try/except 块, 检查是否有 `except (..., TypeError, OSError): pass` 覆盖运行时调用点; 对含外部接口调用的行 (LiveAlertHub/Webhook/API) 用 `python -m mypy --follow-imports=normal` 复现确认非纯类型.

### ReB-20260819-LIVE_ALERT_HUB_SIG_DRIFT
- **Pattern Signature**: `LIVE_ALERT_HUB_SIG_DRIFT`
- **Date Cataloged**: 2026-08-19
- **Source Docket**: DQAF-20260819-003
- **Related**: FIX-20260819-003, TECH_DEBT-008
- **定义**: 外部接口 (LiveAlertHub) 构造参数签名与方法演进后, 历史调用点未迁移 — 三重错配: 构造 kwargs (log_dir/ding_webhook_url 已死, 现行 base_dir/symbol/dingtalk_url/dingtalk_secret), 方法名 (fire() 已死, 现行 send_critical), 状态字段引用 (state._alert_hub 从未存在). 若调用点被 BLE001 宽 except 包裹 → 每次调用抛异常被吞, 接口升级的信号全部丢失.
- **预防**: (1) 接口演进必须同步 grep 全仓调用点 (构造 kwargs + 方法名 + 状态字段三向), 不能只改定义; (2) 对"吞异常"的调用块加一次性告警 (首次异常落盘/打点), 防永久静默; (3) LiveAlertHub 类增加 `__init_subclass__`/descriptor 层防御? 否 — 正确做法是调用点测试锁契约 (`tests/deployment/test_tech_debt_008_alert_hub_contract.py` 锁构造/方法/hasattr(fire)==False).
- **检测**: `grep -rn "LiveAlertHub("` 全仓核对构造 kwargs; `grep -rn "\.fire("` 在 observability 域内应为 0; 回归锁 `test_tech_debt_008_alert_hub_contract.py` 每次 CI 断言契约不变.

### ReB-20260820-MARKET_CLOSED_BLOCK_MISCLASSIFIED_AS_DEADLOCK
- **Pattern Signature**: `MARKET_CLOSED_BLOCK_MISCLASSIFIED_AS_DEADLOCK`
- **Date Cataloged**: 2026-08-20
- **Source Docket**: DQAF-20260820-001
- **Related**: FIX-20260820-001, TECH_DEBT-013, FIX-20260725-002, FIX-20260610-003
- **定义**: 金融品进入市场日历休市窗后, 下层组件在等待新 bar 形成时合法阻塞 (等待时长由 bar 周期决定, 恰与监控守护阈值同量级), 而该等待期不刷新存活心跳 → 上层存活监控 (in-process watchdog) 将**合法的休市等待**误判为**死锁** → 结构性周期硬杀. 与真实死锁的区别: kill 时间戳对齐市场日历休市窗、休市结束进程自行恢复、无持久异常. 相关复合因子: 语义 gate 错配 (bar gate 需要 "off", 休市窗返回 "caution") 使 gate 结构性失败, 无法在等待前短路.
- **预防**: (1) **下层最长等待 < 守护阈值** — 任何被 watchdog 守护的等待, 其最坏超时 (含 degraded wakeup) 必须严格小于守护阈值, 否则结构性必被杀; (2) **合法等待期 heartbeat pulse** — 等待期间周期性刷新存活心跳, 让守护线程对"有进展的等待"放行 (heartbeat delegation 穿透); (3) **语义对齐** — 等待 gate 与运行态分类必须同源 (bar gate 的放行条件与风险窗口语义一致); (4) **警惕 bar 周期 == 守护阈值 的悖论** — 纯超时压缩会提前 degraded 破坏正常交易, 心跳穿透而非缩时是正确解.
- **检测**: `watchdog_kill.log` kill 时间戳与市场日历休市窗对齐 (XAU 21:00-22:00 UTC daily close); kill elapsed ≈ 守护阈值 (300s); 休市窗内连续 kill 且休市结束恢复; BTC (crypto 24/7) 对照组应零误杀. 回归锁 `tests/unit/test_event_bar_sync_heartbeat.py` 每次 CI 断言休市阻塞期无硬杀 + BTC 对照.

### ReB-20260821-CLOSE_LABEL_MULTI_PRODUCER_DIVERGENCE
- **Pattern Signature**: `CLOSE_LABEL_MULTI_PRODUCER_DIVERGENCE`
- **Date Cataloged**: 2026-08-21
- **Source Docket**: DQAF-20260821-001
- **Related**: FIX-20260821-002, TECH_DEBT-007, FIX-20260806-001 (Option C Deferred), FIX-20260730-011, DQAF-20260722-002
- **定义**: 同一逻辑事实 (deal_reason, deal_comment, trail_active) 被多个生产者在**无单一决策点**的情况下各自解析为 label/归因 → 同一 deal 在不同写入路径得到不同标签. 具象: (1) watchdog shortcode 分段数漂移 (2 段 vs 3 段); (2) None/unknown reason 被伪造为特定 broker 归因 (孤儿平仓谎标 client_close); (3) 新生产者 (settlement_queue) 接入时抄写旧逻辑 (sl_hit_first 硬编码) 复活已被修复的语义盲点 (trail 无感知); (4) 携带更高 supersede 优先级的 writer 覆写低优先级的正确标签. 衍生危害: 出场归因/策略评估/p_win 校准/训练标签链全污染 (审计取证 XAU div-A 176 + div-B 8 / BTC div-A 199 + div-B 17).
- **预防**: (1) **单源叶子函数 (SSOT mouth)** — 标签决策收敛于一个纯 stdlib leaf (resolve_close_label), 所有 deal-informed 生产者强制消费, 新路径无第四套逻辑可写; (2) **honest unknown** — 无 deal reason 时输出 `unknown_close` (诚实缺失), 永不伪造 broker 归因; (3) **跨生产者 byte-identical 回归锁** — 参数化矩阵断言每个生产者对相同输入产出逐字节一致 label; (4) **supersede 链审查** — 新 writer 的 `_source` 优先级必须与标签正确性一致 (settlement_queue 带 mt5_reconciliation source 覆写 bridge 标签 = 高优先级 writer 用低质量标签覆写高质量).
- **检测**: 全量 journal 扫 `label` 与 `detail.reason` 交叉 (deal-attributed 却落 PnL label = 未消费 deal_reason); `sl_hit_first` 且 trail_contribution.trail_advances>0 (trail 盲点); watchdog label 分段数漂移 (2 段 vs 3 段). 回归锁 `tests/runtime/test_close_label_convergence.py` 每次 CI 断言 4 生产者矩阵收敛 + `tests/runtime/test_close_label.py` 断言 SSOT 全优先序.