# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
import os

GREEN = '\033[32m'
RESET = '\033[0m'

E_INSTANCES_NUM = "e_instances_num"
P_INSTANCES_NUM = "p_instances_num"
D_INSTANCES_NUM = "d_instances_num"
HYBRID_INSTANCES_NUM = "hybrid_instances_num"
SINGLE_HYBRID_INSTANCE_POD_NUM = "single_hybrid_instance_pod_num"
HYBRID_POD_NPU_NUM = "hybrid_pod_npu_num"
CONFIG_JOB_ID = "job_id"
SINGER_E_INSTANCES_NUM = "single_e_instance_pod_num"
SINGER_P_INSTANCES_NUM = "single_p_instance_pod_num"
SINGER_D_INSTANCES_NUM = "single_d_instance_pod_num"
E_POD_NPU_NUM = "e_pod_npu_num"
P_POD_NPU_NUM = "p_pod_npu_num"
D_POD_NPU_NUM = "d_pod_npu_num"
ASCEND_910_NPU_NUM = "huawei.com/Ascend910"
ASCEND_950_NPU_NUM = "huawei.com/npu"
RING_CONTROLLER_ATLAS_LABEL = "ring-controller.atlas"
INFERSERVICE_ID_LABEL = "inferserviceid"
HUAWEI_SCHEDULE_POLICY_ANNOTATION = "huawei.com/schedule_policy"
A5_SCHEDULE_POLICY_BY_ACCELERATOR_TYPE = {
    "350-Atlas-8": "chip1-node8",
    "350-Atlas-16": "chip1-node16",
    "350-Atlas-4p-8": "chip4-node8",
    "350-Atlas-4p-16": "chip4-node16",
    "850-Atlas-8p-8": "chip8-node8",
    "850-SuperPod-Atlas-8": "chip8-node8-sp",
    "950-SuperPod-Atlas-8": "chip8-node8-ra64-sp",
}
A5_HOST_PATH_VOLUMES = [
    {"name": "host-lib64", "path": "/usr/lib64"},
    {"name": "hixlep", "path": "/etc/hixlep"},
]
HOST_NETWORK = "hostNetwork"
DNS_POLICY = "dnsPolicy"
DNS_POLICY_CLUSTER_FIRST_WITH_HOST_NET = "ClusterFirstWithHostNet"
DNS_CONFIG = "dnsConfig"
DNS_OPTIONS = "options"
A5_DNS_NDOTS_OPTION = "ndots"
A5_DNS_NDOTS_VALUE = "2"
METADATA = "metadata"
CONTROLLER = "controller"
COORDINATOR = "coordinator"
NAMESPACE = "namespace"
NAME = "name"
ENV = "env"
SPEC = "spec"
TEMPLATE = "template"
REPLICAS = "replicas"
LABELS = "labels"
KIND = "kind"
APP = "app"
VALUE = "value"
NODE_SELECTOR = "nodeSelector"
RESOURCES = "resources"
SUBJECTS = "subjects"
DEPLOYMENT = "deployment"
DEPLOYMENT_KIND = "Deployment"
SERVICE_ACCOUNT = "ServiceAccount"
SERVICE = "Service"
CLUSTER_ROLE_BINDING = "ClusterRoleBinding"
HARDWARE_TYPE = 'hardware_type'
ANNOTATIONS = "annotations"
SP_BLOCK = "sp-block"
DATA = "data"
STARTUP_ROOT_PATH = "./startup"
PATCH_ROOT_PATH = "./patch"
BOOT_SHELL_PATH = os.path.join(STARTUP_ROOT_PATH, "boot.sh")
COMMON_SHELL_PATH = os.path.join(STARTUP_ROOT_PATH, "common.sh")
CONTROLLER_SHELL_PATH = os.path.join(STARTUP_ROOT_PATH, "roles/controller.sh")
COORDINATOR_SHELL_PATH = os.path.join(STARTUP_ROOT_PATH, "roles/coordinator.sh")
ENGINE_SHELL_PATH = os.path.join(STARTUP_ROOT_PATH, "roles/engine.sh")
KV_CACHE_STORE_SHELL_PATH = os.path.join(STARTUP_ROOT_PATH, "roles/kv_cache_store.sh")
MF_STORE_SHELL_PATH = os.path.join(STARTUP_ROOT_PATH, "roles/mf_store.sh")
SINGLE_CONTAINER_SHELL_PATH = os.path.join(STARTUP_ROOT_PATH, "roles/all_combine_in_single_container.sh")
MOTOR_COMMON_ENV = "motor_common_env"
WEIGHT_MOUNT = "weight-mount"
KV_CACHE_STORE_CONFIG = "kv_cache_store_config"
KV_STORE_BACKEND = "backend"
KV_CACHE_STORE_PORT = "port"
KV_STORE_EVICTION_HIGH_WATERMARK_RATIO = "eviction_high_watermark_ratio"
KV_STORE_EVICTION_RATIO = "eviction_ratio"
DEFAULT_KV_LEASE_TTL = "default_kv_lease_ttl"
DEFAULT_KV_CACHE_STORE_PORT = 50088
DEFAULT_KV_STORE_BACKEND = "memcache"
MMC_STORE_BACKEND = "memcache"
# memcache MetaService defaults
MMC_CONFIG_STORE_PORT_KEY = "config_store_port"
MMC_METRICS_PORT_KEY = "metrics_port"
DEFAULT_MMC_CONFIG_STORE_PORT = 50089
DEFAULT_MMC_METRICS_PORT = 50090
KV_CONDUCTOR_CONFIG = "kv_conductor_config"
KV_CONDUCTOR_PORT = "http_server_port"
KV_CONDUCTOR_SHELL_PATH = os.path.join(STARTUP_ROOT_PATH, "roles/kv_conductor.sh")
DEFAULT_MF_STORE_PORT = 50089
STANDBY_CONFIG = "standby_config"
MOTOR_CONTROLLER_CONFIG = "motor_controller_config"
MOTOR_COORDINATOR_CONFIG = "motor_coordinator_config"
MOTOR_NODEMANAGER_CONFIG = "motor_nodemanger_config"
ENABLE_MASTER_STANDBY = "enable_master_standby"
INSTANCE_NUM_ZERO = 0
INSTANCE_NUM_MAX = 16
MOTOR_CONFIG_CONFIGMAP_NAME = "motor-config"
ENGINE_TYPE_VLLM = "vllm"
ENGINE_TYPE_MINDIE_LLM = "mindie-llm"
ENGINE_TYPE_MINDIE_SERVER = "mindie-server"
ENGINE_TYPE_SGLANG = "sglang"
SERVER_BASE_NAME_MAP = {
    ENGINE_TYPE_VLLM: ENGINE_TYPE_VLLM,
    ENGINE_TYPE_MINDIE_LLM: ENGINE_TYPE_MINDIE_SERVER,
    ENGINE_TYPE_SGLANG: ENGINE_TYPE_SGLANG,
}
LOG_PATH = "plog-path"
DEPLOY_YAML_ROOT_PATH = "./yaml_template"
OUTPUT_ROOT_PATH = "./output_yamls"
SELECTOR = "selector"
DEPLOY_MODE_INFER_SERVICE_SET = "infer_service_set"
DEPLOY_MODE_MULTI_DEPLOYMENT_YAML = "multi_deployment"
DEPLOY_MODE_SINGLE_CONTAINER = "single_container"
DEPLOY_MODE_CONFIG_KEY = "deploy_mode"
VALID_DEPLOY_MODES = (DEPLOY_MODE_INFER_SERVICE_SET, DEPLOY_MODE_MULTI_DEPLOYMENT_YAML, DEPLOY_MODE_SINGLE_CONTAINER)
MATCHLABELS = "matchLabels"
LOGGING_CONFIG = "logging_config"
HOST_PATH = "hostPath"
ENGINE_TYPE = "engine_type"
SECURITY_CONTEXT = "securityContext"
PRIVILEGED = "privileged"

