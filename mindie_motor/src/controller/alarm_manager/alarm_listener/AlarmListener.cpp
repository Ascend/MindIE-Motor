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
#include "AlarmListener.h"
#include <nlohmann/json.hpp>
#include "AlarmManager.h"
#include "Logger.h"
#include "ConfigParams.h"
#include "ControllerConfig.h"
#include "AlarmConfig.h"
#include "CCAERequestHandler.h"
#include "Util.h"
#include "ServerRequestHandler.h"
#include "NPURecoveryManager.h"

namespace MINDIE {
namespace MS {
constexpr size_t INITIAL_TIME = 2;
constexpr size_t IO_CONTEXT_POOL_SIZE = 1;
constexpr size_t MAX_CONN = 256;
constexpr size_t MAX_IP_LENGTH = 45;
static int32_t g_alarmCategoryMin = static_cast<int32_t>(AlarmCategory::ALARM_CATEGORY_ALARM);
static int32_t g_alarmCategoryMax = static_cast<int32_t>(AlarmCategory::ALARM_CATEGORY_OTHER_CHANGE);
static int32_t g_alarmClearedMin = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
static int32_t g_alarmClearedMax = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
static int32_t g_alarmClearCategoryMin = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);
static int32_t g_alarmClearCategoryMax = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_MANUAL);
static int32_t g_eventTypeMin = static_cast<int32_t>(EventType::EVENT_TYPE_COMMUNICATION);
static int32_t g_eventTypeMax = static_cast<int32_t>(EventType::EVENT_TYPE_HEARTBEAT);
static int32_t g_alarmSeverityMin = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_CRITICAL);
static int32_t g_alarmSeverityMax = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_WARNING);
static int32_t g_serviceAffectedTypeMin = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_NO);
static int32_t g_serviceAffectedTypeyMax = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);

int32_t AlarmListener::Init(std::shared_ptr<NodeStatus> nodeStatus)
{
    mAbortSetupClient = std::make_shared<HttpClient>();
    if (mAbortSetupClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0) {
        LOG_E("[%s] [ProbeServer] Initialize failed because initialize server clients failed!",
              GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::FAULT_MANAGER).c_str());
    }

    mNodeStatus = nodeStatus;
    
    try {
        mServer = std::make_unique<HttpServer>(IO_CONTEXT_POOL_SIZE, MAX_CONN);
    } catch (const std::exception& e) {
        LOG_E("[%s] [AlarmListener] Create http server failed, initialize alarm listener failed.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::ALARM_LISTENER).c_str());
        return -1;
    }
    for (const auto& [url, handler] : mUrlHandlers) {
        if (mServer->RegisterPostUrlHandler(url, handler) != 0) {
            LOG_E("[%s] [AlarmListener] Failed to register URL handler for %s, initialize alarm listener failed.",
                GetErrorCode(ErrorType::OPERATION_REPEAT, ControllerFeature::ALARM_LISTENER).c_str(), url.c_str());
            return -1;
        }
    }
    try {
        mMainThread = std::make_unique<std::thread>([this]() {
            if (Run() != 0) {
                LOG_W("[%s] [AlarmListener] Alarm listener run failed.",
                    GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ALARM_LISTENER).c_str());
            }
            mRun.store(false);
        });
    } catch (const std::exception& e) {
        LOG_E("[%s] [AlarmListener] Failed to create main thread.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::ALARM_LISTENER).c_str());
        return -1;
    }
    std::this_thread::sleep_for(std::chrono::seconds(INITIAL_TIME)); // initial time
    if (!mRun.load()) {
        LOG_W("[%s] [AlarmListener] Main thread run failed, initialize Alarm listener failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ALARM_LISTENER).c_str());
        return -1;
    }
    return 0;
}

AlarmListener::~AlarmListener()
{
    Stop();
    LOG_I("[AlarmListener] Alarm listener destroy successfully.");
}

void AlarmListener::Stop()
{
    if (mServer != nullptr) {
        mServer->Stop();
    }
    if (mMainThread != nullptr && mMainThread->joinable()) {
        mMainThread->join();
    }
    LOG_I("[AlarmListener] Stop successfully.");
}

int32_t AlarmListener::Run()
{
    HttpServerParams serverParams;
    serverParams.ip = ControllerConfig::GetInstance()->GetPodIP();
    serverParams.port = std::stoi(ControllerConfig::GetInstance()->GetControllerAlarmPort());
    serverParams.serverTlsItems = ControllerConfig::GetInstance()->GetAlarmTlsItems();
    serverParams.checkSubject = false;
    LOG_M("[Start] Runing alarm listener, listening on IP %s, port %ld, TLS enable %d, check subject %d.",
        serverParams.ip.c_str(), serverParams.port, serverParams.serverTlsItems.tlsEnable, serverParams.checkSubject);
    if (mServer->Run(serverParams) != 0) {
        LOG_W("[%s] [AlarmListener] Failed to run the http server",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ALARM_LISTENER).c_str());
        return -1;
    }
    return 0;
}

