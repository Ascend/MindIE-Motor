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
from pathlib import Path

import pytest

from motor.config.coordinator import ImageObfuscationConfig, TokenObfuscationConfig
from motor.coordinator.render.image_obfuscation_service import (
    ImageObfuscationError,
    ImageObfuscationService,
    resolve_image_obfuscation_config,
)


class FakeVisionObfuscator:
    """Records every kwargs_data list it receives and permutes the payloads."""

    def __init__(self, seed_result=(0, "Success."), mutate=None) -> None:
        self.seed_result = seed_result
        self.seed_content = None
        self.calls: list = []
        self._mutate = mutate or (lambda item: item[:-1] + ("A" if item[-1] != "A" else "B"))

    def set_seed_content(self, seed_content, *args, **kwargs):
        self.seed_content = seed_content
        return self.seed_result

    def image_render_obf(self, image_items):
        self.calls.append(list(image_items))
        return [None if item is None else self._mutate(item) for item in image_items]


def _features(items=None, modality="image"):
    return {"mm_hashes": {modality: ["hash"]}, "kwargs_data": {modality: list(items or ["payload-a", "payload-b"])}}


def _service(backend: FakeVisionObfuscator) -> ImageObfuscationService:
    return ImageObfuscationService(ImageObfuscationConfig(enable=True), "seed-content", backend)


def test_obfuscates_kwargs_data_and_keeps_shape() -> None:
    backend = FakeVisionObfuscator()
    features = _features()

    assert _service(backend).obfuscate_render_features(features) == 2

    assert features["kwargs_data"]["image"] == [backend._mutate("payload-a"), backend._mutate("payload-b")]
    assert backend.calls == [["payload-a", "payload-b"]]
    assert backend.seed_content == "seed-content"


def test_null_cache_hit_items_are_preserved() -> None:
    backend = FakeVisionObfuscator()
    features = _features(["payload-a", None])

    assert _service(backend).obfuscate_render_features(features) == 1

    assert features["kwargs_data"]["image"][0] != "payload-a"
    assert features["kwargs_data"]["image"][1] is None


def test_every_modality_is_obfuscated() -> None:
    backend = FakeVisionObfuscator()
    features = {"kwargs_data": {"image": ["i1"], "video": ["v1", "v2"]}}

    assert _service(backend).obfuscate_render_features(features) == 3

    assert backend.calls == [["i1"], ["v1", "v2"]]


def test_features_without_kwargs_data_is_a_noop() -> None:
    backend = FakeVisionObfuscator()

    assert _service(backend).obfuscate_render_features({"mm_hashes": {"image": ["h"]}}) == 0
    assert _service(backend).obfuscate_render_features({"kwargs_data": None}) == 0
    assert _service(backend).obfuscate_render_features({"kwargs_data": {}}) == 0
    assert backend.calls == []


def test_kwargs_data_must_be_an_object() -> None:
    with pytest.raises(ImageObfuscationError, match="kwargs_data must be an object"):
        _service(FakeVisionObfuscator()).obfuscate_render_features({"kwargs_data": ["nope"]})


def test_modality_items_must_be_a_list() -> None:
    with pytest.raises(ImageObfuscationError, match=r"kwargs_data\.image must be a list"):
        _service(FakeVisionObfuscator()).obfuscate_render_features({"kwargs_data": {"image": "nope"}})


def test_sdk_failure_fails_closed() -> None:
    def explode(_item):
        raise RuntimeError("sdk blew up")

    backend = FakeVisionObfuscator(mutate=explode)
    with pytest.raises(
        ImageObfuscationError, match="failed to obfuscate Render image items: RuntimeError: sdk blew up"
    ):
        _service(backend).obfuscate_render_features(_features())


def test_rejected_seed_fails_closed() -> None:
    with pytest.raises(ImageObfuscationError, match="seed was rejected"):
        _service(FakeVisionObfuscator((1003, "invalid")))


def test_image_config_has_no_preset_geometry() -> None:
    """Vision geometry is model-specific, so it must not be preset in code."""
    image_config = TokenObfuscationConfig().image_config
    assert image_config.enable is False
    assert (image_config.patch_size, image_config.merge_size, image_config.temporal_patch_size) == (0, 0, 0)
    assert (image_config.shortest_edge, image_config.longest_edge) == (0, 0)


def _write_preprocessor_config(directory, payload) -> "Path":
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "preprocessor_config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


QWEN3_VL_PREPROCESSOR = {
    "patch_size": 16,
    "temporal_patch_size": 2,
    "merge_size": 2,
    "size": {"longest_edge": 16777216, "shortest_edge": 65536},
}


def test_resolve_geometry_from_model_dir_when_unset(tmp_path) -> None:
    """Unset geometry is read from the served model directory."""
    _write_preprocessor_config(tmp_path / "model-obf", QWEN3_VL_PREPROCESSOR)

    resolved, source = resolve_image_obfuscation_config(
        ImageObfuscationConfig(enable=True), [str(tmp_path / "model-obf")]
    )

    assert (resolved.patch_size, resolved.temporal_patch_size) == (16, 2)
    assert (resolved.merge_size, resolved.shortest_edge, resolved.longest_edge) == (2, 65536, 16777216)
    assert "preprocessor_config.json" in source


