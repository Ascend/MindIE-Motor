# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import inspect

import motor.coordinator.process.inference_manager as inference_manager


def test_inference_worker_initializes_the_shared_kv_affinity_tokenizer():
    """The worker uses the in-process KV-affinity tokenizer, not a sidecar."""
    assert hasattr(inference_manager, "TokenizerManager")
    source = inspect.getsource(inference_manager.run_inference_worker_proc)
    assert "TokenizerManager(config)" in source
