#!/usr/bin/env bash
# Unified test runner for EverBot

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PROJECT_ROOT}"

TYPE="${1:-all}"
shift || true

run_unit() {
    echo "Running Unit Tests..."
    pytest tests/unittest/ "$@"
}

run_integration() {
    echo "Running Integration Tests..."
    pytest tests/integration_test/ "$@"
}

run_e2e() {
    echo "Running E2E Tests..."
    pytest tests/e2e/ "$@"
}

case "${TYPE}" in
    unit)
        run_unit "$@"
        ;;
    integration)
        run_integration "$@"
        ;;
    e2e)
        run_e2e "$@"
        ;;
    all)
        run_unit "$@"
        run_integration "$@"
        run_e2e "$@"
        ;;
    *)
        echo "Usage: $0 {unit|integration|e2e|all} [pytest options]"
        exit 1
        ;;
esac
