#!/usr/bin/env bash
# Unified test runner for EverBot
#
# Usage: tests/run_tests.sh <test_type> [options]
#   test_type: unit | integration | e2e | all (all = unit + integration)

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PROJECT_ROOT}"

TYPE="${1:-all}"
shift || true

run_lint() {
    echo "Running Lint..."
    ruff check "${PROJECT_ROOT}"
}

run_unit() {
    echo "Running Unit Tests..."
    pytest tests/unit/ "$@"
}

run_integration() {
    echo "Running Integration Tests..."
    pytest tests/integration/ "$@"
}

run_e2e() {
    echo "Running E2E Tests..."
    pytest tests/e2e/ "$@"
}

case "${TYPE}" in
    unit)
        run_lint
        run_unit "$@"
        ;;
    integration)
        run_lint
        run_integration "$@"
        ;;
    e2e)
        run_lint
        run_e2e "$@"
        ;;
    all)
        run_lint
        run_unit "$@"
        run_integration "$@"
        ;;
    *)
        echo "Usage: $0 {unit|integration|e2e|all} [pytest options]"
        exit 1
        ;;
esac
