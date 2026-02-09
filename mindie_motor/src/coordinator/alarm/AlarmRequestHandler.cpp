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
#include "HttpClient.h"
#include "Configure.h"

namespace MINDIE::MS {

std::string FillAlarmJson(AlarmRecord& alarm)
{
    nlohmann::json alarmRecordsJ = nlohmann::json::array();
    nlohmann::json alarmRecordJ = nlohmann::json::object();

    const char* modelIDEnv = std::getenv("MODEL_ID");
    std::string modelID = (modelIDEnv != nullptr) ? modelIDEnv : "";
    size_t max_env_len = 256;
    if (modelID.size() > max_env_len) {
        modelID = modelID.substr(0, max_env_len);
    }
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

    alarmRecordsJ.emplace_back(alarmRecordJ);

    std::string jsonString = alarmRecordsJ.dump();
    return jsonString;
}
std::string AlarmRequestHandler::FillCoordinatorExceptionAlarmInfo(AlarmCategory category,
    CoordinatorExceptionReason reasonID)
{
    AlarmRecord alarm;

    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.category = static_cast<int32_t>(category);
    if (category == AlarmCategory::ALARM_CATEGORY_ALARM) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    } else {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    }
    
    const char* podIpEnv = std::getenv("POD_IP");
    std::string podIp = (podIpEnv != nullptr) ? podIpEnv : "";
    size_t max_env_len = 256;
    if (podIp.size() > max_env_len) {
        podIp = podIp.substr(0, max_env_len);
    }
    std::string serviceLocation = "service name=Coordinator, service ip=" + podIp;
    
    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.additionalInformation = serviceLocation;

    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_QUALITY_OF_SERVICE);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_CRITICAL);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::COORDINATOR_EXCEPTION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::COORDINATOR_EXCEPTION);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::COORDINATOR_EXCEPTION);

    return FillAlarmJson(alarm);
}

std::string AlarmRequestHandler::FillCoordinatorReqCongestionAlarmInfo(RequestCongestionReason reasonID,
    std::string additionalInformation)
{
    AlarmRecord alarm;
    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.category = static_cast<int32_t>(AlarmCategory::ALARM_CATEGORY_EVENT);

    alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    const char* podIpEnv = std::getenv("POD_IP");
    std::string podIp = (podIpEnv != nullptr) ? podIpEnv : "";
    std::string serviceLocation = "service name=Coordinator, service ip=" + podIp;

    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.additionalInformation = additionalInformation;
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);
    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_STATE_CHANGE);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_MAJOR);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);

    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::REQ_CONGESTION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::REQ_CONGESTION);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::REQ_CONGESTION);

    return FillAlarmJson(alarm);
}

std::string AlarmRequestHandler::FillCoordinatorConnPDExceptionAlarmnfo(AlarmCategory category,
    CoordinatorConnPDReason reasonID, std::string additionalInfo) const
{
    AlarmRecord alarm;

    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.category = static_cast<int32_t>(category);
    if (category == AlarmCategory::ALARM_CATEGORY_ALARM) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    } else {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    }
    
    const char* podIpEnv = std::getenv("POD_IP");
    std::string podIp = (podIpEnv != nullptr) ? podIpEnv : "";
    size_t max_env_len = 256;
    if (podIp.size() > max_env_len) {
        podIp = podIp.substr(0, max_env_len);
    }
    std::string serviceLocation = "service name=Coordinator, service ip=" + podIp + ", " + additionalInfo;

    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.additionalInformation = serviceLocation;

    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_COMMUNICATION);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_MAJOR);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_MANUAL);

    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::COORDINATOR_CONN_PD_EXCEPTION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::COORDINATOR_CONN_PD_EXCEPTION);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::COORDINATOR_CONN_PD_EXCEPTION);

    return FillAlarmJson(alarm);
}

int32_t AlarmRequestHandler::SendAlarmToAlarmManager(std::string alarmInfo)
{
    if (!Configure::Singleton()->IsMaster()) {
        LOG_D("[AlarmRequestHandler] The slave coordinator does not need to report alarms.");
        return 0;
    }
    int32_t code = 400; // 400 bad request
    std::map<boost::beast::http::field, std::string> headMap;
    headMap[boost::beast::http::field::content_type] = "";
    Request req = {"/v1/alarm/coordinator", boost::beast::http::verb::post, headMap, alarmInfo};

    auto ip = Configure::Singleton()->httpConfig.controllerIP;
    auto alarmPort = Configure::Singleton()->httpConfig.alarmPort;

    LOG_M("[Post] Send coordinator alarm: IP %s, port %s.", ip.c_str(), alarmPort.c_str());
    std::string response = "";
    HttpClient client;
    client.Init(ip, alarmPort, Configure::Singleton()->alarmClientTlsItems);
    auto httpRet = client.SendRequest(req, Configure::Singleton()->httpConfig.httpTimeoutS,
        Configure::Singleton()->exceptionConfig.maxRetry, response, code);
    if (httpRet != 0 || code != 200 || response.empty()) { // 200 ok
        LOG_W("[%s] [AlarmRequestHandler] Send coordinator alarm failed, IP %s, port %s, "
            "ret code %d, request ret %d.",
            GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::METRICS).c_str(),
            ip.c_str(), alarmPort.c_str(), code, httpRet);
        return -1;
    }
    LOG_D("[AlarmRequestHandler] Send alarm successfully, %s", response.c_str());
    return 0;
}
}
