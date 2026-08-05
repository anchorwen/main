# 🛰️ Gate 2 先锋哨兵部署方案（自动化监控）

> **状态**: ✅ ACTIVE (2026-08-05) — 投委会全线放行，已部署（FIX-20260805-003）
> **场景路由**: Scene B/E → #0 → #6 → #5 → 编码 → #1 验证 → #1.1 四维 → #7 注册 → #7.1 收口
> **对应裁决**: 投委会裁决 2（Red Gap 1）— 杜绝"盲目等待 8/19"，每天准时看到进度条
> **复用组件**: `scripts/inspect_ofi_history.py`（统计）+ `scripts/alert_dispatcher.py`（DingTalk 告警，含冷却/脱敏）

---

## 1. 目标

每日自动轮询 Gate 2 积累进度，实现：

1. **进度可视** — 每天固定时间输出 `687 → 711 → ...` 进度（H1 窗口数）
2. **停滞告警** — OFI 采集停摆 >24h（H1 窗口零增长）→ Sev1 立即拉响
3. **数据异常告警** — `ofi_history.jsonl` 记录数回落（文件被截断/回写）→ Sev1
4. **备战预警** — 距 1,000 窗口 < 48h → Sev2 提醒准备 8/19 决战
5. **达标庆祝** — Gate 2 READY → 一次性 INFO，触发 Runbook 阶段 3→4

---

## 2. 部署形态决策 — 为什么宿主侧独立哨兵

| 选项 | 评估 | 结论 |
|:---|:---|:---|
| 挂进 `daily_scheduler.sh`（Docker） | ⚠️ Docker 调度器**当前未运行**（`docker ps` 空）；且其挂载 `./data`（XAU 目录），**不含** `data_btc/reports/ofi_history.jsonl` | ✗ 不可行 |
| 宿主侧 `schtasks` + 独立脚本 | ✅ Windows 宿主直接跑，`data_btc` 本地可达，DingTalk webhook 环境变量可用 | **✓ 选定** |
| 集成进 daily_ops.py | ⚠️ daily_ops 是日终批处理，若当天没跑则监控真空 | ✗ 依赖耦合 |

**结论**: 新建 `scripts/gate2_sentinel.py`（宿主侧单测可跑、幂等、无状态残留），由 Windows 计划任务每日触发。零改动现有调度器/Docker（Decoupling 铁律）。

---

## 3. 哨兵脚本设计 `scripts/gate2_sentinel.py`

### 3.1 复用组件（零重复实现，Iterability 铁律）

```python
from scripts.inspect_ofi_history import inspect        # 统计单一事实源
from scripts.alert_dispatcher import dispatch_alert, AlertCard   # 告警单一事实源
```

### 3.2 状态文件（监控自身状态，非账本投影）

`data_btc/state/gate2_sentinel.json`（对齐 `alert_dispatcher` 的 `alert_cooling.json` 模式）:
```json
{
  "last_run": "2026-08-05T05:44:16",
  "prev_h1_windows": 688,
  "prev_n_records": 81582,
  "alerted_stall": false,
  "alerted_ready": false
}
```
> 注: `last_run` 与全部时间字段一律 **UTC naive**（与哨兵写侧约定一致，脚本自产自销，跨进程比对不会出现时区偏移）。

### 3.3 判定逻辑（状态机，幂等）

