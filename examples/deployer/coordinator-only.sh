#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
#
# Start an already-installed Coordinator. Render and kv-conductor are started
# only when CoordinatorConfig says they are enabled. This script does not
# install Motor and does not register Prefill/Decode instances.

set -Eeuo pipefail

USER_CONFIG_PATH=${USER_CONFIG_PATH:-}
MOTOR_LOG_PATH=${MOTOR_LOG_PATH:-/tmp/motor-coord}
MOTOR_START_LOG=${MOTOR_START_LOG:-$MOTOR_LOG_PATH/coord-only.log}
MOTOR_KVC_LOG=${MOTOR_KVC_LOG:-$MOTOR_LOG_PATH/kv-conductor.log}
MOTOR_COORD_LOG=${MOTOR_COORD_LOG:-$MOTOR_LOG_PATH/coordinator.main.log}
MOTOR_RENDER_LOG=${MOTOR_RENDER_LOG:-$MOTOR_LOG_PATH/render.log}
KV_CONDUCTOR_HOST=${KV_CONDUCTOR_HOST:-}
KV_CONDUCTOR_PORT=${KV_CONDUCTOR_PORT:-13333}
COORDINATOR_HOST=${COORDINATOR_HOST:-127.0.0.1}
COORDINATOR_MGMT_PORT=${COORDINATOR_MGMT_PORT:-1026}
RENDER_PORT=${RENDER_PORT:-8100}

usage() {
  cat <<'EOF'
Usage:
  coordinator-only.sh [options]

Starts Coordinator. Motor must already be installed.
Does not install packages, probe health, or register Prefill/Decode instances.

Options:
  --config FILE            coordinator.json / user_config.json. Exported as USER_CONFIG_PATH.
                           Host, ports, Render, and kv-conductor follow that file:
                           api_config.coordinator_api_host,
                           api_config.coordinator_api_mgmt_port,
                           kv_conductor_config.http_server_port,
                           render_config.enabled or render_config.enable,
                           and a local kv_conductor_config.conductor_service.
  -h, --help               Show this help.
EOF
}

die() {
  printf '[coord-only] ERROR: %s\n' "$*" | tee -a "$MOTOR_START_LOG" >&2
  exit 1
}

log() {
  printf '[coord-only] %s\n' "$*" | tee -a "$MOTOR_START_LOG"
}

