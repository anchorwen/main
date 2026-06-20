"""
BLE001 Phase 3e: FOG_DEFERRED → fail_open_guard() wrapping.

Targets all files with BLE001:FOG_DEFERRED sites (files without core imports
that were skipped by Phase 3d). Phase 3e adds the necessary import and wraps
each except body in ``with fail_open_guard("Context"):``.

Strategy: process files one at a time, verify syntax with compile()
after each file, skip files that fail compile verification.
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
    # Try to find enclosing class
    for i in range(lineno - 1, -1, -1):
        m = re.match(r'\s*class\s+(\w+)\s*[:\(]', lines[i])
        if m:
            return f"{Path(filepath).stem}:{m.group(1)}"
    # Fallback to module:lineno
    module = Path(filepath).stem
    return f"{module}:L{lineno}"


def migrate_file(filepath: str) -> tuple[int, int]:
    """Wrap all BLE001:FOG_DEFERRED sites in fail_open_guard()."""
    path = Path(filepath)
    lines = path.read_text(encoding="utf-8").splitlines()

    # Check for existing fail_open_guard import (flexible match)
    _fog_import_re = re.compile(
        r'from\s+core\.runtime\.fault_handler\s+import\b.*\bfail_open_guard\b'
    )
    has_fog_import = any(_fog_import_re.search(line) for line in lines)
    wrapped = 0

    # Find all BLE001:FOG_DEFERRED sites
    sites = []
    for i, line in enumerate(lines):
        if "BLE001:FOG_DEFERRED" in line and re.match(r'\s*except\s+Exception', line):
            sites.append(i)

    if not sites:
        return 0, 0

    # Process in reverse to avoid offset issues
    for except_idx in reversed(sites):
        except_line = lines[except_idx]
        except_indent = get_indent(except_line)
        body_indent = except_indent + 4

        # Detect inline body on the except line itself
        # e.g. "except Exception: pass  # BLE001:FOG_DEFERRED"
        inline_body = ""
        _comment_idx = except_line.find("#")
        _search_end = _comment_idx if _comment_idx != -1 else len(except_line)
        _colon_idx = except_line.find(":", 0, _search_end)
        if _colon_idx != -1:
            _after_colon = except_line[_colon_idx + 1 : _search_end].strip()
            if _after_colon:  # There's an inline body like "pass" or "return None"
                inline_body = _after_colon

        # Find body range (next-line body)
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
                if not (
                    re.match(r'(except|else|finally)\b', stripped)
                    and indent == except_indent
                ):
                    break
                else:
                    break
            body_end = i

        body_lines = (
            lines[body_start : body_end + 1] if body_end >= body_start else []
        )

        # Strip trailing blank lines
        while body_lines and body_lines[-1].strip() == "":
            body_lines.pop()

        ctx = context_name(filepath, except_idx + 1, lines)

        if inline_body:
            # Inline body on the except line — extract and wrap
            new_body = [
                f'{" " * body_indent}with fail_open_guard("{ctx}"):',
                f'{" " * (body_indent + 4)}{inline_body}',
            ]
            # Also remove inline body from except line
            _ci = except_line.find("#")
            _se = _ci if _ci != -1 else len(except_line)
            _co = except_line.find(":", 0, _se)
            _prefix = except_line[:_co + 1]
            _suffix = except_line[_se:]  # includes the comment
            new_except = (_prefix + "  " + _suffix).rstrip()
        elif not body_lines:
            new_body = [
                f'{" " * body_indent}with fail_open_guard("{ctx}"):',
                f'{" " * (body_indent + 4)}pass',
            ]
        else:
            new_body = [f'{" " * body_indent}with fail_open_guard("{ctx}"):']
            for bl in body_lines:
                if bl.strip() == "":
                    new_body.append("")
                else:
                    new_body.append(f"    {bl}")

        # Update annotation: FOG_DEFERRED → FOG
        if not inline_body:
            new_except = except_line.replace("BLE001:FOG_DEFERRED", "BLE001:FOG")
        else:
            new_except = new_except.replace("BLE001:FOG_DEFERRED", "BLE001:FOG")

        # Replace old lines with new
        old_end = (
            except_idx + 1 + len(body_lines) - 1 if body_lines else except_idx
        )
        # Adjust for trailing blanks we stripped
        while old_end + 1 < len(lines) and lines[old_end + 1].strip() == "":
            old_end += 1

        lines = lines[:except_idx] + [new_except] + new_body + lines[old_end + 1 :]
        wrapped += 1

    if wrapped > 0:
        # Add fail_open_guard import if needed
        if not has_fog_import:
            # Find last import line, accounting for multi-line parenthesized imports
            last_import = 0
            for i, line in enumerate(lines):
                if line.startswith("from ") or line.startswith("import "):
                    last_import = i
            # If the line has an unmatched open paren, scan forward past closing paren
            if "(" in lines[last_import] and ")" not in lines[last_import]:
                depth = lines[last_import].count("(") - lines[last_import].count(")")
                j = last_import + 1
                while j < len(lines) and depth > 0:
                    depth += lines[j].count("(") - lines[j].count(")")
                    if depth == 0:
                        last_import = j
                        break
                    j += 1
            lines.insert(
                last_import + 1,
                "from core.runtime.fault_handler import fail_open_guard",
            )

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
    target_dirs = ["core", "scripts", "apps"]
    # Hot-path directories already fully processed by Phase 3b/3c
    skip_dirs = {"core/runtime", "core/execution"}
    total_wrapped = 0
    total_errors = 0
    files_modified = 0

    print("=" * 70)
    print("BLE001 Phase 3e: FOG_DEFERRED → fail_open_guard() wrapping")
    print("=" * 70)

    for target_dir in target_dirs:
        print(f"\n--- {target_dir}/ ---")
        for fp in sorted(Path(target_dir).rglob("*.py")):
            fp_str = str(fp).replace("\\", "/")
            if (
                "test_" in fp.name
                or "__pycache__" in fp_str
                or fp.name.startswith("ble001_phase3")
            ):
                continue
            if any(fp_str.startswith(sd + "/") for sd in skip_dirs):
                continue
            w, e = migrate_file(str(fp))
            if w > 0:
                status = "OK" if e == 0 else "SYNTAX ERROR"
                print(f"  [{status}] {fp}: {w} sites → fail_open_guard")
                files_modified += 1
                total_wrapped += w
                if e > 0:
                    total_errors += e

    print(f"\n{'='*70}")
    print(f"TOTAL: {total_wrapped} wrapped, {total_errors} errors")
    print(f"  Files modified: {files_modified}")
    if total_errors > 0:
        print("WARNING: Syntax errors detected! Run verify.py --quick before committing.")
    else:
        print("All syntax checks passed. Run verify.py --quick to confirm.")
    print(f"{'='*70}")

    # Final count
    return total_errors


if __name__ == "__main__":
    raise SystemExit(main())