HARDWARE_TYPE_800I_A2 = "800I_A2"
HARDWARE_TYPE_800T_A2 = "800T_A2"
HARDWARE_TYPE_800I_A3 = "800I_A3"
HARDWARE_TYPE_800T_A3 = "800T_A3"
# Group by chip generation — both 800I and 800T variants share the same accelerator labels
HARDWARE_TYPE_A2 = {HARDWARE_TYPE_800I_A2, HARDWARE_TYPE_800T_A2}
HARDWARE_TYPE_A3 = {HARDWARE_TYPE_800I_A3, HARDWARE_TYPE_800T_A3}
HARDWARE_TYPE_950I_A5 = [
    "350-Atlas-8",
    "350-Atlas-16",
    "350-Atlas-4p-8",
    "350-Atlas-4p-16",
    "850-Atlas-8p-8",
    "850-SuperPod-Atlas-8",
    "950-SuperPod-Atlas-8",
]
ACCELERATOR_A5 = "huawei-npu"
ACCELERATOR_910 = "huawei-Ascend910"
ACCELERATOR_TYPE = "accelerator-type"
ACCELERATOR = "accelerator"
ACCELERATOR_TYPE_910B = "module-910b-8"
ACCELERATOR_TYPE_A3 = "module-a3-16"

ENABLE_PD_HETEROGENEOUS = "enable_pd_heterogeneous"
PD_HETEROGENEOUS_LABEL_KEY = "pd_heterogeneous_label_key"
PD_HETEROGENEOUS_PREFILL_LABEL_VALUE = "pd_heterogeneous_prefill_label_value"
PD_HETEROGENEOUS_DECODE_LABEL_VALUE = "pd_heterogeneous_decode_label_value"
DEFAULT_PD_HETEROGENEOUS_LABEL_KEY = "card_type"
DEFAULT_PD_HETEROGENEOUS_PREFILL_VALUE = "Ascend950PR"
DEFAULT_PD_HETEROGENEOUS_DECODE_VALUE = "Ascend950DT"

