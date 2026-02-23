#!/usr/bin/env bash
# Unified test runner for EverBot
#
# Usage: tests/run_tests.sh <test_type> [options]
#   test_type: unit | integration | web | all

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PROJECT_ROOT}"

TYPE="${1:-all}"
shift || true

run_unit() {
    echo "Running Unit Tests..."
    pytest tests/unit/ "$@"
}

run_integration() {
    echo "Running Integration Tests..."
    pytest tests/integration/ "$@"
}

run_web() {
    echo "Running Web (E2E) Tests..."
    pytest tests/web/ "$@"
}

case "${TYPE}" in
    unit)
        run_unit "$@"
        ;;
    integration)
        run_integration "$@"
        ;;
    web)
        run_web "$@"
        ;;
    all)
        run_unit "$@"
        run_integration "$@"
        run_web "$@"
        ;;
    *)
        echo "Usage: $0 {unit|integration|web|all} [pytest options]"
        exit 1
        ;;
esac
