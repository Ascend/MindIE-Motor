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
#include "AlarmRequestHandler.h"

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

    if (!instancesRecord->IsFaultyNode(insId)) {
        SendAlarm("decode instance address=" + ip + ":" + port);
        const auto maxRetries = Configure::Singleton()->exceptionConfig.maxRetry;
        for (size_t i = 0; i < maxRetries; ++i) {
            auto ret = LinkWithDNode(ip, port);
            LOG_D("[RequestRepeater] ConnDErrHandler Successfully add link with decode node at %s:%s.",
                ip.c_str(), port.c_str());
            if (ret == 0) {
                LOG_D("[%s] [RequestRepeater] ConnDErrHandler Successfully add link with decode node at %s:%s.",
                    GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::D_EXCEPTIONHANDLER).c_str(),
                    ip.c_str(), port.c_str());
                return;
            }
        }
        LOG_E("[%s] [RequestRepeater] Decode instance %s:%s connect failed.",
              GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::D_EXCEPTIONHANDLER).c_str(),
              ip.c_str(), port.c_str());
        AddToFaulty(insId);
    } else {
        LOG_D("[RequestRepeater] Decode instance %lu has been removed.", insId);
    }
}

void RequestRepeater::AddToFaulty(uint64_t insId)
{
    instancesRecord->AddFaultNode(insId);
    const auto &virtualIds = instancesRecord->GetVirtualIdToIds(insId);
    if (!virtualIds.empty()) {
        for (uint64_t iterId : virtualIds) {
            LOG_D("[RequestRepeater] Remove Decode instance %lu.", iterId);
            instancesRecord->AddFaultNode(iterId);
            scheduler->RemoveInstance({iterId});
            instancesRecord->RemoveInstance(iterId);
        }
    }
}

void RequestRepeater::SendAlarm(std::string additionalInfo)
{
    std::string alarmMsg = AlarmRequestHandler::GetInstance()->FillCoordinatorConnPDExceptionAlarmnfo(
        AlarmCategory::ALARM_CATEGORY_ALARM, CoordinatorConnPDReason::HTTP_EXCEPTION, additionalInfo);
    if (AlarmRequestHandler::GetInstance()->SendAlarmToAlarmManager(alarmMsg) != 0) {
        LOG_E("[%s] [RequestRepeater] Send connection alarm failed.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str());
    }
}
}