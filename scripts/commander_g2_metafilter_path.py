#!/usr/bin/env python
"""Window 1 - G2: MetaFilter Model Path + Control Group Audit
=============================================================
Checks:
  A. MetaFilter model files exist on disk (BTC + XAU)
  B. pred_history: is MetaFilter producing real, varied predictions?
  C. rolling_wr fallback: is the rolling win-rate acting as surrogate?
  D. Model path config vs actual file state

Output: self-contained closing report for G2.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_model_files(label: str, data_dir: Path) -> dict[str, Any]:
    """Check whether MetaFilter model files exist and produce real predictions."""
    result: dict[str, Any] = {
        "label": label,
        "data_dir": str(data_dir),
    }

    # ── A. MetaSignalFilter model (LGB .txt, used by live_intent_loop.py) ──
    #   Config: configs/brains/meta_stage2_filter_v3.json or configs/brains_btc/
    brains_subdir = "brains_btc" if label == "BTC" else "brains"
    cfg_path = Path(f"configs/{brains_subdir}/meta_stage2_filter_v3.json")
    result["msf_config_path"] = str(cfg_path)
    result["msf_config_exists"] = cfg_path.exists()

    if cfg_path.exists():
        cfg = load_json(cfg_path)
        model_path_str = cfg.get("model_path", "")
        result["msf_model_path_configured"] = model_path_str
        if model_path_str:
            p = Path(model_path_str)
            result["msf_model_file_exists"] = p.exists()
            result["msf_model_file_size"] = p.stat().st_size if p.exists() else 0
        else:
            result["msf_model_file_exists"] = False
            result["msf_model_file_size"] = 0

    # ── B. MetaFilterGate model (LGB .pkl, used by live_cycle.py) ──
    mg_pkl = data_dir / "models" / "meta_filter_v3" / "meta_filter_lightgbm.pkl"
    mg_fn = data_dir / "models" / "meta_filter_v3" / "feature_names.json"
    result["mfg_pkl_path"] = str(mg_pkl)
    result["mfg_pkl_exists"] = mg_pkl.exists()
    result["mfg_pkl_size"] = mg_pkl.stat().st_size if mg_pkl.exists() else 0
    result["mfg_fn_exists"] = mg_fn.exists()

    # ── C. pred_history analysis (meta_filter_state.json) ──
    mf_state_path = data_dir / "meta_filter_state.json"
    result["mf_state_path"] = str(mf_state_path)
    result["mf_state_exists"] = mf_state_path.exists()

    if mf_state_path.exists():
        mf = load_json(mf_state_path)
        pred_history = mf.get("pred_history", [])
        result["pred_history_count"] = len(pred_history)
        if pred_history:
            # Each entry is [timestamp, p_win]
            pwins = [entry[1] for entry in pred_history]
            unique_rounded = len(set(round(p, 6) for p in pwins))
            count_exact_05 = sum(1 for p in pwins if p == 0.5)
            count_near_05 = sum(1 for p in pwins if 0.499 < p < 0.501)
            result["pred_history_unique_pwin"] = unique_rounded
            result["pred_history_pct_05"] = round(count_exact_05 / len(pred_history) * 100, 1)
            result["pred_history_pct_near_05"] = round(count_near_05 / len(pred_history) * 100, 1)
            result["pwin_min"] = round(min(pwins), 6)
            result["pwin_max"] = round(max(pwins), 6)
            result["model_producing_real_predictions"] = (
                unique_rounded > 1 and count_exact_05 < len(pred_history) * 0.1
            )
        else:
            result["pred_history_unique_pwin"] = 0
            result["model_producing_real_predictions"] = False
    else:
        result["pred_history_count"] = 0
        result["model_producing_real_predictions"] = False

    # ── D. Journal p_win analysis ──
    journal_path = data_dir / "live_trade_journal.jsonl"
    result["journal_path"] = str(journal_path)
    if journal_path.exists():
        opens = []
        with open(journal_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("action") == "open":
                    opens.append(r)

        if opens:
            pwins_all = [o.get("p_win") for o in opens]
            pwins_valid = [p for p in pwins_all if p is not None]
            result["journal_open_count"] = len(opens)
            result["journal_pwin_none_count"] = sum(1 for p in pwins_all if p is None)
            if pwins_valid:
                unique_j = len(set(round(p, 6) for p in pwins_valid))
                count05_j = sum(1 for p in pwins_valid if p == 0.5)
                result["journal_pwin_valid_count"] = len(pwins_valid)
                result["journal_pwin_unique"] = unique_j
                result["journal_pwin_pct_05"] = round(count05_j / len(pwins_valid) * 100, 1)
                result["journal_pwin_min"] = round(min(pwins_valid), 4)
                result["journal_pwin_max"] = round(max(pwins_valid), 4)
                result["journal_rolling_wr_dominant"] = count05_j > len(pwins_valid) * 0.8
            else:
                result["journal_pwin_valid_count"] = 0
                result["journal_rolling_wr_dominant"] = True

            # Strategy breakdown
            strat_counts = Counter((o.get("strategy") or "?") for o in opens)
            result["journal_strategies"] = dict(strat_counts.most_common(5))
        else:
            result["journal_open_count"] = 0
    else:
        result["journal_open_count"] = 0

    return result


def main():
    targets = [
        ("BTC", Path("data_btc")),
        ("XAU", Path("data")),
    ]

    print("=" * 72)
    print("  WINDOW 1: G2 — MetaFilter Model Path + Control Group Audit")
    print("=" * 72)

    all_results = {}
    for label, data_dir in targets:
        print(f"\n{'─' * 72}")
        print(f"  {label} ({data_dir})")
        print(f"{'─' * 72}")
        r = check_model_files(label, data_dir)
        all_results[label] = r

        # ── A. MetaSignalFilter model ──
        print("\n  [A] MetaSignalFilter (LGB .txt — live_intent_loop.py)")
        print(
            f"      config:          {'EXISTS' if r.get('msf_config_exists') else 'MISSING'} ({r.get('msf_config_path')})"
        )
        print(f"      model_path cfg:  {r.get('msf_model_path_configured', 'N/A')}")
        _msf_size = r.get("msf_model_file_size", 0)
        print(
            f"      model on disk:   {'YES' if r.get('msf_model_file_exists') else 'NO'}  ({_msf_size} bytes)"
        )

        # ── B. MetaFilterGate model ──
        print("\n  [B] MetaFilterGate (LGB .pkl — live_cycle.py)")
        print(f"      pkl path:        {r.get('mfg_pkl_path')}")
        _mfg_size = r.get("mfg_pkl_size", 0)
        print(
            f"      pkl on disk:     {'YES' if r.get('mfg_pkl_exists') else 'NO'}  ({_mfg_size} bytes)"
        )
        print(f"      feature_names:   {'YES' if r.get('mfg_fn_exists') else 'NO'}")

        # ── C. pred_history ──
        print("\n  [C] pred_history (meta_filter_state.json)")
        print(f"      state file:      {'EXISTS' if r.get('mf_state_exists') else 'MISSING'}")
        print(f"      entries:         {r.get('pred_history_count', 0)}")
        if r.get("pred_history_count", 0) > 0:
            print(f"      p_win unique:    {r.get('pred_history_unique_pwin')}")
            print(f"      p_win == 0.5:    {r.get('pred_history_pct_05')}%")
            print(f"      p_win range:     [{r.get('pwin_min')}, {r.get('pwin_max')}]")
            print(
                f"      REAL predictions: {'[YES]' if r.get('model_producing_real_predictions') else '[NO]'}"
            )

        # ── D. Journal ──
        print("\n  [D] live_trade_journal.jsonl")
        jc = r.get("journal_open_count", 0)
        if jc > 0:
            print(f"      opens:           {jc}")
            print(f"      p_win is None:   {r.get('journal_pwin_none_count', 0)}")
            jvc = r.get("journal_pwin_valid_count", 0)
            if jvc > 0:
                print(f"      p_win unique:    {r.get('journal_pwin_unique')}")
                print(f"      p_win == 0.5:    {r.get('journal_pwin_pct_05')}%")
                print(
                    f"      p_win range:     [{r.get('journal_pwin_min')}, {r.get('journal_pwin_max')}]"
                )
                print(
                    f"      rolling_wr dom?: {'[WARN] YES' if r.get('journal_rolling_wr_dominant') else 'NO - model values flowing'}"
                )
            print(f"      strategies:      {r.get('journal_strategies', {})}")

    # ── GAP assessment ──
    print(f"\n{'=' * 72}")
    print("  G2 VERDICT")
    print(f"{'=' * 72}")

    gaps = []
    warnings = []

    for label, r in all_results.items():
        msf_exists = r.get("msf_model_file_exists", False)
        mfg_exists = r.get("mfg_pkl_exists", False)
        real_preds = r.get("model_producing_real_predictions", False)
        wr_dominant = r.get("journal_rolling_wr_dominant", True)
        pwin_none_pct = (
            r.get("journal_pwin_none_count", 0) / max(r.get("journal_open_count", 1), 1) * 100
        )

        if not msf_exists and not mfg_exists:
            gaps.append(f"{label}: NO MetaFilter model files on disk (either type)")
        elif not real_preds:
            gaps.append(f"{label}: pred_history shows NO real variety (model dead?)")

        if wr_dominant:
            warnings.append(
                f"{label}: {r.get('journal_pwin_pct_05', 0)}% of journal opens have p_win=0.5 "
                f"(cold_explore / rolling_wr dominating)"
            )

        if pwin_none_pct > 50:
            warnings.append(
                f"{label}: {pwin_none_pct:.0f}% of journal opens have p_win=None "
                f"(p_win not written to journal)"
            )

    # Gaps are hard failures; warnings are soft
    if gaps:
        print("  [FAIL] HARD GAPS:")
        for g in gaps:
            print(f"     [!] {g}")
        for w in warnings:
            print(f"     [W]  {w}")
        print(f"\n  G2 STATUS: OPEN ({len(gaps)} gap(s), {len(warnings)} warning(s))")
    else:
        print("  [PASS] No hard gaps - model files exist, real predictions verified.")
        if warnings:
            print("  [WARN] Warnings:")
            for w in warnings:
                print(f"     {w}")
        print(f"\n  G2 STATUS: CLOSED (with {len(warnings)} observation(s))")

    print("\n[DONE] Window 1 complete.")


if __name__ == "__main__":
    main()
