#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import logging
from setuptools import setup, find_packages

os.environ['SOURCE_DATE_EPOCH'] = '0'


def get_version() -> str:
    """
    Return version string.

    Priority:
    1. Environment variable MINDIE_MOTOR_VERSION_OVERRIDE
    2. Default version
    """
    version = os.getenv("MINDIE_MOTOR_VERSION_OVERRIDE", "1.0.0")
    logging.info(f"Use mindie llm version: {version}")
    return version


setup(
    name='node_manager',
    version=get_version(),
    description='node health manager',
    packages=find_packages(),
    python_requires=">=3.10",
    entry_points={
        'console_scripts': [
            'node_manager=node_manager.node_manager_run:main',
        ],
    }
)