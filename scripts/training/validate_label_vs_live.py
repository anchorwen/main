"""Label ↔ live hard gate (战役二 — 标签契约的绝对独裁).

FIX-20260803-003 / IC 最高批准: training scripts are DEPRIVED of the power to
set SL/TP/spread.  The ONLY source of truth is the strategy line in the live
YAML (``label_from_live_yaml.py``).  Any training contract whose label triple
(SL / TP / spread) diverges from the live strategy line → RAISE (fuse) +
non-zero exit → REFUSE to produce any model.

This kills the DQAF-20260630-200 root-cause class (training ↔ live parameter
misalignment with the alignment direction reversed).

Iron Law #11: stdout is the only legal evidence source — all printed values
come from script computation.

Usage:
  python scripts/training/validate_label_vs_live.py \
    --strategy btc_swing_m30 --live configs/live_btc.yaml \
    --contract configs/training/label_contracts/label-barrier-btc-m30.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Windows console defaults to GBK — force UTF-8 for Chinese/unicode output.
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8", errors="replace")

from core.contracts.training.label_contract import LabelContract  # noqa: E402
from core.contracts.training.label_from_live_yaml import (  # noqa: E402
    label_params_from_live_yaml,
)

# Tolerated float delta for SL/TP/spread comparison (MT5 float rounding).
_TOL = 1e-6


class LabelLiveMismatchError(RuntimeError):
    """Fuse: a training contract diverges from the live strategy line.

    Raised by the hard gate so any caller refuses to produce a model from
    mismatched labels.  This is a BLOCKING error, not a warning.
    """


def validate_label_contract_vs_live(
    contract: LabelContract,
    strategy_line: str,
    live_yaml_path: str | Path,
    *,
    tolerance: float = _TOL,
) -> list[str]:
    """Return mismatch issues between a label contract and the live strategy line.

    Empty list = aligned (pass).  Non-empty = the contract MUST NOT be used for
    training — the caller is expected to raise ``LabelLiveMismatchError``.
    """
    live = label_params_from_live_yaml(strategy_line, live_yaml_path)
    issues: list[str] = []

    if abs(contract.sl_atr_mult - live.sl_atr_mult) > tolerance:
        issues.append(
            f"SL mismatch: contract={contract.sl_atr_mult:.4g} vs live={live.sl_atr_mult:.4g}"
        )
    if abs(contract.tp_atr_mult - live.tp_atr_mult) > tolerance:
        issues.append(
            f"TP mismatch: contract={contract.tp_atr_mult:.4g} vs live={live.tp_atr_mult:.4g}"
        )
    if abs(contract.spread_points - live.spread_points) > tolerance:
        issues.append(
            f"spread mismatch: contract={contract.spread_points:g} vs live={live.spread_points:g}"
        )
    if contract.bar_timeframe.upper() != live.timeframe.upper():
        issues.append(
            f"timeframe mismatch: contract={contract.bar_timeframe} vs live={live.timeframe}"
        )
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description="Label ↔ live hard gate")
    parser.add_argument("--strategy", required=True, help="Live strategy line name")
    parser.add_argument("--live", default="configs/live_btc.yaml", help="Live YAML path")
    parser.add_argument("--contract", required=True, help="Label contract JSON path")
    args = parser.parse_args()

    contract_path = PROJECT_ROOT / args.contract
    live_path = PROJECT_ROOT / args.live

    print("=" * 72)
    print("Label ↔ Live Hard Gate (FIX-20260803-003 / 战役二)")
    print(f"  Strategy: {args.strategy}")
    print(f"  Contract: {contract_path}")
    print(f"  Live:     {live_path}")
    print("=" * 72)

    contract = LabelContract.from_file(contract_path)
    live = label_params_from_live_yaml(args.strategy, live_path)

    print("\n[1] Live SSOT params:")
    for k, v in live.to_dict().items():
        print(f"    {k}: {v}")

    print("\n[2] Contract params:")
    print(f"    sl_atr_mult:  {contract.sl_atr_mult:.4g}")
    print(f"    tp_atr_mult:  {contract.tp_atr_mult:.4g}")
    print(f"    spread_points:{contract.spread_points:g}")
    print(f"    bar_timeframe:{contract.bar_timeframe}")

    issues = validate_label_contract_vs_live(contract, args.strategy, live_path)
    if issues:
        print("\n[FUSE] LabelLiveMismatchError — label contract diverges from live:")
        for issue in issues:
            print(f"    ✗ {issue}")
        print("\nRefusing to produce any model from mismatched labels.")
        raise LabelLiveMismatchError("; ".join(issues))

    print("\n[PASS] Label contract aligned with live strategy line — gate PASSED.")
    print("  Next: build_btc_dataset_from_ssot.py can derive labels safely.")


if __name__ == "__main__":
    main()