```
输入: inspect(data_btc) → {n_records, distinct_h1_windows, gate2_retrain.ready, verdict}

1. n_records == 0 且文件不存在          → Sev1 数据缺失: ofi_history.jsonl 消失
2. n_records < prev_n_records           → Sev1 数据异常: 记录数回落 (文件被截断)
3. gate2_retrain.ready == True          → INFO 一次性: "Gate 2 READY — 触发 8/19 决战" (alerted_ready 防重)
4. distinct_h1_windows == prev_h1_windows (且 >0) 且距上次运行 ≥22h 且**今日非周末** → Sev1 停滞: OFI 采集停摆 ≥24h (MT5/bridge 掉线?)
   ⚠️ 停滞判定必须跨完整日周期 — H1 窗口每小时才推进一次, 同日重跑/采样区间内计数未变属正常, 不告警
   ⚠️ **休市因素 (IC 2026-08-05 纠偏)**: 经纪商周末闭市 + 每日 ~1h 维护休市 → ① 周六/日跳过停滞判定 (周末运行不告警, 周一对比周五必现真停滞) ② 预期日积累 ~23 窗口 (24−1h休市) ③ ETA 用历史平均 (h1_windows/span_days, 已内化两因素), 不用单日 delta (跨周末被低估)
5. 1000 - distinct_h1_windows <= 48     → Sev2 备战: 距 Gate 2 达标 <48h, 请就位
6. 正常积累                              → 仅更新状态, 无告警
```

**告警冷却**: 复用 `dispatch_alert` 内建冷却（同 key 连续 3 次 → 4h 聚合），防止钉钉刷屏。

### 3.4 告警卡片（AlertCard）

```python
AlertCard(
    source="gate2",
    title="OFI Gate 2: OFI 采集停滞 24h",
    severity="Sev1",
    checks={"ofi_collection": "Sev1"},
    details={
        "h1_windows": 687, "n_records": 81439,
        "prev_h1_windows": 687, "verdict": "EVAL_READY ...",
    },
)
```

### 3.5 CLI（保持 inspect_ofi_history 风格）

```
python scripts/gate2_sentinel.py --data-dir data_btc        # 正常运行: 更新状态+按需告警
python scripts/gate2_sentinel.py --data-dir data_btc --json # 仅输出状态 JSON (调试/排障)
python scripts/gate2_sentinel.py --data-dir data_btc --dry-run  # 不落盘/不告警, 预演
python scripts/gate2_sentinel.py --data-dir data_btc --force-ready-alert  # 强制触发达标告警 (测试)
```

---

## 4. 调度挂载（Windows 计划任务）

**每日 12:30 本地（04:30 UTC）运行** — 位于市场收盘后、日终 daily-ops 前，Gap 2 进度最完整时快照。

```bash
# 创建（2026-08-05 已部署, 实际命令）:
#   ① 前置 cd /d D:\future → webhook fallback 读 configs/live.yaml 生效
#   （webhook 不在用户环境变量, 但 live.yaml:168 有 fallback）
#   ② python 路径与脚本路径均无空格 → 不加引号（schtasks 存储的 \" 会被 cmd.exe
#      当字面量, 导致路径解析失败 — 2026-08-05 实测 Last Result=1, 已改无引号）
#   ③ >> 日志重定向 → 每日进度条落盘 data_btc/state/gate2_sentinel.log (gitignored)
MSYS_NO_PATHCONV=1 schtasks /Create /TN "Future\\Gate2Sentinel" \
  /TR "cmd /c cd /d D:\future && C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe D:\future\scripts\gate2_sentinel.py --data-dir data_btc >> data_btc\state\gate2_sentinel.log 2>&1" \
  /SC DAILY /ST 12:30 /F
# (MSYS_NO_PATHCONV=1 仅在 Git Bash 下执行需要 — 防 /Create 被 MSYS 路径转换劫持)

# 立即手动触发一次（部署后必做验收）
schtasks /Run /TN "Future\\Gate2Sentinel"

# 查看最近运行结果
schtasks /Query /TN "Future\\Gate2Sentinel" /V /FO LIST
# 期望: 上次结果 = 0; 进度条见 data_btc/state/gate2_sentinel.log

# 回滚/移除
schtasks /Delete /TN "Future\\Gate2Sentinel" /F
```

**前置条件**:
- Webhook: `DINGTALK_WEBHOOK_URL` 环境变量首选；缺省回退 `configs/live.yaml`（依赖任务内 `cd /d D:\future`）
- Python 解释器绝对路径（当前 `C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe`），避免 PATH 差异

