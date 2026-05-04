# 实盘运维说明（Live ops）

## 策略定位（必读）

- **`scripts/live_intent_loop.py`** 使用 **锚价 ± 绝对价格阈值** 生成意图，属于 **基础设施级示例策略**，用于打通 `mt5_outbox → bridge → MT5`。
- **机构 V9 / ONNX**（例如 `D:\ai\Survival_V9` 的 `v9_institutional_brain.onnx`）与仓库内 **`V9_INSTITUTIONAL_40_FEATURES`** 契约相关，但 **默认实盘链路并未加载 ONNX**。切勿把锚价 loop 的盈亏表现等同于「机构模型」表现。

## 推荐启动顺序

1. **登录 MT5 终端**（账户已连接，品种可见）。
2. **工作目录**：在仓库根目录执行脚本（例如 `D:\cursor`），避免相对路径写到错误位置。
3. **启动运维侧**：`start_live_ops.ps1`（闸口 `live_dispatch_policy` + `run_bridge_forever` + P1 日跑）。
4. **启动信号侧（若使用全栈）**：`start_live_full_stack.ps1` 会先 **另开窗口** 跑 `start_live_ops`，等待数秒后再在本窗口跑 `live_intent_loop`。
5. **巡检（单次命令）**：
   - `python scripts/live_stack_diagnostic.py --base-dir data --symbol XAUUSDc --output data/reports/live_stack_diagnostic.json`
   - Windows 控制台可能对中文 JSON 乱码，请优先看 **`--output`** 写入的 UTF-8 文件。

## 健康检查含义

- **`NO_OUTBOX_INTENTS`**：当前没有待处理的 `*.mt5.json`。若预期应有订单，说明 **上游未派发意图**（或已全部消费）。
- **`PROTECTION_FLAG_ON_DISK`**：`data/live_dispatch_block.flag` 存在时，`mt5_bridge_worker` 会对队列消息记 **protection 拒单**，且 **`dispatch_live_open_order` 会直接失败**（与意图循环「跳过」语义不同，见下节）。
- **`POLICY_WOULD_BLOCK`**：`live_dispatch_policy --eval-only` 认为当前应阻塞下发（读诊断 JSON 的 `policy_eval_only`）。

## 闸口语义（意图循环 vs 即发 CLI）

| 组件 | 存在 `live_dispatch_block.flag` 时 |
|------|-------------------------------------|
| `live_intent_loop` | **跳过周期**，打印 `protection_skip`（不抛异常） |
| `send_live_order` / `dispatch_live_open_order` | **`RuntimeError`**；CLI 退出码 **2** |

设计意图：长跑循环不被闸口打死进程；手动一发失败更显式。

## `send_live_order.py` 退出码与路径

- **0**：派发成功（JSON 中含 adapter/status）。
- **1**：参数或价格校验失败（如 SL/TP 与参考价几何关系不成立）。
- **2**：闸口激活（`protection guard active`）或其它 `RuntimeError`（如 MT5 初始化失败）。

相对路径 `protection-flag-path`：**优先**在当前工作目录下查找（兼容旧用法）；不存在时再锚定到 **`--base-dir`**（见 `resolve_protection_flag_path`）。

## 已知行为：Bridge 与 MT5

- **`mt5_bridge_worker`** 每笔订单执行 **`initialize → order_send → shutdown`**。与 **`live_intent_loop`** 常驻 MT5 会话并存时，终端可能出现短暂抖动；若拒单增多，请结合 `live_trade_journal.jsonl` 与经纪商日志排查。

## 验收清单（运维）

更具体的自动化步骤见 **`scripts/ops_acceptance_check.ps1`**（聚合诊断与 bridge healthcheck）。

批量重训与模型接入（阶段 C）见 **[TRAINING_PHASE_C_BACKLOG.md](TRAINING_PHASE_C_BACKLOG.md)**（独立里程碑，不与实盘救火混排）。

**执行契约（阶段 B）**：多模型共用同一套 MT5 handoff 字段 — **[LIVE_EXECUTION_CONTRACT.md](LIVE_EXECUTION_CONTRACT.md)**（`volume`、`action`：open/close/modify_sltp、`dispatch_live_mt5_execution`）。