bool AlarmListener::IsCoordinatorAlarmValid(const nlohmann::json &alarm) const
{
    if (!IsJsonIntValid(alarm, "category", g_alarmCategoryMin, g_alarmCategoryMax) ||
        !IsJsonIntValid(alarm, "cleared", g_alarmClearedMin, g_alarmClearedMax) ||
        !IsJsonIntValid(alarm, "clearCategory", g_alarmClearCategoryMin, g_alarmClearCategoryMax) ||
        !IsJsonIntValid(alarm, "occurUtc", INT64_MIN, INT64_MAX) ||
        !IsJsonIntValid(alarm, "occurTime", INT64_MIN, INT64_MAX) ||
        !IsJsonIntValid(alarm, "eventType", g_eventTypeMin, g_eventTypeMax) ||
        !IsJsonIntValid(alarm, "severity", g_alarmSeverityMin, g_alarmSeverityMax) ||
        !IsJsonIntValid(alarm, "serviceAffectedType", g_serviceAffectedTypeMin, g_serviceAffectedTypeyMax) ||
        !IsJsonIntValid(alarm, "reasonId", INT32_MIN, INT32_MAX)) {
        return false;
    }
    if (!IsJsonStringValid(alarm, "originSystem") || !IsJsonStringValid(alarm, "originSystemName") ||
        !IsJsonStringValid(alarm, "originSystemType") || !IsJsonStringValid(alarm, "probableCause") ||
        !IsJsonStringValid(alarm, "location") || !IsJsonStringValid(alarm, "moi") ||
        !IsJsonStringValid(alarm, "alarmId") || !IsJsonStringValid(alarm, "alarmName") ||
        !IsJsonStringValid(alarm, "additionalInformation")) {
        return false;
    }
    return true;
}

// NodeManager告警JSON验证函数
bool IsLLMEngineAlarmValid(const nlohmann::json &alarmJson)
{
    // 验证必需字段存在性
    if (!alarmJson.contains("alarm_info") || !alarmJson.contains("node_manager_ip")) {
        LOG_E("[%s] [AlarmListener] Missing required fields in LLMEngine alarm JSON",
            GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ALARM_LISTENER).c_str());
        return false;
    }

    // 验证基础字段类型
    
    if (!IsJsonStringValid(alarmJson, "node_manager_ip", 1, MAX_IP_LENGTH) ||
        !IsValidIp(alarmJson["node_manager_ip"].get<std::string>())) {
        LOG_E("[%s] [AlarmListener] Invalid field types in LLMEngine alarm JSON",
            GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ALARM_LISTENER).c_str());
        return false;
    }

    // 验证alarm_info格式：嵌套数组，每个元素包含timestamp, errCode, createdBy, errorLocation字段
    if (!alarmJson["alarm_info"].is_array()) {
        LOG_E("[%s] [AlarmListener] In LLMEngine alarm JSON, alarm_info field type must be array, but got %s",
            GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ALARM_LISTENER).c_str(),
            alarmJson["alarm_info"].type_name());
        return false;
    }

    // 校验alarm_info内容格式
    for (const auto& epErrors : alarmJson["alarm_info"]) {
        if (!epErrors.is_array()) {
            LOG_E("[%s] [AlarmListener] In LLMEngine alarm JSON, Each entry in alarm_info field must be an array",
                GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ALARM_LISTENER).c_str());
            return false;
        }

        for (const auto& error : epErrors) {
            if (!error.is_object() ||
                !error.contains("timestamp") || !error.contains("errCode") ||
                !error.contains("createdBy") || !error.contains("errorLocation") ||
                !error["timestamp"].is_number_integer() || !error["errCode"].is_string() ||
                !error["createdBy"].is_string() || !error["errorLocation"].is_string()) {
                LOG_E("[%s] [AlarmListener] Invalid error format in LLMEngine alarm JSON",
                    GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ALARM_LISTENER).c_str());
                return false;
            }
        }
    }

    return true;
}

