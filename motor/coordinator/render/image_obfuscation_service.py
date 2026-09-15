# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Vision (multimodal tensor) obfuscation for data-obfuscated inference models.

The permutation is applied to the Render response payload (`features.kwargs_data`), i.e. after
the image processor has produced `pixel_values`/`image_grid_thw` and before the Coordinator
forwards the request to the engine. The SDK owns the Render payload format and the patch
layout, so the Coordinator needs neither the msgpack codec nor the model's flatten order.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from motor.common.logger import get_logger
from motor.config.coordinator import IMAGE_OBFUSCATION_GEOMETRY_FIELDS, ImageObfuscationConfig
from motor.coordinator.render.obfuscation_library import (
    ObfuscationLibraryError,
    resolve_obfuscation_model_paths,
)

logger = get_logger(__name__)

_KWARGS_DATA = "kwargs_data"
_PREPROCESSOR_CONFIG = "preprocessor_config.json"
# Field names inside the model's preprocessor config. ``size`` is the current layout;
# ``min_pixels``/``max_pixels`` is the legacy one used by Qwen2-VL style processors.
_PREPROCESSOR_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "patch_size": ("patch_size",),
    "merge_size": ("merge_size",),
    "temporal_patch_size": ("temporal_patch_size",),
    "shortest_edge": ("shortest_edge", "min_pixels"),
    "longest_edge": ("longest_edge", "max_pixels"),
}


class VisionDataObfuscator(Protocol):
    """Subset of ai-asset-obfuscate vision API used by Coordinator."""

    def set_seed_content(self, seed_content: str, *args: Any, **kwargs: Any) -> Any: ...

    def image_render_obf(self, image_items: list) -> list: ...


class ImageObfuscationError(ObfuscationLibraryError):
    """The protected vision path cannot continue safely."""


def _read_preprocessor_geometry(model_dir: Path) -> dict[str, int]:
    """Read patch geometry from a served model directory, ignoring unusable fields."""
    config_path = model_dir / _PREPROCESSOR_CONFIG
    if not config_path.is_file():
        raise ImageObfuscationError(f"{config_path} does not exist")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ImageObfuscationError(f"failed to read {config_path}: {type(error).__name__}: {error}") from error
    if not isinstance(raw, dict):
        raise ImageObfuscationError(f"{config_path} must contain a JSON object")

    size = raw.get("size") if isinstance(raw.get("size"), dict) else {}
    geometry: dict[str, int] = {}
    for field_name in IMAGE_OBFUSCATION_GEOMETRY_FIELDS:
        for alias in _PREPROCESSOR_FIELD_ALIASES[field_name]:
            value = size.get(alias, raw.get(alias))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                geometry[field_name] = value
                break
    return geometry


def resolve_image_obfuscation_config(
    config: ImageObfuscationConfig,
    engine_model_paths: list[str] | None = None,
) -> tuple[ImageObfuscationConfig, str]:
    """Fill unset vision geometry from the served model directory.

    Explicitly configured fields always win; only fields left at ``0`` are read from
    ``preprocessor_config.json``. ``image_config.model_path`` takes precedence over the model
    directories declared by the engine sections. Returns the resolved config plus a source
    description for logging. Fails closed when a required value cannot be resolved.
    """
    unset_fields = [field_name for field_name in IMAGE_OBFUSCATION_GEOMETRY_FIELDS if getattr(config, field_name) == 0]
    if not unset_fields:
        return config, "explicit configuration"

    candidates = resolve_obfuscation_model_paths(config.model_path, engine_model_paths)
    if not candidates:
        raise ImageObfuscationError(
            "image obfuscation geometry is not configured and no served model directory is available; "
            f"set token_obfuscation_config.image_config.model_path or configure {', '.join(unset_fields)}"
        )

    errors: list[str] = []
    for candidate in candidates:
        model_dir = Path(candidate)
        try:
            geometry = _read_preprocessor_geometry(model_dir)
        except ImageObfuscationError as error:
            errors.append(f"{candidate}: {error}")
            continue
        still_missing = [name for name in unset_fields if name not in geometry]
        if still_missing:
            errors.append(f"{candidate}: {_PREPROCESSOR_CONFIG} is missing {', '.join(still_missing)}")
            continue
        resolved = replace(config, **{name: geometry[name] for name in unset_fields})
        _validate_geometry_range(resolved, candidate)
        source = f"{_PREPROCESSOR_CONFIG} of {candidate} ({', '.join(f'{n}={geometry[n]}' for n in unset_fields)})"
        logger.info("Vision obfuscation geometry resolved from %s", source)
        return resolved, source

    raise ImageObfuscationError(
        f"failed to resolve image obfuscation geometry from the served model directory: {'; '.join(errors)}"
    )


