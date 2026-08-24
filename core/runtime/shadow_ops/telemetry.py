"""Shadow Telemetry Ledger — append-only JSONL 写入器 (死焊暗影遥测).

独立 ledger (绝不写入 live_trade_journal / golden_master — 防污染实盘分析):

  data/shadow_ops/
  ├── micro_scaler_predictions.jsonl    # 全量预测流 (每 cycle)
  ├── micro_scaler_shadow_orders.jsonl  # 仅 D10 触发 shadow order intent
  └── dispatch_blocks.jsonl             # Layer-2 派发链熔断拦截记录

写入 fail-open 语义: 遥测 I/O 故障不得打断实盘 cycle — 调用方 (runtime /
dispatch fuse) 以 BLE001 包裹捕获.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PREDICTIONS_FILENAME = "micro_scaler_predictions.jsonl"
SHADOW_ORDERS_FILENAME = "micro_scaler_shadow_orders.jsonl"
DISPATCH_BLOCKS_FILENAME = "dispatch_blocks.jsonl"


class ShadowTelemetryLedger:
    """O_APPEND 追加写入 (单进程单线程; 无锁 — live_cycle 是顺序循环)."""

    def __init__(self, telemetry_dir: str | Path) -> None:
        self._dir = Path(telemetry_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._pred_path = self._dir / PREDICTIONS_FILENAME
        self._order_path = self._dir / SHADOW_ORDERS_FILENAME
        self._block_path = self._dir / DISPATCH_BLOCKS_FILENAME

    @property
    def directory(self) -> Path:
        return self._dir

    def append_prediction(self, record: dict[str, Any]) -> None:
        self._append(self._pred_path, record)

    def append_shadow_order(self, record: dict[str, Any]) -> None:
        self._append(self._order_path, record)

    def append_dispatch_block(self, record: dict[str, Any]) -> None:
        self._append(self._block_path, record)

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