bool AlarmListener::CoordinatorAlarmPreproccessor(nlohmann::json &alarmsJson) const
{
    bool isAlarmValid = true;
    auto validAlarmsJson = nlohmann::json::array();
    for (auto& alarm : alarmsJson) {
        if (!IsCoordinatorAlarmValid(alarm)) {
            isAlarmValid = false;
            LOG_E("[%s] [AlarmListener] Invalid coordinator alarm detected and filtered: %s",
                GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ALARM_LISTENER).c_str(),
                alarm.dump().c_str());
        } else {
            validAlarmsJson.push_back(alarm);
        }
    }
    alarmsJson = validAlarmsJson;
    return isAlarmValid;
}

std::pair<ErrorCode, Response> AlarmListener::CoordinatorAlarmHandler(const Http::request<Http::string_body> &req) const
{
    Response resp;
    bool isAlarmValid = true;
    try {
        auto alarmsJson = nlohmann::json::parse(req.body(), CheckJsonDepthCallBack);
        if (!alarmsJson.is_array()) {
            LOG_E("[%s] [AlarmListener] The type of coordinator alarm is wrong.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ALARM_LISTENER).c_str());
            return std::make_pair(ErrorCode::INVALID_PARAMETER, resp);
        }
        isAlarmValid = CoordinatorAlarmPreproccessor(alarmsJson);
        if (alarmsJson.size() != 0) {
            std::string alarmsString = alarmsJson.dump();
            if (UpdateCoordinatorStatus(alarmsString)) {
                AlarmManager::GetInstance()->AlarmAdded(alarmsString);
                LOG_M("[AlarmListener] Add coordinator alarm. Contents: %s", alarmsString.c_str());
            } else {
                LOG_D("[AlarmListener] Filtered first coordinator fault/recover.");
            }
        } else {
            LOG_W("[AlarmListener] There is no coordinator alarm can be added to the alarm deque.");
        }
    } catch (const std::exception& e) {
        LOG_E("[%s] [AlarmListener] Handling coordinator alarm exceptions, error: %s",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ALARM_LISTENER).c_str(),
            e.what());
        return std::make_pair(ErrorCode::INTERNAL_ERROR, resp);
    }
    if (!isAlarmValid) {
        LOG_E("[%s] [AlarmListener] Some alarm parameters of the coordinator are incorrect.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ALARM_LISTENER).c_str());
        return std::make_pair(ErrorCode::INVALID_PARAMETER, resp);
    }
    return std::make_pair(ErrorCode::OK, resp);
}

std::pair<ErrorCode, Response> AlarmListener::LLMEngineAlarmHandler(const Http::request<Http::string_body> &req) const
{
    Response resp;
    try {
        auto alarmJson = nlohmann::json::parse(req.body());
        if (!IsLLMEngineAlarmValid(alarmJson)) {
            LOG_E("[%s] [AlarmListener] LLMEngine alarm JSON validation failed",
                GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ALARM_LISTENER).c_str());
            return std::make_pair(ErrorCode::INVALID_PARAMETER, resp); // ret=2
        }
        
        // LLMEngine alarm的格式需要进一步转换后, 才能加入告警队列
        // 调用NPURecoveryManager处理NodeManager透传的LLMEngine alarm,
        // 如果存在可恢复的故障码, 执行对应的快恢流程
        NPURecoveryManager::GetInstance()->ProcessLLMEngineAlarm(alarmJson);
    
        return std::make_pair(ErrorCode::OK, resp); // ret=0
    } catch (const std::exception& e) {
        LOG_E("[%s] [AlarmListener] Handling LLMEngine alarm exceptions, error: %s",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ALARM_LISTENER).c_str(),
            e.what());
        return std::make_pair(ErrorCode::INTERNAL_ERROR, resp); // ret=3
    }
}