CONTAINERS = "containers"
IMAGE = "image"
IMAGE_NAME = "image_name"
ROLE_ENCODE = "encode"
ROLE_PREFILL = "prefill"
ROLE_DECODE = "decode"
ROLE_UNION = "union"
ROLE_KV_STORE = "kv-store"
ROLE_KV_CONDUCTOR = "kv-conductor"
NODE_TYPE_E = "e"
NODE_TYPE_P = "p"
NODE_TYPE_D = "d"
NODE_TYPE_U = "u"
ROLE_SINGLE_CONTAINER = "SINGLE_CONTAINER"
REQUESTS = "requests"
LIMITS = "limits"

ENV_ROLE = "ROLE"
ENV_JOB_NAME = "JOB_NAME"
ENV_CONTROLLER_SERVICE = "CONTROLLER_SERVICE"
ENV_COORDINATOR_SERVICE = "COORDINATOR_SERVICE"
ENV_COORDINATOR_INFER_SERVICE = "COORDINATOR_INFER_SERVICE"
ENV_COORDINATOR_OBS_SERVICE = "COORDINATOR_OBS_SERVICE"
ENV_KVS_MASTER_SERVICE = "KVS_MASTER_SERVICE"
ENV_KV_CONDUCTOR_SERVICE = "KV_CONDUCTOR_SERVICE"
ENV_KV_CACHE_STORE_PORT = "KV_CACHE_STORE_PORT"
ENV_KV_STORE_EVICTION_HIGH_WATERMARK_RATIO = "KV_STORE_EVICTION_HIGH_WATERMARK_RATIO"
ENV_KV_STORE_EVICTION_RATIO = "KV_STORE_EVICTION_RATIO"
ENV_DEFAULT_KV_LEASE_TTL = "DEFAULT_KV_LEASE_TTL"
ENV_KV_STORE_BACKEND = "KV_STORE_BACKEND"
# memcache MetaService env vars
ENV_MMC_CONFIG_STORE_URL = "MMC_CONFIG_STORE_URL"
ENV_MMC_METRICS_URL = "MMC_METRICS_URL"
ENV_MMC_LOCAL_CONFIG_PATH = "MMC_LOCAL_CONFIG_PATH"
DEFAULT_MMC_LOCAL_CONFIG_PATH = "/usr/local/Ascend/pyMotor/conf/mmc-local.conf"
ENV_MMC_LOCAL_SERVICE_MODE = "MMC_LOCAL_SERVICE_MODE"
MMC_LOCAL_SERVICE_CONFIG_KEY = "local_service_mode"
MMC_DRAM_SIZE_CONFIG_KEY = "dram_size"
ENV_MMC_DRAM_SIZE = "MMC_DRAM_SIZE"
ENV_DISAGGREGATION_BOOTSTRAP_PORT = "DISAGGREGATION_BOOTSTRAP_PORT"
ENV_ASCEND_MF_STORE_URL = "ASCEND_MF_STORE_URL"
ENV_ASCEND_MF_STORE_PORT = "ASCEND_MF_STORE_PORT"
ENV_ASCEND_MF_TRANSFER_PROTOCOL = "ASCEND_MF_TRANSFER_PROTOCOL"
ENV_SGLANG_HOST_IP = "SGLANG_HOST_IP"

