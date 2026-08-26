# DQAF-20260826-005 — V4_SHORT 破局特区蓝图 (Live-Fire Special Zone)

- **场景路由**: [Ω-Routing: Scene E(新建设计蓝图)→ #6(蓝图注册) → [IC 审阅] → #0(安检)→ [编码待批]]
- **性质**: Mandate #3 战略预演的设计蓝图 — **仅供 IC 审阅, 未改任何代码/接线**
- **触发**: IC 2026-08-26 最高开火令「V4_SHORT 实盘特区」; 动机源 CCT-20260826-001 (病根三: OOS门禁↔实盘数据死锁)
- **前置**: 清剿丧尸 (DQAF-20260826-004) 已完成 — 风险预算已回笼
- **状态**: 🟡 **AWAITING_IC_APPROVAL** — 本蓝图只回答三个核心风控问题, 不 commit 代码

---

## 基线事实 (Iron Law #11 取证)

| 关键量 | 值 | 来源 |
|:---|:---|:---|
| V4_SHORT OOS ρ | **0.0596** (>0.05 门禁, 唯一过线脑) | immutable-waddling-bumblebee (V4.2 M15) |
| V4_SHORT governance | **candidate** w=1.0 | data_btc/governance_state.json (verified) |
| V4_SHORT magic / 策略线 | 90451 / btc_expected_r_m15 (90452, mode shadow) | live_btc.yaml + brain json |
| 全局生死状 | max_drawdown_usd **50.0**, magic **90601** | configs/live.yaml:43 + live_fire_breaker.py |
| 血槽作用域 | **per-magic + 跨树共享** (LIVE_FIRE_BASE_DIRS=data,data_btc) | live_fire_breaker.py L117/L206 |
| Path B 重训门槛 | **≥200 真实闭环交易** | FIX-20260609-002-BTC / FIX-20260616-090 / readiness closed_trade_count≥200 |

---

## 一、精准绕过 (Targeted Bypass) — 如何只放 V4_SHORT 穿透

**核心**: 不走全局门禁松绑 (那会让所有不达标脑一起漏), 而是开辟**一条单脑窄门**, 全部其他脑维持原格。

**机制 (三层锁, 缺一停机)**:

```
▸ LOCK-1 脑白名单: 新 strategy_line 特区的 brain_types = {expected_r_short}
   且脑 id 白名单 = {BTC_Expected_R_V4_SHORT}. min_valid_brains=1 →
   只有这一个脑被接入. 任何第二个脑/异 id 加入 → 组不构建 (fail-closed)。
▸ LOCK-2 窄门豁免: StrategyEvaluator/StrategyLine 现按 governance status 判可投 —
   对 candidate 一律不可投. 新增一个**只读单一override**:
   brain `execution_zone==live_fire_vanguard` 且 (a) 该脑是全员唯一,
   (b) OOS ρ ≥ 0.05 (SHORT=0.0596 达标), (c) status ∈ {candidate, shadow}
   → 该脑在特区线内被豁免为可投. 其余任一脑: override 不触发 → 原格不动。
▸ LOCK-3 双保险复用: strategy_builder 既有 frozen/retired 排除 (L130-143)
   原样保留 → 逆转录脑/僵尸脑即使在特区线也无法回归 (IRC #14 语义不破坏)。
```

**防漂移断言 (构build时硬校验)**: 特区线 build 时断言
`len(allowed_brain_ids)==1 and allowed_brain_ids=={V4_SHORT} and rho>=0.05`。
任一真 (re-gate/第二脑/ρ 下降) → 断言抛出, 特区线不构建 → **fail-open→fail-closed**。
**绝不**降低全员 0.05 门禁: 那仍是其他脑晋升的唯一标准。

*不触及*: 模型/数据管线/V4_LONG 塔 (LONG 维持 observation-only w=0.0, 不因 SHORT 特区被牵连)。

---

## 二、熔断挂载 (Breaker Wiring) — 共享 $50 血槽 vs 独立池

**结论: 挂载进全局 $50 血槽 (magic 登记法), 不设独立风控池。**

**依据**:
1. **IC Sev-1 已立语义**: 「敢死队 = 一个计划, 一个血槽, 一个 kill-switch, 非每品种各 $50」
   (live_fire_breaker.py L200-203)。V4_SHORT 特区与 90601 敢死队同属**用命换真金白银的
   live-fire 家族** → 必须共享同一个血槽 + 同一个 fail-closed 终态。设独立池 = 重新引入
   IC 已明文禁止的"每品种各给 $50"碎片化。
2. **单一控制平面 (Iron Law #-1 Decoupling)**: 一个血槽 = 一个 kill-switch = 一个"家族是否阵亡"
   的单一真相。再开一个池 = 第二个 kill-switch + 两个互不感知的停止逻辑 → 控制面不连贯。
3. **复用已炼好机制**: 血槽已带消费端幂等去重 (FIX-20260826-001/-003) + 事件溯源重算 +
   fail-closed。V4_SHORT 直接复用, 零新增故障面。

**接线 (审批后实施, 单点扩展)**: 熔断器现按单 magic 过滤 (L117)。扩展为**全局共享池登记**:
- `live_fire_breaker.py` 单点 (L206 处) 新增 `LIVE_FIRE_TRACKED_MAGICS = (90601, 90451)`;
  `aggregate_live_fire_drawdown` 接受 magic-set, 逐 magic 归集后按 **position_ticket 去重** 求和。
- V4_SHORT 真实交易 (data_btc journal, magic 90451) **自动计入同一 -$50 池**。
- 触发语义: 家族累计 ≤ -$50 → 熔断 flag → **XAU 敢死队 + V4 特区一起 fail-closed 停** (家族终态,
  人工裁决解除) — 这正是"一个计划一个命运"。

**⚠️ 保守警告**: 共享池意味着 90601 的亏损会连带拖停 V4_SHORT 特区 (反之亦然)。
这是 IC 想要的家族级评估结果 (这些是可牺牲的风控实验, 同用一个 $50 预算);
非独立评估需求, 故不做独立池。

---

## 三、闭环周期 (The Verification Cycle) — 多少笔真实标签触发重训

**触发 Path B / Phase 3 重训的最低统计门槛: ≥200 笔真实闭环交易。**

**依据**:
- **200 = Path B 通道的既定最小值**: FIX-20260609-002-BTC「Path B (≥200 live trades retrain) deferred」;
  FIX-20260616-090「Retrain at 200 matchable」; readiness 阶段 `closed_trade_count ≥ 200`。
- 200 笔足以对**方向一致性**做可信检验 (WR 对盈亏平衡 WR 的比较), 这是 V4_SHORT 的
  Binary 标签统计根基。对应记忆注记「Path B: ≥200 笔实盘 (~2-3月)」。

**阶梯门 (防过早下结论)**:
```
  ╟─ 30 笔 (健康门): WR 对盈亏平衡点; WR<50% 或 EV<-$1 → 触发调参不弃坑 (敢死队契约)
  ╟─ 200 笔 (重训触发): 达到 Path B 最小统计门槛 → 用真实 closed 重算 OOS ρ + WR
  ╟─ 500 笔 (就绪升级): readiness sample_count≥500 → 跨过 Phase 3/P3 就绪线
```

**诚实说明 ρ 功效**: 要在 80% 功效下判定 ρ≈0.06 显著, 需 ~2200 样本 (Fisher z 转变)。
**200 笔是"方向/WR 可信"的约定门槛, 不是全功效 ρ 检验**。故 200 只作为**重训/继续**分界,
不作为"终局判胜"。若你想**更强**的 OOS 说服力 → 把重训分界上调到 500 笔 (读就绪线)。

**节奏估算**: V4_SHORT 信号率 ~5.7% (M15) 经冷却/max-positions/门禁过滤, 实际开单率显著低于裸信号率,
按历史估计 **~2-4 closed/天 → 200 笔 ≈ 2-3 月** (与既有 Path B 时间预算一致)。
特区本身存在的意义就是**让这条数据链以真实风险流速跑通**, 而非被门禁永久卡死 (死锁破局)。

---

## 落地边界 (审批后单个 DQAF docket 实施)

| 改动 | 文件 | 类型 |
|:---|:---|:---|
| 特区线 + brain 白名单 | configs/live_btc.yaml (新 strategy_line 或扩 btc_expected_r_m15) | webbing |
| 窄门 override + 断言 | core/runtime/strategy_builder.py / StrategyEvaluator | L3 架构 |
| 脑配置 zone 标记 | configs/brains_btc/BTC_Expected_R_V4_SHORT.json (+V4_LONG 维持 w=0) | config |
| 血槽 magic 登记 | core/runtime/shadow_ops/live_fire_breaker.py (L206 单点) | L3 架构 |
| 审计脚本 | scripts/_audit_v4short_zone_verify_20260826.py (白名单=1/熔断共池/ρ≥0.05) | 法证 |

**明确不做**: 不松全员 0.05 门禁 / 不动 V4_LONG / 不碰模型与数据管线 / 不开独立风险池。

---

## 验证 (审批后, Iron Law #1)

1. `verify.py --full` (FIX_REGISTRY + mypy + ruff + blueprint) PASS
2. `_audit_v4short_zone_verify_20260826.py` 断言: (a) 特区线只接入 1 脑且==V4_SHORT; (b) ρ≥0.05;
   (c) 熔断 aggregation 将 90601+90451 归一池; (d) 非白名单脑 (V4_LONG/其他 candidate) 仍不可投。
3. `validate_live_config('configs/live_btc.yaml')` 零新增 WARN。

## 审批请求

本蓝图**仅回答问题三枚, 不动代码**。请 IC 裁决: ① 窄门白名单方案 ② 共享 $50 血槽 (magic 登记) —
是否批准进入编码阶段。批准后按 DQAF-20260826-005 单个 docket 实施。注册本蓝图 = DQAF-20260826-005 (PENDING)。
