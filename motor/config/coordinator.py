# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import json
import re
import ipaddress
import tempfile
from typing import Optional, Any
from enum import Enum
from dataclasses import dataclass, field, asdict, is_dataclass

from motor.common.logger import get_logger
from motor.common.utils.env import Env
from motor.common.http.key_encryption import set_default_key_encryption_by_name
from motor.config.etcd import EtcdConfig
from motor.config.log_config import LoggingConfig
from motor.config.standby import StandbyConfig
from motor.config.tls_config import TLSConfig
from motor.config.config_utils import (
    ConfigKey,
    apply_config_path_metadata,
    apply_standby_persistence_rule,
    finalize_json_config_load,
    init_motor_config,
    log_json_config_load_error,
    reload_dataclass_config_from_json,
    resolve_config_json_path,
    save_instance_config_to_json,
    _update_tls_config,
    _update_instances_num,
    _update_prefill_kv_event_config,
    MGMT_TLS_CONFIG,
    INFER_TLS_CONFIG,
    ETCD_TLS_CONFIG,
)
from motor.config.resolver import ConfigResolver, normalize_keys
from motor.config.port_allocator_config import PortAllocatorConfig

FILE_ENCODING = "utf-8"

AIGW = "aigw"
ENGINE_CONFIG = "engine_config"
MAX_MODEL_LEN = "max_model_len"
AIGW_ID = "id"
AIGW_OBJECT = "object"
AIGW_OWNED_BY = "owned_by"
AIGW_OBJECT_MODEL = "model"
AIGW_OWNED_BY_MOTOR = "motor"
AIGW_P_MAX_SEQLEN = "p_max_seqlen"
AIGW_D_MAX_SEQLEN = "d_max_seqlen"
SLO_TTFT = "slo_ttft"
SLO_TPOT = "slo_tpot"

logger = get_logger(__name__)

# Role shm and heartbeat (Coordinator Daemon liveness).
# Not configurable; use these constants so Daemon and Mgmt stay in sync.
ROLE_SHM_NAME = "coordinator_standby_role"
ROLE_SHM_SIZE = 9  # 1 byte role (byte0) + 8 bytes heartbeat (bytes 1-8, little-endian uint64)
ROLE_SHM_MASTER = 1  # byte0 value when this node is master
ROLE_SHM_STANDBY = 0  # byte0 value when standby or unknown
ROLE_HEARTBEAT_INTERVAL_SEC = 2.0
ROLE_HEARTBEAT_STALE_SEC = 5.0


def _default_skip_paths() -> set[str]:
    return {
        "/",
        "/startup",
        "/readiness",
        "/liveness",
        "/metrics",
        "/instances/refresh",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
    }


def _default_rate_limit_skip_paths() -> list[str]:
    return [
        "/liveness",
        "/readiness",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
        "/startup",
    ]


def _merge_kv_store_metrics_config(cfg: dict, raw: dict) -> None:
    """Populate ``prometheus_metrics_config`` defaults from ``kv_cache_store_config``.

    Only fills fields that are NOT already explicitly set in the prometheus config.
    Called during ``CoordinatorConfig.from_json()``, before the dict is applied to
    the dataclass.
    """
    kv = raw.get("kv_cache_store_config", {}) if isinstance(raw, dict) else {}
    if not isinstance(kv, dict) or not kv:
        return
    pm = cfg.setdefault("prometheus_metrics_config", {})
    pm.setdefault("kv_store_backend", kv.get("backend", ""))
    pm.setdefault(
        "kv_store_service",
        kv.get("service", "") or os.getenv("KVS_MASTER_SERVICE", ""),
    )
    pm.setdefault("kv_store_metrics_port", kv.get("metrics_port", 0) or int(os.getenv("KV_STORE_METRICS_PORT", "0")))
    pm.setdefault(
        "kv_store_metrics_endpoint",
        os.getenv("KV_STORE_METRICS_URL", ""),
    )
    if not pm.get("enable_kv_store_metrics"):
        # Auto-enable when an explicit kv_cache_store_config is present
        pm["enable_kv_store_metrics"] = True


class SchedulerType(Enum):
    LOAD_BALANCE = "load_balance"
    ROUND_ROBIN = "round_robin"
    KV_CACHE_AFFINITY = "kv_cache_affinity"

    @classmethod
    def from_string(cls, value: str) -> Optional["SchedulerType"]:
        """Convert string to SchedulerType enum."""
        try:
            return cls[value.upper()]
        except (KeyError, AttributeError):
            logger.warning("Invalid scheduler type: %s", value)
            return None


