#!/usr/bin/env bash
# MindIE PyMotor - 镜像源安装 Kubernetes 一键脚本
# 对应文档: docs/zh/user_guide/environment_preparation.md（镜像源安装 Kubernetes 步骤 1～7）
#
# 步骤映射:
#   STEP-01  前置检查（Docker 配置 / Shell 互联网 / Docker 互联网验证 / 磁盘 <85%）
#   STEP-02  yum 安装 kubelet / kubeadm / kubectl
#   STEP-03  kubeadm config images list + 阿里云拉取并 tag 为 k8s.gcr.io
#   STEP-04  清理 kubelet、swapoff、kubeadm reset、unset 代理（计算节点到此为止）
#   STEP-05  kubeadm init（仅管理节点）
#   STEP-06  kubectl get pods -A 检查
#   STEP-07  Calico 网络插件（coredns 异常时安装）
#
# 管理节点（单机集群）:
#   cp env.example env.conf && vim env.conf
#   sudo bash deploy_k8s_env.sh master
#
# 计算节点（仅环境准备，join 请手动执行 kubeadm join）:
#   sudo bash deploy_k8s_env.sh worker
#
# 仅前置检查:
#   sudo bash deploy_k8s_env.sh precheck

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/env.conf}"
CALICO_YAML="${CALICO_YAML:-${SCRIPT_DIR}/calico.yaml}"

# 固定参数
readonly K8S_VERSION="1.23.0"
readonly K8S_RPM_SUFFIX="1.23.0-00"
readonly CALICO_VERSION="3.24.5"
readonly CALICO_MANIFEST_URL="https://docs.projectcalico.org/v3.24/manifests/calico.yaml"
readonly POD_NETWORK_CIDR="192.168.0.0/16"
readonly OPENEULER_RELEASE="24.03-LTS-SP2"
readonly DISK_USAGE_MAX_PERCENT=85
readonly INTERNET_TEST_TIMEOUT=15
readonly DOCKER_TEST_TIMEOUT=120
readonly DOCKER_PULL_TEST_IMAGE="registry.cn-hangzhou.aliyuncs.com/google_containers/pause:3.6"
readonly WAIT_READY_SECONDS=30
readonly WAIT_READY_INTERVAL=5
readonly WAIT_READY_LOOPS=6
# Shell 连通性探测 URL
readonly -a INTERNET_TEST_URLS=(
  "http://www.baidu.com"
  "http://mirrors.aliyun.com"
  "https://mirrors.aliyun.com"
)

# 用户配置（必须由 env.conf 提供，见 load_env / validate_env）
HOST_IP=""
IP_AUTODETECTION_IFACE=""
HTTP_PROXY=""
HTTPS_PROXY=""
NO_PROXY=""
# 前置检查后确定 Shell 网络模式
SHELL_USE_PROXY=false
NETWORK_DETECTED=false

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

CURRENT_STEP=""
CURRENT_STEP_ID=""
STEP_COUNTER=0
FAILED_STEP=""

ts() { date '+%Y-%m-%d %H:%M:%S'; }

log()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }
dbg()  { :; }

die() { err "$*"; exit 1; }

step_begin() {
  local step_id="$1"
  local step_name="$2"
  CURRENT_STEP_ID="${step_id}"
  CURRENT_STEP="${step_name}"
  STEP_COUNTER=$((STEP_COUNTER + 1))
  echo -e "${CYAN}======== [${step_id}] ${step_name} ========${NC}"
}

step_ok() {
  echo -e "${GREEN}-------- [${CURRENT_STEP_ID}] 完成 --------${NC}"
  CURRENT_STEP_ID=""
  CURRENT_STEP=""
}

run_cmd() {
  local desc="$1"
  shift
  echo -e "${CYAN}>>> ${desc}${NC}"
  echo -e "${CYAN}    \$ $*${NC}"
  "$@"
}

on_error() {
  local exit_code=$?
  FAILED_STEP="${CURRENT_STEP_ID:-UNKNOWN}:${CURRENT_STEP:-未知步骤}"
  err "部署中断于步骤 [${FAILED_STEP}]，退出码=${exit_code}"
  exit "${exit_code}"
}

require_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "请使用 root 或 sudo 运行"
}

load_env() {
  [[ -f "${ENV_FILE}" ]] || die "未找到配置文件 ${ENV_FILE}，请先执行: cp env.example env.conf 并修改后重试"
  # shellcheck source=env.conf
  source "${ENV_FILE}"
  log "已加载配置: ${ENV_FILE}"
  normalize_proxy_env
  validate_env
}

