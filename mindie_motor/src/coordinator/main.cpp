/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#include <cstdlib>
#include <algorithm>
#include "Coordinator.h"
#include "Logger.h"
#include "Configure.h"
#include "DynamicConfigHandler.h"


using namespace MINDIE::MS;

static bool ParsingParams(int argc, char *argv[])
{
    std::string errorCode = GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::MAIN);
    if (argc > 3) { // 超过3个参数
        LOG_E("[%s] [Coordinator] Incorrect usage. The expected format is: %s <predict_ip> <manage_ip>",
            errorCode.c_str(), argv[0]);
        return false;
    }
    if (argc > 1) {
        if (!IsValidIp(std::string(argv[1]), Configure::Singleton()->httpConfig.allAllZeroIpListening)) {
            LOG_E("[%s] [Coordinator] Argument 'predict_ip' is invalid.", errorCode.c_str());
            return false;
        }
        Configure::Singleton()->httpConfig.predIp = std::string(argv[1]); // 1：入参索引值，对应predIp
    }
    if (argc > 2) { // 超过2个参数
        if (!IsValidIp(std::string(argv[2]), Configure::Singleton()->httpConfig.allAllZeroIpListening)) {
            LOG_E("[%s] [Coordinator] Argument 'manage_ip' is invalid.", errorCode.c_str());
            return false;
        }
        Configure::Singleton()->httpConfig.managementIp = std::string(argv[2]); // 2：入参索引值，对应managementIp
    }
    return true;
}

int main(int argc, char *argv[])
{
    if (Configure::Singleton()->Init() != 0) {
        LOG_E("[%s] [Coordinator] Configure initialize failed.",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::MAIN).c_str());
        return -1;
    }
    size_t scheduleTimeoutMax = 3600;
    size_t firstTokenTimeoutMax = 3600;
    size_t inferTimeoutMax = 65536;
    DynamicConfigHandler::Start("Coordinator");
    // when detected EnableDynamicAdjustTimeoutConfig, set timeout to max
    DynamicConfigHandler::RegisterCallBackFunction<Configure>("EnableDynamicAdjustTimeoutConfig",
        Configure::Singleton(), &Configure::SetScheduleTimeout, scheduleTimeoutMax);
    DynamicConfigHandler::RegisterCallBackFunction<Configure>("EnableDynamicAdjustTimeoutConfig",
        Configure::Singleton(), &Configure::SetFirstTokenTimeout, firstTokenTimeoutMax);
    DynamicConfigHandler::RegisterCallBackFunction<Configure>("EnableDynamicAdjustTimeoutConfig",
        Configure::Singleton(), &Configure::SetInferTimeout, inferTimeoutMax);
    if (!ParsingParams(argc, argv)) {
        return -1;
    }

    try {
        MINDIE::MS::Coordinator coordinator;
        coordinator.Run();
    } catch (const std::exception& e) {
        LOG_E("[%s] Error is %s", GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::MAIN).c_str(), e.what());
        return EXIT_FAILURE;
    } catch (...) {
        LOG_E("[%s] Unexpected error.", GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::MAIN).c_str());
        return EXIT_FAILURE;
    }
}