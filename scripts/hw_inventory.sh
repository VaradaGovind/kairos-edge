#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/logs/hardware_inventory"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_MD="${OUT_DIR}/hardware_inventory_${TS}.md"
REPORT_RAW="${OUT_DIR}/hardware_inventory_${TS}.txt"
REPORT_JSON="${OUT_DIR}/hardware_inventory_${TS}.json"

mkdir -p "${OUT_DIR}"

run_section() {
    local title="$1"
    shift
    {
        echo "### ${title}"
        if "$@" 2>&1; then
            :
        else
            echo "[command failed] $*"
        fi
        echo
    } >> "${REPORT_RAW}"
}

bytes_to_gib() {
    awk -v kib="$1" 'BEGIN { printf "%.2f", kib/1024/1024 }'
}

CPU_MODEL="$(lscpu 2>/dev/null | awk -F: '/Model name/ {gsub(/^[ \t]+/, "", $2); print $2; exit}')"
CPU_SOCKET_COUNT="$(lscpu 2>/dev/null | awk -F: '/Socket\\(s\\)/ {gsub(/^[ \t]+/, "", $2); print $2; exit}')"
CPU_CORE_COUNT="$(lscpu 2>/dev/null | awk -F: '/^CPU\\(s\\)/ {gsub(/^[ \t]+/, "", $2); print $2; exit}')"

RAM_TOTAL_KIB="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
RAM_TOTAL_GIB="$(bytes_to_gib "${RAM_TOTAL_KIB}")"

GPU_QUERY_OUTPUT=""
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_QUERY_OUTPUT="$(nvidia-smi \
        --query-gpu=index,name,memory.total,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pci.bus_id \
        --format=csv,noheader,nounits 2>/dev/null || true)"
fi

if [[ -z "${GPU_QUERY_OUTPUT}" ]]; then
    GPU_QUERY_OUTPUT="No NVIDIA GPU query data detected."
fi

PCIE_GPU_SUMMARY=""
if command -v lspci >/dev/null 2>&1; then
    while IFS= read -r dev; do
        [[ -z "${dev}" ]] && continue
        BUS_ID="$(awk '{print $1}' <<< "${dev}")"
        DESC="$(cut -d' ' -f2- <<< "${dev}")"
        LNKS="$(lspci -s "${BUS_ID}" -vv 2>/dev/null | awk '/LnkCap:/ || /LnkSta:/ {print}' | tr '\n' '; ')"
        PCIE_GPU_SUMMARY+="- ${BUS_ID} ${DESC} | ${LNKS}"$'\n'
    done < <(lspci -D | grep -Ei 'VGA compatible controller|3D controller|Display controller' || true)
fi

if [[ -z "${PCIE_GPU_SUMMARY}" ]]; then
    PCIE_GPU_SUMMARY="No PCIe GPU endpoints found via lspci."
fi

TOPOLOGY_SUMMARY="No GPU interconnect topology detected."
if command -v nvidia-smi >/dev/null 2>&1; then
    TOPOLOGY_SUMMARY="$(nvidia-smi topo -m 2>/dev/null || true)"
fi
if [[ -z "${TOPOLOGY_SUMMARY}" ]]; then
    TOPOLOGY_SUMMARY="No GPU interconnect topology detected."
fi

PDN_PLACEHOLDERS=$(cat <<'EOF'
Board power cap (PoE/Battery limit): <= 30W (Expected)
DVFS step limit: unknown
Package droop guardrail: unknown
Thermal sensor granularity: unknown
EOF
)

UMA_LIMITS="UMA (Unified Memory Architecture) Limits: RAM total is ${RAM_TOTAL_GIB} GiB. Swap usage and CmaTotal can be observed via /proc/meminfo."

INA3221_TELEMETRY="No INA3221 sensors detected in /sys/class/hwmon/ (Expected on WSL, present on Jetson)"
if ls -d /sys/class/hwmon/hwmon* >/dev/null 2>&1; then
    INA3221_TELEMETRY="$(grep -i ina3221 /sys/class/hwmon/hwmon*/name 2>/dev/null || echo 'No INA3221 found')"
fi

run_section "uname -a" uname -a
run_section "lscpu" lscpu
run_section "nvidia-smi -L" nvidia-smi -L
run_section "nvidia-smi detailed query" nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pci.bus_id --format=csv
run_section "lspci (GPU + NVMe)" bash -lc "lspci -D | grep -Ei 'VGA|3D|Display|Non-Volatile memory controller' || true"
run_section "lsblk" lsblk -e7 -o NAME,MODEL,SIZE,ROTA,TYPE,MOUNTPOINT,FSTYPE
run_section "df -h" df -h
run_section "findmnt" findmnt -D
run_section "nvidia-smi topo -m" bash -lc "nvidia-smi topo -m 2>/dev/null || true"

{
    echo "# Kairos Hardware Inventory"
    echo
    echo "- Generated UTC: ${TS}"
    echo "- Hostname: $(hostname)"
    echo
    echo "## CPU and Memory"
    echo "- CPU model: ${CPU_MODEL:-unknown}"
    echo "- CPU sockets: ${CPU_SOCKET_COUNT:-unknown}"
    echo "- Logical CPUs: ${CPU_CORE_COUNT:-unknown}"
    echo "- Total system RAM (GiB): ${RAM_TOTAL_GIB}"
    echo
    echo "## GPU VRAM and PCIe Link Snapshot"
    echo '```text'
    echo "${GPU_QUERY_OUTPUT}"
    echo '```'
    echo
    echo "## Interconnect Topology"
    echo '```text'
    echo "${TOPOLOGY_SUMMARY}"
    echo '```'
    echo
    echo "## PCIe Generation/Lane Details (lspci)"
    echo "${PCIE_GPU_SUMMARY}"
    echo
    echo "## PDN Capability Placeholders"
    echo '```text'
    echo "${PDN_PLACEHOLDERS}"
    echo '```'
    echo
    echo "## Edge-Specific Memory and Telemetry"
    echo "- ${UMA_LIMITS}"
    echo "- ${INA3221_TELEMETRY}"
    echo
    echo "## Storage Paths"
    echo "### Mounted Filesystems"
    echo '```text'
    df -h | sed '1d'
    echo '```'
    echo
    echo "### Block Devices"
    echo '```text'
    lsblk -e7 -o NAME,MODEL,SIZE,ROTA,TYPE,MOUNTPOINT,FSTYPE
    echo '```'
    echo
    echo "## Raw Command Log"
    echo "Detailed probe outputs were written to:"
    echo "- \`${REPORT_RAW}\`"
} > "${REPORT_MD}"

python3 - "${REPORT_JSON}" "${TS}" "${CPU_MODEL:-unknown}" "${RAM_TOTAL_GIB}" "${GPU_QUERY_OUTPUT}" "${TOPOLOGY_SUMMARY}" "${PDN_PLACEHOLDERS}" <<'PY'
import json
import sys

path = sys.argv[1]
payload = {
    "generated_utc": sys.argv[2],
    "cpu_model": sys.argv[3],
    "ram_total_gib": sys.argv[4],
    "gpu_snapshot": sys.argv[5],
    "interconnect_topology": sys.argv[6],
    "pdn_placeholders": sys.argv[7],
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

echo "Hardware inventory report created:"
echo "  ${REPORT_MD}"
echo "Raw probe log created:"
echo "  ${REPORT_RAW}"
echo "JSON snapshot created:"
echo "  ${REPORT_JSON}"

