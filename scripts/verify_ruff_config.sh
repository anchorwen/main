#!/bin/bash
# Verify ruff produces identical output before/after --ignore SIM105 migration.
# Run this BEFORE committing any ruff configuration change.
set -e
echo "=== Before (with --ignore SIM105) ==="
ruff check core/ apps/ scripts/ --ignore SIM105 --output-format concise 2>&1 | grep -c "error" > /tmp/ruff_before_count.txt || true
echo "=== After (pyproject.toml only, no CLI flag) ==="
ruff check core/ apps/ scripts/ --output-format concise 2>&1 | grep -c "error" > /tmp/ruff_after_count.txt || true
BEFORE=$(cat /tmp/ruff_before_count.txt 2>/dev/null || echo 0)
AFTER=$(cat /tmp/ruff_after_count.txt 2>/dev/null || echo 0)
echo "Before: $BEFORE errors, After: $AFTER errors"
if [ "$BEFORE" -eq "$AFTER" ]; then
    echo "PASS: identical error count"
else
    echo "FAIL: error count differs (BEFORE=$BEFORE, AFTER=$AFTER)"
    exit 1
fi
