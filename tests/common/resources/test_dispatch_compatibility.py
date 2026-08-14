# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for dispatch-profile inference."""

from types import SimpleNamespace

from motor.common.resources.dispatch import (
    DispatchProfile,
    infer_vllm_dispatch_profile_from_config,
)


class _EngineConfig:
    def __init__(self, configs):
        self.configs = configs

    def get(self, key, default=None):
        return self.configs.get(key, default)


class _Config:
    def __init__(self, engine_type="vllm", engine_config=None, dispatch_profile=None):
        self._endpoint_config = SimpleNamespace(
            engine_type=engine_type,
            deploy_config=SimpleNamespace(
                engine_config=_EngineConfig(engine_config or {}),
                dispatch_profile=dispatch_profile,
            ),
        )

    def get_endpoint_config(self):
        return self._endpoint_config


def test_infer_vllm_dispatch_profile_from_config_layerwise():
    config = _Config(
        engine_config={
            "kv_transfer_config": {
                "kv_connector": "MooncakeLayerwiseConnector",
            }
        }
    )
    assert infer_vllm_dispatch_profile_from_config(config) == DispatchProfile.TRIGGER


def test_infer_vllm_dispatch_profile_from_config_handoff():
    config = _Config(
        engine_config={
            "kv_transfer_config": {
                "kv_connector": "MooncakeHybridConnector",
            }
        }
    )
    assert infer_vllm_dispatch_profile_from_config(config) == DispatchProfile.HANDOFF


def test_infer_vllm_dispatch_profile_from_config_non_vllm_engine():
    config = _Config(engine_type="sglang")
    assert infer_vllm_dispatch_profile_from_config(config) == DispatchProfile.UNKNOWN
