"""
BLE001 Phase 3b: Hot-path pass-only REVIEWED → fail_open_guard() migration.
ONLY converts ``except Exception: pass`` patterns — the truly blind spots.
Complex bodies are annotated BLE001:FOG_DEFERRED for manual Phase 3c review.
"""
import re
from pathlib import Path


def derive_context_name(filepath: str, lineno: int) -> str:
    """Derive a unique fail_open_guard context name."""
    module = Path(filepath).stem
    module_name = "".join(w.capitalize() for w in module.split("_"))
    return f"{module_name}:L{lineno}"


def extract_function_name(lines: list[str], lineno: int) -> str:
    """Find enclosing function name for a given line number."""
    for i in range(lineno - 1, -1, -1):
        m = re.match(r'\s*def\s+(\w+)\s*\(', lines[i])
        if m:
            return m.group(1)
    return ""


def get_indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def migrate_file(filepath: str) -> tuple[int, int]:
    """Migrate pass-only REVIEWED sites. Returns (migrated, deferred)."""
    path = Path(filepath)
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()

    has_fog_import = "from core.runtime.fault_handler import fail_open_guard" in content
    needs_fog_import = False
    migrated = 0
    deferred = 0

    # Find all REVIEWED sites
    review_sites = []
    for i, line in enumerate(lines):
        if "BLE001:REVIEWED" in line and re.match(r'\s*except\s+Exception', line):
            review_sites.append(i)

    # Process in reverse to avoid offset issues
    for except_idx in reversed(review_sites):
        except_line = lines[except_idx]
        except_indent = get_indent(except_line)
        body_indent = except_indent + 4

        # Collect body lines
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

        # Strip trailing blank lines from body
        while body_lines and body_lines[-1].strip() == "":
            body_lines.pop()

        # Only convert pass-only sites
        is_pass_only = (
            len(body_lines) == 1 and body_lines[0].strip() == "pass"
        ) or len(body_lines) == 0

        if is_pass_only:
            func_name = extract_function_name(lines, except_idx)
            ctx = derive_context_name(filepath, except_idx + 1)
            if func_name:
                ctx = f"{Path(filepath).stem}:{func_name}"

            # Build replacement lines
            ws_body = " " * body_indent
            ws_with = " " * (body_indent + 4)

            new_lines = []
            # Keep the except line, add BLE001:FOG annotation
            clean_except = except_line.rstrip()
            # Remove old REVIEWED annotation, add FOG
            clean_except = re.sub(r'\s*#\s*BLE001:REVIEWED.*$', '', clean_except).rstrip()
            new_lines.append(f'{clean_except}  # BLE001:FOG')

            if body_lines:
                # Wrap the pass in fail_open_guard
                new_lines.append(f'{ws_body}with fail_open_guard("{ctx}"):')
                new_lines.append(f'{ws_with}pass')
            else:
                # Empty body — add pass inside fail_open_guard
                new_lines.append(f'{ws_body}with fail_open_guard("{ctx}"):')
                new_lines.append(f'{ws_with}pass')

            # Replace old lines with new
            old_end = max(body_end, except_idx)
            lines = lines[:except_idx] + new_lines + lines[old_end + 1:]
            migrated += 1
            needs_fog_import = True
        else:
            # Complex body — mark as deferred for manual review
            if "BLE001:FOG_DEFERRED" not in lines[except_idx]:
                lines[except_idx] = lines[except_idx].replace(
                    "BLE001:REVIEWED", "BLE001:FOG_DEFERRED"
                )
            deferred += 1

    if migrated > 0 or deferred > 0:
        # Add fail_open_guard import if needed
        if not has_fog_import and needs_fog_import:
            last_import = 0
            for i, line in enumerate(lines):
                if line.startswith("from ") or line.startswith("import "):
                    last_import = i
            lines.insert(last_import + 1, "from core.runtime.fault_handler import fail_open_guard")

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return migrated, deferred


def main():
    target_dirs = ["core/runtime", "core/execution"]
    total_migrated = 0
    total_deferred = 0

    print("=" * 70)
    print("BLE001 Phase 3b: pass-only REVIEWED → fail_open_guard()")
    print("=" * 70)

    for target_dir in target_dirs:
        print(f"\n--- {target_dir}/ ---")
        for fp in sorted(Path(target_dir).rglob("*.py")):
            if "test_" in fp.name or "__pycache__" in str(fp):
                continue
            m, d = migrate_file(str(fp))
            if m > 0:
                print(f"  [MIGRATED] {fp}: {m} pass-only → fail_open_guard")
            if d > 0:
                print(f"  [DEFERRED] {fp}: {d} complex body → FOG_DEFERRED (Phase 3c)")
            total_migrated += m
            total_deferred += d

    print(f"\n{'='*70}")
    print(f"TOTAL: {total_migrated} migrated, {total_deferred} deferred to Phase 3c")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
