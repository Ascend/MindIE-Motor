/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#ifndef MINDIE_MS_ALARM_REQUEST_HANDLER_H
#define MINDIE_MS_ALARM_REQUEST_HANDLER_H
#include <iostream>
#include <nlohmann/json.hpp>
#include "AlarmConfig.h"
#include "Logger.h"

namespace MINDIE {
namespace MS {
class AlarmRequestHandler {
public:
    static AlarmRequestHandler *GetInstance()
    {
        static AlarmRequestHandler instance;
        return &instance;
    }
    std::string FillServiceLevelDegradationAlarmInfo(AlarmCategory category,
        const std::string& serviceLocation,
        const std::string& additionalInformation,
        ScaleInReason reasonID) const;
    std::string FillControllerToSlaveEventInfo(ControllerToSlaveReason reasonID) const;
    std::string FillInstanceExceptionAlarmInfo(AlarmCategory category, std::string id,
        InstanceExceptionReason reasonID) const;
    std::string FillServerExceptionEventInfo(std::string ip, ServerExceptionReason reasonID) const;
    std::string FillClusterConnectionAlarmInfo(AlarmCategory category, ClusterConnectionReason reasonID) const;
    std::string FillLLMEngineAlarmInfo(AlarmCategory category, LLMEngineFaultReason reasonID,
        std::string errorLocation) const;

    AlarmRequestHandler(const AlarmRequestHandler &obj) = delete;
    AlarmRequestHandler &operator=(const AlarmRequestHandler &obj) = delete;
    AlarmRequestHandler(AlarmRequestHandler &&obj) = delete;
    AlarmRequestHandler &operator=(AlarmRequestHandler &&obj) = delete;
private:
    AlarmRequestHandler() = default;
    ~AlarmRequestHandler() = default;
};
} // MS
} // MindIE
#endif
