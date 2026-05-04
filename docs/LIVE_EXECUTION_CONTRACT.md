# 实盘 MT5 执行契约（Phase B，通用）

所有信号源（`live_intent_loop`、未来 ONNX/批量推理、`send_live_order`）写入 **`CommunicationEnvelope.payload`** 后，由 [`MT5CommunicationAdapter`](../core/protocol/services/mt5_communication_adapter.py) 落地为 `*.mt5.json`，[`scripts/mt5_bridge_worker.py`](../scripts/mt5_bridge_worker.py) 负责解释并调用 MetaTrader5。

## 版本字段

| 字段 | 说明 |
|------|------|
| `execution_payload_schema` | 推荐固定为 `live_mt5_execution_payload.v2`（常量见 `core/protocol/live_execution_contract.py`）。便于阶段 C 数据集与回放对齐。 |

## `action`（默认 `open`）

| 值 | 含义 |
|----|------|
| `open` / `reverse` | 市价开仓（`TRADE_ACTION_DEAL`），沿用原 bridge 行为。 |
| `close` | 按持仓票据平仓（见下）。 |
| `modify_sltp` | 修改现有持仓止损止盈（`TRADE_ACTION_SLTP`）。 |

省略 `action` 时 **视为 `open`**，兼容旧 handoff。

## 成交量

| 字段 | 说明 |
|------|------|
| `volume` 或 `lots` | 为正浮点时 **优先于** bridge 的 `--default-volume`。用于多模型头寸缩放。 |

## 开仓字段（`open` / `reverse`）

与现网一致：`symbol`、`side`（long/short）、`sl`/`tp`（或 `stop_loss`/`take_profit`）。

## 平仓字段（`close`）

| 字段 | 说明 |
|------|------|
| `position_ticket`（或 `ticket` / `position_id`） | **必填**，MT5 持仓票据。 |
| `volume` | 可选；省略则平掉该持仓剩余全部量；若指定则为其与持仓量的较小值（部分平仓）。 |

## 修改止损止盈（`modify_sltp`）

| 字段 | 说明 |
|------|------|
| `position_ticket`（或 `ticket`） | **必填**。 |
| `sl` / `stop_loss`、`tp` / `take_profit` | 至少提供一个有效正数；否则拒单。 |

## 闸口与保护

与 [`docs/LIVE_OPS.md`](LIVE_OPS.md) 相同：`live_dispatch_block.flag` 存在时 worker **拒单**（保护），但仍归档并写 journal。