def test_resolve_geometry_keeps_explicit_values(tmp_path) -> None:
    """Explicitly configured fields always win, the rest is read from the model directory."""
    _write_preprocessor_config(tmp_path / "model-obf", QWEN3_VL_PREPROCESSOR)

    resolved, _ = resolve_image_obfuscation_config(
        ImageObfuscationConfig(enable=True, patch_size=8, temporal_patch_size=1),
        [str(tmp_path / "model-obf")],
    )

    assert (resolved.patch_size, resolved.temporal_patch_size) == (8, 1)
    assert (resolved.merge_size, resolved.shortest_edge, resolved.longest_edge) == (2, 65536, 16777216)


def test_resolve_geometry_supports_legacy_pixel_bounds(tmp_path) -> None:
    """Older processors describe the pixel bounds as min_pixels / max_pixels."""
    _write_preprocessor_config(
        tmp_path / "qwen2-vl",
        {"patch_size": 14, "temporal_patch_size": 2, "merge_size": 2, "min_pixels": 3136, "max_pixels": 12845056},
    )

    resolved, _ = resolve_image_obfuscation_config(ImageObfuscationConfig(enable=True), [str(tmp_path / "qwen2-vl")])

    assert (resolved.patch_size, resolved.shortest_edge, resolved.longest_edge) == (14, 3136, 12845056)


def test_resolve_geometry_prefers_explicit_model_path(tmp_path) -> None:
    """image_config.model_path wins over the engine-declared directories."""
    _write_preprocessor_config(tmp_path / "pinned", QWEN3_VL_PREPROCESSOR)

    resolved, source = resolve_image_obfuscation_config(
        ImageObfuscationConfig(enable=True, model_path=str(tmp_path / "pinned")),
        ["/does/not/exist"],
    )

    assert resolved.patch_size == 16
    assert str(tmp_path / "pinned") in source


def test_resolve_geometry_rejects_inverted_bounds_after_resolution(tmp_path) -> None:
    """A partially configured geometry must stay consistent after completion."""
    _write_preprocessor_config(tmp_path / "model-obf", QWEN3_VL_PREPROCESSOR)

    with pytest.raises(ImageObfuscationError, match="greater than or equal to shortest_edge"):
        resolve_image_obfuscation_config(
            ImageObfuscationConfig(enable=True, longest_edge=1024),
            [str(tmp_path / "model-obf")],
        )


def test_resolve_geometry_fails_closed_without_model_dir() -> None:
    with pytest.raises(ImageObfuscationError, match="no served model directory is available"):
        resolve_image_obfuscation_config(ImageObfuscationConfig(enable=True), [])


def test_resolve_geometry_skips_unusable_candidate(tmp_path) -> None:
    """A later readable directory is still used when the first one fails."""
    _write_preprocessor_config(tmp_path / "good", QWEN3_VL_PREPROCESSOR)

    resolved, _ = resolve_image_obfuscation_config(
        ImageObfuscationConfig(enable=True), [str(tmp_path / "bad"), str(tmp_path / "good")]
    )

    assert resolved.patch_size == 16


def test_resolve_geometry_fails_closed_on_unusable_model_dir(tmp_path) -> None:
    """Unreadable or incomplete model directories must fail closed with a clear error."""
    with pytest.raises(ImageObfuscationError, match="failed to resolve image obfuscation geometry"):
        resolve_image_obfuscation_config(ImageObfuscationConfig(enable=True), [str(tmp_path / "missing")])

    _write_preprocessor_config(tmp_path / "partial", {"patch_size": 16})
    with pytest.raises(ImageObfuscationError, match="is missing"):
        resolve_image_obfuscation_config(ImageObfuscationConfig(enable=True), [str(tmp_path / "partial")])


def test_sdk_result_is_committed_verbatim() -> None:
    """The SDK owns the permutation result; Coordinator forwards it without re-shaping or judging."""

    class VerbatimObfuscator(FakeVisionObfuscator):
        def image_render_obf(self, image_items):
            return ["only-one"]

    features = _features()

    replaced = _service(VerbatimObfuscator()).obfuscate_render_features(features)

    assert features["kwargs_data"]["image"] == ["only-one"]
    assert replaced == 1


def test_partial_rewrite_is_not_committed() -> None:
    """第二个模态抛错时，已成功的模态也不能被写入（原子性）。"""
    features = {"kwargs_data": {"image": ["payload-a"], "video": ["v1", "v2"]}}
    before = deepcopy(features)

    class SecondModalityBadObfuscator(FakeVisionObfuscator):
        def image_render_obf(self, image_items):
            if len(image_items) == 2:
                raise RuntimeError("sdk failed")
            return [self._mutate(item) for item in image_items]

    with pytest.raises(ImageObfuscationError, match="failed to obfuscate Render video items"):
        _service(SecondModalityBadObfuscator()).obfuscate_render_features(features)
    assert features == before
