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
#ifndef MINDIE_MS_COORDINATOR_ALARM_HANDLER_H
#define MINDIE_MS_COORDINATOR_ALARM_HANDLER_H
#include "Logger.h"
#include "AlarmConfig.h"
#include "Util.h"
#include "digs_instance.h"

namespace MINDIE::MS {

class AlarmRequestHandler {
public:
    static AlarmRequestHandler *GetInstance()
    {
        static AlarmRequestHandler instance;
        return &instance;
    }
    std::string FillCoordinatorExceptionAlarmInfo(AlarmCategory category, CoordinatorExceptionReason reasonID);
    std::string FillCoordinatorReqCongestionAlarmInfo(RequestCongestionReason reasonID,
        std::string additionalInformation);
    std::string FillCoordinatorConnPDExceptionAlarmnfo(AlarmCategory category,
        CoordinatorConnPDReason reasonID, std::string additionalInfo) const;
    int32_t SendAlarmToAlarmManager(std::string alarmInfo);
private:
    AlarmRequestHandler(const AlarmRequestHandler &obj) = delete;
    AlarmRequestHandler &operator=(const AlarmRequestHandler &obj) = delete;
    AlarmRequestHandler(AlarmRequestHandler &&obj) = delete;
    AlarmRequestHandler &operator=(AlarmRequestHandler &&obj) = delete;
    AlarmRequestHandler() = default;
    ~AlarmRequestHandler() = default;
};
} // MINDIE::MS
#endif