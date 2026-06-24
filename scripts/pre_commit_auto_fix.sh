#!/bin/bash
# Run BEFORE git commit to pre-format and pre-fix all changes.
# This prevents pre-commit stash conflicts by ensuring hooks
# only validate, not modify.
#
# Usage:
#   bash scripts/pre_commit_auto_fix.sh
#   git add -u
#   git commit ...
set -e

echo "=== Pre-commit auto-fix ==="
ruff check --fix .
ruff format .
echo "=== Auto-fix complete. You can now git add and git commit. ==="
