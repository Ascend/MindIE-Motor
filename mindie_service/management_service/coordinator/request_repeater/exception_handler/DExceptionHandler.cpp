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

#include <string>
#include "boost/uuid/uuid_io.hpp"
#include "Logger.h"
#include "nlohmann/json.hpp"
#include "Configure.h"
#include "Communication.h"
#include "Communication.h"
#include "RequestRepeater.h"

namespace MINDIE::MS {

void RequestRepeater::ConnDErrHandler(uint64_t insId)
{
    auto ip = instancesRecord->GetIp(insId);
    auto port = instancesRecord->GetPort(insId);
    if (ip.empty() || port.empty()) {
        LOG_E("[%s] [RequestRepeater] Decode instance %lu has invalid IP or port, cannot handle connection error.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::D_EXCEPTIONHANDLER).c_str(), insId);
        return;
    }
    auto role = instancesRecord->GetRole(insId);
    if (role != MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        LOG_I("[DExceptionHandler] Skip processing instance %lu due to role change (not DECODE_INSTANCE).", insId);
        return; // 身份切换
    }

    if (Configure::Singleton()->CheckBackup() && !Configure::Singleton()->IsMaster()) {
        LOG_I("[DExceptionHandler] Skip processing instance %lu in backup mode (not master node).", insId);
        return; // 开启主备且为备的情况下不进行重连
    }

    // 调用 /v1/terminate-service
    LOG_E("[%s] [HttpClientAsync] Abnormal node, ip: %s, port:%s.",
        GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::HTTPCLIENT_ASYNC).c_str(),
        ip.c_str(), port.c_str());

    nlohmann::json jsonObj = nlohmann::json::object();
    jsonObj["ip"] = ip;
    jsonObj["port"] = port;
    std::string jsonString = jsonObj.dump();

    LOG_I("[DExceptionHandler] Reporting abnormal node %lu (%s:%s) to controller.", insId, ip.c_str(), port.c_str());
    ReportAbnormalNodeToController(jsonString);
}
}