"""Architecture Scanner — AST-level codebase introspection.

Walks core/ apps/ scripts/ configs/ and extracts:
- Module inventory (classes, functions, signatures)
- Import dependency graph
- Schema version references
- Module health indicators

Used by architecture_gate.py (auto-update) and doc_generator.py.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class FunctionInfo:
    name: str
    args: list[str]
    returns: str | None
    decorators: list[str]
    is_async: bool
    doc: str | None


@dataclass
class ClassInfo:
    name: str
    bases: list[str]
    methods: list[FunctionInfo]
    decorators: list[str]
    doc: str | None


@dataclass
class ImportInfo:
    module: str  # empty string for bare imports
    names: list[str]
    is_from: bool


@dataclass
class ModuleInfo:
    """Complete introspection of a single Python file."""

    rel_path: str  # e.g. "core/brains/adapters/v9_onnx_brain_adapter.py"
    abs_path: str
    doc: str | None
    classes: list[ClassInfo]
    functions: list[FunctionInfo]
    imports: list[ImportInfo]
    schema_versions: list[str]  # SCHEMA_* constant names found
    line_count: int
    has_init_py: bool  # __init__.py exists in the same directory
    has_tests: bool  # corresponding test file exists under tests/
    status: str  # "active" | "stub" | "empty" | "config" | "unreadable"


@dataclass
class ScanResult:
    """Top-level scan output."""

    root: str
    modules: list[ModuleInfo]
    dependency_graph: dict[str, list[str]]  # module -> [dependencies]
    package_tree: dict[str, list[str]]  # package -> [modules]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decorator_name(d: ast.expr) -> str:
    """Safely extract a string representation of a decorator node."""
    try:
        if hasattr(ast, "unparse"):
            return ast.unparse(d)
    except Exception:
        pass
    if isinstance(d, ast.Name):
        return f"@{d.id}"
    if isinstance(d, ast.Attribute):
        return f"@{ast.unparse(d)}" if hasattr(ast, "unparse") else f"@{d.attr}"
    return "@<decorator>"


def _safe_docstring(node: ast.AST) -> str | None:
    """Get docstring only for nodes that support it."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module):
        return ast.get_docstring(node)
    return None


# ---------------------------------------------------------------------------
# AST visitor
# ---------------------------------------------------------------------------


def _extract_functions(tree: ast.AST) -> list[FunctionInfo]:
    funcs: list[FunctionInfo] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = [arg.arg for arg in node.args.args]
            returns = None
            if node.returns and hasattr(ast, "unparse"):
                returns = ast.unparse(node.returns)
            elif node.returns:
                returns = str(node.returns)
            funcs.append(
                FunctionInfo(
                    name=node.name,
                    args=args,
                    returns=returns,
                    decorators=[_decorator_name(d) for d in node.decorator_list],
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                    doc=_safe_docstring(node),
                )
            )
    return funcs


def _extract_classes(tree: ast.AST) -> list[ClassInfo]:
    classes: list[ClassInfo] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases: list[str] = []
            for b in node.bases:
                if hasattr(ast, "unparse"):
                    bases.append(ast.unparse(b))
                elif isinstance(b, ast.Name):
                    bases.append(b.id)
                elif isinstance(b, ast.Attribute):
                    bases.append(b.attr)
                else:
                    bases.append(str(b))
            classes.append(
                ClassInfo(
                    name=node.name,
                    bases=bases,
                    methods=_extract_functions(node),
                    decorators=[_decorator_name(d) for d in node.decorator_list],
                    doc=_safe_docstring(node),
                )
            )
    return classes


def _extract_imports(tree: ast.AST) -> list[ImportInfo]:
    imports: list[ImportInfo] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    ImportInfo(
                        module=alias.name,
                        names=[alias.asname or alias.name],
                        is_from=False,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.asname or alias.name for alias in node.names]
            imports.append(ImportInfo(module=module, names=names, is_from=True))
    return imports


def _extract_schema_versions(tree: ast.AST) -> list[str]:
    """Find SCHEMA_* constant references."""
    schemas: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and "SCHEMA_" in node.id:
            if node.id not in schemas:
                schemas.append(node.id)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and "SCHEMA_" in target.id:
                    if target.id not in schemas:
                        schemas.append(target.id)
    return schemas


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _find_test_file(module_path: Path, root: Path) -> bool:
    """Check if a corresponding test file exists under tests/."""
    rel = module_path.relative_to(root)
    test_path = root / "tests" / rel.with_suffix("").with_name(f"test_{rel.stem}.py")
    if test_path.exists():
        return True
    # Also try tests/<pkg>/test_<name>.py
    if len(rel.parts) > 1:
        test_path2 = root / "tests" / Path(*rel.parts[:-1]) / f"test_{rel.stem}.py"
        return test_path2.exists()
    return False


