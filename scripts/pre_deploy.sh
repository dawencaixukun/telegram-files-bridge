#!/usr/bin/env bash
# ==============================================================================
# scripts/pre_deploy.sh — Pre-deploy 门禁自动化流水线一键触发包装脚本
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${WORKSPACE_ROOT}"

echo "[INFO] 触发 Pre-deploy 质量与安全门禁流水线..."
python scripts/pre_deploy.py "$@"
