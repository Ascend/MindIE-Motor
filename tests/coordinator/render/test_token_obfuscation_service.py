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
from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from motor.config.coordinator import TokenObfuscationConfig
from motor.coordinator.render.token_obfuscation_service import (
    TokenObfuscationError,
    TokenObfuscationService,
    configure_obfuscation_library_path,
    resolve_token_obfuscation_config,
)


class FakeObfuscator:
    def __init__(self, seed_result=(0, "Success.")) -> None:
        self.seed_result = seed_result
        self.seed_content = None

    def set_seed_content(self, seed_content):
        self.seed_content = seed_content
        return self.seed_result

    def data_1d_obf(self, tokens):
        return [token + 100 for token in tokens]

    def data_1d_deobf(self, tokens):
        return [token - 100 for token in tokens]


class LengthChangingObfuscator(FakeObfuscator):
    """Permutation from a stub SDK that changes the prompt length."""

    def data_1d_obf(self, tokens):
        return [token + 100 for token in tokens[:-1]]


def test_token_obfuscation_round_trip_and_response_copy() -> None:
    config = TokenObfuscationConfig()
    backend = FakeObfuscator()
    service = TokenObfuscationService(config, backend)
    response = [{"choices": [{"index": 0, "token_ids": [111, 112]}], "usage": {"completion_tokens": 2}}]
    original = deepcopy(response)

    assert backend.seed_content == config.seed_content
    assert service.obfuscate([11, 12]) == [111, 112]
    assert service.deobfuscate([111, 112]) == [11, 12]
    assert service.deobfuscate_generate_responses(response)[0]["choices"][0]["token_ids"] == [11, 12]
    assert response == original


def test_sdk_result_is_forwarded_verbatim() -> None:
    """The SDK owns the permutation result; Coordinator forwards it without judging its length."""
    service = TokenObfuscationService(TokenObfuscationConfig(), LengthChangingObfuscator())

    assert service.obfuscate([11, 12]) == [111]


def test_token_obfuscation_rejects_failed_seed_initialization() -> None:
    with pytest.raises(TokenObfuscationError, match="seed was rejected"):
        TokenObfuscationService(TokenObfuscationConfig(), FakeObfuscator((1003, "invalid")))


def test_configure_obfuscation_library_path_prepends_sdk_libs(monkeypatch, tmp_path: Path) -> None:
    package_init = tmp_path / "ai_asset_obfuscate" / "__init__.py"
    package_init.parent.mkdir()
    package_init.touch()
    monkeypatch.setattr(
        "motor.coordinator.render.obfuscation_library.importlib.util.find_spec",
        lambda _name: SimpleNamespace(origin=str(package_init)),
    )
    monkeypatch.setenv("LD_LIBRARY_PATH", "/existing")

    configure_obfuscation_library_path()

    assert Path(package_init.parent / "libs").as_posix() == os.environ["LD_LIBRARY_PATH"].split(":")[0]


def _write_model_config(directory, payload) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def test_resolve_vocab_size_from_multimodal_model_dir(tmp_path) -> None:
    """Multimodal models nest the language model config under text_config."""
    _write_model_config(tmp_path / "model-obf", {"text_config": {"vocab_size": 151936}})

    resolved, source = resolve_token_obfuscation_config(
        TokenObfuscationConfig(enabled=True), [str(tmp_path / "model-obf")]
    )

    assert resolved.vocab_size == 151936
    assert "config.json" in source


def test_resolve_vocab_size_from_text_model_dir(tmp_path) -> None:
    """Text models keep vocab_size at the top level."""
    _write_model_config(tmp_path / "model-obf", {"vocab_size": 128256})

    resolved, _ = resolve_token_obfuscation_config(TokenObfuscationConfig(enabled=True), [str(tmp_path / "model-obf")])

    assert resolved.vocab_size == 128256


def test_resolve_vocab_size_keeps_explicit_value(tmp_path) -> None:
    """An explicitly configured vocab_size wins and no file is read."""
    resolved, source = resolve_token_obfuscation_config(
        TokenObfuscationConfig(enabled=True, vocab_size=151936), ["/does/not/exist"]
    )

    assert resolved.vocab_size == 151936
    assert source == "explicit configuration"


def test_resolve_vocab_size_skips_unusable_candidate(tmp_path) -> None:
    _write_model_config(tmp_path / "good", {"text_config": {"vocab_size": 151936}})

    resolved, _ = resolve_token_obfuscation_config(
        TokenObfuscationConfig(enabled=True), [str(tmp_path / "bad"), str(tmp_path / "good")]
    )

    assert resolved.vocab_size == 151936


def test_resolve_vocab_size_rejects_out_of_range_white_list(tmp_path) -> None:
    """The white-list range check must also run when vocab_size comes from the model directory."""
    _write_model_config(tmp_path / "model-obf", {"vocab_size": 128})

    with pytest.raises(TokenObfuscationError, match="within the resolved vocab_size 128"):
        resolve_token_obfuscation_config(
            TokenObfuscationConfig(enabled=True, token_white_list=[1, 200]),
            [str(tmp_path / "model-obf")],
        )


def test_resolve_vocab_size_fails_closed(tmp_path) -> None:
    """Missing model directory, unreadable file and missing vocab_size must all fail closed."""
    with pytest.raises(TokenObfuscationError, match="no served model directory is available"):
        resolve_token_obfuscation_config(TokenObfuscationConfig(enabled=True), [])

    with pytest.raises(TokenObfuscationError, match="failed to resolve token obfuscation vocab_size"):
        resolve_token_obfuscation_config(TokenObfuscationConfig(enabled=True), [str(tmp_path / "missing")])

    _write_model_config(tmp_path / "model-obf", {"text_config": {}})
    with pytest.raises(TokenObfuscationError, match="failed to resolve token obfuscation vocab_size"):
        resolve_token_obfuscation_config(TokenObfuscationConfig(enabled=True), [str(tmp_path / "model-obf")])
