# Copyright 2023-2024 SGLang Team
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under both the Mulan PSL v2 and the Apache License, Version 2.0.
# You may choose to use this software under the terms of either license.
#
# ---------------------------------------------------------------------------
# Mulan PSL v2:
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
#
# Apache License, Version 2.0:
# You may obtain a copy of the License at:
#         http://www.apache.org/licenses/LICENSE-2.0
# ---------------------------------------------------------------------------
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from typing import Any

from motor.common.logger import get_logger
from motor.node_manager.core.services.native_engine.backends.base import IConfig
from motor.engine_server.core.engine import Engine

logger = get_logger(__name__)


def _kill_engine_children() -> None:
    """Kill scheduler and detokenizer child processes."""
    try:
        from sglang.srt.utils import kill_process_tree
        import os

        kill_process_tree(os.getpid(), include_parent=False)
    except Exception as e:
        logger.exception("Error killing SGLang engine child processes: %s", e)


class SGLangEngine(Engine):
    def __init__(self, config: IConfig):
        self._template_manager: Any | None = None
        self._tokenizer_manager: Any | None = None
        self._multi_tokenizer_args_shm: Any = None
        self.config = config

    def launch(self) -> Any:
        from sglang.srt.entrypoints.engine import _launch_subprocesses

        server_args = self.config.get_args()
        if server_args is None:
            raise ValueError("SGLang ServerArgs not available.")

        logger.info(
            "EngineServer launching SGLang engine only (role=%s), InferEndpoint will be started separately.",
            self.config.get_endpoint_config().role,
        )

        (
            self._tokenizer_manager,
            self._template_manager,
            scheduler_infos,
            port_args,
        ) = _launch_subprocesses(
            server_args=server_args,
        )

        if self._tokenizer_manager is None or self._template_manager is None:
            raise RuntimeError(
                "SGLang _launch_subprocesses returned None for tokenizer/template manager (e.g. node_rank >= 1)."
            )

        if server_args.tokenizer_worker_num > 1:
            from sglang.srt.managers.multi_tokenizer_mixin import write_data_for_multi_tokenizer

            self._multi_tokenizer_args_shm = write_data_for_multi_tokenizer(port_args, server_args, scheduler_infos[0])

        return self._tokenizer_manager, self._template_manager

    def shutdown(self) -> None:
        if self._multi_tokenizer_args_shm is not None:
            self._multi_tokenizer_args_shm.close()
        self._multi_tokenizer_args_shm = None
        self._tokenizer_manager.socket_mapping.clear_all_sockets()
        _kill_engine_children()
