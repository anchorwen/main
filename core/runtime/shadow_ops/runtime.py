"""ShadowOpsRuntime — 每 cycle 编排器 (Micro Scaler v2 评分 + Trigger + 遥测).

生命周期计算点: live_cycle Phase 4 特征计算之后, 每 cycle 调用 ``run()`` 一次,
复用同一份 V9_40 特征向量 (零额外 MT5 调用). 输出端死焊 shadow_ops 遥测
ledger, 绝不构造派发 payload (DEFCON 1).

防御层:
  Layer 1 — 构造性隔离: 本模块 + 同包模块 import 黑名单静态断言 (startup tripwire).
  Layer 2 — 派发链熔断: dispatch_filter (live_order_sender 入口, 独立文件).
  Layer 3 — ShadowOpsWatchdog: scripts/_shadow_ops_watchdog.py 每日巡检.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from core.contracts.exceptions import DataIntegrityError
from core.runtime.shadow_ops.micro_scaler_scorer import MicroScalerScorer, ShadowOpsSignal
from core.runtime.shadow_ops.telemetry import ShadowTelemetryLedger
from core.runtime.shadow_ops.trigger_contract import TriggerContract, TriggerContractState

# Layer-1 构造性隔离黑名单 (派发能力模块). 静态断言: 本包任何 import 触线即拒启.
IMPORT_DENYLIST = (
    "mt5_bridge_worker",
    "live_order_sender",
    "communication_dispatcher",
    "zmq",
    "execution_queue",
    "live_execution_contract",
    "dispatch_context",
)

_REPORT_FILENAME = "micro_scaler_v2_reg_report.json"


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


class ShadowOpsRuntime:
    """纯观察者运行时: scorer + trigger + telemetry 每 cycle 编排 (fail-open)."""

    def __init__(
        self,
        *,
        symbol: str = "XAUUSDc",
        base_dir: str = "data",
        config_path: str = "configs/live.yaml",
        model_version: str | None = None,
    ) -> None:
        self._symbol = symbol
        self._base_dir = base_dir
        self._enabled = False
        self._violation_reported = False
        self._model_version = model_version or "v2_20260824"
        self._trigger_path: Path | None = None
        self._trigger: TriggerContract | None = None
        self._scorer: MicroScalerScorer | None = None
        self._ledger: ShadowTelemetryLedger | None = None
        # FIX-20260824-004: 敢死队开关/参数 — 默认 disabled (任何 early-return 前已定型)
        self._live_fire_enabled = False
        self._live_fire_config: dict[str, Any] = {}

        cfg = self._load_shadow_ops_config(config_path)
        if not cfg.get("enabled", False):
            return  # 配置关闭 → 运行时为 no-op (fail-open, 不打断实盘)

        ms = cfg.get("micro_scaler_v2")
        if not isinstance(ms, dict):
            raise DataIntegrityError("shadow_ops.enabled=true but micro_scaler_v2 section missing")
        # 必需字段严格索引 (DataIntegrityError 语义 — 缺失即数据病, 严禁 dict.get 抹平)
        try:
            model_path: str = ms["model_path"]
            trigger_path: str = ms["trigger_path"]
            telemetry_dir: str = ms["telemetry_dir"]
            feature_schema: str = ms["feature_schema"]
        except KeyError as exc:
            raise DataIntegrityError(
                f"shadow_ops.micro_scaler_v2 missing required field: {exc.args[0]}"
            ) from exc
        ttl_seconds = float(ms.get("trigger_refresh_ttl_seconds", 60.0))
        denylist_enforced = bool(ms.get("import_denylist_enforced", True))
        mv = ms.get("model_version")
        if mv:
            self._model_version = str(mv)

        if denylist_enforced:
            self._assert_import_denylist()

        me = cfg.get("meta_exit_v3")
        if isinstance(me, dict) and me.get("enforce_shadow_only", True):
            self._assert_meta_exit_shadow_only()

        repo_root = Path(__file__).resolve().parents[3]
        model_path_p = self._resolve(model_path, repo_root)
        trigger_path_p = self._resolve(trigger_path, repo_root)
        telemetry_dir_p = self._resolve(telemetry_dir, repo_root)

        self._trigger_path = trigger_path_p
        self._trigger = TriggerContract(trigger_path_p, ttl_seconds=ttl_seconds)
        report_path = model_path_p.parent / _REPORT_FILENAME
        self._scorer = MicroScalerScorer(
            model_path=model_path_p,
            trigger=self._trigger,
            calibration_report_path=report_path,
            feature_schema=feature_schema,
        )
        self._ledger = ShadowTelemetryLedger(telemetry_dir_p)
        self._enabled = True

        # ── Live Fire 敢死队 (FIX-20260824-004, 投委会方向 B 裁决) ──
        # live_fire 为 micro_scaler_v2 子段: 默认 disabled (部署安全),
        # 投委会点火时 config 置 enabled: true → live_cycle 依据 D10 信号真实派发.
        # 本模块仅暴露配置与开关 (只读), 派发逻辑在 live_cycle 模块级函数
        # (Layer-1 import denylist 语义: 本包不 import 派发能力).
        if isinstance(ms, dict):
            lf = ms.get("live_fire")
            if isinstance(lf, dict) and lf.get("enabled", False):
                try:
                    self._live_fire_config = {
                        "magic": int(lf.get("magic", 90601)),
                        "volume": float(lf.get("volume", 0.01)),
                        "cooldown_seconds": float(lf.get("cooldown_seconds", 1500.0)),
                        "max_drawdown_usd": float(lf.get("max_drawdown_usd", 50.0)),
                        "sl_atr_mult": float(lf.get("sl_atr_mult", 2.0)),
                        "tp_pred_mult": float(lf.get("tp_pred_mult", 1.0)),
                        "min_sl_pct": float(lf.get("min_sl_pct", 0.05)),
                        "min_tp_pct": float(lf.get("min_tp_pct", 0.03)),
                        "block_when_positions": bool(lf.get("block_when_positions", True)),
                    }
                    self._live_fire_enabled = True
                except (TypeError, ValueError):
                    # 配置畸形 → 保持 disabled (fail-closed: 不点火畸形敢死队)
                    self._live_fire_enabled = False
                    self._live_fire_config = {}

    # ── 对外契约 ────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def live_fire_enabled(self) -> bool:
        """敢死队点火开关 (live_cycle 每 cycle 读取; 默认 False = 纯影子)."""
        return self._live_fire_enabled

    @property
    def live_fire_config(self) -> dict[str, Any]:
        """敢死队执行参数 (magic/volume/cooldown/生死状/SL·TP). 只读投影."""
        return dict(self._live_fire_config)

    def describe(self) -> dict[str, Any]:
        """诊断快照 (startup/实证锁)."""
        return {
            "enabled": self._enabled,
            "symbol": self._symbol,
            "model_version": self._model_version,
            "trigger_state": (self._trigger.state if self._trigger is not None else "disabled"),
            "threshold_abs_pred_pct": (
                self._trigger.threshold_abs_pred_pct() if self._trigger is not None else None
            ),
            "telemetry_dir": (str(self._ledger.directory) if self._ledger is not None else None),
            "live_fire_enabled": self._live_fire_enabled,
            "live_fire_magic": (
                self._live_fire_config.get("magic") if self._live_fire_config else None
            ),
            "live_fire_max_drawdown_usd": (
                self._live_fire_config.get("max_drawdown_usd") if self._live_fire_config else None
            ),
        }

    def run(
        self,
        *,
        feature_vector: Any,
        cycle_count: int,
        now_utc: str | None = None,
        feature_ts_utc: str | None = None,
    ) -> ShadowOpsSignal | None:
        """每 cycle 一次: TTL 刷新 → 评分 → Quantile Trigger → 遥测.

        Returns:
            D10 触发且契约 OK 的 ``ShadowOpsSignal`` (供 live_cycle 敢死队
            live-fire 真实派发), 否则 None.

        fail-open: 任何遥测/评分故障打印 JSON 事件后返回 None, 绝不打断实盘 cycle.
        """
        if (
            not self._enabled
            or self._scorer is None
            or self._trigger is None
            or self._ledger is None
        ):
            return None
        ts = now_utc or _iso_utc_now()
        fts = feature_ts_utc or ts
        try:
            self._trigger.refresh()
            contract_state = self._trigger.state
            if contract_state == TriggerContractState.VIOLATION and not self._violation_reported:
                self._violation_reported = True
                self._emit_violation(cycle_count, ts)

            signal = self._scorer.predict(feature_vector, contract_state=contract_state)
            pred_rec = signal.to_prediction_record(
                time_utc=ts,
                symbol=self._symbol,
                model_version=self._model_version,
                feature_ts_utc=fts,
                cycle_count=cycle_count,
            )
            self._ledger.append_prediction(pred_rec)
            # D10 触发 + 契约 OK → 记 shadow order intent + 返回信号 (供 live-fire)
            if signal.triggered and contract_state == TriggerContractState.OK:
                order_rec = signal.to_shadow_order_record(
                    time_utc=ts,
                    symbol=self._symbol,
                    model_version=self._model_version,
                    feature_ts_utc=fts,
                    cycle_count=cycle_count,
                )
                self._ledger.append_shadow_order(order_rec)
                return signal
            return None
        except Exception as exc:  # noqa: BLE001  # BLE001:FOG fail-open — 遥测故障不得打断实盘
            print(
                json.dumps(
                    {
                        "event": "shadow_ops_runtime_error",
                        "time": ts,
                        "iteration": cycle_count,
                        "error": repr(exc),
                        "action": "fail_open — shadow telemetry skipped, live path untouched",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return None

    # ── 内部 ────────────────────────────────────────────────────
    def _emit_violation(self, cycle_count: int, ts: str) -> None:
        rec = {
            "event": "shadow_ops_trigger_contract_violation",
            "time_utc": ts,
            "cycle_count": cycle_count,
            "venue": "shadow_ops",
            "action": "OBSERVE",
            "trigger_path": str(self._trigger_path),
            "detail": (
                "trigger_mode/mandate tampered — shadow orders suppressed "
                "(fail-closed, FIXED_THRESHOLD_FORBIDDEN)"
            ),
        }
        if self._ledger is not None:
            try:
                self._ledger.append_prediction(rec)
            except Exception:  # noqa: BLE001  # BLE001:FOG
                pass
        print(
            json.dumps(
                {
                    "event": "shadow_ops_trigger_contract_violation",
                    "time": ts,
                    "iteration": cycle_count,
                    "severity": "CRITICAL",
                    "detail": rec["detail"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    @staticmethod
    def _resolve(path_value: str, repo_root: Path) -> Path:
        p = Path(path_value)
        return p if p.is_absolute() else repo_root / p

    @staticmethod
    def _load_shadow_ops_config(config_path: str) -> dict[str, Any]:
        """读 yaml 顶层 shadow_ops 段; 文件/段缺失 → 视为 disabled (fail-open)."""
        repo_root = Path(__file__).resolve().parents[3]
        path = Path(config_path)
        if not path.is_absolute():
            path = repo_root / path
        if not path.exists():
            return {"enabled": False}
        try:
            with open(path, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            return {"enabled": False}
        if not isinstance(doc, dict):
            return {"enabled": False}
        sec = doc.get("shadow_ops")
        if not isinstance(sec, dict):
            return {"enabled": False}
        return sec

    def _assert_import_denylist(self) -> None:
        """静态断言: 本包任何 .py 源 import 的顶层模块 ⊄ 派发能力模块集合."""
        package_dir = Path(__file__).resolve().parent
        sources: list[str] = []
        for py in sorted(package_dir.glob("*.py")):
            sources.append(py.read_text(encoding="utf-8"))
        src = "\n".join(sources)
        import_re = re.compile(r"^\s*(?:import|from)\s+([A-Za-z0-9_\.]+)", re.MULTILINE)
        imported_tops = {m.group(1).split(".")[0] for m in import_re.finditer(src)}
        for tok in IMPORT_DENYLIST:
            if any(tok in top for top in imported_tops):
                raise DataIntegrityError(f"shadow_ops import denylist violated: {tok!r} imported")

    def _assert_meta_exit_shadow_only(self) -> None:
        """防回退断言: management_phase 的 MetaExit shadow-only 块必须仍存在.

        DEFCON 1 语义: 若未来代码误删 shadow 块 (dispatch close 路径复活),
        引擎启动即拒绝 (fail-closed).
        """
        repo_root = Path(__file__).resolve().parents[3]
        mgmt = repo_root / "core" / "runtime" / "management_phase.py"
        if not mgmt.exists():
            raise DataIntegrityError(
                "management_phase.py missing — cannot assert MetaExit shadow-only block"
            )
        src = mgmt.read_text(encoding="utf-8")
        if "meta_exit_shadow_telemetry" not in src:
            raise DataIntegrityError(
                "MetaExit SHADOW-ONLY block removed — 'meta_exit_shadow_telemetry' marker missing"
            )
        if "close NOT dispatched" not in src:
            raise DataIntegrityError(
                "MetaExit SHADOW-ONLY block removed — 'close NOT dispatched' marker missing"
            )
