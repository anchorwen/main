"""
Extract all @health_check decorated methods from data_health_service.py
into a HealthCheckMethods mixin class in health_checks.py.
"""
import re
from pathlib import Path

SRC = Path("core/observability/data_health_service.py")
DST = Path("core/observability/health_checks.py")

content = SRC.read_text(encoding="utf-8")
lines = content.splitlines()

# Find all @health_check decorated methods
# Multi-line decorator pattern:
#   @health_check(
#       tier=...,
#       source="...",
#       description="...",
#   )
#   def check_xxx(self) -> SourceCheckResult:

check_ranges = []
i = 0
while i < len(lines):
    if lines[i].strip().startswith("@health_check("):
        start = i
        # Find end of multi-line decorator (closing paren)
        paren_depth = 0
        found_open = False
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped.startswith("@health_check("):
                found_open = True
            if found_open:
                paren_depth += stripped.count("(") - stripped.count(")")
            if found_open and paren_depth == 0:
                break
            i += 1
        # i now points to the line with closing )
        i += 1  # move past the decorator closing

        # Skip blank lines
        while i < len(lines) and lines[i].strip() == "":
            i += 1

        if i < len(lines) and re.match(r'\s*def\s+check_\w+', lines[i]):
            def_start = i
            def_indent = len(lines[i]) - len(lines[i].lstrip())
            i += 1
            # Find method end
            while i < len(lines):
                if lines[i].strip() == "":
                    i += 1
                    continue
                stripped = lines[i].strip()
                cur_indent = len(lines[i]) - len(lines[i].lstrip())
                # Next decorator or method at same indent ends this method
                if stripped.startswith("@health_check("):
                    break
                if stripped.startswith("def ") and cur_indent <= def_indent:
                    break
                i += 1
            end = i
            check_ranges.append((start, end))
            continue
    i += 1

print(f"Found {len(check_ranges)} @health_check methods")

# Find cross-check/helper methods
cross_check_methods = []
for method_name in ["_check_brain_registry_governance_alignment",
                     "_check_journal_vs_pnl_ledger",
                     "_check_open_vs_close_convergence",
                     "_detect_orphan_subsystems",
                     "_hydrate_behavioral_metrics"]:
    for j, line in enumerate(lines):
        if f"def {method_name}" in line:
            def_indent = len(line) - len(line.lstrip())
            k = j + 1
            while k < len(lines):
                if lines[k].strip() == "":
                    k += 1
                    continue
                stripped = lines[k].strip()
                cur_indent = len(lines[k]) - len(lines[k].lstrip())
                if stripped.startswith("@health_check("):
                    break
                if stripped.startswith("def ") and cur_indent <= def_indent:
                    break
                k += 1
            cross_check_methods.append((j, k))
            break

print(f"Found {len(cross_check_methods)} cross-check/helper methods")

# Build the mixin file
mixin_lines = []
mixin_lines.append('"""Health check methods — extracted from data_health_service.py (Strangler Fig #28).')
mixin_lines.append('')
mixin_lines.append('Each check reads one data source and returns a SourceCheckResult.')
mixin_lines.append('Methods decorated with @health_check auto-register on import.')
mixin_lines.append('The HealthCheckMethods mixin is inherited by DataHealthService.')
mixin_lines.append('"""')
mixin_lines.append('')
mixin_lines.append('from __future__ import annotations')
mixin_lines.append('')
mixin_lines.append('import json')
mixin_lines.append('import os')
mixin_lines.append('from collections import Counter')
mixin_lines.append('from datetime import UTC, datetime, timedelta')
mixin_lines.append('from pathlib import Path')
mixin_lines.append('from typing import Any')
mixin_lines.append('')
mixin_lines.append('from core.observability.data_health_schema import (')
mixin_lines.append('    CrossCheckResult,')
mixin_lines.append('    HealthReport,')
mixin_lines.append('    OrphanFinding,')
mixin_lines.append('    SourceCheckResult,')
mixin_lines.append('    SourceStatus,')
mixin_lines.append('    Tier,')
mixin_lines.append('    health_check,')
mixin_lines.append(')')
mixin_lines.append('')
mixin_lines.append('')
mixin_lines.append('class HealthCheckMethods:')
mixin_lines.append('    """Mixin providing all health check, cross-validation, and orphan')
mixin_lines.append('    detection methods for DataHealthService.')
mixin_lines.append('')
mixin_lines.append('    Methods access ``self._base_dir``, ``self._thresholds``,')
mixin_lines.append('    ``self._t()``, and ``self._symbol`` from the host class.')
mixin_lines.append('    """')
mixin_lines.append('')

# Copy methods
all_ranges = sorted(check_ranges + cross_check_methods, key=lambda x: x[0])
for start, end in all_ranges:
    for line in lines[start:end]:
        mixin_lines.append(line)
    mixin_lines.append('')

# Write mixin
DST.write_text('\n'.join(mixin_lines) + '\n', encoding='utf-8')
print(f"Written {DST} ({len(mixin_lines)} lines)")

# Remove methods from source
new_lines = list(lines)
for start, end in sorted(all_ranges, reverse=True):
    # Remove preceding blank lines
    while start > 0 and new_lines[start - 1].strip() == "":
        start -= 1
    del new_lines[start:end]

# Add mixin import
last_import = 0
for j, line in enumerate(new_lines):
    if line.startswith("from ") or line.startswith("import "):
        last_import = j
new_lines.insert(last_import + 1, "from core.observability.health_checks import HealthCheckMethods")

# Change class to inherit from mixin
for j, line in enumerate(new_lines):
    if line.strip().startswith("class DataHealthService:"):
        new_lines[j] = "class DataHealthService(HealthCheckMethods):"
        break

SRC.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
print(f"Updated {SRC} ({len(new_lines)} lines)")

# Verify
for method_name in ["check_execution_state", "check_journal_completeness",
                     "_check_brain_registry_governance_alignment", "_detect_orphan_subsystems"]:
    if method_name in DST.read_text(encoding='utf-8'):
        print(f"  [OK] {method_name} in mixin")
    else:
        print(f"  [MISSING] {method_name} not in mixin!")

print("\nDone! Run: python scripts/verify.py --quick")