def _validate_geometry_range(config: ImageObfuscationConfig, source: str) -> None:
    """Reject inverted pixel bounds once geometry has been resolved from the model directory.

    ``CoordinatorConfig.validate_config()`` only compares the two edges when both are configured;
    a partially configured geometry would otherwise be completed with an inconsistent value.
    """
    if config.longest_edge < config.shortest_edge:
        raise ImageObfuscationError(
            "token_obfuscation_config.image_config.longest_edge must be greater than or equal to "
            f"shortest_edge (longest_edge={config.longest_edge}, shortest_edge={config.shortest_edge}, "
            f"resolved from {source})"
        )


class ImageObfuscationService:
    """Own one process-local image permutation matching the obfuscated vision weights.

    One instance per Coordinator process is shared by all in-flight requests. The SDK calls are
    synchronous and therefore already serialized by the single event loop driving them, so no lock
    is needed here; move them to a worker thread only once ``ai_asset_obfuscate`` is confirmed to be
    thread-safe.
    """

    def __init__(
        self,
        config: ImageObfuscationConfig,
        seed_content: str,
        obfuscator: VisionDataObfuscator | None = None,
    ) -> None:
        self._config = config
        success_value: Any = (0, "Success.")
        if obfuscator is None:
            try:
                from ai_asset_obfuscate.vision_api import ImageDataAssetObfuscation
            except Exception as error:
                raise ImageObfuscationError(
                    f"ai-asset-obfuscate vision API is unavailable: {type(error).__name__}: {error}"
                ) from error
            try:
                obfuscator = ImageDataAssetObfuscation(
                    patch_size=config.patch_size,
                    merge_size=config.merge_size,
                    longest_edge=config.longest_edge,
                    shortest_edge=config.shortest_edge,
                    temporal_patch_size=config.temporal_patch_size,
                )
            except Exception as error:
                raise ImageObfuscationError("failed to initialize image obfuscation") from error
        self._obfuscator = obfuscator
        try:
            result = self._obfuscator.set_seed_content(seed_content)
        except Exception as error:
            raise ImageObfuscationError("failed to set image obfuscation seed") from error
        if result != success_value:
            raise ImageObfuscationError("image obfuscation seed was rejected")

    def obfuscate_render_features(self, features: dict[str, Any]) -> int:
        """Obfuscate one Render `features` payload in place; return the replaced item count.

        `features.kwargs_data` maps each modality to a list parallel to `mm_hashes`; every
        non-null entry is a serialized multimodal item whose image tensors must be permuted
        before the engine reads them. Null entries are Render cache hits and stay untouched.
        The permutation itself is owned by the SDK: Coordinator forwards the item list the SDK
        returns without inspecting how it was produced.
        """
        kwargs_data = features.get(_KWARGS_DATA)
        if kwargs_data is None:
            return 0
        if not isinstance(kwargs_data, dict):
            raise ImageObfuscationError("Render features.kwargs_data must be an object")

        replaced = 0
        pending: list[tuple[str, list]] = []
        for modality, items in kwargs_data.items():
            if not isinstance(items, list):
                raise ImageObfuscationError(f"Render kwargs_data.{modality} must be a list")
            if not items:
                continue
            try:
                obfuscated = self._obfuscator.image_render_obf(items)
            except Exception as error:
                raise ImageObfuscationError(
                    f"failed to obfuscate Render {modality} items: {type(error).__name__}: {error}"
                ) from error
            pending.append((modality, list(obfuscated)))
            replaced += sum(1 for item in obfuscated if item is not None)
        # Commit only after every modality returned, so a partial rewrite can never reach the
        # engine alongside unobfuscated items.
        for modality, obfuscated in pending:
            kwargs_data[modality] = obfuscated
        return replaced