---

## 5. 验收测试序列

| # | 命令 | 期望 |
|:---:|:---|:---|
| 1 | `python scripts/gate2_sentinel.py --data-dir data_btc --dry-run` | 输出当前 H1 窗口数 + "dry-run, no state write" |
| 2 | `python scripts/gate2_sentinel.py --data-dir data_btc --json` | 输出 status JSON, `prev_h1_windows == 687` |
| 3 | `schtasks /Run /TN "Future\\Gate2Sentinel"` | 正常退出码 0, 无钉钉告警（正常积累路径） |
| 4 | `python scripts/gate2_sentinel.py --data-dir data_btc --force-ready-alert` | 触发一次性 INFO 钉钉卡 |
| 5 | 次日再跑一次 | `prev_h1_windows` 已更新为昨日值, 差值 ≈ 24 |

**2026-08-05 实测结果**:
- #1 ✅ `--dry-run` → `688/1000 H1 windows (312 to go)` + `[STATUS] OK` EXIT 0
- #2 ✅ `--json` → 只读 JSON (data_dir/n_records/h1_windows/gate2_ready/state)
- #3 ✅ schtasks `/Run` → **Last Result 0**, 日志 `data_btc/state/gate2_sentinel.log` 捕获进度条
- #4 ✅ `--force-ready-alert` → `GATE2_READY` INFO 卡触发; webhook 未配置时 `sent=False` 安全静默 (fail-open)
- #5 ⏳ 首个自然日 12:30 自动运行后确认差值
- **停滞误报防护实测**: 同日重跑 (elapsed<22h, H1 未推进) → OK 不告警; 伪造 state 25h 前 last_run → SEV1_STALL 正确触发

---

## 6. 回滚

```bash
schtasks /Delete /TN "Future\\Gate2Sentinel" /F
rm data_btc/state/gate2_sentinel.json
```
零耦合回滚：删除任务+状态文件即完全脱离，不影响 daily-ops/反馈循环/任何账本。

---

## 7. 风险与限制

| 风险 | 缓解 |
|:---|:---|
| 宿主重启后 schtasks 仍在（默认持久） | 是特性非缺陷；若需停用走 §6 回滚 |
| 钉钉 webhook 未配置 → 告警静默 | 哨兵 stdout/日志仍记录 `[ALERT] ... sent=False`；验收 #4 显式验证通道 |
| **周末闭市** (IC 2026-08-05 纠偏) | 周六/日**跳过停滞判定** — 经纪商 BTC 周末闭市; 周一对比周五: 完整交易日应有 ~20+ 窗口, delta==0 仍判真停滞 ✅ |
| **每日 ~1h 维护休市** (IC 2026-08-05 纠偏) | 预期日积累 ~23 窗口 (非 24); ETA 改用**历史平均速率** (h1_windows/span_days) 已内化休市因素; delta==0 跨完整日周期仍是真停滞 ✅ |
| 误报（同日重跑/采样区间内 H1 未推进） | **停滞判定需跨完整日周期 (elapsed ≥22h)** — 已实测: 同日重跑不告警, 25h 停滞触发 Sev1 ✅ |
| bridge 重启瞬间 0 增长 | 冷却机制 3 次才升级；Sev1 首警可人工复核 |

---

## 附：8/19 前哨兵应输出的进度样例

```
[2026-08-18 04:30 UTC] Gate 2: 998/1,000 H1 windows (EOD +2, ETA 4h)
[2026-08-18 12:30 UTC] Gate 2: 1,003/1,000 H1 windows → RETRAIN_READY
                        📢 [gate2] INFO: OFI Gate 2 READY — 触发 8/19 决战 Runbook 阶段 3
```

*蓝图注册: ✅ 已登记 — `FIX-20260805-003` (blueprints/modules/monitoring.md Fix History + deployment_config.md)。*