# Sub-strategy selected by SchedulerConfig.kv_affinity_mode when scheduler_type=kv_cache_affinity.
#   "unified"    - single score fusing affinity and live load (default).
#   "load_gated" - keep the N least-loaded endpoints, then pick the longest cached prefix.
KV_AFFINITY_MODE_UNIFIED = "unified"
KV_AFFINITY_MODE_LOAD_GATED = "load_gated"
KV_AFFINITY_MODES = (KV_AFFINITY_MODE_UNIFIED, KV_AFFINITY_MODE_LOAD_GATED)


@dataclass
class SchedulerConfig:
    scheduler_type: SchedulerType = field(default=SchedulerType.LOAD_BALANCE)
    enable_pd_separation_fallback_to_hybrid: bool = True
    # Weight of the instance average workload in endpoint-first load balancing.
    # 0 means pure global endpoint minimum; small values preserve instance pressure awareness.
    endpoint_instance_score_weight: float = 0.05
    # --- kv_cache_affinity tunables (affinity + load) ---
    # Which kv_cache_affinity sub-strategy to use (see KV_AFFINITY_MODES):
    #   "unified"    - single score fusing affinity and live load, pick the minimum (default).
    #   "load_gated" - keep the N least-loaded endpoints, then pick the longest cached prefix.
    kv_affinity_mode: str = KV_AFFINITY_MODE_UNIFIED
    # Weight of an endpoint's live workload in the "unified" score. 1.0 puts load on equal footing
    # with the affinity-discounted prefill cost; 0 makes the unified score affinity-only (longest
    # prefix wins, load-blind).
    kv_affinity_load_weight: float = 1.0
    # How much a cached prefix discounts prefill work (default 1.0).
    kv_affinity_overlap_credit: float = 1.0
    # Weight of the (affinity-discounted) prefill cost in the unified score (default 1.0).
    kv_affinity_prefill_load_scale: float = 1.0
    # Number of least-loaded endpoints kept by the "load_gated" mode before the affinity
    # tie-break. Only used when kv_affinity_mode="load_gated"; 0 (default) falls back to 2.
    kv_affinity_load_gate_topn: int = 0


@dataclass
class PrometheusMetricsConfig:
    """Prometheus metrics configuration class"""

    reuse_time: int = 3
    # --- KV store metrics (auto-populated from kv_cache_store_config) ---
    enable_kv_store_metrics: bool = False
    kv_store_metrics_endpoint: str = ""
    kv_store_backend: str = ""  # e.g. "memcache", "mooncake"
    kv_store_service: str = ""  # default falls back to $KVS_MASTER_SERVICE
    kv_store_metrics_port: int = 0  # 0 → auto: 50088 (mooncake) / 50090 (default)


@dataclass
class ExceptionConfig:
    """Exception handling configuration class"""

    max_retry: int = 5
    # Cache token IDs so a streaming request can be rescheduled after a transient transport failure.
    # Engine-side recompute is independent of this switch.
    reschedule_enabled: bool = True
    transport_max_retry: int | None = None
    retry_delay: float = 0.2
    first_token_timeout: int = 600  # 10 minutes
    infer_timeout: int = 3600  # 60 minutes
    connect_timeout: float = 5.0  # TCP connect phase only; set 0 to inherit first_token_timeout
    upstream_error_body_max_bytes: int = 64 * 1024

    @property
    def transport_retry_limit(self) -> int:
        return self.transport_max_retry if self.transport_max_retry is not None else self.max_retry

    @property
    def recompute_enabled(self) -> bool:
        """Deprecated compatibility alias for ``reschedule_enabled``."""
        return self.reschedule_enabled

    @recompute_enabled.setter
    def recompute_enabled(self, value: bool) -> None:
        self.reschedule_enabled = value


@dataclass
class TokenSamplingConfig:
    """Periodic token-ID and logprob sampling per PD instance group.

    For each PD instance group (keyed by D instance ID or P+D instance ID pair),
    at most one full request's token_ids and logprobs are sampled within
    interval_seconds for precision detection reporting.
    When precision_check_enabled=False, no sampling or request modification
    occurs — zero performance overhead.
    """

    interval_seconds: float = 30.0  # Sampling interval per PD instance group (seconds)
    logprobs_count: int = 1  # Number of top_logprobs (chat) / logprobs (completion) injected during sampling
    # Also determines the detection types enabled for msprobe:
    # 1 → repetition; >=3 → +garbled; >=5 → +rare characters (requires multiple keys)
    precision_check_enabled: bool = False  # Master switch: enables sampling + precision detection/probe/alarm
    precision_issue_threshold: int = 10  # Consecutive precision anomaly count before triggering probe and alarm
    probe_max_attempts: int = 3  # Number of probe attempts
    probe_timeout_seconds: float = (
        600.0  # Single probe request timeout (seconds); no extra interval between consecutive probes
    )


@dataclass
class TimeoutConfig:
    request_timeout: int = 30
    connection_timeout: int = 10
    read_timeout: int = 15
    write_timeout: int = 15
    keep_alive_timeout: int = 60