ENV_ENGINE_TYPE = "ENGINE_TYPE"
ENV_SERVICE_ID = "SERVICE_ID"
ENV_NORTH_PLATFORM = "NORTH_PLATFORM"
ENV_MODEL_NAME = "MODEL_NAME"

VOLUMES = "volumes"
VOLUME_MOUNTS = "volumeMounts"
PATH = "path"
WEIGHT_MOUNT_PATH = "weight_mount_path"

MOTOR_DEPLOY_CONFIG = "motor_deploy_config"
MOTOR_ENGINE_PREFILL_CONFIG = "motor_engine_prefill_config"
MOTOR_ENGINE_UNION_CONFIG = "motor_engine_union_config"
ENGINE_CONFIG = "engine_config"
KV_TRANSFER_CONFIG = "kv_transfer_config"
KV_CONNECTOR = "kv_connector"
MULTI_CONNECTOR = "MultiConnector"

PORTS = "ports"
PORT = "port"
TARGET_PORT = "targetPort"
MOUNT_PATH = "mountPath"
DEFAULT_WEIGHT_MOUNT_PATH = "/mnt/weight"
JOB_NAME = "job-name"

# Engine pod storage: a dynamically-provisioned PVC (motor_deploy_config.storage) mounted into
# engine pods, plus an independent /dev/shm sizing knob (motor_deploy_config.dshm_size).
# Used by UCM storage_backends but not UCM-specific.
# k8s volume primitives
PERSISTENT_VOLUME_CLAIM = "persistentVolumeClaim"
CLAIM_NAME = "claimName"
EMPTY_DIR = "emptyDir"
SIZE_LIMIT = "sizeLimit"
STORAGE_CLASS_NAME = "storageClassName"  # k8s PVC field
ACCESS_MODES = "accessModes"  # k8s PVC field
DSHM_VOLUME = "dshm"
# motor_deploy_config.storage sub-keys. Each entry MUST declare a type:
#   "pvc"      — a PVC: dynamically provisioned via storage_class_name, or an existing claim
#                referenced by claim_name (mounted as-is; no PVC object is generated)
#   "nfs"      — k8s-native NFS volume (server+path; no PVC / StorageClass required)
#   "hostpath" — node-local hostPath (path; cross-node sharing only if that path is itself a
#                shared mount, e.g. an identically-mounted NFS export, on every node)
# Volume names (and generated-PVC names) are always auto-derived (mindie-motor-store-<i>) —
# not user-configurable; claim_name entries mount the named existing claim instead.
STORAGE = "storage"
STORAGE_ENABLE = "enable"
STORAGE_TYPE = "type"
STORAGE_TYPE_PVC = "pvc"
STORAGE_TYPE_NFS = "nfs"
STORAGE_TYPE_HOSTPATH = "hostpath"
STORAGE_CLASS = "storage_class_name"
STORAGE_CLAIM_NAME = "claim_name"
STORAGE_SIZE = "size"
STORAGE_MOUNT_PATH = "mount_path"
STORAGE_ACCESS_MODE = "access_mode"
NFS_SERVER = "server"
STORAGE_HOST_PATH_TYPE = "host_path_type"
STORAGE_READ_ONLY = "read_only"
K8S_READ_ONLY = "readOnly"
# motor_deploy_config.dshm_size — independent /dev/shm emptyDir sizeLimit knob
DSHM_SIZE = "dshm_size"
# defaults (volume name == PVC name, auto-derived per entry index)
DEFAULT_STORAGE_NAME = "mindie-motor-store"
DEFAULT_STORAGE_MOUNT_PATH = "/mnt/store"
DEFAULT_STORAGE_SIZE = "200Gi"
DEFAULT_STORAGE_ACCESS_MODE = "ReadWriteMany"
ROLES = "roles"
SERVICES = "services"
KIND_KEY = "kind"