def _determine_status(
    module_path: Path, classes: list[ClassInfo], functions: list[FunctionInfo]
) -> str:
    """Heuristic to determine module readiness."""
    try:
        content = module_path.read_text(encoding="utf-8")
    except Exception:
        return "unreadable"

    if not classes and not functions:
        tree = ast.parse(content)
        meaningful = [
            node
            for node in tree.body
            if not isinstance(node, ast.Expr | ast.Import | ast.ImportFrom)
        ]
        if not meaningful:
            return "empty"
        return "config"

    has_not_impl = "raise NotImplementedError" in content
    if has_not_impl:
        return "stub"

    return "active"


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------


def scan_module(file_path: Path, root: Path) -> ModuleInfo:
    """Scan a single Python file and return ModuleInfo."""
    rel_path = str(file_path.relative_to(root)).replace("\\", "/")

    try:
        source = file_path.read_text(encoding="utf-8")
    except Exception:
        return ModuleInfo(
            rel_path=rel_path,
            abs_path=str(file_path),
            doc=None,
            classes=[],
            functions=[],
            imports=[],
            schema_versions=[],
            line_count=0,
            has_init_py=False,
            has_tests=False,
            status="unreadable",
        )

    tree = ast.parse(source)
    line_count = len(source.splitlines())

    classes = _extract_classes(tree)
    functions = [
        f for f in _extract_functions(tree) if not any(f is m for c in classes for m in c.methods)
    ]
    imports = _extract_imports(tree)
    schema_versions = _extract_schema_versions(tree)
    has_init_py = (file_path.parent / "__init__.py").exists()
    has_tests = _find_test_file(file_path, root)
    status = _determine_status(file_path, classes, functions)

    return ModuleInfo(
        rel_path=rel_path,
        abs_path=str(file_path),
        doc=_safe_docstring(tree),
        classes=classes,
        functions=functions,
        imports=imports,
        schema_versions=schema_versions,
        line_count=line_count,
        has_init_py=has_init_py,
        has_tests=has_tests,
        status=status,
    )


def scan_all(root: Path | None = None) -> ScanResult:
    """Scan the entire codebase under core/ apps/ scripts/ configs/ main.py."""
    if root is None:
        root = Path(__file__).resolve().parents[2]  # d:\future

    modules: list[ModuleInfo] = []
    dependency_graph: dict[str, list[str]] = {}
    package_tree: dict[str, list[str]] = {}

    scan_roots = [
        root / "core",
        root / "apps",
        root / "scripts",
        root / "configs",
    ]
    main_py = root / "main.py"
    if main_py.exists():
        modules.append(scan_module(main_py, root))

    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for py_file in sorted(scan_root.rglob("*.py")):
            if py_file.name == "__init__.py":
                continue
            if py_file.name.startswith("__"):
                continue
            mod = scan_module(py_file, root)
            modules.append(mod)

            # Package tree
            pkg = str(py_file.parent.relative_to(root)).replace("\\", "/")
            if pkg not in package_tree:
                package_tree[pkg] = []
            package_tree[pkg].append(mod.rel_path)

            # Dependency graph
            deps: list[str] = []
            for imp in mod.imports:
                if imp.module and imp.module.startswith(("core.", "apps.", "scripts.")):
                    deps.append(imp.module)
            dependency_graph[mod.rel_path] = sorted(set(deps))

    return ScanResult(
        root=str(root),
        modules=modules,
        dependency_graph=dependency_graph,
        package_tree=package_tree,
    )


def scan_to_json(root: Path | None = None, pretty: bool = True) -> str:
    """Run scan and return JSON string (for caching / diffing)."""
    result = scan_all(root)
    return json.dumps(
        {
            "root": result.root,
            "module_count": len(result.modules),
            "modules": [
                {
                    "rel_path": m.rel_path,
                    "status": m.status,
                    "line_count": m.line_count,
                    "class_count": len(m.classes),
                    "function_count": len(m.functions),
                    "has_init_py": m.has_init_py,
                    "has_tests": m.has_tests,
                    "schema_versions": m.schema_versions,
                    "classes": [{"name": c.name, "bases": c.bases} for c in m.classes],
                    "functions": [
                        {"name": f.name, "args": f.args, "is_async": f.is_async}
                        for f in m.functions
                    ],
                    "dependencies": [imp.module for imp in m.imports if imp.module],
                }
                for m in result.modules
            ],
        },
        indent=2 if pretty else None,
        ensure_ascii=False,
    )


if __name__ == "__main__":
    import sys

    root_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    print(scan_to_json(root_path))
