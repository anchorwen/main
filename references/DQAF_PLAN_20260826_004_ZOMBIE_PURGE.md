# DQAF-20260826-004 — 清剿丧尸策略: 治理(L2)↔接线(L3)对齐

- **场景路由**: [Ω-Routing: Scene C → #0(安检) → #8(STOP+MAP) → [改配置] → #1(验证)]
- **性质**: Scene C 配置修改运维战役 (Iron Law #14 SSOT 对齐)
- **Severity**: Sev 2 (运行质量下降 + 执行层泥潭 — 已退休 swing 仍带电, 违反 L2 governance 不可跌破 L1 floor 的物理接线纪律)
- **触发**: IC 2026-08-26 最高战术指令「清剿丧尸策略 The Zombie Purge」
- **动机来源**: CCT-20260826-001 (BTC 三病根之二: 治理↔接线不同步)
- **状态**: ✅ **EXECUTED** (2026-08-26, IC 最高开火令后执行) — 4 处 strategy_line 已打显式断电标签, 丧尸归零审计 PASS. 验证详见 DQAF_DOCKET_REGISTRY.md DQAF-20260826-004.

---

## 1. 诊断 (Evidence — 交叉比对 L2 vs L3)

**L2 governance (data_btc/governance_state.json, brain_states)**: `BTC_Swing_M30` / `BTC_Swing_H1_V2` / `BTC_Swing_H4` / `BTC_Swing_V4` 全 **retired** (vote_weight 0.0)。

**L3 wiring (configs/live_btc.yaml, strategy_lines)** — 丧尸 = governance 已判退休但接线仍通电:

| strategy_line | magic | `enabled` | `mode` | governance 对应 brain | 冲突 |
|:---|:---|:---|:---|:---|:---|
| `btc_swing` | 90410 | `false` | **`live`** ⚠️ | BTC_Swing_V4 (retired) | **mode 矛盾** — 退休脑却声明 live |
| `btc_swing_m30` | 90430 | **`true`** 🧟 | `probation` | BTC_Swing_M30 (retired) | 退休脑仍通电+probation |
| `btc_swing_h1_v2` | 90460 | **`true`** 🧟 | `probation` | BTC_Swing_H1_V2 (retired) | 退休脑仍通电+probation |
| `btc_swing_h4` | 904240 | **`true`** 🧟 | `probation` | BTC_Swing_H4 (retired) | 退休脑仍通电+probation |

**registry_entries 侧**: 退休 brains json (V4/M15/M30/H1_V2/H4/Survival/V12) 均已 `enabled: false` (对齐 ✅)。唯一反向错位: `BTC_Swing_V4_LGB.json: enabled:false` 但 governance **probation** (过度限制, 非漏血, **不在本次清剿范围**, 记为独立低优先级对齐项)。

**近30天证据**: 自有策略仍亏 ~89 (btc_swing 30.77 / h1_v2 23.87 / h4 35.66 / m30 0.07) — 其中 h1_v2/h4 的 3-4 单大额亏损单多为退休判决 (2026-08-05) 前的持仓或同类绑定, 清剿切断任何"再次带电重开"的路径。

---

## 2. 修改清单 (精确 diff 提案)

### A. strategy_lines — 4 处 (核心)

全部采用 **显式 `enabled: false` + `mode: retired`** 双处对齐 (与已退休的 `btc_swing_h1`/`btc_swing_m15` 保持一致, 自文档化)。

| 行号 | 字段 | 现值 | 目标值 |
|:---|:---|:---|:---|
| L379 | `btc_swing.mode` | `live` | `retired` |
| L435 | `btc_swing_m30.enabled` | `true` | `false` |
| L436 | `btc_swing_m30.mode` | `probation` | `retired` |
| L476 | `btc_swing_h1_v2.enabled` | `true` | `false` |
| L477 | `btc_swing_h1_v2.mode` | `probation` | `retired` |
| L517 | `btc_swing_h4.enabled` | `true` | `false` |
| L518 | `btc_swing_h4.mode` | `probation` | `retired` |

> `btc_swing` 已 `enabled: false`, 仅需修正矛盾的 `mode: live` → `retired` (文档一致性, 防后续误读重开)。

### B. registry_entries — **不改** (退休 brains 已对齐)

### C. 明确「不做」清单
- 不改 XAU 配置 (`configs/live.yaml`, `live_fire` vanguard **magic 90601** 属全局共享, 由 shadow_ops 驱动, 非 strategy_line — **保护敢死队不受影响**)。
- 不删任何 `strategy_line` 块 (见 §3 fail-safe: 删块=地雷)。
- 不触碰 registry_entries、governance_state.json、brain config json、模型、数据管线。
- `btc_expected_r_m15` (magic 90452, mode shadow, expected_r candidate brains) — **保留, 非丧尸**。

---

## 3. Fail-safe 论证 (为何如此改不会崩) — 关键

**决定性事实 [strategy_builder.py:223-238](core/runtime/strategy_builder.py#L223-L238)**:

```python
for _gname in list(_known_groups.keys()):
    if not _cfg(_gname, "enabled", True):   # ← 默认值 = True !
        _known_groups[_gname].clear()        # enabled:false ⇒ 清空 brain 组, 不构建策略 obj
```

**🔴 反直觉陷阱**: `enabled` 的默认值是 **`True`**。这意味着:

| 动作 | 后果 |
|:---|:---|
| **删除**整个 strategy_line 块 | `strategy_configs.get(name, {})` 返回 `{}` → `_cfg(name,"enabled",True)` 命中默认 `True` → 组**不会**被清空 → 若该合约组有 brain 将默认通电 → **制造新漏洞** |
| **显式 `enabled: false`** | `_known_groups[_gname].clear()` → 策略对象不构建 → 干净跳过, 无崩溃 ✅ |
| `enabled: false` + `mode: retired` | (最优) 双字段对齐, 防误读, 与 h1/m15 已退休样式一致 ✅ |

**因此: 清剿必须显式置 false, 绝不能删块。** 这是本次战役的核心安全纪律。

**配套 fail-safe (全部已核实)**:
1. **载入层容忍**: [live_intent_loop.py:333](scripts/live_intent_loop.py#L333) `full_cfg = yaml.safe_load(...)` + [:373](scripts/live_intent_loop.py#L373) `strategy_configs = full_cfg.get("strategy_lines", {})` — `enabled:false` 是合法 YAML 字段, `strategy_configs` 是 dict, 改布尔值不触发解析错误。
2. **校验器不卡 enabled/mode**: [strategy_config_validator.py:43-48](core/runtime/strategy_config_validator.py#L43-L48) 仅比对 `exit:` 子键未知项; `enabled`/`mode` 不在校验范围 → 不新增 warn。
3. **运行时双保险**: [strategy_builder.py:130-143](core/runtime/strategy_builder.py#L130-L143) `brain_status in ("frozen","retired") → exclude from voting` — 即使接线残留, 退休脑也不投票 (防新开)。
4. **YAML 结构安全**: 仅改标量值 (true→false, live/probation→retired), 不删键/不嵌套/不改 schema → 反序列化零风险。
5. **原子化**: 单文件 `configs/live_btc.yaml` 一次改完, 可整文件 git 回滚。

---

## 4. 验证 (Iron Law #1 — 修改后必须验证)

```bash
# 1. 静态全链
python verify.py --quick

# 2. 配置加载/Schema 校验 (确认不会崩)
python -c "from core.config.consistency import validate_live_config; validate_live_config('configs/live_btc.yaml')"

# 3. 丧尸归零审计 (脚本先行, 铁律 #11)
python scripts/_audit_zombie_purge_verify_20260826.py
```
**审计脚本断言**: 对每个 strategy_line, 若 governance 对应 brain `status=="retired"` 则 wiring 必须 `enabled:false 且 mode 非 live/probation` — 输出"wired-on but governance-retired"计数, **必须 = 0**。

**通过标准**: verify --quick PASS + 校准脚本 exit 0 + 丧尸计数 = 0。

---

## 5. 回滚 (Rollback)

- 单文件 `configs/live_btc.yaml` → `git checkout -- configs/live_btc.yaml` 原子回滚。
- 回滚后需重启 BTC 引擎使配置生效 (改的是 wiring, 不热重载)。

---

## 6. 审批请求

请 IC 审阅本计划。**⚠️ 在 IC Approved 前, 不 touch configs/live_btc.yaml, 不重启任何进程。** 需注意: 改动需重启 BTC live 进程生效, 重启前需核对当前进程 CommandLine (铁律: 杀进程必须核对 CommandLine, 禁凭 PID/时间戳推断)。

注册为 DQAF-20260826-004 (PENDING), 审批后转 CLOSED。