# ---------------------------------------------------------------------------
# TUI ANSI style constants
# ---------------------------------------------------------------------------


class Style:
    """ANSI escape sequences for TUI colors, formatting, and box-drawing."""

    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    BLINK = '\033[5m'
    REVERSE = '\033[7m'

    # 8-bit foreground colours
    CYAN = '\033[38;5;51m'
    GREEN = '\033[38;5;82m'
    YELLOW = '\033[38;5;226m'
    RED = '\033[38;5;196m'
    WHITE = '\033[38;5;15m'
    GRAY = '\033[38;5;245m'
    BLUE = '\033[38;5;39m'
    ORANGE = '\033[38;5;214m'
    MAGENTA = '\033[38;5;201m'
    BLACK = '\033[38;5;16m'

    # 8-bit background colours
    BG_GREEN = '\033[48;5;22m'
    BG_YELLOW = '\033[48;5;58m'
    BG_RED = '\033[48;5;52m'
    BG_BLUE = '\033[48;5;24m'
    BG_SELECTED = '\033[48;5;236m'

    # Box-drawing glyphs (single)
    H = '─'
    V = '│'
    TL = '┌'
    TR = '┐'
    BL = '└'
    BR = '┘'
    LT = '├'
    RT = '┤'

    # Box-drawing glyphs (double)
    DH = '═'
    DV = '║'
    DTL = '╔'
    DTR = '╗'
    DBL = '╚'
    DBR = '╝'
    DLT = '╠'
    DRT = '╣'


# ---------------------------------------------------------------------------
# TUI timing & sizing constants
# ---------------------------------------------------------------------------

# Poll interval for key input (seconds)
KEY_POLL_INTERVAL = 0.1
# How often to check for pods during waiting phase (seconds)
POD_WAIT_INTERVAL = 2
# How long status messages stay visible (seconds)
STATUS_DURATION = 1.5
# How long confirmation prompts stay active (seconds)
CONFIRM_DURATION = 3.0
# How long the menu-item flash lasts after activation (seconds)
FLASH_DURATION = 1.2
# Minimum box width
MIN_BOX_WIDTH = 88
# Maximum box width
MAX_BOX_WIDTH = 140
