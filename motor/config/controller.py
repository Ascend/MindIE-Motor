# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
import json
from dataclasses import dataclass, field, asdict
from typing import Any

from motor.common.logger import get_logger
from motor.common.utils.env import Env
from motor.config.etcd import EtcdConfig
from motor.config.port_allocator_config import PortAllocatorConfig
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
    raise_if_config_errors,
    reload_dataclass_config_from_json,
    resolve_config_json_path,
    save_instance_config_to_json,
    format_config_summary_header,
    _update_tls_config,
    MGMT_TLS_CONFIG,
    ETCD_TLS_CONFIG,
    GRPC_TLS_CONFIG,
    OBSERVABILITY_TLS_CONFIG,
)


FILE_ENCODING = "utf-8"

logger = get_logger(__name__)


@dataclass
class ApiConfig:
    """API configuration class"""

    # controller API configuration
    controller_api_host: str = field(default_factory=lambda: Env.pod_ip or '127.0.0.1')
    controller_api_dns: str | None = field(default_factory=lambda: Env.controller_service or "127.0.0.1")
    controller_api_port: int = 1026
    observability_api_port: int = 1027


@dataclass
class ObservabilityConfig:
    """observability configuration class"""

    # observability enable/disable
    observability_enable: bool = False

    metrics_ttl: int = 5


@dataclass
class InstanceConfig:
    """Instance management configuration class"""

    # instance assembler configuration
    instance_assemble_timeout: int = 600  # 600 seconds
    # The assembler background threads use threading.Condition and are woken
    # immediately by notify_all() when a new registration arrives or an instance
    # becomes ASSEMBLED.  Registration is infrequent, so the interval only needs
    # to cover assembly-timeout detection and start-command retries.
    instance_assembler_check_interval: int = 30  # 30 seconds
    instance_assembler_cmd_send_interval: int = 30  # 30 seconds

    # instance manager configuration
    instance_manager_check_interval: int = 1  # 1 second
    instance_heartbeat_timeout: int = 10  # 10 seconds
    instance_expired_timeout: int = 1200  # 1200 seconds

    # other instance configuration
    send_cmd_retry_times: int = 3


@dataclass
class EventPusherConfig:
    """Event configuration class"""

    # event consumer configuration (deprecated: consumer now uses queue.get(timeout=1))
    event_consumer_sleep_interval: float = 1.0  # 1 second

    # coordinator heartbeat configuration
    # Uses threading.Condition — woken immediately by notify_all() on stop.
    # Coordinator restart detection is rare, so 10 s is sufficient.
    coordinator_heartbeat_interval: float = 10.0  # 10 seconds


@dataclass
class FaultToleranceConfig:
    """Fault tolerance configuration class"""

    # fault tolerance enable/disable
    enable_fault_tolerance: bool = True

    # strategy center configuration
    # The strategy center thread uses threading.Condition and is woken
    # immediately by notify_all() on fault reports, node status changes, or
    # instance lifecycle events.  The interval is only a fallback for periodic
    # strategy re-evaluation, so 10 s is sufficient.
    strategy_center_check_interval: int = 10  # 10 seconds

    # configmap monitoring configuration - ConfigMap namespace and name prefix
    configmap_namespace: str = "kube-system"
    configmap_prefix: str = "mindx-dl-deviceinfo-"

    # k8s certificate path (can be configured per ConfigMap if needed)
    k8s_cert_path: str = ""  # Path to Kubernetes certificates (default: use in-cluster config)

    # scale and recovery strategy configuration
    enable_scale_p2d: bool = True  # Enable/disable scale p2d strategy
    enable_token_reinference: bool = True  # Enable/disable token reinference strategy
    scale_p2d_d_instance_reinit_wait_timeout: int = 60  # seconds to wait for D instance re-init before ScaleP2D


