# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from typing import Any

from motor.common.logger import get_logger

logger = get_logger(__name__)


def normalize_keys(obj: Any) -> Any:
    """Recursively convert ``-`` to ``_`` in all dict keys."""
    if isinstance(obj, dict):
        return {k.replace("-", "_"): normalize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_keys(item) for item in obj]
    return obj


class BaseConfigResolver:
    """Base resolver — not instantiated directly. Use the ConfigResolver() factory.

    Reads from both model_config (legacy) and engine_config (new).
    Priority: engine_config > model_config.
    When both define the same parameter with different values, a warning is logged.
    """

    _GENERIC_KEY_VARIANTS: dict[str, tuple[str, ...]] = {}
    _PARALLEL_KEY_VARIANTS: dict[str, tuple[str, ...]] = {}
    _warned_conflict_keys: set[str] = set()

    def __init__(self, engine_section: dict[str, Any]):
        raw_model = engine_section.get("model_config") or {}
        raw_engine = engine_section.get("engine_config") or {}
        self._model_cfg: dict[str, Any] = normalize_keys(raw_model)
        self._engine_cfg: dict[str, Any] = normalize_keys(raw_engine)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _warn_conflict(self, key, engine_val, model_val, model_source="model_config"):
        if engine_val is not None and model_val is not None and engine_val != model_val:
            if key not in BaseConfigResolver._warned_conflict_keys:
                logger.warning(
                    "Config conflict for '%s': engine_config=%s, %s=%s. Using engine_config.",
                    key,
                    engine_val,
                    model_source,
                    model_val,
                )
                BaseConfigResolver._warned_conflict_keys.add(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a resolved value. Checks engine_config first, falls back to model_config."""
        variants = self._GENERIC_KEY_VARIANTS.get(key, (key,))
        engine_val = self._get_engine_key(*variants)
        model_val = self._model_cfg.get(key)

        self._warn_conflict(key, engine_val, model_val)

        if engine_val is not None:
            return engine_val
        if model_val is not None:
            return model_val
        return default

    def get_model_name(self, default: str = "") -> str:
        return self.get("model_name", default)

    def get_model_path(self, default: str = "") -> str:
        return self.get("model_path", default)

    def get_npu_mem_utils(self, default: float = 0.9) -> float:
        return self.get("npu_mem_utils", default)

    def get_enable_multi_endpoints(self, default: bool = True) -> bool:
        """Get enable_multi_endpoints, defaulting per engine type."""
        return bool(self._engine_cfg.get("enable_multi_endpoints", default))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_engine_key(self, *keys: str) -> Any:
        """Try multiple key variants from engine_config, returning the first match.

        Used to normalize underscore/hyphen differences in parallel config keys
        (e.g. ``data_parallel_size`` vs ``data-parallel-size``).
        """
        for key in keys:
            val = self._engine_cfg.get(key)
            if val is not None:
                return val
        return None

    def get_parallel_config(self) -> dict[str, Any]:
        """Get resolved parallel configuration as a dict.

        Resolution order:
        1. Adapter-provided engine-specific keys via _resolve_engine_parallel_keys().
        2. model_config.parallel_config (legacy fallback).
        Warns when the same key exists in both sources with different values.
        """
        result: dict[str, Any] = {}
        result.update(self._resolve_engine_parallel_keys())

        legacy_parallel: dict[str, Any] = self._model_cfg.get("parallel_config") or {}
        for key, val in legacy_parallel.items():
            if key in result:
                self._warn_conflict(key, result[key], val, "model_config.parallel_config")
            else:
                result[key] = val

        # Always inject computed values, silently overriding user-supplied ones.
        result["local_world_size"] = self._compute_local_world_size(result)
        result["world_size"] = self._compute_world_size(result)

        return result

    def _compute_local_world_size(self, config: dict[str, Any]) -> int:
        """Compute local_world_size = pcp * tp * pp.

        Override in subclasses for engine-specific local-world-size semantics
        (e.g. when different engines calculate per-endpoint device count
        differently).
        """
        pcp = config.get("pcp_size", 1)
        tp = config.get("tp_size", 1)
        pp = config.get("pp_size", 1)
        return pcp * tp * pp

    def _compute_world_size(self, config: dict[str, Any]) -> int:
        """Compute world_size = dp * local_world_size = dp * pcp * tp * pp."""
        dp = config.get("dp_size", 1)
        return dp * self._compute_local_world_size(config)

    def _resolve_engine_parallel_keys(self) -> dict[str, Any]:
        """Map engine-native keys to Motor-internal keys via _PARALLEL_KEY_VARIANTS.

        Each entry maps an internal key (e.g. ``dp_size``) to a tuple of
        engine_config key variants (e.g. ``("data_parallel_size",
        "data-parallel-size")``). The first matching variant wins.

        Override in subclasses for engine-specific keys that need custom
        resolution logic beyond simple key mapping.
        """
        result: dict[str, Any] = {}
        for internal_key, variants in self._PARALLEL_KEY_VARIANTS.items():
            val = self._get_engine_key(*variants)
            if val is not None:
                result[internal_key] = val
        return result

    def has_model_config(self) -> bool:
        """Check if model_config block exists (for deprecation detection)."""
        return bool(self._model_cfg)

    @property
    def model_config(self) -> dict[str, Any]:
        """Raw model_config dict (read-only, for backward compatibility)."""
        return self._model_cfg

    @property
    def engine_config(self) -> dict[str, Any]:
        """Raw engine_config dict."""
        return self._engine_cfg

    def get_d2d_config(self) -> dict | None:
        """Return D2D config {source, listen_port} or None if not configured."""
        return None

    @staticmethod
    def load_section(config_path: str, section_key: str) -> "BaseConfigResolver":
        """Load a resolver from a config file and extract the engine section.

        Reads *config_path*, picks *section_key* from the top-level JSON dict,
        and returns a ConfigResolver for that engine section.
        """
        import json as _json

        with open(config_path, 'r') as f:
            raw = _json.load(f)
        section = raw.get(section_key, {})
        return ConfigResolver(section)


# ------------------------------------------------------------------
# Engine-specific adapters
# ------------------------------------------------------------------


class VLLMConfigResolver(BaseConfigResolver):
    """Adapter: maps internal keys to vLLM-native engine_config keys."""

    _GENERIC_KEY_VARIANTS = {
        "model_name": ("served_model_name", "served-model-name"),
        "model_path": ("model",),
        "npu_mem_utils": ("gpu_memory_utilization", "gpu-memory-utilization"),
    }

    _PARALLEL_KEY_VARIANTS = {
        "dp_size": ("data_parallel_size", "data-parallel-size"),
        "tp_size": ("tensor_parallel_size", "tensor-parallel-size"),
        "pp_size": ("pipeline_parallel_size", "pipeline-parallel-size"),
        "pcp_size": ("prefill_context_parallel_size", "prefill-context-parallel-size"),
        "dp_rpc_port": ("data_parallel_rpc_port", "data-parallel-rpc-port"),
        "enable_ep": ("enable_expert_parallel", "enable-expert-parallel"),
        "cp_kv_cache_interleave_size": ("cp_kv_cache_interleave_size", "cp-kv-cache-interleave-size"),
    }

    def get_d2d_config(self) -> dict | None:
        """Read D2D config from model_loader_extra_config.

        Returns {source, listen_port} or None.
        source may be "auto" (controller fills real IPs) or a static peer list.
        """
        import json as _json

        ml_extra = self._engine_cfg.get("model_loader_extra_config")
        if ml_extra is None:
            logger.info("get_d2d_config: model_loader_extra_config not found in engine_config")
            return None
        if isinstance(ml_extra, str):
            try:
                ml_extra = _json.loads(ml_extra)
            except _json.JSONDecodeError:
                logger.warning("get_d2d_config: model_loader_extra_config is invalid JSON: %s", ml_extra)
                return None
        if not isinstance(ml_extra, dict):
            logger.warning("get_d2d_config: model_loader_extra_config is not a dict: %s", type(ml_extra))
            return None

        source = ml_extra.get("source") or ml_extra.get("SOURCE")
        if not source:
            logger.info(
                "get_d2d_config: source key not found or empty in model_loader_extra_config, keys=%s",
                list(ml_extra.keys()),
            )
            return None
        listen_port = ml_extra.get("listen_port") or ml_extra.get("LISTEN_PORT")
        logger.info("get_d2d_config: resolved source=%s listen_port=%s", source, listen_port)
        return {"source": source, "listen_port": listen_port}


class SGLangConfigResolver(BaseConfigResolver):
    """Adapter: maps internal keys to SGLang-native engine_config keys."""

    _GENERIC_KEY_VARIANTS = {
        "model_name": ("served-model-name", "served_model_name"),
        "model_path": ("model-path", "model"),
        "npu_mem_utils": ("mem-fraction-static", "mem_fraction_static"),
    }

    _PARALLEL_KEY_VARIANTS = {
        "dp_size": ("dp-size", "dp_size"),
        "tp_size": ("tp-size", "tp_size"),
        "pp_size": ("pp-size", "pp_size"),
    }

    def get_enable_multi_endpoints(self, default: bool = True) -> bool:
        return bool(self._engine_cfg.get("enable_multi_endpoints", False))

    def _resolve_engine_parallel_keys(self) -> dict[str, Any]:
        result = super()._resolve_engine_parallel_keys()

        cp_size = self._get_engine_key("context-parallel-size", "context_parallel_size")
        cp_enabled = self._get_engine_key("enable-prefill-context-parallel", "enable_prefill_context_parallel") or False
        if cp_size and cp_enabled:
            result["pcp_size"] = cp_size

        return result


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


def ConfigResolver(
    engine_section: dict[str, Any],
    engine_type: str | None = None,
) -> BaseConfigResolver:
    """Factory: create the appropriate engine-specific config resolver.

    *engine_type* is normally read from the section; pass it explicitly only
    when the section dict doesn't carry ``engine_type`` itself.
    """
    if engine_type is None:
        engine_type = engine_section.get("engine_type")
    if not engine_type:
        logger.warning("engine_type not specified, defaulting to vllm")
        engine_type = "vllm"
    if engine_type == "sglang":
        return SGLangConfigResolver(engine_section)
    if engine_type != "vllm":
        logger.warning("unknown engine_type '%s', falling back to vllm", engine_type)
    return VLLMConfigResolver(engine_section)
