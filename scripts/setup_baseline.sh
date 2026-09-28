#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH=""
KEEP_RUNNING=0

for arg in "$@"; do
    case "${arg}" in
        --keep-running)
            KEEP_RUNNING=1
            ;;
        *)
            if [[ -z "${CONFIG_PATH}" ]]; then
                CONFIG_PATH="${arg}"
            else
                echo "Unexpected extra argument: ${arg}" >&2
                exit 1
            fi
            ;;
    esac
done

if [[ -z "${CONFIG_PATH}" ]]; then
    CONFIG_PATH="${ROOT_DIR}/configs/baseline_full_cache.yaml"
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "Config not found: ${CONFIG_PATH}" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required but was not found." >&2
    exit 1
fi

LOG_DIR="${ROOT_DIR}/logs/baseline_setup"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
SERVER_LOG="${LOG_DIR}/baseline_server_${TS}.log"
mkdir -p "${LOG_DIR}"

readarray -t CFG < <(python3 - "${CONFIG_PATH}" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], "r", encoding="utf-8"))
profile = cfg.get("baseline_profile", {}).get("server_args", {})
print(cfg["model"]["id"])
print(cfg["server"]["host"])
print(cfg["server"]["port"])
print(profile.get("ctx_size", 4096))
print(profile.get("batch_size", 2048))
PY
)

MODEL_ID="${CFG[0]}"
HOST="${CFG[1]}"
PORT="${CFG[2]}"
CTX_SIZE="${CFG[3]}"
BATCH_SIZE="${CFG[4]}"

BASE_URL="http://${HOST}:${PORT}"
ACTIVE_BACKEND="llama.cpp"

cleanup() {
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
        kill "${SERVER_PID}" >/dev/null 2>&1 || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

wait_for_readiness() {
    local url="$1"
    local attempts="${2:-60}"
    local sleep_s="${3:-2}"
    local i
    for i in $(seq 1 "${attempts}"); do
        if curl -fsS "${url}" >/dev/null 2>&1; then
            return 0
        fi
        sleep "${sleep_s}"
    done
    return 1
}

LLAMA_DIR="${ROOT_DIR}/.llama_cpp"
LLAMA_SERVER="${LLAMA_DIR}/build/bin/llama-server"

if [[ ! -x "${LLAMA_SERVER}" ]]; then
    echo "Downloading pre-compiled llama.cpp server for Linux x86_64..."
    mkdir -p "${LLAMA_DIR}"
    # Download latest release for ubuntu x86_64
    wget -qO "${LLAMA_DIR}/llama-b3280-bin-ubuntu-x64.zip" "https://github.com/ggerganov/llama.cpp/releases/download/b3280/llama-b3280-bin-ubuntu-x64.zip" || {
        echo "Failed to download llama.cpp release." >&2
        exit 1
    }
    python3 -m zipfile -e "${LLAMA_DIR}/llama-b3280-bin-ubuntu-x64.zip" "${LLAMA_DIR}"
    chmod +x "${LLAMA_SERVER}"
fi

echo "Installing minimal python dependencies..."
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv-baseline}"
python3 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"
python3 -m pip install --upgrade pip wheel
python3 -m pip install "requests>=2.31.0" "PyYAML>=6.0.1" "psutil>=5.9.8"

# Assuming MODEL_ID is a local path or we have a script to fetch GGUF.
# For simplicity, if it's not a local file, we will fallback to a dummy/download it.
MODEL_FILE="${MODEL_ID}"
if [[ ! -f "${MODEL_FILE}" ]]; then
    echo "Warning: Model file ${MODEL_FILE} not found locally. We will attempt to download a tiny LLaMA for testing if possible."
    # Let's download a small GGUF if MODEL_ID doesn't exist
    TINY_MODEL="${ROOT_DIR}/configs/tinyllama.gguf"
    if [[ ! -f "${TINY_MODEL}" ]]; then
        echo "Downloading TinyLlama GGUF for testing..."
        wget -qO "${TINY_MODEL}" "https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    fi
    MODEL_FILE="${TINY_MODEL}"
fi

LLAMA_CMD=(
    "${LLAMA_SERVER}"
    -m "${MODEL_FILE}"
    --host "${HOST}"
    --port "${PORT}"
    -c "${CTX_SIZE}"
    -b "${BATCH_SIZE}"
    --threads 4
)

echo "Launching baseline server (${ACTIVE_BACKEND})..."
printf 'Command: %q ' "${LLAMA_CMD[@]}"
echo
"${LLAMA_CMD[@]}" >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!

echo "Waiting for llama.cpp API readiness at ${BASE_URL}..."
if ! wait_for_readiness "${BASE_URL}/v1/models" 120 2; then
    echo "llama.cpp did not become ready in time. Check ${SERVER_LOG}" >&2
    cat "${SERVER_LOG}"
    exit 1
fi

echo "Running generation smoke test..."
SMOKE_PAYLOAD="$(cat <<JSON
{
  "model": "default",
  "prompt": "Explain in one sentence why deterministic seeds matter for LLM benchmarking.",
  "max_tokens": 32,
  "temperature": 0.0,
  "top_p": 1.0
}
JSON
)"

SMOKE_RESPONSE="$(curl -fsS "${BASE_URL}/v1/completions" -H "Content-Type: application/json" -d "${SMOKE_PAYLOAD}")"
GEN_TEXT="$(python3 - "${SMOKE_RESPONSE}" <<'PY'
import json
import sys
body = json.loads(sys.argv[1])
choices = body.get("choices", [])
text = ""
if choices:
    text = choices[0].get("text", "").strip()
print(text)
PY
)"

echo "Baseline stack is operational."
echo "Backend: ${ACTIVE_BACKEND}"
echo "Sample output: ${GEN_TEXT}"
echo "Server log: ${SERVER_LOG}"

if [[ "${KEEP_RUNNING}" -eq 1 ]]; then
    trap - EXIT
    disown "${SERVER_PID}" 2>/dev/null || true
    echo "Server kept running (PID ${SERVER_PID}) due to --keep-running."
fi