validate_env() {
  [[ -n "${HOST_IP}" ]] || die "env.conf 缺少必填项 HOST_IP"
  [[ -n "${IP_AUTODETECTION_IFACE}" ]] || die "env.conf 缺少必填项 IP_AUTODETECTION_IFACE"
}

normalize_proxy_env() {
  # 兼容 env.conf 中 HTTP_PROXY / http_proxy 两种写法
  HTTP_PROXY="${HTTP_PROXY:-${http_proxy:-}}"
  HTTPS_PROXY="${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY}}}"
  NO_PROXY="${NO_PROXY:-${no_proxy:-}}"
  SAVED_HTTP_PROXY="${HTTP_PROXY}"
  SAVED_HTTPS_PROXY="${HTTPS_PROXY}"
  SAVED_NO_PROXY="${NO_PROXY}"
  if [[ -n "${SAVED_HTTP_PROXY}" ]]; then
    dbg "代理配置: HTTP_PROXY=${SAVED_HTTP_PROXY} NO_PROXY=${SAVED_NO_PROXY}"
  fi
}

log_session_header() {
  local cmd="$1"
  echo ""
  echo "################################################################"
  log "MindIE K8s 部署 | $(ts) | $(hostname -f 2>/dev/null || hostname) | 命令: ${cmd}"
  echo "################################################################"
  echo ""
}

detect_arch() {
  MACHINE="$(uname -m)"
  case "${MACHINE}" in
    aarch64|arm64) ARCH="aarch64"; K8S_YUM_ARCH="aarch64"; CALICO_IMAGE_SUFFIX="-linuxarm64" ;;
    x86_64|amd64)  ARCH="x86_64";  K8S_YUM_ARCH="x86_64";  CALICO_IMAGE_SUFFIX="" ;;
    *) die "不支持的架构: ${MACHINE}" ;;
  esac
  log "系统架构: ${ARCH} (${MACHINE})"
}

apply_host_ip_no_proxy() {
  # STEP-04 已 unset 代理变量后，仅更新 SAVED_NO_PROXY 供后续 Shell 代理使用
  if [[ "${SAVED_NO_PROXY:-}" != *"${HOST_IP}"* ]]; then
    if [[ -n "${SAVED_NO_PROXY:-}" ]]; then
      SAVED_NO_PROXY="${SAVED_NO_PROXY},${HOST_IP}"
    else
      SAVED_NO_PROXY="${HOST_IP}"
    fi
  fi
  log "HOST_IP=${HOST_IP}"
}

unset_k8s_proxy() {
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY
  dbg "已 unset 代理环境变量（kubeadm/kubectl 使用）"
}

set_pull_proxy() {
  if [[ "${SHELL_USE_PROXY}" != "true" ]]; then
    unset_k8s_proxy
    return 0
  fi
  [[ -n "${SAVED_HTTP_PROXY:-}" ]] || die "Shell 需使用代理但未配置 HTTP_PROXY"
  export http_proxy="${SAVED_HTTP_PROXY}"
  export HTTP_PROXY="${SAVED_HTTP_PROXY}"
  export https_proxy="${SAVED_HTTPS_PROXY:-${SAVED_HTTP_PROXY}}"
  export HTTPS_PROXY="${SAVED_HTTPS_PROXY:-${SAVED_HTTP_PROXY}}"
  export no_proxy="${SAVED_NO_PROXY}"
  export NO_PROXY="${SAVED_NO_PROXY}"
}

# Calico 镜像已拉取成功，不再重复探测互联网；沿用 STEP-01 结果或 env.conf
apply_shell_proxy_for_download() {
  if [[ "${NETWORK_DETECTED:-}" != "true" ]]; then
    if [[ -n "${SAVED_HTTP_PROXY:-}" ]]; then
      SHELL_USE_PROXY=true
    else
      SHELL_USE_PROXY=false
    fi
    log "下载 calico.yaml：跳过连通性测试，默认互联网可用"
  fi
  if [[ "${SHELL_USE_PROXY}" == "true" ]]; then
    set_pull_proxy
    log "Shell 使用代理: HTTP_PROXY=${SAVED_HTTP_PROXY}"
  else
    unset_k8s_proxy
    log "Shell 使用直连（无代理）"
  fi
}

