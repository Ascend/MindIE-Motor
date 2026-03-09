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
#include "AlarmRequestHandler.h"

namespace MINDIE {
namespace MS {
std::string FillAlarmJson(AlarmRecord& alarm)
{
    nlohmann::json alarmRecordsJ = nlohmann::json::array();
    nlohmann::json alarmRecordJ = nlohmann::json::object();

    const char* modelIDEnv = std::getenv("MODEL_ID");
    std::string modelID = (modelIDEnv != nullptr) ? modelIDEnv : "";
    alarm.nativeMeDn = modelID;

    alarm.originSystemType = "MindIE";
    alarm.originSystem = "MindIE";
    alarm.originSystemName = "MindIE";
    alarm.additionalInformation = alarm.additionalInformation + ", pod id=" + modelID;

    alarmRecordJ["category"] = alarm.category;
    alarmRecordJ["cleared"] = alarm.cleared;
    alarmRecordJ["clearCategory"] = alarm.clearCategory;
    alarmRecordJ["occurUtc"] = alarm.occurUtc;
    alarmRecordJ["occurTime"] = alarm.occurTime;
    alarmRecordJ["nativeMeDn"] = alarm.nativeMeDn;
    alarmRecordJ["originSystem"] = alarm.originSystem;
    alarmRecordJ["originSystemName"] = alarm.originSystemName;
    alarmRecordJ["originSystemType"] = alarm.originSystemType;
    alarmRecordJ["location"] = alarm.location;
    alarmRecordJ["moi"] = alarm.moi;
    alarmRecordJ["eventType"] = alarm.eventType;
    alarmRecordJ["alarmId"] = alarm.alarmId;
    alarmRecordJ["alarmName"] = alarm.alarmName;
    alarmRecordJ["severity"] = alarm.severity;
    alarmRecordJ["probableCause"] = alarm.probableCause;
    alarmRecordJ["reasonId"] = alarm.reasonId;
    alarmRecordJ["serviceAffectedType"] = alarm.serviceAffectedType;
    alarmRecordJ["additionalInformation"] = alarm.additionalInformation;
    alarmRecordJ["matchKey"] = alarm.matchKey;

    alarmRecordsJ.emplace_back(alarmRecordJ);

    std::string jsonString = alarmRecordsJ.dump();
    return jsonString;
}

std::string AlarmRequestHandler::FillControllerToSlaveEventInfo(ControllerToSlaveReason reasonID) const
{
    AlarmRecord alarm;
    alarm.category = static_cast<int32_t>(AlarmCategory::ALARM_CATEGORY_EVENT);
    alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    const char* podIpEnv = std::getenv("POD_IP");
    std::string podIp = (podIpEnv != nullptr) ? podIpEnv : "";
    std::string serviceLocation = "service name=Controller, service ip=" + podIp;

    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_PROTECTION_SWITCH);
    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::CONTROLLER_TO_SLAVE);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::CONTROLLER_TO_SLAVE);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_MAJOR);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::CONTROLLER_TO_SLAVE);
    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_NO);
    alarm.additionalInformation = serviceLocation;

    return FillAlarmJson(alarm);
}

std::string AlarmRequestHandler::FillServiceLevelDegradationAlarmInfo(
    AlarmCategory category,
    const std::string& serviceLocation,
    const std::string& additionalInformation,
    ScaleInReason reasonID) const
{
    AlarmRecord alarm;
    alarm.category = static_cast<int32_t>(category);

    if (category == AlarmCategory::ALARM_CATEGORY_ALARM) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    } else if (category == AlarmCategory::ALARM_CATEGORY_OTHER_CHANGE) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    } else if (category == AlarmCategory::ALARM_CATEGORY_CLEAR) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    }

    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.additionalInformation = additionalInformation;

    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_STATE_CHANGE);
    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::SERVICE_LEVEL_DEGRADATION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::SERVICE_LEVEL_DEGRADATION);

    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_MAJOR); // 缩容告警-严重告警
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::SERVICE_LEVEL_DEGRADATION);
    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES); // 默认0

    return FillAlarmJson(alarm);
}

std::string AlarmRequestHandler::FillInstanceExceptionAlarmInfo(AlarmCategory category, std::string id,
    InstanceExceptionReason reasonID) const
{
    AlarmRecord alarm;
    alarm.category = static_cast<int32_t>(category);
    if (category == AlarmCategory::ALARM_CATEGORY_ALARM) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    } else {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    }
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    std::string serviceLocation = "servicename = controller,inst_type=p_inst_type or d_inst_type,inst_id=" + id;
    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_STATE_CHANGE);
    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::INSTANCE_EXCEPTION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::INSTANCE_EXCEPTION);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_CRITICAL);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::INSTANCE_EXCEPTION);
    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);
    alarm.additionalInformation = serviceLocation;

    return FillAlarmJson(alarm);
}

std::string AlarmRequestHandler::FillServerExceptionEventInfo(std::string ip, ServerExceptionReason reasonID) const
{
    AlarmRecord alarm;
    alarm.category = static_cast<int32_t>(AlarmCategory::ALARM_CATEGORY_EVENT);
    alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    std::string serviceLocation = "service name=Controller, mindie server ip=" + ip;

    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.additionalInformation = serviceLocation;
    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_STATE_CHANGE);
    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::SERVER_EXCEPTION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::SERVER_EXCEPTION);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_MAJOR);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::SERVER_EXCEPTION);
    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);

    return FillAlarmJson(alarm);
}

std::string AlarmRequestHandler::FillClusterConnectionAlarmInfo(
    AlarmCategory category, ClusterConnectionReason reasonID) const
{
    AlarmRecord alarm;
    alarm.category = static_cast<int32_t>(category);
    
    if (category == AlarmCategory::ALARM_CATEGORY_ALARM) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    } else if (category == AlarmCategory::ALARM_CATEGORY_CLEAR) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    }
    
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    const char* podIpEnv = std::getenv("POD_IP");
    std::string podIp = (podIpEnv != nullptr) ? podIpEnv : "";
    std::string serviceLocation = "service name=Controller, service ip=" + podIp;

    const char* clusterIpEnv = std::getenv("MINDX_SERVER_IP");
    std::string clusterIp = (clusterIpEnv != nullptr) ? clusterIpEnv : "";
    if (!clusterIp.empty()) {
        serviceLocation += ", cluster ip=" + clusterIp;
    }

    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_STATE_CHANGE);
    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::CLUSTER_CONNECTION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::CLUSTER_CONNECTION);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_CRITICAL);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::CLUSTER_CONNECTION);
    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);
    alarm.additionalInformation = serviceLocation;
    alarm.matchKey = std::to_string(alarm.reasonId);

    return FillAlarmJson(alarm);
}

std::string AlarmRequestHandler::FillLLMEngineAlarmInfo(
    AlarmCategory category,
    LLMEngineFaultReason reasonID,
    std::string errorLocation
    ) const
{
    AlarmRecord alarm;

    alarm.category = static_cast<int32_t>(category);
    alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    std::string serviceLocation = "service name=LLM Engine, service ip=" + errorLocation;
    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.additionalInformation = serviceLocation;
    
    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_STATE_CHANGE);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_MAJOR);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);
    alarm.reasonId = static_cast<int32_t>(reasonID);

    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::LLM_ENGINE_FAULT);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::LLM_ENGINE_FAULT);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::LLM_ENGINE_FAULT);

    return FillAlarmJson(alarm);
}

}
}