@dataclass
class ControllerConfig:
    """Controller configuration class with validation, reload and error handling support"""

    # Configuration sections
    logging_config: LoggingConfig = field(default_factory=LoggingConfig)
    api_config: ApiConfig = field(default_factory=ApiConfig)
    mgmt_tls_config: TLSConfig = field(default_factory=TLSConfig)
    etcd_tls_config: TLSConfig = field(default_factory=TLSConfig)
    grpc_tls_config: TLSConfig = field(default_factory=TLSConfig)
    observability_tls_config: TLSConfig = field(default_factory=TLSConfig)
    instance_config: InstanceConfig = field(default_factory=InstanceConfig)
    event_config: EventPusherConfig = field(default_factory=EventPusherConfig)
    fault_tolerance_config: FaultToleranceConfig = field(default_factory=FaultToleranceConfig)
    observability_config: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    standby_config: StandbyConfig = field(default_factory=StandbyConfig)
    etcd_config: EtcdConfig = field(default_factory=EtcdConfig)
    port_allocator_config: PortAllocatorConfig = field(default_factory=PortAllocatorConfig)
    # Token sampling precision alarm: when True, controller terminates decode instance on precision alarm
    precision_auto_recovery_enabled: bool = field(default=False)

    # internal fields
    config_path: str | None = field(default=None, init=False)
    last_modified: float | None = field(default=None, init=False)

    def __post_init__(self):
        """Validate configuration after initialization"""
        init_motor_config(self, "controller")

    @classmethod
    def from_json(cls, json_path: str | None = None) -> 'ControllerConfig':
        """Load configuration from JSON file"""
        json_path, config_path = resolve_config_json_path(json_path)

        cfg = {}
        try:
            if config_path and config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:  # Only parse if file is not empty
                        raw = json.loads(content)
                        if isinstance(raw, dict) and "motor_controller_config" in raw:
                            cfg = raw.get("motor_controller_config", {})
                        else:
                            cfg = raw
                        tls_configs = [MGMT_TLS_CONFIG, ETCD_TLS_CONFIG, GRPC_TLS_CONFIG, OBSERVABILITY_TLS_CONFIG]
                        _update_tls_config(tls_configs, cfg, raw)
        except (json.JSONDecodeError, Exception) as e:
            log_json_config_load_error(json_path, e)

        try:
            config = cls()

            # Helper function to update config object from dict
            def update_config_from_dict(config_obj, config_dict):
                """Update configuration object fields from dictionary, only for existing keys"""
                for key, value in config_dict.items():
                    if hasattr(config_obj, key):
                        setattr(config_obj, key, value)

            # Update configuration sections if they exist in JSON
            if 'logging_config' in cfg:
                update_config_from_dict(config.logging_config, cfg['logging_config'])

            if 'api_config' in cfg:
                update_config_from_dict(config.api_config, cfg['api_config'])

            if 'mgmt_tls_config' in cfg:
                update_config_from_dict(config.mgmt_tls_config, cfg['mgmt_tls_config'])

            if 'etcd_tls_config' in cfg:
                update_config_from_dict(config.etcd_tls_config, cfg['etcd_tls_config'])

            if 'grpc_tls_config' in cfg:
                update_config_from_dict(config.grpc_tls_config, cfg['grpc_tls_config'])

            if 'observability_tls_config' in cfg:
                update_config_from_dict(config.observability_tls_config, cfg['observability_tls_config'])

            if 'instance_config' in cfg:
                update_config_from_dict(config.instance_config, cfg['instance_config'])

            if 'event_config' in cfg:
                update_config_from_dict(config.event_config, cfg['event_config'])

            if 'fault_tolerance_config' in cfg:
                update_config_from_dict(config.fault_tolerance_config, cfg['fault_tolerance_config'])

            if 'standby_config' in cfg:
                update_config_from_dict(config.standby_config, cfg['standby_config'])

            if 'etcd_config' in cfg:
                update_config_from_dict(config.etcd_config, cfg['etcd_config'])

            if 'observability_config' in cfg:
                update_config_from_dict(config.observability_config, cfg['observability_config'])

            if 'port_allocator_config' in cfg:
                update_config_from_dict(config.port_allocator_config, cfg['port_allocator_config'])

            if 'precision_auto_recovery_enabled' in cfg:
                config.precision_auto_recovery_enabled = bool(cfg['precision_auto_recovery_enabled'])

            apply_config_path_metadata(config, config_path)
            if not config_path:
                config.last_modified = None
                config.last_modified = None

            apply_standby_persistence_rule(config)

            finalize_json_config_load(
                config_path,
                no_path_message="Using default configuration (no config file specified)",
            )

            return config

        except Exception as e:
            logger.error("Failed to create configuration instance: %s", e)
            raise

    def validate_config(self) -> None:
        """Validate the validity of configuration values"""
        errors = []

        # Validate logging configuration
        valid_log_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR']
        if self.logging_config.log_level.upper() not in valid_log_levels:
            errors.append(f"log_level must be one of: {', '.join(valid_log_levels)}")

        if self.logging_config.log_max_line_length <= 0:
            errors.append("log_max_line_length must be greater than 0")

        # Validate API configuration
        if not (1 <= self.api_config.controller_api_port <= 65535):
            errors.append("controller_api_port must be in range 1-65535")

        # Validate instance configuration
        if self.instance_config.instance_assemble_timeout <= 0:
            errors.append("instance_assemble_timeout must be greater than 0")

        if self.instance_config.instance_heartbeat_timeout <= 0:
            errors.append("instance_heartbeat_timeout must be greater than 0")

        if self.instance_config.instance_expired_timeout <= 0:
            errors.append("instance_expired_timeout must be greater than 0")

        if self.instance_config.instance_assembler_check_interval <= 0:
            errors.append("instance_assembler_check_interval must be greater than 0")

        if self.instance_config.instance_manager_check_interval <= 0:
            errors.append("instance_manager_check_interval must be greater than 0")

        if self.instance_config.send_cmd_retry_times < 0:
            errors.append("send_cmd_retry_times cannot be negative")

        # Validate event configuration
        if self.event_config.event_consumer_sleep_interval <= 0:
            errors.append("event_consumer_sleep_interval must be greater than 0")

        if self.event_config.coordinator_heartbeat_interval <= 0:
            errors.append("coordinator_heartbeat_interval must be greater than 0")

        # Validate fault tolerance configuration
        if self.fault_tolerance_config.strategy_center_check_interval <= 0:
            errors.append("strategy_center_check_interval must be greater than 0")

        if not (1 <= self.fault_tolerance_config.scale_p2d_d_instance_reinit_wait_timeout <= 600):
            errors.append("scale_p2d_d_instance_reinit_wait_timeout must be in range 1-600")

        # Validate standby configuration
        if self.standby_config.master_standby_check_interval <= 0:
            errors.append("master_standby_check_interval must be greater than 0")

        # Validate ETCD configuration
        if not (1 <= self.etcd_config.etcd_port <= 65535):
            errors.append("etcd_port must be in range 1-65535")

        if self.etcd_config.etcd_timeout <= 0:
            errors.append("etcd_timeout must be greater than 0")

        if not (1 <= self.api_config.observability_api_port <= 65535):
            errors.append("observability_api_port must be in range 1-65535")

        raise_if_config_errors(errors)

    def reload(self) -> bool:
        """Reload configuration file"""
        return reload_dataclass_config_from_json(
            self,
            self.from_json,
            skip_private=True,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to dictionary with grouped structure"""

        # Use dataclasses.asdict to automatically serialize all config objects
        config_dict = asdict(self)

        # Remove internal fields that shouldn't be in the output
        config_dict.pop('config_path', None)
        config_dict.pop('last_modified', None)

        return config_dict

    def save_to_json(self, json_path: str | None = None) -> bool:
        """Save configuration to JSON file"""
        return save_instance_config_to_json(
            self,
            json_path,
            config_key=ConfigKey.MOTOR_CONTROLLER,
            file_encoding=FILE_ENCODING,
            component_name="controller",
        )

    def get_config_summary(self) -> str:
        """Get configuration summary information"""
        title = " " * 22 + "Controller Configuration Summary"
        enable_fault_tolerance = self.fault_tolerance_config.enable_fault_tolerance
        enable_scale_p2d = self.fault_tolerance_config.enable_scale_p2d and enable_fault_tolerance
        enable_token_reinference = self.fault_tolerance_config.enable_token_reinference and enable_fault_tolerance
        enable_observability = self.observability_config.observability_enable
        master_standby_check_interval = self.standby_config.master_standby_check_interval
        metrics_ttl = self.observability_config.metrics_ttl
        master_lock_ttl = self.standby_config.master_lock_ttl
        master_lock_key = self.standby_config.master_lock_key
        controller_api = f"{self.api_config.controller_api_host}:{self.api_config.controller_api_port}"
        controller_api_dns = f"{self.api_config.controller_api_dns}:{self.api_config.controller_api_port}"
        return (
            format_config_summary_header(title) + "  Logging Configuration:\n"
            f"    ├─ Log Level:            {self.logging_config.log_level}\n"
            f"    ├─ Log File:             {self.logging_config.host_log_dir}\n"
            f"    └─ Log Max Line Length:  {self.logging_config.log_max_line_length}\n"
            "\n"
            "  Network Configuration:\n"
            f"    ├─ Pod IP:              {Env.pod_ip}\n"
            f"    ├─ Controller API:      {controller_api}\n"
            f"    ├─ Controller API DNS:  {controller_api_dns}\n"
            f"    ├─ Etcd TLS:            {'Enabled' if self.etcd_tls_config.enable_tls else 'Disabled'}\n"
            f"    ├─ GRPC TLS:            {'Enabled' if self.grpc_tls_config.enable_tls else 'Disabled'}\n"
            f"    ├─ Management TLS:      {'Enabled' if self.mgmt_tls_config.enable_tls else 'Disabled'}\n"
            f"    └─ Observability TLS:   {'Enabled' if self.observability_tls_config.enable_tls else 'Disabled'}\n"
            "\n"
            "  Instance Management:\n"
            f"    ├─ Assemble Timeout:     {self.instance_config.instance_assemble_timeout} seconds\n"
            f"    ├─ Heartbeat Timeout:    {self.instance_config.instance_heartbeat_timeout} seconds\n"
            f"    └─ Expired Timeout:      {self.instance_config.instance_expired_timeout} seconds\n"
            "\n"
            "  High Availability:\n"
            f"    ├─ Advanced RAS:         {'Enabled' if enable_fault_tolerance else 'Disabled'}\n"
            f"    │   ├─ Scale P2D:        {'Enabled' if enable_scale_p2d else 'Disabled'}\n"
            f"    │   └─ Token Reinference:   {'Enabled' if enable_token_reinference else 'Disabled'}\n"
            f"    ├─ ETCD:\n"
            f"    │   ├─ Persistence:      {'Enabled' if self.etcd_config.enable_etcd_persistence else 'Disabled'}\n"
            f"    │   ├─ Host:             {self.etcd_config.etcd_host}\n"
            f"    │   ├─ Port:             {self.etcd_config.etcd_port}\n"
            f"    │   └─ Timeout:          {self.etcd_config.etcd_timeout} seconds\n"
            f"    ├─ Observability:        {'Enabled' if enable_observability else 'Disabled'}\n"
            f"    │   └─ Metrics TTL:      {metrics_ttl} seconds\n"
            f"    └─ Master/Standby:       {'Enabled' if self.standby_config.enable_master_standby else 'Disabled'}\n"
            f"        ├─ Check Interval:   {master_standby_check_interval} seconds\n"
            f"        ├─ Lock TTL:         {master_lock_ttl} seconds\n"
            f"        └─ Lock Key:         {master_lock_key}\n"
            "\n"
            "  Configuration:\n"
            f"    └─ Config Path:         {self.config_path or 'Not set'}\n"
            f"{'=' * 80}"
        )