curl_download() {
  local out="$1"
  local url="$2"
  local -a curl_args=(-skL -o "${out}")
  if [[ "${SHELL_USE_PROXY}" == "true" ]]; then
    set_pull_proxy
  else
    unset_k8s_proxy
  fi
  curl "${curl_args[@]}" "${url}"
}

log_network_mode_summary() {
  local shell_mode
  if [[ "${SHELL_USE_PROXY}" == "true" ]]; then
    shell_mode="代理(${SAVED_HTTP_PROXY})"
  else
    shell_mode="直连"
  fi
  log "网络模式已确定: Shell=${shell_mode} | Docker=使用当前 dockerd 配置"
}

test_shell_internet() {
  local mode="$1"
  local url code curl_err

  if [[ "${mode}" == "direct" ]]; then
    unset_k8s_proxy
    dbg "Shell 连通性测试（直连）"
  else
    [[ -n "${SAVED_HTTP_PROXY:-}" ]] || return 1
    export http_proxy="${SAVED_HTTP_PROXY}"
    export HTTP_PROXY="${SAVED_HTTP_PROXY}"
    export https_proxy="${SAVED_HTTPS_PROXY:-${SAVED_HTTP_PROXY}}"
    export HTTPS_PROXY="${SAVED_HTTPS_PROXY:-${SAVED_HTTP_PROXY}}"
    export no_proxy="${SAVED_NO_PROXY}"
    export NO_PROXY="${SAVED_NO_PROXY}"
    dbg "Shell 连通性测试（代理 ${SAVED_HTTP_PROXY}）"
  fi

  for url in "${INTERNET_TEST_URLS[@]}"; do
    code="$(curl -sL -k --connect-timeout "${INTERNET_TEST_TIMEOUT}" --max-time "${INTERNET_TEST_TIMEOUT}" \
      -o /dev/null -w "%{http_code}" "${url}" 2>/dev/null || echo "000")"
    if [[ "${code}" =~ ^[23] ]]; then
      log "Shell 测试通过 mode=${mode} url=${url} http_code=${code}"
      return 0
    fi
    dbg "Shell 测试失败 mode=${mode} url=${url} http_code=${code}"
  done
  return 1
}

test_docker_internet() {
  run_cmd "Docker 连通性测试" timeout "${DOCKER_TEST_TIMEOUT}" docker pull "${DOCKER_PULL_TEST_IMAGE}"
}

detect_shell_network() {
  log "[PRECHECK-2/4] 验证 Shell 能否访问互联网"

  if test_shell_internet "direct"; then
    SHELL_USE_PROXY=false
    log "Shell 直连互联网: 通过"
    if [[ -n "${SAVED_HTTP_PROXY:-}" ]]; then
      if test_shell_internet "proxy"; then
        log "Shell 代理互联网: 也通过（优先选用直连）"
      else
        warn "Shell 代理不可用，但直连可用，后续使用直连"
      fi
      unset_k8s_proxy
    fi
    return 0
  fi

  warn "Shell 直连互联网: 失败"
  if [[ -n "${SAVED_HTTP_PROXY:-}" ]]; then
    log "尝试 Shell 代理: ${SAVED_HTTP_PROXY}"
    if test_shell_internet "proxy"; then
      SHELL_USE_PROXY=true
      log "Shell 代理互联网: 通过，后续使用代理"
      return 0
    fi
    warn "Shell 代理互联网: 失败"
  else
    warn "env.conf 未配置 HTTP_PROXY，无法尝试代理"
  fi

  die "Shell 无法访问互联网（直连与代理均失败），请检查网络或 env.conf 代理配置"
}

log_docker_proxy_status() {
  local env_out
  env_out="$(systemctl show docker --property=Environment 2>/dev/null || true)"
  log "当前 dockerd Environment: ${env_out:-（无）}"
}

verify_docker_network() {
  log "[PRECHECK-3/4] 验证 Docker 能否访问互联网"
  verify_docker_running
  log_docker_proxy_status
  if test_docker_internet; then
    log "Docker 访问互联网: 通过"
    return 0
  fi
  die "Docker 无法访问互联网，请检查 docker 配置、insecure-registries；如需代理请手动配置 dockerd 后重试"
}

