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

TEST_MODE=${3:-"gcov"}

_download_kmc()
{
    artget pull "MindIE-KMC 1.0.RC3.B110" -ru software -rp "x86_64-linux/3.10" -ap "${MINDIE_MS_SRC_PATH}/open_source/kmc"
}

_build_kmc()
{
    cd ${PROJECT_PATH}/utils/cert
    chmod +x -R *
    sh build.sh release true
    cp -r ${PROJECT_PATH}/utils/cert/output ${MINDIE_MS_SRC_PATH}/open_source/kmc
}

_build_proto() {
    # 确保进入目录成功
    cd "${PROJECT_PATH}/mindie_service/management_service" || { echo "Error: Failed to enter build directory"; exit 1; }
    
    # 仅对必要文件赋权
    if [ -f "build.sh" ]; then
        chmod +x "build.sh"
    else
        echo "Error: build.sh not found"
        exit 1
    fi

    # 定义要处理的 .proto 文件列表
    protos=(
        "rpc.proto:rpc.pb.h"
        "etcdserver.proto:etcdserver.pb.h"
        "kv.proto:kv.pb.h"
    )

    # 加载函数（假设 build.sh 中定义了 gen_etcd_grpc_proto）
    source ./build.sh || { echo "Error: Failed to load build.sh"; exit 1; }

    # 遍历生成代码
    for proto_pair in "${protos[@]}"; do
        proto_file="${proto_pair%%:*}"  # 提取 .proto 文件名
        target_header="${proto_pair##*:}"  # 提取目标头文件名
        gen_etcd_grpc_proto "${proto_file}" "${target_header}" || exit 1
    done
}

_build_src()
{
    local test_mode=${1?}
    cd $MINDIE_MS_SRC_PATH
    rm -rf build
    mkdir build
    cd build
    cmake .. -DCMAKE_BUILD_TYPE=Release -DBUILDING_STAGE=publish -DCMAKE_INSTALL_PREFIX:PATH=`pwd`/install -DCMAKE_CXX_FLAGS="-DUT_FLAG=ON"
    make -j8
    make install
}

_set_env()
{
    export LD_LIBRARY_PATH=${BUILD_MIES_3RDPARTY_INSTALL_DIR}/openssl/lib:${LD_LIBRARY_PATH}
    export LD_LIBRARY_PATH=${BUILD_MINDIE_SERVICE_INSTALL_DIR}/lib:${LD_LIBRARY_PATH}
    export LD_LIBRARY_PATH=${BUILD_MIES_3RDPARTY_INSTALL_DIR}/libboundscheck/lib:${LD_LIBRARY_PATH}
}

_build_dt()
{
    local test_mode=${1?}

    cd ${MINDIE_MS_TEST_PATH}/dt
    rm -rf build && mkdir build
    cd build

    cmake -DCMAKE_BUILD_TYPE=Debug -DBUILDING_STAGE=${test_mode} -DCMAKE_CXX_FLAGS="-DUT_FLAG=ON" ..
    make -j8
}

# 蓝区门禁需要执行的冒烟用例，在该处补充
_run_smoke_dt_test()
{
    cd ${MINDIE_MS_TEST_PATH}/dt
    ./mindie_ms_controller_ut
    ./mindie_ms_ipc_ut
    ./mindie_ms_securityutils_ut
    ./mindie_service_utils_ut

    ./mindie_ms_coordinator_ut --gtest_filter=TestClusterMonitor.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestCoordinatorConfig*
    ./mindie_ms_coordinator_ut --gtest_filter=TestHttp.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestCoordinatorScheduler.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestDigsScheduler.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestDIGSSchedulerImpl.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestControllerListener.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestDigsMetaResource.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestGlobalScheduler.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestSchedulerFramework.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestDigsResourceInfo.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestDigsRequest.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestDigsRequestManager.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestDigsRequestProfiler.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestStaticAllocPolicy.*

    ./mindie_ms_coordinator_ut --gtest_filter=TestCoordinatorMain.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestPDInferReq.*
    # ./mindie_ms_coordinator_ut --gtest_filter=TestInferReq.* 
    ./mindie_ms_coordinator_ut --gtest_filter=TestDResError.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestMaxReq.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestOpenAI.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestPResError.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestPResInvalid.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestRetry.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestSelfDevelop.*
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestDefaultPrefixCacheTC01
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestDefaultPrefixCacheTC02
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestDefaultPrefixCacheTC03
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestDefaultRoundRobinTC01
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestDefaultRoundRobinTC02
    ./mindie_ms_coordinator_ut --gtest_filter=TestTGI.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestTokenizerAndProbe.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestTriton.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestVLLM.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestSingleInferReqPrompt.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestMaxConnection.*

    ./mindie_ms_coordinator_ut --gtest_filter=TestSingleDFX.TestSingleErrorTC01
    ./mindie_ms_coordinator_ut --gtest_filter=TestSingleDFX.TestSingleTimeoutTC01
    ./mindie_ms_coordinator_ut --gtest_filter=TestSingleDFX.SingleInstancesAdd
    ./mindie_ms_coordinator_ut --gtest_filter=TestSingleDFX.SingleInstancesUpdate
    ./mindie_ms_coordinator_ut --gtest_filter=TestSingleDFX.SingleInstancesRemove



    ./mindie_ms_coordinator_ut --gtest_filter=*.TestReqKeepAliveTimeoutTC01
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestReqKeepAliveTimeoutTC02
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestReqKeepAliveTimeoutTC03

    ./mindie_ms_coordinator_ut --gtest_filter=*.TestSingleNodeMetricsTC01
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestMultiSingleNodeMetricsTC01
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestSingleNodeMetricsSortTimeReUseTC01
    ./mindie_ms_coordinator_ut --gtest_filter=*.TestPDSeparateMetricsTC01
    ./mindie_ms_coordinator_ut --gtest_filter=TestMetrics.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestSchedulerInfo.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestConfigure.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestFlexProcess.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestParseMetric.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestFillInstancesInfoSplitedByFlex.*
    ./mindie_ms_coordinator_ut --gtest_filter=TestMemoryUtil.*
}


