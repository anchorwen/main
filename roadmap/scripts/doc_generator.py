"""Auto-generate architecture/ docs from scanner output.

Produces:
  - MODULE_INVENTORY.md   (module status table)
  - DEPENDENCY_GRAPH.md   (import dependency matrix)
  - CHANGELOG.md append   (diff-based change log)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from roadmap.scripts.scanner import ScanResult, scan_all

ROOT = Path(__file__).resolve().parents[2]


def _gen_module_inventory(result: ScanResult) -> str:
    """Generate MODULE_INVENTORY.md from scan."""
    lines = [
        "# MODULE INVENTORY — 模块清单与完成度",
        "",
        f"> **自动生成**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"> **扫描模块数**: {len(result.modules)}",
        "> **图例**: ✅ active | 🧪 stub | 📄 config | ⬜ empty",
        "",
    ]
    for pkg in sorted(result.package_tree):
        pkg_modules = [m for m in result.modules if m.rel_path in result.package_tree[pkg]]
        if not pkg_modules:
            continue
        lines.append(f"## {pkg}")
        lines.append("")
        lines.append("| 模块 | 状态 | 类 | 函数 | 行数 | 测试 | 说明 |")
        lines.append("|------|------|----|------|------|------|------|")
        for m in sorted(pkg_modules, key=lambda x: x.rel_path):
            status_icon = {
                "active": "✅",
                "stub": "🧪",
                "config": "📄",
                "empty": "⬜",
                "unreadable": "❌",
            }.get(m.status, "❓")
            name = Path(m.rel_path).name
            test_icon = "✅" if m.has_tests else "—"
            classes_str = ", ".join(c.name for c in m.classes) if m.classes else "—"
            lines.append(
                f"| `{name}` | {status_icon} {m.status} | {classes_str} | "
                f"{len(m.functions)} | {m.line_count} | {test_icon} | |"
            )
        lines.append("")
    return "\n".join(lines)


def _gen_dependency_graph(result: ScanResult) -> str:
    """Generate DEPENDENCY_GRAPH.md."""
    lines = [
        "# DEPENDENCY GRAPH — 模块依赖关系",
        "",
        f"> **自动生成**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "## Package-Level Dependencies",
        "",
    ]
    for pkg in sorted(result.package_tree):
        lines.append(f"### `{pkg}/`")
        lines.append("")
        for mod_path in result.package_tree[pkg]:
            deps = result.dependency_graph.get(mod_path, [])
            mod_name = Path(mod_path).name
            if deps:
                dep_links = ", ".join(f"`{d}`" for d in deps)
                lines.append(f"- `{mod_name}` → {dep_links}")
            else:
                lines.append(f"- `{mod_name}` → (无内部依赖)")
        lines.append("")
    return "\n".join(lines)


def _gen_changelog_entry(result: ScanResult) -> str:
    """Compare to previous scan cache and generate changelog entry."""
    cache_path = ROOT / ".roadmap_scan_cache.json"
    now = datetime.now(UTC).isoformat()

    if not cache_path.exists():
        lines = [
            f"## {now} — 初始扫描",
            "",
            f"- 扫描 {len(result.modules)} 个模块",
            f"- active: {sum(1 for m in result.modules if m.status == 'active')}",
            f"- stub: {sum(1 for m in result.modules if m.status == 'stub')}",
            "",
        ]
        return "\n".join(lines)

    # TODO: compare with cached scan for diff-based changelog
    changed = len(result.modules)
    lines = [
        f"## {now} — 自动扫描更新",
        "",
        f"- 模块总数: {changed}",
        "",
    ]
    return "\n".join(lines)


def generate_all(result: ScanResult | None = None) -> dict[str, str]:
    """Run full document generation. Returns {filename: content}."""
    if result is None:
        result = scan_all(ROOT)

    return {
        "MODULE_INVENTORY.md": _gen_module_inventory(result),
        "DEPENDENCY_GRAPH.md": _gen_dependency_graph(result),
    }


def write_all(result: ScanResult | None = None):
    """Generate and write all architecture docs."""
    docs = generate_all(result)
    arch_dir = ROOT / "roadmap" / "architecture"

    for name, content in docs.items():
        path = arch_dir / name
        path.write_text(content, encoding="utf-8")
        print(f"[doc_generator] Wrote {path}")

    # Update changelog
    changelog_path = ROOT / "roadmap" / "changelog" / "CHANGELOG.md"
    entry = _gen_changelog_entry(result or scan_all(ROOT))
    with open(changelog_path, "a", encoding="utf-8") as f:
        f.write(f"\n{entry}\n")
    print(f"[doc_generator] Appended changelog to {changelog_path}")


if __name__ == "__main__":
    write_all()
