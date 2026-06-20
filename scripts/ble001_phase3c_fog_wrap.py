"""
BLE001 Phase 3c: FOG_DEFERRED → fail_open_guard() wrapping.
Wraps each complex-body except handler in ``with fail_open_guard("Context"):``
and re-indents the body by +4 spaces.

Strategy: process files one at a time, verify syntax after each batch.
"""
import re
from pathlib import Path


def get_indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def context_name(filepath: str, lineno: int, lines: list[str]) -> str:
    """Derive a unique fail_open_guard context name."""
    # Try to find enclosing function
    for i in range(lineno - 1, -1, -1):
        m = re.match(r'\s*def\s+(\w+)\s*\(', lines[i])
        if m:
            return f"{Path(filepath).stem}:{m.group(1)}"
    # Fallback to module:lineno
    module = Path(filepath).stem
    return f"{module}:L{lineno}"


def migrate_file(filepath: str) -> tuple[int, int]:
    """Wrap all FOG_DEFERRED sites in fail_open_guard()."""
    path = Path(filepath)
    lines = path.read_text(encoding="utf-8").splitlines()

    has_fog_import = "from core.runtime.fault_handler import fail_open_guard" in "\n".join(lines)
    needs_fog_import = False
    wrapped = 0
    errors = 0

    # Find all FOG_DEFERRED sites
    sites = []
    for i, line in enumerate(lines):
        if "BLE001:FOG_DEFERRED" in line and re.match(r'\s*except\s+Exception', line):
            sites.append(i)

    # Process in reverse to avoid offset issues
    for except_idx in reversed(sites):
        except_line = lines[except_idx]
        except_indent = get_indent(except_line)
        body_indent = except_indent + 4  # standard body indent

        # Find body range
        body_start = except_idx + 1
        body_end = except_idx
        for i in range(body_start, len(lines)):
            line = lines[i]
            if line.strip() == "":
                body_end = i
                continue
            indent = get_indent(line)
            stripped = line.strip()
            if indent <= except_indent:
                if not (re.match(r'(except|else|finally)\b', stripped) and indent == except_indent):
                    break
                else:
                    break
            body_end = i

        body_lines = lines[body_start:body_end + 1] if body_end >= body_start else []

        # Strip trailing blank lines (they'll be re-added after the with block)
        while body_lines and body_lines[-1].strip() == "":
            body_lines.pop()

        if not body_lines:
            # Empty body — just add pass inside fail_open_guard
            new_body = [
                f'{" " * body_indent}with fail_open_guard("{context_name(filepath, except_idx+1, lines)}"):',
                f'{" " * (body_indent + 4)}pass',
            ]
        else:
            # Wrap body in fail_open_guard, re-indenting all body lines
            ctx = context_name(filepath, except_idx + 1, lines)
            new_body = [f'{" " * body_indent}with fail_open_guard("{ctx}"):']
            for bl in body_lines:
                if bl.strip() == "":
                    new_body.append("")  # preserve blank lines
                else:
                    # Add 4 spaces to existing indentation
                    new_body.append(f"    {bl}")

        # Update annotation
        new_except = except_line.replace("BLE001:FOG_DEFERRED", "BLE001:FOG")

        # Replace old lines with new
        old_end = except_idx + 1 + len(body_lines) - 1 if body_lines else except_idx
        # Adjust for trailing blanks we stripped
        while old_end + 1 < len(lines) and lines[old_end + 1].strip() == "":
            old_end += 1

        lines = lines[:except_idx] + [new_except] + new_body + lines[old_end + 1:]
        wrapped += 1
        needs_fog_import = True

    if wrapped > 0:
        # Add fail_open_guard import if needed
        if not has_fog_import and needs_fog_import:
            last_import = 0
            for i, line in enumerate(lines):
                if line.startswith("from ") or line.startswith("import "):
                    last_import = i
            lines.insert(last_import + 1, "from core.runtime.fault_handler import fail_open_guard")

        # Verify syntax
        new_content = "\n".join(lines) + "\n"
        try:
            compile(new_content, filepath, "exec")
        except SyntaxError as e:
            print(f"  [SYNTAX ERROR] {filepath}: {e}")
            return wrapped, 1

        path.write_text(new_content, encoding="utf-8")
        return wrapped, 0

    return 0, 0


def main():
    target_dirs = ["core/runtime", "core/execution"]
    total_wrapped = 0
    total_errors = 0
    files_modified = 0

    print("=" * 70)
    print("BLE001 Phase 3c: FOG_DEFERRED → fail_open_guard() wrapping")
    print("=" * 70)

    for target_dir in target_dirs:
        print(f"\n--- {target_dir}/ ---")
        for fp in sorted(Path(target_dir).rglob("*.py")):
            if "test_" in fp.name or "__pycache__" in str(fp):
                continue
            w, e = migrate_file(str(fp))
            if w > 0:
                status = "OK" if e == 0 else f"SYNTAX ERROR ({e})"
                print(f"  [{status}] {fp}: {w} sites wrapped → fail_open_guard")
                files_modified += 1
                total_wrapped += w
                total_errors += e

    print(f"\n{'='*70}")
    print(f"TOTAL: {total_wrapped} wrapped, {total_errors} errors, {files_modified} files")
    if total_errors > 0:
        print("WARNING: Syntax errors detected! Run verify.py --quick before committing.")
    else:
        print("All syntax checks passed. Run verify.py --quick to confirm.")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