check_disk_usage() {
  local use_pct
  use_pct="$(df / | tail -1 | awk '{gsub(/%/,"",$5); print $5}')"
  log "根分区使用率: ${use_pct}%（阈值 < ${DISK_USAGE_MAX_PERCENT}%）"
  if [[ "${use_pct}" -ge "${DISK_USAGE_MAX_PERCENT}" ]]; then
    die "根分区使用率 ${use_pct}% >= ${DISK_USAGE_MAX_PERCENT}%，可能发生镜像丢失，请先清理磁盘"
  fi
}

verify_docker_daemon_json() {
  local daemon_json="/etc/docker/daemon.json"

  local py_out
  py_out="$(python3 - "${daemon_json}" <<'PY'
import json, os, sys

path = sys.argv[1]
required_registries = [
    "registry.cn-hangzhou.aliyuncs.com",
    "swr.cn-north-4.myhuaweicloud.com",
]
required_exec = "native.cgroupdriver=systemd"

if not os.path.isfile(path):
    print(f"ERROR: {path} 不存在，请手动配置 insecure-registries 与 exec-opts", file=sys.stderr)
    sys.exit(1)

with open(path, encoding="utf-8") as f:
    try:
        data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} JSON 非法: {e}", file=sys.stderr)
        sys.exit(1)

exec_opts = data.get("exec-opts", [])
if not isinstance(exec_opts, list):
    exec_opts = []
if required_exec not in exec_opts:
    print(f"ERROR: exec-opts 缺少 {required_exec}，请手动在 {path} 中配置", file=sys.stderr)
    sys.exit(1)

registries = data.get("insecure-registries", [])
if not isinstance(registries, list):
    registries = []
for r in required_registries:
    if r not in registries:
        print(f"ERROR: insecure-registries 缺少 {r}，请手动在 {path} 中配置", file=sys.stderr)
        sys.exit(1)

print("exec-opts:", data.get("exec-opts"))
print("insecure-registries:", data.get("insecure-registries"))
PY
)" || die "daemon.json 校验失败"

  log "daemon.json 校验通过"
  echo "${py_out}"
}

verify_docker_running() {
  systemctl is-active docker >/dev/null 2>&1 || die "docker 未运行"
  docker ps >/dev/null 2>&1 || die "docker ps 失败"
  log "docker 服务正常"
}

precheck_docker_and_disk() {
  log "[PRECHECK-1/4] 验证 Docker 配置是否正确"
  verify_docker_daemon_json
  log "--- /etc/docker/daemon.json ---"
  cat /etc/docker/daemon.json 2>/dev/null || echo "不存在"
  systemctl start docker 2>/dev/null || true
  verify_docker_running
  log "Docker 配置校验通过"

  detect_shell_network
  verify_docker_network
  NETWORK_DETECTED=true
  log_network_mode_summary

  log "[PRECHECK-4/4] 验证根分区空间使用率 < ${DISK_USAGE_MAX_PERCENT}%"
  run_cmd "df -h" df -h
  check_disk_usage

  log "默认路由网卡（Calico IP_AUTODETECTION 参考）:"
  ip route 2>/dev/null | grep default || warn "未找到 default 路由，请检查 IP_AUTODETECTION_IFACE"
}

