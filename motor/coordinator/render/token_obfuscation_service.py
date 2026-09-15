# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Token permutation for data-obfuscated inference models."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from motor.common.logger import get_logger
from motor.config.coordinator import TokenObfuscationConfig
from motor.coordinator.render.obfuscation_library import (
    ObfuscationLibraryError,
    configure_obfuscation_library_path,
    resolve_obfuscation_model_paths,
)

__all__ = [
    "TokenObfuscationError",
    "TokenObfuscationService",
    "configure_obfuscation_library_path",
    "resolve_token_obfuscation_config",
]

logger = get_logger(__name__)

_MODEL_CONFIG = "config.json"


class DataObfuscator(Protocol):
    """Subset of ai-asset-obfuscate used by Coordinator."""

    def set_seed_content(self, seed_content: str) -> Any: ...

    def data_1d_obf(self, tokens: list[int]) -> list[int]: ...

    def data_1d_deobf(self, tokens: list[int]) -> list[int]: ...


class TokenObfuscationError(ObfuscationLibraryError):
    """The protected token path cannot continue safely."""


def _read_vocab_size(model_dir: Path) -> int:
    """Read the vocabulary size from a served model directory."""
    config_path = model_dir / _MODEL_CONFIG
    if not config_path.is_file():
        raise TokenObfuscationError(f"{config_path} does not exist")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TokenObfuscationError(f"failed to read {config_path}: {type(error).__name__}: {error}") from error
    if not isinstance(raw, dict):
        raise TokenObfuscationError(f"{config_path} must contain a JSON object")

    # Multimodal models nest the language model config; text models keep vocab_size at top level.
    text_config = raw.get("text_config") if isinstance(raw.get("text_config"), dict) else {}
    for candidate in (text_config.get("vocab_size"), raw.get("vocab_size")):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            return candidate
    raise TokenObfuscationError(f"{config_path} does not declare a positive text_config.vocab_size")


def _validate_white_list_range(config: TokenObfuscationConfig) -> None:
    """Reject white-list ids the resolved vocabulary cannot hold.

    ``CoordinatorConfig.validate_config()`` only compares ids against an explicitly configured
    ``vocab_size``; the auto-read path resolves it after validation, so the range check has to be
    repeated on the resolved config.
    """
    out_of_range = [token_id for token_id in config.token_white_list if token_id >= config.vocab_size]
    if out_of_range:
        raise TokenObfuscationError(
            "token_obfuscation_config.token_white_list must contain token ids within the resolved "
            f"vocab_size {config.vocab_size}, but found {out_of_range[:10]}"
        )


def resolve_token_obfuscation_config(
    config: TokenObfuscationConfig,
    engine_model_paths: list[str] | None = None,
) -> tuple[TokenObfuscationConfig, str]:
    """Fill an unset ``vocab_size`` from the served model directory.

    An explicitly configured ``vocab_size`` always wins. ``token_obfuscation_config.model_path``
    takes precedence over the model directories declared by the engine sections. Returns the
    resolved config plus a source description for logging; fails closed when it cannot resolve.
    """
    if config.vocab_size:
        return config, "explicit configuration"

    candidates = resolve_obfuscation_model_paths(config.model_path, engine_model_paths)
    if not candidates:
        raise TokenObfuscationError(
            "token obfuscation vocab_size is not configured and no served model directory is available; "
            "set token_obfuscation_config.model_path or configure vocab_size"
        )

    errors: list[str] = []
    for candidate in candidates:
        try:
            vocab_size = _read_vocab_size(Path(candidate))
        except TokenObfuscationError as error:
            errors.append(f"{candidate}: {error}")
            continue
        resolved = replace(config, vocab_size=vocab_size)
        _validate_white_list_range(resolved)
        source = f"{_MODEL_CONFIG} of {candidate} (vocab_size={vocab_size})"
        logger.info("Token obfuscation vocab_size resolved from %s", source)
        return resolved, source

    raise TokenObfuscationError(
        f"failed to resolve token obfuscation vocab_size from the served model directory: {'; '.join(errors)}"
    )


class TokenObfuscationService:
    """Own one process-local token permutation matching the obfuscated model weights.

    One instance per Coordinator process is shared by all in-flight requests. The SDK calls are
    synchronous and therefore already serialized by the single event loop driving them, so no lock
    is needed here; move them to a worker thread only once ``ai_asset_obfuscate`` is confirmed to be
    thread-safe.
    """

    def __init__(self, config: TokenObfuscationConfig, obfuscator: DataObfuscator | None = None) -> None:
        self._config = config
        success_value: Any = (0, "Success.")
        if obfuscator is None:
            try:
                from ai_asset_obfuscate import DataAssetObfuscation, ErrorCode
            except Exception as error:
                raise TokenObfuscationError("ai-asset-obfuscate is unavailable") from error
            try:
                obfuscator = DataAssetObfuscation(
                    vocab_size=config.vocab_size,
                    token_white_list=config.token_white_list,
                )
            except Exception as error:
                raise TokenObfuscationError("failed to initialize token obfuscation") from error
            success_value = ErrorCode.SUCCESS.value
        self._obfuscator = obfuscator
        try:
            result = self._obfuscator.set_seed_content(seed_content=config.seed_content)
        except Exception as error:
            raise TokenObfuscationError("failed to set token obfuscation seed") from error
        if result != success_value:
            raise TokenObfuscationError("token obfuscation seed was rejected")

    def obfuscate(self, token_ids: list[int]) -> list[int]:
        """Map semantic token IDs into the protected model's physical vocabulary.

        The permutation is owned by the SDK: Coordinator only forwards whatever the SDK returns.
        """
        try:
            return list(self._obfuscator.data_1d_obf(list(token_ids)))
        except Exception as error:
            raise TokenObfuscationError("failed to obfuscate prompt token ids") from error

    def deobfuscate(self, token_ids: list[int]) -> list[int]:
        """Restore protected model output IDs to the tokenizer vocabulary."""
        try:
            return list(self._obfuscator.data_1d_deobf(list(token_ids)))
        except Exception as error:
            raise TokenObfuscationError("failed to deobfuscate output token ids") from error

    def deobfuscate_generate_responses(self, responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copy GenerateResponses and restore only their declared output token-id fields."""
        restored = deepcopy(responses)
        for response in restored:
            for choice in response.get("choices") or []:
                if not isinstance(choice, dict) or not isinstance(choice.get("token_ids"), list):
                    raise TokenObfuscationError("token-only response is missing output token ids")
                choice["token_ids"] = self.deobfuscate(choice["token_ids"])
        return restored
