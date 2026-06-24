#!/bin/bash
# One-time dev environment setup after git clone.
# Must be run before first commit/push.
# Installs pre-commit hooks for all gate stages and updates
# the blueprint baseline to current state.
set -e

echo "=== Installing pre-commit hooks ==="
pre-commit install                     # install .git/hooks/pre-commit
pre-commit install --hook-type pre-push   # install .git/hooks/pre-push
pre-commit install --hook-type commit-msg # install .git/hooks/commit-msg

echo ""
echo "=== Updating blueprint baseline ==="
python scripts/pre_commit_blueprint.py --update-baseline

echo ""
echo "=== Dev environment ready ==="
echo "  pre-commit:  .git/hooks/pre-commit  (ruff, mypy, blueprints, ...)"
echo "  commit-msg:  .git/hooks/commit-msg  (omega routing, architecture gate)"
echo "  pre-push:    .git/hooks/pre-push     (ruff, mypy, pytest, omega)"
