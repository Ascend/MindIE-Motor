# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.logger import get_logger
from motor.controller.fault_tolerance.strategy import StrategyBase


logger = get_logger(__name__)


class TokenReinferenceStrategy(StrategyBase):
    def __init__(self) -> None:
        super().__init__()
    
    def execute(self, instance_id: int) -> None:
        while not self.event.is_set():
            pass
    
    def stop(self) -> None:
        logger.info("Token Reinference strategy can not be stopped.")