#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TB_SRC="${ROOT_DIR}/hw_tmu/tmu_trace_tb.sv"
TRACE_CSV="${1:-${ROOT_DIR}/hw_tmu/traces/thermal_critical.csv}"
REPORT_OUT="${2:-${ROOT_DIR}/hw_tmu/outputs/tiering_event_report.csv}"
CYCLES_PER_SECOND="${3:-4096}"
L2_WINDOW_SECONDS="${4:-1}"

mkdir -p "${ROOT_DIR}/hw_tmu/outputs"

SIM_EXE="${ROOT_DIR}/hw_tmu/outputs/tmu_trace_tb.out"

iverilog -g2012 -o "${SIM_EXE}" "${TB_SRC}"
vvp "${SIM_EXE}" \
  +TRACE_CSV="${TRACE_CSV}" \
  +MSIX_REPORT="${REPORT_OUT}" \
  +TIER_REPORT="${REPORT_OUT}" \
  +CYCLES_PER_SECOND="${CYCLES_PER_SECOND}" \
  +L2_WINDOW_SECONDS="${L2_WINDOW_SECONDS}"

echo "Tiering event report: ${REPORT_OUT}"
