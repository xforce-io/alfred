#!/bin/bash
# Backward-compatible shim. Prefer bin/everbot.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/bin/everbot" "$@"