@dataclass
class APIKeyConfig:
    enable_api_key: bool = False
    valid_keys: set[str] = field(default_factory=set)
    header_name: str = "Authorization"
    key_prefix: str = "Bearer "
    skip_paths: set[str] = field(default_factory=_default_skip_paths)
    encryption_algorithm: str = "PBKDF2_SHA256"  # Encryption algorithm to use

    def __post_init__(self):
        """Initialize encryption algorithm after dataclass creation"""
        self._setup_encryption()

    def _setup_encryption(self):
        """Setup the encryption algorithm based on configuration"""

        try:
            set_default_key_encryption_by_name(self.encryption_algorithm)
            logger.info("Using encryption algorithm: %s", self.encryption_algorithm)
        except ValueError as e:
            logger.error("Invalid encryption algorithm: %s", e)
            raise ValueError(f"Invalid encryption algorithm '{self.encryption_algorithm}': {e}") from e


@dataclass
class InferenceWorkersConfig:
    num_workers: int = 4  # Number of inference API worker processes; >1 = multiprocess


@dataclass
class SchedulerProcessConfig:
    """Scheduler process configuration (default only; not user-configurable in first version)."""

    ipc_dir: str = ""  # Base dir for IPC sockets; empty => system temp directory.
    timeout: float = 5.0  # Client request timeout (seconds)
    reconnect_interval: float = 5.0  # Client reconnect interval (seconds)

    def _resolved_ipc_base(self) -> str:
        return (self.ipc_dir or tempfile.gettempdir()).rstrip("/")

    @property
    def frontend_address(self) -> str:
        """IPC address for ROUTER (API Server <-> Scheduler). Derived from ipc_dir."""
        return f"ipc://{self._resolved_ipc_base()}/scheduler_frontend"

    @property
    def instance_pub_address(self) -> str:
        """IPC address for instance-change PUB. Derived from ipc_dir."""
        return f"ipc://{self._resolved_ipc_base()}/scheduler_instance_pub"


# First version: single default instance used by all scheduler process / client code.
DEFAULT_SCHEDULER_PROCESS_CONFIG = SchedulerProcessConfig()


@dataclass
class RateLimitConfig:
    """Rate limiting configuration class"""

    enable_rate_limit: bool = False
    provider: str = "simple"

    max_requests: int = 1000
    window_size: int = 60
    scope: str = "global"
    skip_paths: list[str] = field(default_factory=_default_rate_limit_skip_paths)
    error_message: str = "too many requests, please try again later"
    error_status_code: int = 429

    olc_config_path: str = ""


@dataclass
class ApiConfig:
    """API configuration class"""

    # coordinator API configuration
    coordinator_api_host: str = field(default_factory=lambda: Env.pod_ip or "127.0.0.1")
    coordinator_api_dns: str = field(default_factory=lambda: Env.coordinator_service or "127.0.0.1")
    coordinator_api_infer_dns: str = field(
        default_factory=lambda: Env.coordinator_infer_service or Env.coordinator_service or "127.0.0.1"
    )
    coordinator_api_obs_dns: str = field(
        default_factory=lambda: Env.coordinator_obs_service or Env.coordinator_service or "127.0.0.1"
    )
    coordinator_api_infer_port: int = 1025
    coordinator_api_mgmt_port: int = 1026
    coordinator_obs_port: int = 1027


@dataclass
class DeployConfig:
    """Deploy configuration class"""

    p_instances_num: int = 1
    d_instances_num: int = 1
    hybrid_instances_num: int | None = None
    single_hybrid_instance_pod_num: int | None = None
    hybrid_pod_npu_num: int | None = None


@dataclass
class TracerConfig:
    """Tracer configuration class"""

    endpoint: str = ""
    root_sampling_rate: float = 1.0
    remote_parent_sampled: float = 1.0
    remote_parent_not_sampled: float = 1.0
    local_parent_sampled: float = 1.0
    local_parent_not_sampled: float = 1.0


@dataclass
class PrefillKvEventConfig:
    """
    Prefill kv event configuration class
    If the value of conductor_service is empty, the kv conductor is disabled.
    """

    conductor_service: str = field(default_factory=lambda: Env.conductor_service or "")
    http_server_port: int = 13333
    block_size: int = 128
    endpoint: str = ""
    replay_endpoint: str = ""
    engine_type: str = "vLLM"
    model_path: str = ""
    re_register_interval_sec: int = 0


