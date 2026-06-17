#!/usr/bin/env bash
# Build a local grafana/loki image from a static binary (no Docker Hub / alpine pull).

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
STACK_DIR="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
cd "${STACK_DIR}"
. "${SCRIPT_DIR}/load-dotenv.sh"

if [[ -f "${STACK_DIR}/.env" ]]; then
  load_dotenv "${STACK_DIR}/.env"
elif [[ -f "${STACK_DIR}/.env.example" ]]; then
  load_dotenv "${STACK_DIR}/.env.example"
fi

LOKI_VERSION="${LOKI_VERSION:-3.3.0}"
REGISTRY_PREFIX="${REGISTRY_PREFIX:-}"
LOKI_IMAGE="${LOKI_IMAGE:-${REGISTRY_PREFIX}grafana/loki:${LOKI_VERSION}}"
BUILD_DIR="${STACK_DIR}/loki/.build"
BINARY="${BUILD_DIR}/loki"
DOCKER_BIN="${DOCKER_BIN:-docker}"
LOKI_DOWNLOAD_INSECURE="${LOKI_DOWNLOAD_INSECURE:-0}"

usage() {
  cat <<EOF
Usage: $0

Build ${LOKI_IMAGE} from a static Loki binary (scratch-based Dockerfile).

Environment:
  LOKI_VERSION            Loki release tag (default: 3.3.0)
  REGISTRY_PREFIX         Optional image registry prefix
  LOKI_IMAGE              Output image tag (default: \${REGISTRY_PREFIX}grafana/loki:\${LOKI_VERSION})
  LOKI_DOWNLOAD_INSECURE  Set to 1 to skip TLS verification (curl -k / wget --no-check-certificate)
  DOCKER_BIN              docker binary (default: docker)

Tip: place a pre-downloaded binary at loki/.build/loki to skip download.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

download_file() {
  local url="$1"
  local out_file="$2"
  local curl_args=(-fL --retry 3 --retry-delay 2 -o "${out_file}" "${url}")
  if [[ "${LOKI_DOWNLOAD_INSECURE}" == "1" ]]; then
    curl_args=(-fkL --retry 3 --retry-delay 2 -o "${out_file}" "${url}")
  fi
  if command -v curl >/dev/null 2>&1; then
    curl "${curl_args[@]}"
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    local wget_args=("-O" "${out_file}")
    if [[ "${LOKI_DOWNLOAD_INSECURE}" == "1" ]]; then
      wget_args+=("--no-check-certificate")
    fi
    wget "${wget_args[@]}" "${url}"
    return
  fi
  echo "[build-loki] neither curl nor wget is available" >&2
  exit 1
}

install_binary() {
  if [[ -x "${BINARY}" ]]; then
    echo "[build-loki] using existing binary: ${BINARY}"
    return 0
  fi

  mkdir -p "${BUILD_DIR}"
  local ver="${LOKI_VERSION#v}"
  local archive="${BUILD_DIR}/loki-${ver}.zip"
  local url="https://github.com/grafana/loki/releases/download/v${ver}/loki-linux-amd64.zip"

  echo "[build-loki] downloading Loki ${LOKI_VERSION} from GitHub..."
  download_file "${url}" "${archive}"

  if ! command -v unzip >/dev/null 2>&1; then
    echo "[build-loki] error: unzip is required to extract ${archive}" >&2
    exit 1
  fi

  local extract_dir="${BUILD_DIR}/extract"
  rm -rf "${extract_dir}"
  mkdir -p "${extract_dir}"
  unzip -o -q "${archive}" -d "${extract_dir}"

  if [[ -f "${extract_dir}/loki-linux-amd64" ]]; then
    cp "${extract_dir}/loki-linux-amd64" "${BINARY}"
  elif [[ -f "${extract_dir}/loki" ]]; then
    cp "${extract_dir}/loki" "${BINARY}"
  else
    echo "[build-loki] unexpected archive layout in ${archive}" >&2
    exit 1
  fi
  chmod +x "${BINARY}"
  echo "[build-loki] binary ready: ${BINARY}"
}

if ! "${DOCKER_BIN}" info >/dev/null 2>&1; then
  echo "[build-loki] error: docker daemon is not available" >&2
  exit 1
fi

install_binary

echo "[build-loki] building image ${LOKI_IMAGE}..."
"${DOCKER_BIN}" build \
  -f "${STACK_DIR}/loki/Dockerfile" \
  -t "${LOKI_IMAGE}" \
  "${BUILD_DIR}"

if [[ -n "${REGISTRY_PREFIX}" ]] \
  && [[ "${LOKI_IMAGE}" != "grafana/loki:${LOKI_VERSION}" ]] \
  && ! "${DOCKER_BIN}" image inspect "grafana/loki:${LOKI_VERSION}" >/dev/null 2>&1; then
  "${DOCKER_BIN}" tag "${LOKI_IMAGE}" "grafana/loki:${LOKI_VERSION}"
fi

echo "[build-loki] done: ${LOKI_IMAGE}"
