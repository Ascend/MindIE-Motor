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
#ifndef MINDIE_MS_ALARM_LISTENER_H
#define MINDIE_MS_ALARM_LISTENER_H
#include <memory>
#include <cstdint>
#include "HttpServer.h"
#include "HttpClient.h"
#include "NodeStatus.h"
namespace MINDIE {
namespace MS {
using AlarmHandler = std::function<std::pair<ErrorCode, Response>(const Http::request<Http::string_body>&)>;
/// The server of the controller.
class AlarmListener {
public:
    AlarmListener(const AlarmListener &obj) = delete;
    AlarmListener &operator=(const AlarmListener &obj) = delete;
    AlarmListener(AlarmListener &&obj) = delete;
    AlarmListener &operator=(AlarmListener &&obj) = delete;
    AlarmListener() = default;

    ~AlarmListener();

    int32_t Init(std::shared_ptr<NodeStatus> nodeStatus);

    int32_t Run();

    /// Stop server and threads.
    void Stop();

    std::pair<ErrorCode, Response> CoordinatorAlarmHandler(const Http::request<Http::string_body> &req) const;

    std::pair<ErrorCode, Response> LLMEngineAlarmHandler(const Http::request<Http::string_body> &req) const;

    std::pair<ErrorCode, Response> TerminateServiceHandler(const Http::request<Http::string_body> &req) const;
private:
    std::unique_ptr<HttpServer> mServer = nullptr;       /// The HTTP server.
    std::atomic<bool> mRun = { true };                   /// The run status of the HTTP server.
    std::unique_ptr<std::thread> mMainThread = nullptr;  /// The thread that executes the Run method.
    mutable std::atomic<bool> firstCoordinatorFaultFiltered{false};
    mutable std::atomic<bool> firstCoordinatorRecoverFiltered{false};
    bool  UpdateCoordinatorStatus(const std::string& alarmBody) const;
    bool IsCoordinatorAlarmValid(const nlohmann::json &alarm) const;
    bool CoordinatorAlarmPreproccessor(nlohmann::json &alarmsJson) const;
    const std::map<std::string, AlarmHandler> mUrlHandlers = {
        {"/v1/alarm/coordinator", std::bind(&AlarmListener::CoordinatorAlarmHandler, this, std::placeholders::_1)},
        {"/v1/alarm/llm_engine", std::bind(&AlarmListener::LLMEngineAlarmHandler, this, std::placeholders::_1)},
        {"/v1/terminate-service", std::bind(&AlarmListener::TerminateServiceHandler, this, std::placeholders::_1)}
    };
    std::shared_ptr<NodeStatus> mNodeStatus = nullptr;
    std::shared_ptr<HttpClient> mAbortSetupClient = nullptr;
};
}
}
#endif // MINDIE_MS_ALARM_LISTENER_H
