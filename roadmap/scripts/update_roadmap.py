#!/usr/bin/env python3
"""
路线图自动更新脚本

功能:
1. 扫描 core/ 目录检测新模块
2. 对比 roadmap.json 中的预期里程碑
3. 自动标记已完成项
4. 追加 changelog/CHANGELOG.md
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# === 配置 ===
ROADMAP_ROOT = Path(__file__).resolve().parent.parent  # roadmap/
PROJECT_ROOT = ROADMAP_ROOT.parent  # D:\future\
ROADMAP_JSON_PATH = ROADMAP_ROOT / "roadmap.json"
CHANGELOG_PATH = ROADMAP_ROOT / "changelog" / "CHANGELOG.md"
CORE_DIR = PROJECT_ROOT / "core"

# 预期模块定义：模块名 → 文件路径（相对于项目根目录）
EXPECTED_MODULES = {
    "ServiceContainer": "core/deployment/service_container.py",
    "BrainFactory": "core/brains/services/brain_factory.py",
    "BrainRegistryService": "core/features/feature_service.py",
    "BrainRunService": "core/brains/services/brain_run_service.py",
    "V9OnnxBrainAdapter": "core/brains/adapters/v9_onnx_brain_adapter.py",
    "RuntimeLoop": "apps/engine/runtime_loop.py",
    "DecisionCycleOrchestrator": "apps/engine/orchestrator.py",
    "ParliamentService": "core/parliament/parliament_service.py",
    "GovernanceRuleEngine": "core/governance/governance_rule_engine.py",
    "RiskEvaluationService": "core/risk/risk_evaluation_service.py",
    "FeedbackLoop": "core/feedback/feedback_loop.py",
    "ConfigHotReload": "core/deployment/config_hot_reload.py",
    "SystemModeState": "core/state/stores/system_mode_store.py",
    "CommunicationDispatcher": "core/protocol/services/communication_dispatcher.py",
    "PortfolioAllocator": "core/alpha/portfolio_allocator.py",
    "PromotionGate": "core/alpha/promotion_gate.py",
    "BrainPerformanceTracker": "core/feedback/brain_performance_tracker.py",
    "DecisionCompiler": "core/protocol/services/decision_compiler.py",
    "ExecutionManager": "core/execution/execution_manager.py",
    "MT5CommunicationAdapter": "core/protocol/services/mt5_communication_adapter.py",
    "FIXGatewayAdapter": "core/execution/fix_gateway_adapter.py",
}

# 里程碑的关键文件要求
MILESTONE_FILE_CHECKS = {
    "A1": ["main.py"],
    "A2": [
        "core/brains/services/brain_factory.py",
        "core/brains/adapters/v9_onnx_brain_adapter.py",
    ],
    "A3": ["core/features/feature_service.py", "core/features/local_feature_store.py"],
    "A4": [],  # 部署相关，需手动确认
    "B1": ["core/brains/services/brain_run_service.py"],
    "B2": ["core/parliament/parliament_service.py", "core/governance/governance_rule_engine.py"],
    "B3": ["core/alpha/portfolio_allocator.py", "core/alpha/performance_store.py"],
    "C1": ["scripts/training/your_trainer.py"],
    "C2": ["core/alpha/promotion_gate.py", "scripts/live_shadow_intent_producer.py"],
}

# 脚本层面可能的新模块（扫描 scripts/ 目录）
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SCRIPTS_EXPECTED = {
    "TrainerRegistry": "scripts/training/your_trainer.py",
    "ShadowLiveIntentProducer": "scripts/live_shadow_intent_producer.py",
    "LiveIntentLoop": "scripts/live_intent_loop.py",
    "SendLiveOrder": "scripts/send_live_order.py",
}


def check_file_exists(relative_path: str) -> bool:
    """检查文件是否存在"""
    return (PROJECT_ROOT / relative_path).exists()


def scan_modules() -> dict:
    """
    扫描预期模块的文件存在状态
    返回 {module_name: {"path": str, "exists": bool}}
    """
    results = {}
    all_modules = {**EXPECTED_MODULES, **SCRIPTS_EXPECTED}
    for module_name, path in all_modules.items():
        results[module_name] = {
            "path": path,
            "exists": check_file_exists(path),
        }
    return results


def check_milestones(module_scan: dict) -> list:
    """
    基于文件存在状态检查里程碑完成情况
    返回已完成的里程碑ID列表
    """
    completed = []
    for milestone_id, required_files in MILESTONE_FILE_CHECKS.items():
        if not required_files:
            continue  # 跳过需要手动确认的里程碑
        all_exist = all(check_file_exists(f) for f in required_files)
        if all_exist:
            completed.append(milestone_id)
    return completed


def update_roadmap_json(scan_results: dict, completed_milestones: list) -> list:
    """
    更新 roadmap.json 并返回变更列表
    """
    if not ROADMAP_JSON_PATH.exists():
        print(f"[ERROR] roadmap.json not found at {ROADMAP_JSON_PATH}")
        return []

    with open(ROADMAP_JSON_PATH, encoding="utf-8") as f:
        roadmap = json.load(f)

    changes = []

    # 更新现有基础设施状态
    existing = roadmap.get("existing_infrastructure", {})
    completed_list = existing.get("completed", [])

    for item in completed_list:
        module_name = item.get("module", "")
        if module_name in scan_results:
            if scan_results[module_name]["exists"] and item.get("status") != "active":
                item["status"] = "active"
                changes.append(f"模块 {module_name} 状态更新为 active")
            elif not scan_results[module_name]["exists"] and item.get("status") == "active":
                item["status"] = "missing"
                changes.append(f"模块 {module_name} 文件缺失，状态更新为 missing")

    # 检查是否有新模块（在 scan_results 中但不在 completed_list 中）
    existing_names = {item["module"] for item in completed_list}
    for module_name, info in scan_results.items():
        if module_name not in existing_names and info["exists"]:
            completed_list.append(
                {
                    "module": module_name,
                    "path": info["path"],
                    "status": "active",
                }
            )
            changes.append(f"发现新模块: {module_name} ({info['path']})")

    # 更新里程碑状态
    for milestone in roadmap.get("milestones", []):
        mid = milestone.get("id", "")
        if mid in completed_milestones and milestone.get("status") == "pending":
            milestone["status"] = "completed"
            changes.append(f"里程碑 {mid} ({milestone.get('name', '')}) 自动标记为已完成")

    # 更新时间戳
    tz_shanghai = timezone(timedelta(hours=8))
    now = datetime.now(tz_shanghai).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    roadmap["meta"]["last_updated"] = now

    with open(ROADMAP_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(roadmap, f, ensure_ascii=False, indent=2)

    return changes


def append_changelog(changes: list):
    """追加变更日志到 CHANGELOG.md"""
    if not changes:
        return

    with open(CHANGELOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n## [{datetime.now().strftime('%Y-%m-%d')}] 自动扫描更新\n\n")

        datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for change in changes:
            f.write(f"- {change}\n")

        f.write("\n---\n")


def main():
    print("=" * 60)
    print("  路线图自动更新脚本")
    print("=" * 60)

    # 1. 扫描模块
    print("\n[1/3] 扫描模块文件...")
    scan_results = scan_modules()
    found = sum(1 for v in scan_results.values() if v["exists"])
    total = len(scan_results)
    print(f"  找到 {found}/{total} 个预期模块")

    # 2. 检查里程碑
    print("\n[2/3] 检查里程碑完成状态...")
    completed = check_milestones(scan_results)
    if completed:
        print(f"  已完成里程碑: {', '.join(completed)}")
    else:
        print("  无新完成的里程碑")

    # 3. 更新 roadmap.json
    print("\n[3/3] 更新 roadmap.json...")
    changes = update_roadmap_json(scan_results, completed)

    if changes:
        print(f"  发现 {len(changes)} 项变更:")
        for change in changes:
            print(f"    - {change}")

        # 追加 changelog
        append_changelog(changes)
        print(f"\n  变更已记录到 {CHANGELOG_PATH}")
    else:
        print("  无变更")

    print("\n" + "=" * 60)
    print("  更新完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