while (($#)); do
  case "$1" in
    --config) USER_CONFIG_PATH=${2:?missing value for --config}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; printf '[coord-only] ERROR: unknown argument: %s\n' "$1" >&2; exit 1 ;;
  esac
done

if [[ -L "$MOTOR_LOG_PATH" ]]; then
  printf '[coord-only] ERROR: log directory is a symlink: %s\n' "$MOTOR_LOG_PATH" >&2
  exit 1
fi
if [[ -e "$MOTOR_LOG_PATH" && ! -O "$MOTOR_LOG_PATH" ]]; then
  printf '[coord-only] ERROR: log directory is not owned by the current user: %s\n' "$MOTOR_LOG_PATH" >&2
  exit 1
fi
install -d -m 700 "$MOTOR_LOG_PATH"
if [[ -L "$MOTOR_LOG_PATH" || ! -O "$MOTOR_LOG_PATH" ]]; then
  printf '[coord-only] ERROR: log directory is not owned by the current user: %s\n' "$MOTOR_LOG_PATH" >&2
  exit 1
fi
for _log_file in "$MOTOR_START_LOG" "$MOTOR_KVC_LOG" "$MOTOR_COORD_LOG" "$MOTOR_RENDER_LOG"; do
  if [[ -L "$_log_file" ]]; then
    printf '[coord-only] ERROR: log path is a symlink: %s\n' "$_log_file" >&2
    exit 1
  fi
done
unset _log_file
touch "$MOTOR_START_LOG" "$MOTOR_KVC_LOG" "$MOTOR_COORD_LOG" "$MOTOR_RENDER_LOG"

if [[ -n "$USER_CONFIG_PATH" && ! -f "$USER_CONFIG_PATH" ]]; then
  die "config not found: $USER_CONFIG_PATH"
fi

export ROLE=coordinator MOTOR_LOG_PATH
if [[ -n "$USER_CONFIG_PATH" ]]; then
  export USER_CONFIG_PATH
fi

if [[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ]]; then
  # shellcheck disable=SC1091
  source /usr/local/Ascend/ascend-toolkit/set_env.sh || true
elif [[ -f /usr/local/Ascend/set_env.sh ]]; then
  # shellcheck disable=SC1091
  source /usr/local/Ascend/set_env.sh || true
fi

python3 -c 'import motor' >/dev/null 2>&1 || die "motor is not installed; install it before running this script"

CONFIG_VALUES=$(python3 - "${USER_CONFIG_PATH:-}" <<'PY'
import sys

_stdout = sys.stdout
sys.stdout = sys.stderr

from motor.config.coordinator import CoordinatorConfig

path = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
config = CoordinatorConfig.from_json(path)
render = config.render_config
render_on = getattr(render, "enable", False) is True or getattr(render, "enabled", False) is True
kv = config.scheduler_config.kv_conductor_config
service = (getattr(kv, "conductor_service", "") or "").strip()
kv_port = getattr(kv, "http_server_port", "")
if not service:
    legacy = getattr(config, "prefill_kv_event_config", None)
    service = (getattr(legacy, "conductor_service", "") or "").strip()
    if service:
        kv_port = getattr(legacy, "http_server_port", kv_port)
def service_host(value):
    text = (value or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    if text.count(":") == 1:
        host, maybe_port = text.split(":", 1)
        if maybe_port.isdigit():
            return host
    return text


def text_or_empty(value):
    return "" if value is None else str(value)


local = service_host(service).lower() in {"127.0.0.1", "localhost", "::1"}
endpoint = getattr(render, "endpoint", None)
render_port = getattr(endpoint, "port", None) if endpoint is not None else None
if isinstance(render_port, bool) or not isinstance(render_port, int):
    render_port_text = "" if render_port is None else str(render_port)
else:
    render_port_text = str(render_port)
render_host = str(getattr(endpoint, "host", "") or "").strip() if endpoint is not None else ""
record = "\x1f".join([
    text_or_empty(config.api_config.coordinator_api_mgmt_port),
    text_or_empty(kv_port),
    "true" if render_on else "false",
    render_port_text,
    "true" if service and local else "false",
    service,
    str(config.api_config.coordinator_api_host or "").strip(),
    render_host,
    service_host(service) if service else "",
])
sys.stdout = _stdout
print(record, file=_stdout)
PY
) || die "failed to load CoordinatorConfig from ${USER_CONFIG_PATH:-defaults}"
IFS=$'\x1f' read -r CONFIG_MGMT_PORT CONFIG_KV_PORT CONFIG_RENDER_ENABLE CONFIG_RENDER_PORT START_KV CONDUCTOR_SERVICE CONFIG_API_HOST CONFIG_RENDER_HOST CONFIG_KV_BIND <<<"$CONFIG_VALUES"
RENDER_BIND_HOST=127.0.0.1
if [[ -n "$CONFIG_RENDER_HOST" ]]; then
  RENDER_BIND_HOST=${CONFIG_RENDER_HOST#[}
  RENDER_BIND_HOST=${RENDER_BIND_HOST%]}
  if [[ "${RENDER_BIND_HOST,,}" == "localhost" ]]; then
    RENDER_BIND_HOST=127.0.0.1
  fi
fi
if [[ -n "$CONFIG_API_HOST" ]]; then
  COORDINATOR_HOST=$CONFIG_API_HOST
else
  COORDINATOR_HOST=127.0.0.1
fi
if [[ -n "$CONFIG_MGMT_PORT" ]]; then
  COORDINATOR_MGMT_PORT=$CONFIG_MGMT_PORT
else
  COORDINATOR_MGMT_PORT=1026
fi
if [[ -n "$CONFIG_KV_PORT" ]]; then
  KV_CONDUCTOR_PORT=$CONFIG_KV_PORT
fi
if [[ "$KV_CONDUCTOR_PORT" == *":"* ]]; then
  KV_CONDUCTOR_PORT="${KV_CONDUCTOR_PORT##*:}"
fi
if [[ "$START_KV" == "true" && "$KV_CONDUCTOR_PORT" == "0" ]]; then
  START_KV=false
fi
if [[ "$START_KV" == "true" ]]; then
  if [[ ! "$KV_CONDUCTOR_PORT" =~ ^[0-9]+$ ]] || (( 10#$KV_CONDUCTOR_PORT < 1 || 10#$KV_CONDUCTOR_PORT > 65535 )); then
    die "kv-conductor port is not a TCP port: $KV_CONDUCTOR_PORT"
  fi
fi
if [[ "$CONFIG_RENDER_ENABLE" == "true" ]]; then
  case "${RENDER_BIND_HOST,,}" in
    127.0.0.1 | ::1) ;;
    *) die "render_config.endpoint.host must be localhost, 127.0.0.1, or ::1: ${CONFIG_RENDER_HOST:-<empty>}" ;;
  esac
  if [[ -n "$CONFIG_RENDER_PORT" ]]; then
    RENDER_PORT=$CONFIG_RENDER_PORT
  else
    RENDER_PORT=8100
  fi
  if [[ ! "$RENDER_PORT" =~ ^[0-9]+$ ]] || (( 10#$RENDER_PORT < 1 || 10#$RENDER_PORT > 65535 )); then
    die "render_config.endpoint.port is not a TCP port: ${RENDER_PORT:-<empty>}"
  fi
fi

coordinator_probe_host() {
  local host=${1#[}
  host=${host%]}
  case "$host" in
    0.0.0.0) host=127.0.0.1 ;;
    ::) host=::1 ;;
  esac
  printf '%s' "$host"
}

listening() {
  python3 - "$1" "$2" <<'PY'
import socket
import sys

try:
    sock = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.5)
except (OSError, ValueError):
    raise SystemExit(1)
else:
    sock.close()
PY
}

write_render_launcher() {
  local launcher=$1
  cat >"$launcher" <<'PY'
#!/usr/bin/env python3
"""Start vLLM Render in this process, using the same argv as the Motor deployer."""
import json
import os
import sys

CPU_SITECUSTOMIZE = """\
import importlib.util

_find_spec = importlib.util.find_spec
importlib.util.find_spec = lambda name, *args, **kwargs: (
    None if name in {"triton", "pytorch-triton-xpu"} else _find_spec(name, *args, **kwargs)
)
import vllm.triton_utils
importlib.util.find_spec = _find_spec

from vllm import platforms
from vllm.platforms.cpu import CpuPlatform

platforms.current_platform = CpuPlatform()
"""
LAUNCHER = "from vllm.entrypoints.cli.main import main; main()"
MANAGED = {
    "disable_access_log_for_endpoints",
    "host",
    "model",
    "model_tag",
    "port",
    "served_model_name",
}


def fail(msg):
    print("[render-launcher] ERROR: " + msg, file=sys.stderr)
    raise SystemExit(2)


def coordinator_section(config):
    if not isinstance(config, dict):
        return {}
    wrapped = config.get("motor_coordinator_config")
    if isinstance(wrapped, dict):
        return wrapped
    return config


def resolve_model(config):
    roots = []
    if isinstance(config, dict):
        roots.append(config)
        wrapped = config.get("motor_coordinator_config")
        if isinstance(wrapped, dict):
            roots.append(wrapped)
    for root in roots:
        if "motor_engine_union_config" in root:
            section_name = "motor_engine_union_config"
        elif "motor_engine_prefill_config" in root:
            section_name = "motor_engine_prefill_config"
        else:
            continue
        section = root.get(section_name, {})
        if not isinstance(section, dict):
            section = {}
        engine = section.get("engine_config", {})
        legacy = section.get("model_config", {})
        if not isinstance(engine, dict):
            engine = {}
        if not isinstance(legacy, dict):
            legacy = {}
        model = engine.get("model") or legacy.get("model_path")
        if "served_model_name" in engine:
            served = engine.get("served_model_name") or model
        else:
            served = legacy.get("model_name") or model
        if not isinstance(model, str) or not model:
            fail(f"{section_name}.engine_config.model is required when Render is enabled")
        if isinstance(served, str) and served:
            served = [served]
        if not (isinstance(served, list) and served and all(isinstance(item, str) and item for item in served)):
            fail("served_model_name must be a non-empty string or list of strings")
        return model, served
    fail("motor_engine_prefill_config.engine_config.model is required when Render is enabled")


def serialize(launch_args):
    args = []
    seen = set()
    for name, value in launch_args.items():
        normalized = str(name).strip().lstrip("-").replace("-", "_").lower()
        if not normalized or normalized in MANAGED:
            fail(f"launch_args.{name} is managed by Motor")
        if normalized in seen:
            fail(f"launch_args contains duplicate argument: {name}")
        seen.add(normalized)
        if value is None or value is False:
            continue
        args.append("--" + normalized.replace("_", "-"))
        if value is True:
            continue
        if isinstance(value, dict):
            args.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    args.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                elif isinstance(item, (str, int, float)) and not isinstance(item, bool):
                    args.append(str(item))
                else:
                    fail(f"launch_args.{name} contains an unsupported value")
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            args.append(str(value))
        else:
            fail(f"launch_args.{name} has an unsupported value")
    return args


def main():
    if len(sys.argv) != 2:
        fail("usage: render-launcher.py USER_CONFIG")
    with open(sys.argv[1], encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        fail("config must be a JSON object")
    render = coordinator_section(config).get("render_config") or {}
    if not isinstance(render, dict):
        render = {}
    if render.get("enabled") is not True and render.get("enable") is not True:
        fail("render_config.enabled/enable is not true")
    endpoint = render.get("endpoint") or {}
    if not isinstance(endpoint, dict):
        fail("render_config.endpoint must be an object")
    port = endpoint.get("port", 8100)
    if port is None or port == "":
        port = 8100
    if isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65535:
        fail("render_config.endpoint.port is not a TCP port: " + str(port))
    host = endpoint.get("host", "127.0.0.1")
    if host is None:
        host = "127.0.0.1"
    if not isinstance(host, str):
        fail("render_config.endpoint.host must be localhost, 127.0.0.1, or ::1")
    bind_host = host.strip().strip("[]")
    if not bind_host or bind_host.lower() == "localhost":
        bind_host = "127.0.0.1"
    if bind_host.lower() not in {"127.0.0.1", "::1"}:
        fail("render_config.endpoint.host must be localhost, 127.0.0.1, or ::1")
    probed_host = os.environ.get("MOTOR_RENDER_BIND_HOST", "").strip()
    probed_port = os.environ.get("MOTOR_RENDER_BIND_PORT", "").strip()
    if probed_host and probed_host != bind_host:
        fail(f"probed render host {probed_host} does not match endpoint.host {bind_host}")
    if probed_port:
        if not probed_port.isdigit() or int(probed_port) != port:
            fail(f"probed render port {probed_port} does not match endpoint.port {port}")
        port = int(probed_port)
    if probed_host:
        bind_host = probed_host
    configured = render.get("launch_args", {})
    if not isinstance(configured, dict):
        fail("render_config.launch_args must be an object")
    launch_args = {"renderer_num_workers": 4, **configured}
    workers = launch_args.get("renderer_num_workers")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers <= 0:
        fail("render_config.launch_args.renderer_num_workers must be a positive integer")
    model, served = resolve_model(config)
    argv = [
        "launch",
        "render",
        model,
        "--served-model-name",
        *served,
        "--host",
        bind_host,
        "--port",
        str(port),
        "--disable-access-log-for-endpoints",
        "/health,/v1/chat/completions/derender,/v1/completions/derender",
        *serialize(launch_args),
    ]
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "VLLM_LOGGING_LEVEL": "INFO",
            "VLLM_MLA_DISABLE": "1",
            "OMP_NUM_THREADS": "4",
            "VLLM_PLUGINS": "",
            "TORCH_DEVICE_BACKEND_AUTOLOAD": "0",
        }
    )
    cache_root = os.environ.get("VLLM_CACHE_ROOT") or f"/tmp/vllm-render-cache-{os.getuid()}"
    os.environ["VLLM_CACHE_ROOT"] = cache_root
    os.environ["TRITON_CACHE_DIR"] = os.path.join(cache_root, "triton")
    try:
        os.makedirs(cache_root, mode=0o700, exist_ok=True)
        if os.path.islink(cache_root) or os.lstat(cache_root).st_uid != os.getuid():
            fail("render cache is not owned by the current user: " + cache_root)
        os.chmod(cache_root, 0o700)
        bootstrap = os.path.join(cache_root, "bootstrap")
        if os.path.islink(bootstrap):
            fail("render bootstrap path is a symlink: " + bootstrap)
        os.makedirs(bootstrap, mode=0o700, exist_ok=True)
        if os.path.islink(bootstrap) or os.lstat(bootstrap).st_uid != os.getuid():
            fail("render bootstrap path is a symlink: " + bootstrap)
        os.chmod(bootstrap, 0o700)
        site_path = os.path.join(bootstrap, "sitecustomize.py")
        if os.path.islink(site_path):
            fail("sitecustomize path is a symlink: " + site_path)
        site_fd = os.open(site_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(site_fd, "w", encoding="utf-8") as out:
            out.write(CPU_SITECUSTOMIZE)
    except OSError as exc:
        fail("cannot prepare render cache: " + str(exc))
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = bootstrap + (":" + existing if existing else "")
    driver = "/usr/local/Ascend/driver/lib64"
    if os.path.isdir(driver):
        parts = [f"{driver}/driver", f"{driver}/common", os.environ.get("LD_LIBRARY_PATH", "")]
        os.environ["LD_LIBRARY_PATH"] = ":".join(part for part in parts if part)
    os.execvp("python3", ["python3", "-c", LAUNCHER, *argv])


if __name__ == "__main__":
    main()
PY
  chmod +x "$launcher"
}

confirm_supervisor() {
  local name=$1 pid=$2 logfile=$3
  local i rc=0
  for ((i = 0; i < 10; i++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || rc=$?
      log "$name stopped with status $rc; log=$logfile"
      exit "$rc"
    fi
    sleep 0.3
  done
  log "$name supervisor pid=$pid"
}

STARTED_PIDS=()

record_started() {
  STARTED_PIDS+=("$1")
}

stop_started() {
  local rc=$? pid
  if [[ "$rc" -eq 0 ]]; then
    return 0
  fi
  for pid in ${STARTED_PIDS[@]+"${STARTED_PIDS[@]}"}; do
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
  done
}
trap stop_started EXIT

start_render() {
  [[ -n "$USER_CONFIG_PATH" ]] || die "Render is enabled in CoordinatorConfig but --config was not given"
  [[ "$CONFIG_RENDER_ENABLE" == "true" ]] || die "render config is not enabled"
  if listening "$RENDER_BIND_HOST" "$RENDER_PORT"; then
    log "Render already listens on ${RENDER_BIND_HOST}:$RENDER_PORT"
    return
  fi
  if [[ -z "${VLLM_CACHE_ROOT:-}" ]]; then
    VLLM_CACHE_ROOT=/tmp/vllm-render-cache-$(id -u)
  fi
  export VLLM_CACHE_ROOT
  if [[ -L "$VLLM_CACHE_ROOT" ]]; then
    die "render cache path is a symlink: $VLLM_CACHE_ROOT"
  fi
  if [[ -e "$VLLM_CACHE_ROOT" && ! -O "$VLLM_CACHE_ROOT" ]]; then
    die "render cache is not owned by the current user: $VLLM_CACHE_ROOT"
  fi
  if [[ -e "$VLLM_CACHE_ROOT" && ! -w "$VLLM_CACHE_ROOT" ]]; then
    die "render cache is not writable: $VLLM_CACHE_ROOT"
  fi
  install -d -m 700 "$VLLM_CACHE_ROOT" || die "cannot create $VLLM_CACHE_ROOT"
  if [[ -L "$VLLM_CACHE_ROOT" || ! -O "$VLLM_CACHE_ROOT" ]]; then
    die "render cache is not owned by the current user: $VLLM_CACHE_ROOT"
  fi
  local launcher="$VLLM_CACHE_ROOT/render-launcher.py"
  if [[ -L "$launcher" ]]; then
    die "render launcher path is a symlink: $launcher"
  fi
  write_render_launcher "$launcher"
  export MOTOR_RENDER_BIND_HOST="$RENDER_BIND_HOST" MOTOR_RENDER_BIND_PORT="$RENDER_PORT"
  log "starting Render; log=$MOTOR_RENDER_LOG"
  nohup bash -c 'while true; do python3 "$0" "$1"; rc=$?; if [[ "$rc" -eq 2 ]]; then echo "[supervise] render failed with a configuration error (rc=2); giving up" >&2; exit 2; fi; echo "[supervise] render exited rc=$rc; restarting in 3s"; sleep 3; done' \
    "$launcher" "$USER_CONFIG_PATH" >>"$MOTOR_RENDER_LOG" 2>&1 &
  record_started "$!"
  confirm_supervisor Render "$!" "$MOTOR_RENDER_LOG"
}

start_kv_conductor() {
  if [[ -z "${KV_CONDUCTOR_SERVICE:-}" && -n "$CONDUCTOR_SERVICE" ]]; then
    export KV_CONDUCTOR_SERVICE=$CONDUCTOR_SERVICE
  fi
  local kv_probe_host
  kv_probe_host=$(coordinator_probe_host "$KV_CONDUCTOR_HOST")
  if listening "$kv_probe_host" "$KV_CONDUCTOR_PORT"; then
    log "kv-conductor already listens on ${kv_probe_host}:${KV_CONDUCTOR_PORT}"
    return
  fi
  log "starting kv-conductor; log=$MOTOR_KVC_LOG"
  nohup env KV_HOST="$KV_CONDUCTOR_HOST" KV_PORT="$KV_CONDUCTOR_PORT" \
    bash -c 'while true; do python3 -m motor.kv_conductor --host "$KV_HOST" --port "$KV_PORT"; rc=$?; if [[ "$rc" -eq 2 ]]; then echo "[supervise] kv-conductor failed with a configuration error (rc=2); giving up" >&2; exit 2; fi; echo "[supervise] kv-conductor exited rc=$rc; restarting in 3s"; sleep 3; done' \
    >>"$MOTOR_KVC_LOG" 2>&1 &
  record_started "$!"
  confirm_supervisor kv-conductor "$!" "$MOTOR_KVC_LOG"
}

if [[ "$START_KV" == "true" ]]; then
  if [[ -z "$KV_CONDUCTOR_HOST" || "$KV_CONDUCTOR_HOST" == "0.0.0.0" || "$KV_CONDUCTOR_HOST" == "::" ]]; then
    KV_CONDUCTOR_HOST=$CONFIG_KV_BIND
  fi
  KV_CONDUCTOR_HOST=${KV_CONDUCTOR_HOST#[}
  KV_CONDUCTOR_HOST=${KV_CONDUCTOR_HOST%]}
  if [[ "${KV_CONDUCTOR_HOST,,}" == "localhost" ]]; then
    KV_CONDUCTOR_HOST=127.0.0.1
  fi
  case "${KV_CONDUCTOR_HOST,,}" in
    127.0.0.1 | ::1) ;;
    *) die "kv-conductor bind host must be loopback: ${KV_CONDUCTOR_HOST:-<empty>}" ;;
  esac
  python3 - <<'PY' || die "kv-conductor binary is missing; build motor/kv_conductor first"
from motor.kv_conductor import get_binary_path

if get_binary_path() is None:
    raise SystemExit(1)
PY
fi

if [[ "$CONFIG_RENDER_ENABLE" == "true" ]]; then
  start_render
else
  log "render_config is disabled; not starting Render"
fi

if [[ "$START_KV" == "true" ]]; then
  start_kv_conductor
elif [[ "$KV_CONDUCTOR_PORT" == "0" ]]; then
  log "http_server_port is 0; kv conductor is disabled"
elif [[ -n "$CONDUCTOR_SERVICE" ]]; then
  log "conductor_service=$CONDUCTOR_SERVICE is not local; not starting kv-conductor here"
else
  log "conductor_service is empty; kv conductor is disabled"
fi

probe_host=$(coordinator_probe_host "$COORDINATOR_HOST")
if listening "$probe_host" "$COORDINATOR_MGMT_PORT"; then
  log "Coordinator already listens on ${COORDINATOR_HOST}:${COORDINATOR_MGMT_PORT}"
else
  log "starting Coordinator; log=$MOTOR_COORD_LOG"
  nohup python3 -m motor.coordinator.main >>"$MOTOR_COORD_LOG" 2>&1 &
  record_started "$!"
  confirm_supervisor Coordinator "$!" "$MOTOR_COORD_LOG"
fi

log "processes started; health is left to Coordinator"
log "from CoordinatorConfig: render=$CONFIG_RENDER_ENABLE kv-conductor=$START_KV conductor_service=${CONDUCTOR_SERVICE:-<empty>}"
log "logs: $MOTOR_START_LOG $MOTOR_COORD_LOG $MOTOR_KVC_LOG $MOTOR_RENDER_LOG"