install_k8s_rpms() {
  set_pull_proxy
  mkdir -p /etc/yum.repos.d/disabled
  shopt -s nullglob
  for f in /etc/yum.repos.d/*.repo; do
    mv "${f}" /etc/yum.repos.d/disabled/ 2>/dev/null || true
  done
  shopt -u nullglob
  log "已备份旧 yum 源到 /etc/yum.repos.d/disabled"

  cat > /etc/yum.repos.d/openEuler-huawei.repo <<EOF
[openEuler-everything]
name=openEuler Everything
baseurl=https://repo.huaweicloud.com/openeuler/openEuler-${OPENEULER_RELEASE}/everything/${ARCH}/
enabled=1
gpgcheck=0
EOF

  cat > /etc/yum.repos.d/kubernetes.repo <<EOF
[kubernetes]
name=Kubernetes
baseurl=http://mirrors.aliyun.com/kubernetes/yum/repos/kubernetes-el7-${K8S_YUM_ARCH}/
enabled=1
gpgcheck=0
EOF

  run_cmd "yum clean" yum clean all
  run_cmd "yum makecache" yum makecache -y || true
  run_cmd "yum install k8s" yum install -y "kubelet-${K8S_RPM_SUFFIX}" "kubeadm-${K8S_RPM_SUFFIX}" "kubectl-${K8S_RPM_SUFFIX}"
  systemctl enable kubelet
  log "Kubernetes RPM 安装完成: $(kubelet --version 2>/dev/null || echo unknown)"
  warn "如需复原旧 yum 源: sudo -E bash deploy_k8s_env.sh restore-yum-repos"
}

restore_yum_repos() {
  local disabled="/etc/yum.repos.d/disabled"
  if [[ ! -d "${disabled}" ]]; then
    warn "未找到 ${disabled}，无需复原"
    return 0
  fi
  shopt -s nullglob
  local f count=0
  for f in "${disabled}"/*.repo; do
    mv "${f}" /etc/yum.repos.d/
    count=$((count + 1))
  done
  shopt -u nullglob
  log "已从 ${disabled} 复原 ${count} 个 yum 源仓库"
}

pull_k8s_images() {
  # 文档：kubeadm config images list 必须在无代理环境下执行
  unset_k8s_proxy
  run_cmd "kubeadm config images list（无代理）" \
    kubeadm config images list --kubernetes-version="v${K8S_VERSION}"

  # 文档方案一：阿里云拉取 -> tag 为 k8s.gcr.io -> 删除阿里云镜像（使用当前 dockerd 配置）
  local items=(
    "kube-apiserver:v${K8S_VERSION}|k8s.gcr.io/kube-apiserver:v${K8S_VERSION}"
    "kube-controller-manager:v${K8S_VERSION}|k8s.gcr.io/kube-controller-manager:v${K8S_VERSION}"
    "kube-scheduler:v${K8S_VERSION}|k8s.gcr.io/kube-scheduler:v${K8S_VERSION}"
    "kube-proxy:v${K8S_VERSION}|k8s.gcr.io/kube-proxy:v${K8S_VERSION}"
    "pause:3.6|k8s.gcr.io/pause:3.6"
    "etcd:3.5.1-0|k8s.gcr.io/etcd:3.5.1-0"
    "coredns:v1.8.6|k8s.gcr.io/coredns/coredns:v1.8.6"
  )
  local item name target mirror
  for item in "${items[@]}"; do
    name="${item%%|*}"
    target="${item##*|}"
    mirror="registry.cn-hangzhou.aliyuncs.com/google_containers/${name}"
    if ! run_cmd "docker pull ${mirror}" docker pull "${mirror}"; then
      die "镜像拉取失败: ${mirror}，请检查 docker 配置、insecure-registries 或 dockerd 代理"
    fi
    log "tag ${mirror} -> ${target}"
    docker tag "${mirror}" "${target}"
    docker rmi "${mirror}" 2>/dev/null || true
  done
  log "K8s 控制面镜像拉取完成"
  docker images | grep -E 'k8s\.gcr\.io|pause|etcd|coredns' || true
}

stop_kubelet_safely() {
  if systemctl is-active kubelet >/dev/null 2>&1; then
    log "停止 kubelet 服务..."
    systemctl stop kubelet 2>/dev/null || true
    sleep 2
  fi
}

unmount_kubelet_volumes() {
  local mnt
  if [[ ! -d /var/lib/kubelet ]]; then
    return 0
  fi
  while read -r mnt; do
    [[ -z "${mnt}" ]] && continue
    umount -l "${mnt}" 2>/dev/null || true
  done < <(find /var/lib/kubelet -type d -name 'kube-api-access-*' 2>/dev/null || true)
  while read -r mnt; do
    [[ -z "${mnt}" ]] && continue
    umount -l "${mnt}" 2>/dev/null || true
  done < <(mount 2>/dev/null | awk '/\/var\/lib\/kubelet/{print $3}' || true)
}

cleanup_kubelet_dir() {
  stop_kubelet_safely
  unmount_kubelet_volumes
  if [[ -d /var/lib/kubelet ]]; then
    rm -rf /var/lib/kubelet 2>/dev/null || {
      warn "/var/lib/kubelet 部分目录占用，重试 lazy umount"
      unmount_kubelet_volumes
      sleep 1
      rm -rf /var/lib/kubelet 2>/dev/null || warn "仍无法完全删除 /var/lib/kubelet，继续执行"
    }
  fi
  mkdir -p /var/lib/kubelet
}

prepare_system() {
  # 文档步骤 3；kubelet清理
  stop_kubelet_safely
  cleanup_kubelet_dir
  swapoff -a || true
  sed -i '/ swap / s/^\(.*\)$/#\1/g' /etc/fstab 2>/dev/null || true
  run_cmd "kubeadm reset" kubeadm reset -f || true
  rm -rf /etc/cni/net.d /root/.kube/
  unset_k8s_proxy
  log "系统与 kubeadm 状态已清理（rm kubelet、swapoff、reset、清理 cni/kubeconfig、unset 代理）"
}

init_master() {
  unset_k8s_proxy

  log "初始化集群 apiserver=${HOST_IP} version=v${K8S_VERSION} pod-cidr=${POD_NETWORK_CIDR}"
  if ! run_cmd "kubeadm init" kubeadm init \
    --kubernetes-version="v${K8S_VERSION}" \
    --pod-network-cidr="${POD_NETWORK_CIDR}" \
    --apiserver-advertise-address="${HOST_IP}"; then
    die "kubeadm init 失败，请查看上方命令输出"
  fi

  mkdir -p "${HOME}/.kube"
  cp -f /etc/kubernetes/admin.conf "${HOME}/.kube/config"
  chown "$(id -u):$(id -g)" "${HOME}/.kube/config"

  log "kubeconfig 已写入 ${HOME}/.kube/config"
  local join_cmd
  join_cmd="$(kubeadm token create --print-join-command 2>/dev/null || true)"
  log "Join 命令（请保存给计算节点）: ${join_cmd}"
}

verify_pods_after_init() {
  unset_k8s_proxy
  ensure_kubeconfig
  export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
  log "检查初始化后 Pod 状态..."
  local i
  for i in $(seq 1 "${WAIT_READY_LOOPS}"); do
    if kubectl get pods -n kube-system --no-headers 2>/dev/null | grep -q .; then
      break
    fi
    log "等待控制面 Pod 创建... (${i}/${WAIT_READY_LOOPS}，最多 ${WAIT_READY_SECONDS}s)"
    sleep "${WAIT_READY_INTERVAL}"
  done
  kubectl get pods -A -o wide
}

need_calico() {
  unset_k8s_proxy
  if kubectl get pods -n kube-system 2>/dev/null | grep -q 'calico-node.*Running'; then
    log "Calico 已在运行，跳过安装"
    return 1
  fi
  if kubectl get pods -n kube-system 2>/dev/null | grep coredns | grep -vE 'Running|Completed' | grep -q .; then
    warn "检测到 coredns 非 Running，需要安装 Calico 网络插件"
    return 0
  fi
  if ! kubectl get pods -n kube-system 2>/dev/null | grep coredns | grep -q Running; then
    warn "coredns 未就绪，需要安装 Calico 网络插件"
    return 0
  fi
  log "Pod 状态正常，按文档可跳过 Calico 安装"
  return 1
}

pull_calico_images() {
  local swr="swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/calico"
  local ver="v${CALICO_VERSION}${CALICO_IMAGE_SUFFIX}"
  local c
  local mirror target
  for c in kube-controllers cni node; do
    mirror="${swr}/${c}:${ver}"
    target="calico/${c}:v${CALICO_VERSION}"
    if ! run_cmd "docker pull ${mirror}" docker pull "${mirror}"; then
      die "Calico 镜像拉取失败: ${mirror}"
    fi
    log "tag ${mirror} -> ${target}"
    docker tag "${mirror}" "${target}"
    docker rmi "${mirror}" 2>/dev/null || true
  done
}

validate_calico_yaml() {
  local f="$1"
  [[ -f "${f}" ]] || return 1
  [[ "$(wc -l < "${f}")" -gt 1000 ]] || return 1
  grep -q 'CALICO_IPV4POOL_IPIP' "${f}" && grep -q 'kind: DaemonSet' "${f}"
}

download_calico_yaml() {
  # 文档：curl -k -O https://docs.projectcalico.org/v3.24/manifests/calico.yaml
  apply_shell_proxy_for_download
  rm -f "${CALICO_YAML}"
  log "下载 calico.yaml: ${CALICO_MANIFEST_URL}"
  if ! curl_download "${CALICO_YAML}" "${CALICO_MANIFEST_URL}"; then
    die "calico.yaml 下载失败: ${CALICO_MANIFEST_URL}"
  fi
  chmod u+w "${CALICO_YAML}" 2>/dev/null || true
  if ! validate_calico_yaml "${CALICO_YAML}"; then
    local lines size
    lines="$(wc -l < "${CALICO_YAML}" 2>/dev/null || echo 0)"
    size="$(wc -c < "${CALICO_YAML}" 2>/dev/null || echo 0)"
    warn "calico.yaml 内容异常（${lines} 行 / ${size} 字节），前 10 行:"
    head -10 "${CALICO_YAML}" || true
    die "calico.yaml 无效（需包含 CALICO_IPV4POOL_IPIP），请检查代理或手动放置到 ${CALICO_YAML}"
  fi
  log "calico.yaml 校验通过，行数: $(wc -l < "${CALICO_YAML}")"
}

show_calico_patch_preview() {
  log "calico.yaml 补丁预览（CALICO_IPV4POOL_IPIP 附近）:"
  grep -B2 -A4 'CALICO_IPV4POOL_IPIP' "${CALICO_YAML}" 2>/dev/null || true
}

patch_calico_yaml() {
  [[ -f "${CALICO_YAML}" ]] || die "缺少 ${CALICO_YAML}"

  python3 - "${CALICO_YAML}" "${IP_AUTODETECTION_IFACE}" <<'PY' || die "calico.yaml patch 失败"
import re
import sys

path, iface = sys.argv[1], sys.argv[2]
ipip_re = re.compile(r'^(\s+)- name: CALICO_IPV4POOL_IPIP\s*$')
auto_re = re.compile(r'^\s+- name: IP_AUTODETECTION_METHOD\s*$')

with open(path, encoding='utf-8') as f:
    lines = f.readlines()

# 先删掉已有 IP_AUTODETECTION_METHOD（含旧版错误缩进），再干净插入
cleaned = []
i = 0
while i < len(lines):
    if auto_re.match(lines[i]):
        i += 1
        if i < len(lines) and re.match(r'^\s+value:\s*"interface=', lines[i]):
            i += 1
        continue
    cleaned.append(lines[i])
    i += 1
lines = cleaned

inserted = False
for i, line in enumerate(lines):
    m = ipip_re.match(line)
    if not m:
        continue
    indent = m.group(1)
    val_indent = indent + '  '
    lines[i:i] = [
        f'{indent}- name: IP_AUTODETECTION_METHOD\n',
        f'{val_indent}value: "interface={iface}"\n',
    ]
    inserted = True
    break

if not inserted:
    sys.exit('未找到 CALICO_IPV4POOL_IPIP env 项')

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(lines)
PY

  log "已插入 IP_AUTODETECTION_METHOD=interface=${IP_AUTODETECTION_IFACE}"
  show_calico_patch_preview
}

ensure_kubeconfig() {
  if [[ -f /etc/kubernetes/admin.conf ]]; then
    export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
    mkdir -p "${HOME}/.kube"
    cp -f /etc/kubernetes/admin.conf "${HOME}/.kube/config" 2>/dev/null || true
  fi
}

wait_for_apiserver() {
  unset_k8s_proxy
  ensure_kubeconfig
  log "等待 API Server 就绪（6443，最多 ${WAIT_READY_SECONDS}s）..."
  systemctl start kubelet 2>/dev/null || true
  local i
  for i in $(seq 1 "${WAIT_READY_LOOPS}"); do
    if kubectl get --raw='/healthz' >/dev/null 2>&1; then
      log "API Server 已就绪"
      return 0
    fi
    if [[ "${i}" -eq 3 ]]; then
      warn "API Server 未响应，尝试重启 kubelet"
      systemctl restart kubelet 2>/dev/null || true
    fi
    sleep "${WAIT_READY_INTERVAL}"
  done
  die "API Server 不可用（:6443 connection refused），请检查: systemctl status kubelet docker"
}

install_calico() {
  # 文档：Calico 镜像拉取后默认互联网可用，yaml 下载沿用 STEP-01 网络模式；apply 前 unset
  pull_calico_images
  download_calico_yaml
  patch_calico_yaml

  log "calico.yaml 已就绪，取消网络代理后启动 Calico"
  unset_k8s_proxy
  wait_for_apiserver
  if ! run_cmd "kubectl apply calico" kubectl apply -f "${CALICO_YAML}"; then
    warn "若曾部分 apply 失败，可先清理后重试:"
    warn "  kubectl delete -f ${CALICO_YAML} --ignore-not-found"
    warn "  rm -f ${CALICO_YAML} && bash deploy_k8s_env.sh calico-only"
    die "kubectl apply calico.yaml 失败"
  fi
  kubectl taint nodes --all node-role.kubernetes.io/master- 2>/dev/null || \
    kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>/dev/null || true

  log "等待 Calico / CoreDNS Ready（最多 ${WAIT_READY_SECONDS}s）..."
  local i
  for i in $(seq 1 "${WAIT_READY_LOOPS}"); do
    if kubectl get pods -n kube-system 2>/dev/null | grep -E 'calico-node.*Running|coredns.*Running' >/dev/null; then
      if ! kubectl get pods -n kube-system 2>/dev/null | grep -v Running | grep -E 'calico|coredns' | grep -qv Completed; then
        break
      fi
    fi
    sleep "${WAIT_READY_INTERVAL}"
  done
  kubectl get pods -A

  if kubectl get pods -n kube-system 2>/dev/null | grep -E 'calico|coredns' | grep -v Running | grep -qv Completed; then
    warn "部分 calico/coredns Pod 仍未 Running，请执行: kubectl get pods -A -o wide"
  fi
}

worker_prepare() {
  step_begin "STEP-02" "获取 Kubernetes 组件"
  install_k8s_rpms
  step_ok

  step_begin "STEP-03" "拉取 Kubernetes 依赖镜像"
  pull_k8s_images
  step_ok

  step_begin "STEP-04" "系统清理与取消代理"
  prepare_system
  step_ok
  log "计算节点基础环境就绪"
}

print_mindcluster_next_steps() {
  cat <<EOF

============================================================
K8s 环境部署完成。MindCluster 需按官方文档手动安装：
  1. 安装前准备
  2. Ascend Docker Runtime
  3. Ascend Device Plugin
  4. Volcano（单机需改 useClusterInfoManager=false）
  5. Infer Operator / Ascend Operator / ClusterD
文档: https://gitcode.com/Ascend/mind-cluster/tree/branch_v26.0.0/docs/zh/scheduling/installation_guide
============================================================
EOF
}

usage() {
  cat <<EOF
用法:
  cp env.example env.conf && vim env.conf
  sudo bash deploy_k8s_env.sh precheck        # 仅 STEP-01 四项前置检查
  sudo bash deploy_k8s_env.sh master          # 管理节点全量（STEP 1-7）
  sudo bash deploy_k8s_env.sh worker          # 计算节点（STEP 1-4，join 手动执行）
  sudo bash deploy_k8s_env.sh calico-only     # 仅安装 Calico
  sudo bash deploy_k8s_env.sh restore-yum-repos # 复原 yum 源

配置文件（必填）: ENV_FILE=${ENV_FILE}
EOF
}

run_master() {
  step_begin "STEP-01" "前置检查（Docker/Shell/Docker互联网/磁盘）"
  precheck_docker_and_disk
  step_ok

  step_begin "STEP-02" "获取 Kubernetes 组件"
  install_k8s_rpms
  step_ok

  step_begin "STEP-03" "拉取 Kubernetes 依赖镜像"
  pull_k8s_images
  step_ok

  step_begin "STEP-04" "系统清理与取消代理"
  prepare_system
  step_ok

  step_begin "STEP-05" "kubeadm 初始化集群"
  init_master
  step_ok

  step_begin "STEP-06" "检查 Pod 状态"
  verify_pods_after_init
  step_ok

  step_begin "STEP-07" "安装 Calico 网络插件（coredns 异常时）"
  if need_calico; then
    install_calico
  else
    log "跳过 Calico（所有 Pod 已 Running）"
  fi
  step_ok

  print_mindcluster_next_steps
}

main() {
  require_root
  trap on_error ERR

  load_env
  detect_arch
  apply_host_ip_no_proxy

  local cmd="${1:-master}"
  log_session_header "${cmd}"

  case "${cmd}" in
    precheck)
      step_begin "STEP-01" "前置检查（Docker/Shell/Docker互联网/磁盘）"
      precheck_docker_and_disk
      step_ok
      log "前置检查完成"
      ;;
    master)
      run_master
      ;;
    worker)
      step_begin "STEP-01" "前置检查（Docker/Shell/Docker互联网/磁盘）"
      precheck_docker_and_disk
      step_ok
      worker_prepare
      log "计算节点环境就绪；请在管理节点执行 kubeadm token create --print-join-command，再手动 join"
      ;;
    calico-only)
      install_calico
      ;;
    restore-yum-repos)
      restore_yum_repos
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      die "未知命令: ${cmd}；执行 --help 查看用法"
      ;;
  esac
}

main "$@"
