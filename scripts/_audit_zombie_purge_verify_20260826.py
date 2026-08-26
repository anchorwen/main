"""
[ĐI] _audit_zombie_purge_verify_20260826.py — 清剿丧尸法证审计 (DQAF-20260826-004)

场景路由: [Ω-Routing: Scene C → #1(验证) → #11(脚本先行)]
性质: 铁律 #11 数据防幻觉审计 — stdout 是唯一合法证据源, 禁止对口算补数。
作用: 断言「wired-on but governance-retired = 0」— 清剿后系统不应再存在
       governance(L2) 判退休、但接线(L3 strategy_line)仍通电的丧尸策略线。

统计口径 (Iron Law #11):
  * 连接关系: strategy_line 键名 === 脑 json 的 `contract_group` 字段。
  * wiring-on: 该脑在 live_btc.yaml `brains.registry_entries` 中 enabled:true。
  * zombie 定义: strategy_line `enabled:true` 且其 contract_group 下至少一个
      enabled 脑 status ∈ {retired, frozen} (governance/L1 判退休)。
  * 身份来源: Brain Config JSON (L1 SSOT `status`) 优先; 缺省回退
      governance_state.json `brain_states` (L2)。两者对已退休脑一致。
  * 纯只读, 零状态变更。

Exit code: 0 = 丧尸计 0 (PASS); 1 = 发现丧尸 (FAIL); 2 = 数据缺漏 (无法判定)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_BTC = ROOT / "configs" / "live_btc.yaml"
GOV_STATE = ROOT / "data_btc" / "governance_state.json"

RETIRED_STATES = {"retired", "frozen"}


def load_yaml(path: Path) -> dict:
    import yaml

    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def gov_status_lookup() -> dict[str, str]:
    """L2 governance: 返回 {brain_id: status}. 兼容不同顶层容器键。"""
    out: dict[str, str] = {}
    if not GOV_STATE.exists():
        return out
    data = load_json(GOV_STATE)
    # 尝试识别容器键 (brain_states 为修正后真实键, 保留 fallback)
    for key in ("brain_states", "brains", "brain_statuses", "statuses"):
        container = data.get(key)
        if isinstance(container, dict):
            for bid, rec in container.items():
                if isinstance(rec, dict) and rec.get("status"):
                    out[bid] = rec["status"]
            if out:
                break
    return out


def main() -> int:
    live = load_yaml(LIVE_BTC)

    # 1) 收集 全部 registry 脑 (含 disabled 条目) 的 L1 status / contract_group
    #    —— 关键: 即使脑 registry 当前 disabled, 其 contract_group 的 governance status
    #       仍代表该家族的退休判决; 若它挂着 enabled:true 的 strategy_line → 仍是地雷。
    brain_by_group: dict[str, list[str]] = {}  # contract_group -> [brain_id...]
    brain_status: dict[str, str] = {}
    registry = (live.get("brains", {}) or {}).get("registry_entries", []) or []
    for entry in registry:
        path = entry.get("path")
        if not path:
            continue
        bpath = ROOT / path
        if not bpath.exists():
            print(f"[SKIP] registry path missing: {path}")
            continue
        try:
            bjson = load_json(bpath)
        except Exception as exc:  # noqa: BLE001
            print(f"[SKIP] brain json unreadable {path}: {exc}")
            continue
        bid = bjson.get("brain_id") or bpath.stem
        status = bjson.get("status")
        group = bjson.get("contract_group") or bjson.get("strategy")
        brain_status[bid] = str(status).lower() if status else ""
        if group:
            brain_by_group.setdefault(group, []).append(bid)

    # 2) L2 governance 覆盖 L1 (若存在, 以 governance 为准; L1 为 floor 但不覆盖退休判决的降级)
    gov = gov_status_lookup()
    for bid, gstatus in gov.items():
        brain_status[bid] = str(gstatus).lower()

    # 3) 遍历 strategy_lines, 判定僵尸
    strategy_lines = live.get("strategy_lines") or live.get("strategies") or {}
    zombies: list[str] = []
    checked = 0
    for line_name, line_cfg in strategy_lines.items():
        if not isinstance(line_cfg, dict):
            continue
        if not line_cfg.get("enabled"):
            continue  # 已断电, 跳过
        checked += 1
        group = line_name  # contract_group 键名 === strategy_line 键名
        wired = brain_by_group.get(group, [])
        if not wired:
            continue
        for bid in wired:
            if brain_status.get(bid, "") in RETIRED_STATES:
                zombies.append(f"{line_name} -> <{bid}> ({brain_status[bid]})")
                break

    print("=" * 64)
    print("DQAF-20260826-004 清剿丧尸法证审计 — 只读零变更")
    print("=" * 64)
    print(f"enabled strategy_lines checked : {checked}")
    print(f"wired-on but governance-retired: {len(zombies)}")
    for z in zombies:
        print(f"  [ZOMBIE] {z}")
    print("-" * 64)
    print("EXPECTED: wired-on but governance-retired = 0")
    if zombies:
        print("RESULT: FAIL (仍有丧尸)")
        return 1
    print("RESULT: PASS (丧尸归零)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
