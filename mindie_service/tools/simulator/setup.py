#!/usr/bin/python3.11
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
from setuptools import setup, find_packages


os.environ["SOURCE_DATE_EPOCH"] = "0"

setup(
    version='0.0.1',
    author='Ascend',
    name='mindiesimulator',
    install_requires=[],
    description='MindIE Service Tools: A simulator tool to tune parameters automatically',
    python_requires='>=3.9',
    include_package_data=True,
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'simulator=mindiesimulator.main:main'
        ],
    },
    package_data={
        'mindiesimulator': ['config/config.json']
    }
)