#!/usr/bin/env python3
"""ShadowOps Watchdog — Layer-3 可证伪断言 (Phase 4 Shadow Ops, DEFCON 1).

每日巡检 (Iron Law #11 — 脚本 stdout 是唯一合法证据源):
  (a) liveness: shadow 遥测 ledger 有信号流, 无静默断流;
  (b) 零真实订单证明: live_trade_journal / golden_master 中 ZERO 条带
      shadow_ops 策略归属的真实持仓/订单;
  (c) trigger json mandate 完整性: trigger_mode==quantile_top_decile_abs_pred
      且 mandate 含 "FIXED_THRESHOLD_FORBIDDEN" — 被篡改 → CRITICAL.

用法:
  python scripts/_shadow_ops_watchdog.py --data-dir data [--recent-minutes 1440]

Exit code:
  0 = all green
  1 = CRITICAL (零真实订单违约 / mandate 篡改 / 构造性隔离违约)
  2 = WARN (ledger 空/静默 — 引擎可能未重启或休市, 非 DEFCON 违约)
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DENYLIST = (
    "mt5_bridge_worker",
    "live_order_sender",
    "communication_dispatcher",
    "zmq",
    "execution_queue",
    "live_execution_contract",
    "dispatch_context",
)
_SHADOW_TOKENS = ("shadow_ops", "micro_scaler_v2")


def _read_jsonl_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"_raw_parse_error": True})
    return out


def _check_liveness(pred_path: Path, recent_minutes: int) -> dict[str, Any]:
    import time

    if not pred_path.exists():
        return {
            "status": "NO_LEDGER",
            "detail": "micro_scaler_predictions.jsonl 不存在 (引擎未重启或未部署)",
        }
    rows = _read_jsonl_lines(pred_path)
    if not rows:
        return {"status": "EMPTY", "detail": "ledger 存在但零行"}
    # liveness 近似: ledger append-only + 文件 mtime (最近写入年龄)
    last_mtime = pred_path.stat().st_mtime
    age_minutes = (time.time() - last_mtime) / 60.0
    if age_minutes > recent_minutes:
        return {
            "status": "STALLED",
            "last_write_age_minutes": round(age_minutes, 1),
            "detail": f"最近写入 {age_minutes:.1f} 分钟前 (> {recent_minutes}m)",
        }
    return {
        "status": "STREAMING",
        "rows": len(rows),
        "last_write_age_minutes": round(age_minutes, 1),
    }


def _check_zero_real_orders(base_dir: Path) -> dict[str, Any]:
    """扫描实盘 ledger: ZERO 条带 shadow_ops 归属的真实订单."""
    journals = [
        base_dir / "live_trade_journal.jsonl",
        base_dir / "golden_master.jsonl",
    ]
    findings: list[str] = []
    total_lines = 0
    for jp in journals:
        if not jp.exists():
            continue
        with open(jp, encoding="utf-8") as fh:
            for i, line in enumerate(fh, start=1):
                total_lines += 1
                lowered = line.lower()
                if any(tok in lowered for tok in _SHADOW_TOKENS):
                    findings.append(f"{jp.name}:{i}: {line.strip()[:160]}")
    return {
        "violations": findings,
        "zero_real_orders": len(findings) == 0,
        "scanned_lines": total_lines,
    }


def _check_mandate_integrity(trigger_path: Path) -> dict[str, Any]:
    if not trigger_path.exists():
        return {"status": "CRITICAL", "detail": "trigger json 缺失"}
    try:
        spec = json.loads(trigger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "CRITICAL", "detail": f"trigger json 解析失败: {exc!r}"}
    problems: list[str] = []
    if spec.get("trigger_mode") != "quantile_top_decile_abs_pred":
        problems.append("trigger_mode != quantile_top_decile_abs_pred")
    if "FIXED_THRESHOLD_FORBIDDEN" not in str(spec.get("mandate", "")):
        problems.append("mandate 缺 FIXED_THRESHOLD_FORBIDDEN")
    thr = spec.get("threshold_abs_pred_pct")
    if not isinstance(thr, int | float) or float(thr) <= 0.0:
        problems.append("threshold_abs_pred_pct 非正")
    return {
        "status": "OK" if not problems else "CRITICAL",
        "problems": problems,
        "threshold_abs_pred_pct": thr,
    }


def _check_constructional_isolation(pkg_dir: Path) -> dict[str, Any]:
    """静态扫描 shadow_ops 包 import — 派发能力模块违禁即 CRITICAL."""
    import_re = re.compile(r"^\s*(?:import|from)\s+([A-Za-z0-9_\.]+)", re.MULTILINE)
    src = "\n".join(py.read_text(encoding="utf-8") for py in sorted(pkg_dir.glob("*.py")))
    tops = {m.group(1).split(".")[0] for m in import_re.finditer(src)}
    violations = [tok for tok in _DENYLIST if any(tok in t for t in tops)]
    return {"ok": len(violations) == 0, "violations": violations}


def main() -> int:
    p = argparse.ArgumentParser(description="ShadowOps Watchdog — DEFCON 1 audit")
    p.add_argument("--data-dir", default="data", help="Base data dir (XAU live)")
    p.add_argument(
        "--recent-minutes",
        type=int,
        default=1440,
        help="Liveness window (minutes); default 24h",
    )
    args = p.parse_args()

    base_dir = Path(args.data_dir)
    so_dir = base_dir / "shadow_ops"
    pred_path = so_dir / "micro_scaler_predictions.jsonl"
    trigger_path = (
        Path(_REPO_ROOT) / "data" / "training" / "micro_scaler_v2" / "micro_scaler_v2_trigger.json"
    )

    liveness = _check_liveness(pred_path, args.recent_minutes)
    zero_orders = _check_zero_real_orders(base_dir)
    mandate = _check_mandate_integrity(trigger_path)
    isolation = _check_constructional_isolation(
        Path(_REPO_ROOT) / "core" / "runtime" / "shadow_ops"
    )

    print("=" * 78)
    print("  SHADOW OPS WATCHDOG — DEFCON 1 AUDIT (Phase 4)")
    print("=" * 78)
    print(f"  [liveness ] {liveness['status']:<9} {liveness.get('detail','')}")
    if "rows" in liveness:
        print(
            f"             rows={liveness['rows']} last_write_age={liveness['last_write_age_minutes']}m"
        )
    print(
        f"  [zero-real] {'PASS (ZERO shadow orders in real journals)' if zero_orders['zero_real_orders'] else 'VIOLATION'}"
        f"  scanned={zero_orders['scanned_lines']} lines"
    )
    for v in zero_orders["violations"]:
        print(f"             ! {v}")
    print(f"  [mandate  ] {mandate['status']}  threshold={mandate.get('threshold_abs_pred_pct')}")
    for prob in mandate.get("problems", []):
        print(f"             ! {prob}")
    print(f"  [isolate  ] {'PASS' if isolation['ok'] else 'VIOLATION'}")
    for viol in isolation["violations"]:
        print(f"             ! import denylist: {viol}")
    print("=" * 78)

    critical = (
        not zero_orders["zero_real_orders"]
        or mandate["status"] == "CRITICAL"
        or not isolation["ok"]
    )
    if critical:
        print("[CRITICAL] DEFCON 1 red line breached — investigate immediately.")
        return 1
    if liveness["status"] not in ("STREAMING",):
        print(
            "[WARN] Ledger empty/stalled — engine restart pending or market closed. Not a DEFCON violation."
        )
        return 2
    print("[PASS] Shadow Ops telemetry healthy. Zero penetration to MT5 bridge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