# 除冒烟外的测试用例，在该处补充
_run_other_dt_test()
{
    cd ${MINDIE_MS_TEST_PATH}/dt

    ./mindie_ms_coordinator_ut --gtest_filter=*.TestRetryFailedTC01
    ./mindie_ms_coordinator_ut --gtest_filter=TestTimeout.TestFirstTokenTimeoutTC01
    ./mindie_ms_coordinator_ut --gtest_filter=TestTimeout.TestInferTimeoutTC01
    ./mindie_ms_coordinator_ut --gtest_filter=TestRetryFailed.TestRetryFailedTC02
}

# 仅ms黄区执行的用例
_run_dt_y_test()
{
    cd ${MINDIE_MS_TEST_PATH}/dt

    sudo ulimit -n 99999
    sudo ifconfig eth0:1 172.16.0.1 netmask 255.255.254.0 up
    sudo ifconfig eth0:2 172.16.0.2 netmask 255.255.254.0 up
    sudo ifconfig eth0:3 172.16.0.3 netmask 255.255.254.0 up
    sudo ifconfig eth0:4 172.16.0.4 netmask 255.255.254.0 up
    sudo ifconfig eth0:5 172.16.0.5 netmask 255.255.254.0 up
    sudo ifconfig eth0:6 172.16.0.6 netmask 255.255.254.0 up
    sudo ifconfig eth0:7 172.16.0.7 netmask 255.255.254.0 up
    sudo ifconfig eth0:8 172.16.0.8 netmask 255.255.254.0 up

    ./mindie_ms_coordinator_ut --gtest_filter=TestSSLRequest.*
    ./mindie_ms_coordinator_ut --gtest_filter=StressTestPD.TestPDOpenAIStressTC01
    ./mindie_ms_coordinator_ut --gtest_filter=StressTestPD.TestPDOpenAIStressTC02
    ./mindie_ms_coordinator_ut --gtest_filter=StressTestSingleNode.TestSingleNodeOpenAIStressTC01
    ./mindie_ms_coordinator_ut --gtest_filter=StressTestSingleNode.TestSingleNodeOpenAIStressTC02
    ./mindie_ms_coordinator_ut --gtest_filter=TestMetricsPDSeparate.TestPDSeparateMetricsTC02

    
}


_gen_coverage()
{
    rm -rf tests/ms/dt/build/coordinator/config_test/CMakeFiles/mindie_ms_coordinator_config_ut.dir/TestCoordinatorConfig.gc*

    cd ${PROJECT_PATH}
    rm -rf ${PROJECT_PATH}/result ${PROJECT_PATH}/test.info ${PROJECT_PATH}/clean.info
    lcov --directory . --capture --output-file test.info -rc lcov_branch_coverage=1
    lcov --remove test.info '*third_party*' '*open_source*' '*test*' '*/usr/*' '*mock*' '*grpc_proto*' '*etcd_proto*' -o clean.info --rc lcov_branch_coverage=1
    genhtml --branch-coverage clean.info -o result -p ${PROJECT_PATH}

    # 合并测试结果
    cd ${PROJECT_PATH}/result
    python3 ${MINDIE_MS_TEST_PATH}/scripts/merge_gtest_result.py test_detail ${MINDIE_MS_TEST_PATH}/dt

    HTML_FILE="${PROJECT_PATH}/result/index.html"
    line_coverage=$(awk -F'[<>]' '/<td class="headerItem">Lines:/{getline; getline; getline; print $3}' "$HTML_FILE" | sed 's/\s*//g')
    echo "Line coverage: $line_coverage"
    line_coverage_value=$(echo "$line_coverage" | sed 's/%//')

    COVERAGE_TARGET='68'
    if (( $(echo "$line_coverage_value < $COVERAGE_TARGET" | bc -l) )); then
    echo "execute dt pipeline failed, line coverage ($line_coverage) is less than target value ($COVERAGE_TARGET%)"
        exit 1
    fi
    echo "execute dt pipeline success"
}
