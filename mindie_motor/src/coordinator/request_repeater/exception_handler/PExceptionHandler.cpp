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

void RequestRepeater::ConnPErrHandler(uint64_t insId)
{
    auto ip = instancesRecord->GetIp(insId);
    auto port = instancesRecord->GetPort(insId);
    for (size_t i = 0; i < Configure::Singleton()->exceptionConfig.maxRetry; ++i) {
        auto connP = connectionPool->ApplyConn(ip, port, pHandler);
        if (connP != nullptr) {
            connP->SetAvailable(true);
            return;
        }
    }
    LOG_E("[%s] [RequestRepeater] Prefill instance %s:%s connect failed.",
        GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::P_EXCEPTIONHANDLER).c_str(),
        ip.c_str(), port.c_str());
    std::string alarmMsg = AlarmRequestHandler::GetInstance()->FillCoordinatorConnPDExceptionAlarmnfo(
        AlarmCategory::ALARM_CATEGORY_ALARM, CoordinatorConnPDReason::HTTP_EXCEPTION,
        "prefill instance address=" + ip + ":" + port);
    if (AlarmRequestHandler::GetInstance()->SendAlarmToAlarmManager(alarmMsg) != 0) {
        LOG_E("[%s] [RequestRepeater] Send connection alarm failed.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str());
    }
    scheduler->RemoveInstance({insId});
    instancesRecord->RemoveInstance(insId);
}

void RequestRepeater::ReqSendPErrHandler(const std::string &reqId)
{
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_E("[%s] [RequestRepeater] Unable to proceed with prefill node error handling. Get request %s failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::P_EXCEPTIONHANDLER).c_str(),
            reqId.c_str());
        return;
    }
    auto insId = reqInfo->GetRoute()[0];
    auto ip = instancesRecord->GetIp(insId);
    auto port = instancesRecord->GetPort(insId);
    auto serverConn = reqInfo->GetConnection();
    auto req = reqInfo->GetReq();
    for (uint32_t i = 1; i <= 4; ++i) { // 重试4次
        auto connP = connectionPool->ApplyConn(ip, port, pHandler);
        if (connP != nullptr) {
            reqInfo->SetClientConn(connP);
            reqInfo->AddRetry();
            connP->SendReq(req, reqId);
            return;
        }
        LOG_W("[%s] [RequestRepeater] Unable to proceed with prefill node error handling. "
            "Connect to prefill instance %s:%s failed, try %u times", GetWarnCode(ErrorType::WARNING,
            CoordinatorFeature::P_EXCEPTIONHANDLER).c_str(), ip.c_str(), port.c_str(), i);
    }
    LOG_E("[%s] [RequestRepeater] Unable to proceed with prefill node error handling."
        " Apply connection to node %s:%s failed",
        GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::P_EXCEPTIONHANDLER).c_str(),
        ip.c_str(), port.c_str());
    SendErrorRes(serverConn, boost::beast::http::status::internal_server_error,
        "Send messges to p instance failed\r\n");
    reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    exceptionMonitor->PushInsException(InsExceptionType::CONN_P_ERR, insId);
}

void RequestRepeater::RetryDuplicateReqIdHandler(const std::string &reqId)
{
    LOG_D("[RequestRepeater] Start retry process for the request with ID %s.", reqId.c_str());
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_E("[%s] [RequestRepeater] Failed to get request information for request ID %s, stop retry.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::P_EXCEPTIONHANDLER).c_str(), reqId.c_str());
        return;
    }
    if (reqInfo->GetRetry() > Configure::Singleton()->exceptionConfig.maxRetry) {
        LOG_E("[%s] [RequestRepeater] Retry limit exceeded for the request ID %s. Retry count: %zu.",
            GetErrorCode(ErrorType::RESOURCE_LIMIT, CoordinatorFeature::P_EXCEPTIONHANDLER).c_str(),
            reqId.c_str(), reqInfo->GetRetry());
        SendErrorRes(reqInfo->GetConnection(), boost::beast::http::status::internal_server_error,
            "Request retry failed\r\n");
        return reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    }
    auto insId = reqInfo->GetRoute()[0];
    auto ip = instancesRecord->GetIp(insId);
    auto port = instancesRecord->GetPort(insId);
    auto serverConn = reqInfo->GetConnection();
    auto req = reqInfo->GetReq();
    for (uint32_t i = 1; i <= 4; ++i) { // 重试4次
        auto connP = connectionPool->ApplyConn(ip, port, pHandler);
        if (connP != nullptr) {
            reqInfo->SetClientConn(connP);
            reqInfo->AddRetry();
            timer.AsyncWait(1000, [connP, req, reqId, ip, port] () { // 定时时间1000毫秒
                LOG_I("[RequestRepeater] Sending request %s to prefill node %s:%s.", reqId.c_str(),
                    ip.c_str(), port.c_str());
                connP->SendReq(req, reqId);
            });
            return;
        }
        LOG_W("[%s] [RequestRepeater] Connect to instance %s:%s failed, try %u times.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::P_EXCEPTIONHANDLER).c_str(), ip.c_str(),
            port.c_str(), i);
    }
    LOG_E("[%s] [RequestRepeater] Apply connection to node %s:%s failed.",
        GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::P_EXCEPTIONHANDLER).c_str(),
        ip.c_str(), port.c_str());
    SendErrorRes(serverConn, boost::beast::http::status::internal_server_error,
        "Send messges to p instance failed\r\n");
    reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    exceptionMonitor->PushInsException(InsExceptionType::CONN_P_ERR, insId);
}

}