@dataclass
class CoordinatorConfig:
    """Coordinator configuration class with validation, reload and error handling support"""

    logging_config: LoggingConfig = field(default_factory=LoggingConfig)
    prometheus_metrics_config: PrometheusMetricsConfig = field(default_factory=PrometheusMetricsConfig)
    exception_config: ExceptionConfig = field(default_factory=ExceptionConfig)
    scheduler_config: SchedulerConfig = field(default_factory=SchedulerConfig)
    inference_workers_config: InferenceWorkersConfig = field(default_factory=InferenceWorkersConfig)
    infer_tls_config: TLSConfig = field(default_factory=TLSConfig)
    mgmt_tls_config: TLSConfig = field(default_factory=TLSConfig)
    etcd_tls_config: TLSConfig = field(default_factory=TLSConfig)
    timeout_config: TimeoutConfig = field(default_factory=TimeoutConfig)
    api_key_config: APIKeyConfig = field(default_factory=APIKeyConfig)
    rate_limit_config: RateLimitConfig = field(default_factory=RateLimitConfig)
    standby_config: StandbyConfig = field(default_factory=StandbyConfig)

    etcd_config: EtcdConfig = field(default_factory=EtcdConfig)
    aigw_model: dict[str, Any] | None = None
    api_config: ApiConfig = field(default_factory=ApiConfig)
    deploy_config: DeployConfig = field(default_factory=DeployConfig)
    tracer_config: TracerConfig = field(default_factory=TracerConfig)
    prefill_kv_event_config: PrefillKvEventConfig = field(default_factory=PrefillKvEventConfig)
    token_sampling_config: TokenSamplingConfig = field(default_factory=TokenSamplingConfig)
    port_allocator_config: PortAllocatorConfig = field(default_factory=PortAllocatorConfig)

    # internal fields
    config_path: str | None = field(default=None, init=False)
    last_modified: float | None = field(default=None, init=False)
    _errors: list[str] = field(default_factory=list, init=False)
    worker_index: int | None = field(default=None, repr=False)

    def __post_init__(self):
        """Validate configuration after initialization"""
        init_motor_config(self, "coordinator")

    @classmethod
    def from_json(cls, json_path: str = None) -> "CoordinatorConfig":
        """Load configuration from JSON file"""
        json_path, config_path = resolve_config_json_path(json_path)

        cfg = {}
        user_config_data = None
        try:
            if config_path and config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:  # Only parse if file is not empty
                        raw = json.loads(content)
                        if isinstance(raw, dict) and "motor_coordinator_config" in raw:
                            user_config_data = raw
                            cfg = raw.get("motor_coordinator_config", {})
                        else:
                            cfg = raw
                        tls_configs = [
                            MGMT_TLS_CONFIG,
                            INFER_TLS_CONFIG,
                            ETCD_TLS_CONFIG,
                        ]
                        _update_tls_config(tls_configs, cfg, raw)
                        _update_instances_num(cfg, raw)
                        _update_prefill_kv_event_config(cfg, raw)
                        _merge_kv_store_metrics_config(cfg, raw)
        except (json.JSONDecodeError, Exception) as e:
            log_json_config_load_error(json_path, e)

        try:
            config = cls()

            def update_config_from_dict(config_obj, config_dict, special_handlers=None):
                """Update configuration object fields from dictionary, only for existing keys.
                Nested dicts are merged into existing dataclass attributes (e.g. inference_workers_config).
                """
                for key, value in config_dict.items():
                    if special_handlers and key in special_handlers:
                        special_handlers[key](config_obj, key, value)
                    elif hasattr(config_obj, key):
                        existing = getattr(config_obj, key)
                        if is_dataclass(existing) and isinstance(value, dict):
                            update_config_from_dict(existing, value, special_handlers)
                        else:
                            setattr(config_obj, key, value)

            def set_enum_field(obj, key, value, enum_class):
                """Set enum field value from string"""
                if isinstance(value, str):
                    enum_value = enum_class.from_string(value)
                    if enum_value is not None:
                        setattr(obj, key, enum_value)

            scheduler_handlers = {
                'scheduler_type': lambda obj, key, value: set_enum_field(obj, key, value, SchedulerType),
            }

            exception_config_data = cfg.get("exception_config", {})

            def set_deprecated_recompute_enabled(obj, _key, value):
                if "reschedule_enabled" in exception_config_data:
                    logger.warning(
                        "exception_config.recompute_enabled is deprecated and ignored because "
                        "reschedule_enabled is also configured"
                    )
                    return
                logger.warning(
                    "exception_config.recompute_enabled is deprecated; use reschedule_enabled. "
                    "Engine-side recompute is not controlled by Coordinator."
                )
                obj.reschedule_enabled = value

            def ignore_removed_recompute_retry(_obj, _key, _value):
                logger.warning(
                    "exception_config.recompute_max_retry is no longer supported and is ignored; "
                    "Coordinator does not perform engine recompute"
                )

            exception_handlers = {
                "recompute_enabled": set_deprecated_recompute_enabled,
                "recompute_max_retry": ignore_removed_recompute_retry,
            }

            # Build AIGW model metadata from engine configs.
            # This runs whenever engine sections are present, regardless of
            # whether the user wrote an "aigw" key in motor_coordinator_config.
            if user_config_data:
                try:
                    prefill = user_config_data.get(ConfigKey.MOTOR_ENGINE_PREFILL.value)
                    decode = user_config_data.get(ConfigKey.MOTOR_ENGINE_DECODE.value)
                    if prefill and decode:
                        if AIGW not in cfg:
                            cfg[AIGW] = {}
                        prefill_resolver = ConfigResolver(prefill)
                        cfg[AIGW][AIGW_ID] = prefill_resolver.get_model_name("")
                        cfg[AIGW][AIGW_OBJECT] = AIGW_OBJECT_MODEL
                        cfg[AIGW][AIGW_OWNED_BY] = AIGW_OWNED_BY_MOTOR
                        prefill_engine = normalize_keys(prefill[ENGINE_CONFIG])
                        decode_engine = normalize_keys(decode[ENGINE_CONFIG])

                        if MAX_MODEL_LEN not in prefill_engine:
                            raise KeyError(
                                f"'{MAX_MODEL_LEN}' not found in prefill engine_config. "
                                f"Available keys: {sorted(prefill_engine.keys())}"
                            )
                        if MAX_MODEL_LEN not in decode_engine:
                            raise KeyError(
                                f"'{MAX_MODEL_LEN}' not found in decode engine_config. "
                                f"Available keys: {sorted(decode_engine.keys())}"
                            )

                        cfg[AIGW][AIGW_P_MAX_SEQLEN] = prefill_engine[MAX_MODEL_LEN]
                        cfg[AIGW][AIGW_D_MAX_SEQLEN] = decode_engine[MAX_MODEL_LEN]
                        cfg[AIGW].setdefault(SLO_TTFT, 1000)
                        cfg[AIGW].setdefault(SLO_TPOT, 50)
                except Exception as e:
                    logger.warning("Failed to build aigw model metadata: %s", e)

            # Update configuration sections if they exist in JSON
            config_mappings = [
                ("logging_config", config.logging_config, None),
                ("prometheus_metrics_config", config.prometheus_metrics_config, None),
                ("exception_config", config.exception_config, exception_handlers),
                ("scheduler_config", config.scheduler_config, scheduler_handlers),
                ("inference_workers_config", config.inference_workers_config, None),
                ("timeout_config", config.timeout_config, None),
                ("api_key_config", config.api_key_config, None),
                ("rate_limit_config", config.rate_limit_config, None),
                ("standby_config", config.standby_config, None),
                ("etcd_config", config.etcd_config, None),
                ("infer_tls_config", config.infer_tls_config, None),
                ("mgmt_tls_config", config.mgmt_tls_config, None),
                ("etcd_tls_config", config.etcd_tls_config, None),
                ("api_config", config.api_config, None),
                ("deploy_config", config.deploy_config, None),
                ("tracer_config", config.tracer_config, None),
                ("prefill_kv_event_config", config.prefill_kv_event_config, None),
                ("token_sampling_config", config.token_sampling_config, None),
                ("port_allocator_config", config.port_allocator_config, None),
            ]

            for section_name, config_obj, special_handlers in config_mappings:
                if section_name in cfg:
                    update_config_from_dict(config_obj, cfg[section_name], special_handlers)

            if "aigw" in cfg:
                config.aigw_model = dict(cfg["aigw"])

            apply_config_path_metadata(config, config_path)

            # Auto-enable etcd persistence when master/standby is enabled
            apply_standby_persistence_rule(config)

            # Re-validate configuration after applying values from JSON
            config.validate_config()

            finalize_json_config_load(
                config_path,
                no_path_message="No configuration file specified, using default configuration",
            )

            return config

        except Exception as e:
            logger.error("Failed to create configuration instance: %s", e)
            raise

    def validate_config(self) -> None:
        """Validate the validity of configuration values"""
        self._errors = []

        # Validate logging configuration
        valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
        if self.logging_config.log_level.upper() not in valid_log_levels:
            self._errors.append(f"log_level must be one of: {', '.join(valid_log_levels)}")

        self._validate_positive_number(self.logging_config.log_max_line_length, "log_max_line_length")

        # Validate timeout configuration
        self._validate_positive_number(self.timeout_config.request_timeout, "request_timeout")
        self._validate_positive_number(self.timeout_config.connection_timeout, "connection_timeout")
        self._validate_positive_number(self.timeout_config.read_timeout, "read_timeout")
        self._validate_positive_number(self.timeout_config.write_timeout, "write_timeout")
        self._validate_positive_number(self.timeout_config.keep_alive_timeout, "keep_alive_timeout")

        # Validate exception configuration
        self._validate_positive_number(self.exception_config.max_retry, "max_retry", allow_zero=True)
        if self.exception_config.transport_max_retry is not None:
            self._validate_positive_number(
                self.exception_config.transport_max_retry,
                "transport_max_retry",
                allow_zero=True,
            )
        self._validate_positive_number(self.exception_config.retry_delay, "retry_delay")
        self._validate_positive_number(self.exception_config.first_token_timeout, "first_token_timeout")
        self._validate_positive_number(self.exception_config.infer_timeout, "infer_timeout")
        self._validate_positive_number(self.exception_config.connect_timeout, "connect_timeout", allow_zero=True)
        self._validate_positive_number(
            self.exception_config.upstream_error_body_max_bytes,
            "upstream_error_body_max_bytes",
            allow_zero=True,
        )

        # Validate tracer_config configuration
        self._validate_positive_number(self.tracer_config.root_sampling_rate, "root_sampling_rate")
        self._validate_positive_number(self.tracer_config.remote_parent_sampled, "remote_parent_sampled")
        self._validate_positive_number(self.tracer_config.remote_parent_not_sampled, "remote_parent_not_sampled")
        self._validate_positive_number(self.tracer_config.local_parent_sampled, "local_parent_sampled")
        self._validate_positive_number(self.tracer_config.local_parent_not_sampled, "local_parent_not_sampled")

        # Validate HTTP configuration
        self._validate_port_range(self.api_config.coordinator_api_infer_port, "coordinator_api_infer_port")
        self._validate_port_range(self.api_config.coordinator_api_mgmt_port, "coordinator_api_mgmt_port")
        self._validate_positive_number(
            self.inference_workers_config.num_workers,
            "num_workers",
        )

        # Validate scheduler score configuration
        self._validate_positive_number(
            self.scheduler_config.endpoint_instance_score_weight,
            "endpoint_instance_score_weight",
            allow_zero=True,
        )
        self._validate_positive_number(
            self.scheduler_config.kv_affinity_load_weight,
            "kv_affinity_load_weight",
            allow_zero=True,
        )
        self._validate_positive_number(
            self.scheduler_config.kv_affinity_overlap_credit,
            "kv_affinity_overlap_credit",
            allow_zero=True,
        )
        self._validate_positive_number(
            self.scheduler_config.kv_affinity_prefill_load_scale,
            "kv_affinity_prefill_load_scale",
            allow_zero=True,
        )
        self._validate_positive_number(
            self.scheduler_config.kv_affinity_load_gate_topn,
            "kv_affinity_load_gate_topn",
            allow_zero=True,
        )
        if self.scheduler_config.kv_affinity_mode not in KV_AFFINITY_MODES:
            self._errors.append(
                f"kv_affinity_mode must be one of {KV_AFFINITY_MODES}, got {self.scheduler_config.kv_affinity_mode!r}"
            )

        # Validate host address
        self._validate_ip_or_hostname(self.api_config.coordinator_api_host, "coordinator_api_host")

        # Validate rate limit configuration
        self._validate_positive_number(self.rate_limit_config.max_requests, "max_requests")
        self._validate_positive_number(self.rate_limit_config.window_size, "window_size")

        if not (100 <= self.rate_limit_config.error_status_code <= 599):
            self._errors.append("error_status_code must be in range 100-599")

        if self.rate_limit_config.provider not in ("simple", "olc"):
            self._errors.append(
                f"rate_limit_config.provider must be 'simple' or 'olc', got '{self.rate_limit_config.provider}'"
            )

        if self.rate_limit_config.enable_rate_limit and self.rate_limit_config.provider == "olc":
            if not self.rate_limit_config.olc_config_path:
                self._errors.append("rate_limit_config.olc_config_path is required when provider is 'olc'")
            elif not os.path.isdir(self.rate_limit_config.olc_config_path):
                self._errors.append(
                    f"rate_limit_config.olc_config_path does not exist: {self.rate_limit_config.olc_config_path}"
                )

        # Validate Prometheus metrics configuration
        self._validate_positive_number(self.prometheus_metrics_config.reuse_time, "reuse_time")

        # Validate standby configuration
        self._validate_positive_number(
            self.standby_config.master_standby_check_interval,
            "master_standby_check_interval",
        )
        self._validate_positive_number(self.standby_config.master_lock_ttl, "master_lock_ttl")
        self._validate_positive_number(self.standby_config.master_lock_retry_interval, "master_lock_retry_interval")
        self._validate_positive_number(
            self.standby_config.master_lock_max_failures,
            "master_lock_max_failures",
            allow_zero=True,
        )

        # Validate master lock key path
        self._validate_endpoint_path(self.standby_config.master_lock_key, "master_lock_key")

        # Validate ETCD configuration
        self._validate_port_range(self.etcd_config.etcd_port, "etcd_port")
        self._validate_positive_number(self.etcd_config.etcd_timeout, "etcd_timeout")
        self._validate_ip_or_hostname(self.etcd_config.etcd_host, "etcd_host")

        # Validate token_sampling_config (fields always validated for positive values)
        self._validate_positive_number(
            self.token_sampling_config.interval_seconds, "token_sampling_config.interval_seconds"
        )
        self._validate_positive_number(
            self.token_sampling_config.logprobs_count, "token_sampling_config.logprobs_count"
        )
        self._validate_positive_number(
            self.token_sampling_config.precision_issue_threshold, "token_sampling_config.precision_issue_threshold"
        )
        self._validate_positive_number(
            self.token_sampling_config.probe_max_attempts, "token_sampling_config.probe_max_attempts"
        )
        self._validate_positive_number(
            self.token_sampling_config.probe_timeout_seconds, "token_sampling_config.probe_timeout_seconds"
        )

        # Note: TLS certificate file validation is handled by the TLS configuration's check_files flag
        # and is performed during TLS handshake, not during configuration validation

        # Validate API key configuration
        if self.api_key_config.enable_api_key:
            if not self.api_key_config.valid_keys:
                self._errors.append("valid_keys cannot be empty when api_key authentication is enabled")
            if not self.api_key_config.header_name:
                self._errors.append("header_name cannot be empty when api_key authentication is enabled")
            if not self.api_key_config.key_prefix:
                self._errors.append("key_prefix cannot be empty when api_key authentication is enabled")

        if self._errors:
            error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {error}" for error in self._errors)
            logger.error(error_msg)
            raise ValueError(error_msg)

    def get_aigw_models(self) -> dict[str, Any] | None:
        """Return configured AIGW model."""
        return self.aigw_model

    def reload(self) -> bool:
        """Reload configuration file"""
        return reload_dataclass_config_from_json(
            self,
            self.from_json,
            skip=frozenset({"worker_index", "worker_metaserver_port"}),
            skip_private=True,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to dictionary with grouped structure"""

        # Use dataclasses.asdict to automatically serialize all config objects
        config_dict = asdict(self)

        # Remove internal fields that shouldn't be in the output
        config_dict.pop("config_path", None)
        config_dict.pop("last_modified", None)

        # Convert enums to their string values for JSON serialization
        if 'scheduler_config' in config_dict:
            scheduler_config = config_dict['scheduler_config']
            if 'scheduler_type' in scheduler_config and isinstance(scheduler_config['scheduler_type'], SchedulerType):
                scheduler_config['scheduler_type'] = scheduler_config['scheduler_type'].value
        # Convert sets to lists for JSON serialization
        if "api_key_config" in config_dict:
            api_key_config = config_dict["api_key_config"]
            if "valid_keys" in api_key_config and isinstance(api_key_config["valid_keys"], set):
                api_key_config["valid_keys"] = list(api_key_config["valid_keys"])
            if "skip_paths" in api_key_config and isinstance(api_key_config["skip_paths"], set):
                api_key_config["skip_paths"] = list(api_key_config["skip_paths"])

        return config_dict

    def save_to_json(self, json_path: str | None = None) -> bool:
        """Save configuration to JSON file"""
        return save_instance_config_to_json(
            self,
            json_path,
            config_key=ConfigKey.MOTOR_COORDINATOR,
            file_encoding=FILE_ENCODING,
            component_name="coordinator",
        )

    def get_config_summary(self) -> str:
        """Get configuration summary information"""
        separator = "=" * 80
        title = " " * 20 + "Coordinator Configuration Summary"
        etcd_host = self.etcd_config.etcd_host
        etcd_port = self.etcd_config.etcd_port
        etcd_timeout = self.etcd_config.etcd_timeout
        master_standby_check_interval = self.standby_config.master_standby_check_interval
        master_lock_ttl = self.standby_config.master_lock_ttl
        master_lock_key = self.standby_config.master_lock_key
        deploy_summary = (
            f"    ├─ p_instances_num:     {self.deploy_config.p_instances_num}\n"
            f"    └─ d_instances_num:     {self.deploy_config.d_instances_num}\n"
        )
        if self.deploy_config.hybrid_instances_num is not None:
            deploy_summary = (
                f"    ├─ p_instances_num:        {self.deploy_config.p_instances_num}\n"
                f"    ├─ d_instances_num:        {self.deploy_config.d_instances_num}\n"
                f"    ├─ hybrid_instances_num:   {self.deploy_config.hybrid_instances_num}\n"
                f"    ├─ single_hybrid_instance_pod_num: "
                f"{self.deploy_config.single_hybrid_instance_pod_num}\n"
                f"    └─ hybrid_pod_npu_num:     {self.deploy_config.hybrid_pod_npu_num}\n"
            )
        return (
            f"{separator}\n"
            f"{title}\n"
            f"{separator}\n"
            "  Deploy Configuration:\n"
            f"{deploy_summary}"
            "  Logging Configuration:\n"
            f"    ├─ Log Level:           {self.logging_config.log_level}\n"
            f"    ├─ Log File:            {self.logging_config.host_log_dir}\n"
            f"    └─ Log Max Line Length: {self.logging_config.log_max_line_length}\n"
            "\n"
            "  Network Configuration:\n"
            f"    ├─ HTTP Pod IP:         {self.api_config.coordinator_api_host}\n"
            f"    ├─ HTTP Pod DNS:        {self.api_config.coordinator_api_dns}\n"
            f"    ├─ Inference Port:      {self.api_config.coordinator_api_infer_port}\n"
            f"    ├─ Management Port:     {self.api_config.coordinator_api_mgmt_port}\n"
            f"    └─ Observability Port:  {self.api_config.coordinator_obs_port}\n"
            "\n"
            "  Scheduler Configuration:\n"
            f"    ├─ Scheduler Type:             {self.scheduler_config.scheduler_type.value}\n"
            f"    ├─ Endpoint Instance Weight:   {self.scheduler_config.endpoint_instance_score_weight}\n"
            f"    ├─ KV Affinity Mode:           {self.scheduler_config.kv_affinity_mode}\n"
            f"    ├─ KV Affinity Load Weight:    {self.scheduler_config.kv_affinity_load_weight}\n"
            f"    └─ KV Affinity Load Gate TopN: {self.scheduler_config.kv_affinity_load_gate_topn}\n"
            "\n"
            "  Multiprocess (Inference Workers):\n"
            f"    └─ Num Workers: {self.inference_workers_config.num_workers}\n"
            "\n"
            "  Security:\n"
            f"    ├─ Infer TLS:           {'Enabled' if self.infer_tls_config.enable_tls else 'Disabled'}\n"
            f"    ├─ Management TLS:      {'Enabled' if self.mgmt_tls_config.enable_tls else 'Disabled'}\n"
            f"    ├─ Etcd TLS:            {'Enabled' if self.etcd_tls_config.enable_tls else 'Disabled'}\n"
            f"    ├─ API Key Auth:        {'Enabled' if self.api_key_config.enable_api_key else 'Disabled'}\n"
            f"    └─ Rate Limiting:       {'Enabled' if self.rate_limit_config.enable_rate_limit else 'Disabled'}\n"
            "\n"
            "  High Availability:\n"
            f"    ├─ ETCD:\n"
            f"    │   ├─ Persistence:       {'Enabled' if self.etcd_config.enable_etcd_persistence else 'Disabled'}\n"
            f"    │   ├─ Host:              {etcd_host}\n"
            f"    │   ├─ Port:              {etcd_port}\n"
            f"    │   └─ Timeout:           {etcd_timeout} seconds\n"
            f"    └─ Master/Standby:      {'Enabled' if self.standby_config.enable_master_standby else 'Disabled'}\n"
            f"        ├─ Check Interval:   {master_standby_check_interval} seconds\n"
            f"        ├─ Lock TTL:         {master_lock_ttl} seconds\n"
            f"        └─ Lock Key:         {master_lock_key}\n"
            "\n"
            "  Configuration:\n"
            f"    └─ Config Path: {self.config_path or 'Not set'}\n"
            f"{separator}"
        )

    def _validate_positive_number(self, value: float | int, field_name: str, allow_zero: bool = False) -> None:
        """Validate that a number is positive (optionally allow zero)"""
        if allow_zero and value < 0:
            self._errors.append(f"{field_name} cannot be negative")
        elif not allow_zero and value <= 0:
            self._errors.append(f"{field_name} must be greater than 0")

    def _validate_port_range(self, port: int, field_name: str) -> None:
        """Validate that a port number is in valid range (1-65535)"""
        if not (1 <= port <= 65535):
            self._errors.append(f"{field_name} must be in range 1-65535")

    def _validate_ip_or_hostname(self, value: str, field_name: str) -> None:
        """Validate that a string is a valid IP address or hostname"""
        if not value or not isinstance(value, str):
            self._errors.append(f"{field_name} cannot be empty")
            return

        # Try to parse as IP address first
        try:
            ipaddress.ip_address(value)
            return
        except ValueError:
            pass

        # If not IP, validate as hostname (basic validation)
        if not re.match(
            r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*$",
            value,
        ):
            self._errors.append(f"{field_name} must be a valid IP address or hostname")

    def _validate_endpoint_path(self, path: str, field_name: str) -> None:
        """Validate that an endpoint path starts with '/' and is not empty"""
        if not path or not isinstance(path, str):
            self._errors.append(f"{field_name} cannot be empty")
        elif not path.startswith("/"):
            self._errors.append(f"{field_name} must start with '/'")
