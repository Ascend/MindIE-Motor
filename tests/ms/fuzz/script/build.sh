#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

set -e

BASE_PATH=$(cd $(dirname $0); pwd)
PROJECT_PATH=$BASE_PATH/../../../..
PROJECT_PATH=`realpath $PROJECT_PATH`
MINDIE_MS_SRC_PATH=$PROJECT_PATH/mindie_service/management_service
MINDIE_MS_TEST_PATH=$PROJECT_PATH/tests/ms
MINDIE_MS_OUTPUT_PATH=$PROJECT_PATH/mindie_service/management_service/output
MINDIE_MS_OPEN_SOURCE_PATH=/open_source

source ${MINDIE_MS_TEST_PATH}/ms_test_util.sh

_prepare_3rd_party()
{
    rm -rf ${MINDIE_MS_SRC_PATH}/open_source/*
    rm -rf ${MINDIE_MS_TEST_PATH}/open_source/*

    mkdir -p ${MINDIE_MS_SRC_PATH}/open_source/
    mkdir -p ${MINDIE_MS_TEST_PATH}/open_source/

    cp -r ${MINDIE_MS_OPEN_SOURCE_PATH}/boost ${MINDIE_MS_SRC_PATH}/open_source/
    cp -r ${MINDIE_MS_OPEN_SOURCE_PATH}/kmc ${MINDIE_MS_SRC_PATH}/open_source/
    cp -r ${MINDIE_MS_OPEN_SOURCE_PATH}/libboundscheck ${MINDIE_MS_SRC_PATH}/open_source/
    cp -r ${MINDIE_MS_OPEN_SOURCE_PATH}/nlohmann ${MINDIE_MS_SRC_PATH}/open_source/
    cp -r ${MINDIE_MS_OPEN_SOURCE_PATH}/openssl ${MINDIE_MS_SRC_PATH}/open_source/

    cp -r ${MINDIE_MS_OPEN_SOURCE_PATH}/cpp-stub ${MINDIE_MS_TEST_PATH}/open_source/
    cp -r ${MINDIE_MS_OPEN_SOURCE_PATH}/googletest ${MINDIE_MS_TEST_PATH}/open_source/
    cp -r ${MINDIE_MS_OPEN_SOURCE_PATH}/lcov ${MINDIE_MS_TEST_PATH}/open_source/
    cp -r ${MINDIE_MS_OPEN_SOURCE_PATH}/secodefuzz ${MINDIE_MS_TEST_PATH}/open_source/
}

_build_secodefuzz()
{
    cd ${MINDIE_MS_TEST_PATH}/open_source/secodefuzz/OpenSource/test/test1
    export CC=gcc
    sed -i "s#/test/mayp/gcc8.1.0/bin/gcc#gcc#g" Makefile
    make Secodefuzz_so
    make Secodepits_so_ex
}

_build_lcov()
{
    cd ${MINDIE_MS_TEST_PATH}/open_source/lcov
    make -j8 && make install
}

_build_fuzz()
{
    _build_secodefuzz
    cd ${MINDIE_MS_TEST_PATH}/dt
    rm -rf build && mkdir build
    cd build
    cmake -DCMAKE_BUILD_TYPE=Debug -DBUILDING_STAGE=gcov ..
    make -j8

    cd ${MINDIE_MS_TEST_PATH}/fuzz
    rm -rf build && mkdir build
    cd build
    cmake -DCMAKE_BUILD_TYPE=Debug -DBUILDING_STAGE=fuzz ..
    make -j8
}

main()
{
    # 替换LOG宏，使其不统计对应分支覆盖率
    python3 ${MINDIE_MS_TEST_PATH}/scripts/replace_log_macro.py
    _prepare_3rd_party

    cd $MINDIE_MS_SRC_PATH
    sh build.sh -s asan

    _set_env

    _build_lcov

    _build_fuzz
    cp ${MINDIE_MS_TEST_PATH}/fuzz/mindie_ms_controller_fuzz /out/
    cp ${MINDIE_MS_TEST_PATH}/fuzz/mindie_ms_coordinator_fuzz /out/

    chmod 640 -R $MINDIE_MS_SRC_PATH/config/**/*.json
    chmod 640 -R $MINDIE_MS_SRC_PATH/output/config/**/*.json

    cd ${MINDIE_MS_TEST_PATH}/dt

    export ASAN_OPTIONS=log_path=/crashlog/report_crash_asan
}

main "$@"