std::pair<ErrorCode, Response> AlarmListener::TerminateServiceHandler(const Http::request<Http::string_body>
    &req) const
{
    LOG_I("[AlarmListener] Enter TerminateServiceHandler.");
    auto nodeInfoJson = nlohmann::json::parse(req.body());
    std::string ip = nodeInfoJson["ip"];
    std::string port = nodeInfoJson["port"];
    Response resp;
    if (mNodeStatus == nullptr) {
        LOG_E("[AlarmListener] NodeStatus is nullptr.");
        return std::make_pair(ErrorCode::INTERNAL_ERROR, resp);
    }
    auto node = mNodeStatus->GetNode(ip, port);
    if (node == nullptr) {
        LOG_E("[AlarmListener] Get node failed, node address %s:%s", ip.c_str(), port.c_str());
        return std::make_pair(ErrorCode::INTERNAL_ERROR, resp);
    }

    auto ret = ServerRequestHandler::GetInstance()->TerminateService(*mAbortSetupClient, *node);
    if (ret != 0) {
        LOG_E("[AlarmListener] Terminate service failed, node address %s:%s", ip.c_str(), port.c_str());
        return std::make_pair(ErrorCode::INTERNAL_ERROR, resp);
    }

    LOG_I("[AlarmListener] Terminate service succeed, node address %s:%s", ip.c_str(), port.c_str());

    std::vector<uint64_t> peers = node->dpGroupPeers;
    for (const auto &peerId : peers) {
        mNodeStatus->AddExpiredNode(peerId);
        mNodeStatus->UpdateNodeDeleteTime(peerId);
    }

    return std::make_pair(ErrorCode::OK, resp);
}

bool AlarmListener::UpdateCoordinatorStatus(const std::string& alarmBody) const
{
    if (alarmBody.empty()) {
        LOG_E("[%s] Coordinator alarm request body is invalid",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ALARM_LISTENER).c_str());
        return false;
    }
    
    nlohmann::json alarmJson = nlohmann::json::parse(alarmBody, CheckJsonDepthCallBack);
    const auto& alarmRecord = alarmJson[0];
    if (!alarmRecord.contains("alarmId") || !alarmRecord.contains("cleared")) {
        return false;
    }

    std::string alarmId = alarmRecord["alarmId"].get<std::string>();
    int32_t cleared = alarmRecord["cleared"].get<int32_t>();
    const std::string coordinatorExceptionAlarmId = AlarmConfig::GetInstance()->GetAlarmIDString(
        AlarmType::COORDINATOR_EXCEPTION);
    // 更新CCAERequestHandler中的coordinatorServiceReady
    if (alarmId == coordinatorExceptionAlarmId && cleared == 0) {
        CCAERequestHandler::SetCoordinatorServiceReady(false);
    } else {
        CCAERequestHandler::SetCoordinatorServiceReady(true);
    }
    // 仅对 COORDINATOR_EXCEPTION 做首次过滤
    if (alarmId == coordinatorExceptionAlarmId) {
        if (cleared == 0) {
            bool expected = false;
            if (firstCoordinatorFaultFiltered.compare_exchange_strong(expected, true,
                std::memory_order_relaxed, std::memory_order_relaxed)) {
                return false;
            }
            return true;
        } else if (cleared == 1) {
            bool expected = false;
            if (firstCoordinatorRecoverFiltered.compare_exchange_strong(expected, true,
                std::memory_order_relaxed, std::memory_order_relaxed)) {
                return false;
            }
            return true;
        }
    }
    // 非该类型的告警：不做过滤
    return true;
}
}
}