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
void RequestRepeater::DecodeDisConnHandler(const std::string &reqId)
{
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_W("[%s] [RequestRepeater] decode disconnection failed, get request id %s info failed",
              GetWarnCode(ErrorType::WARNING, CoordinatorFeature::USERDISCON_EXCEPTIONHANDLER).c_str(),
              reqId.c_str());
        return;
    }
    if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION)
        || reqInfo->HasState(ReqState::FIRST_TOKEN_FINISH) || reqInfo->HasState(ReqState::FINISH)) {
        LOG_W("[%s] [Request_Trace] decode disconnection failed, request with ID %s is already ended.",
              GetWarnCode(ErrorType::WARNING, CoordinatorFeature::USERDISCON_EXCEPTIONHANDLER).c_str(),
              reqId.c_str());
        return;
    }
    if (reqInfo->HasState(ReqState::REPEATED)) {
        LOG_I("[DecodeDisConnHandler] Request %s stop inference with PD separate mode.", reqId.c_str());
        ReqPDStopInferHandler(reqId, false);
    } else {
        LOG_I("[DecodeDisConnHandler] Request updating state to EXCEPTION.", reqId.c_str());
        reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    }
}

void RequestRepeater::UserDisConnHandler(const std::string &reqId)
{
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_W("[%s] [RequestRepeater] Handle disconnection failed, get request id %s info failed",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::USERDISCON_EXCEPTIONHANDLER).c_str(),
            reqId.c_str());
        return;
    }
    if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION)) {
        LOG_W("[%s] [Request_Trace] Handle disconnection failed, request with ID %s is already ended.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::USERDISCON_EXCEPTIONHANDLER).c_str(),
            reqId.c_str());
        return;
    }
    if (reqInfo->HasState(ReqState::REPEATED)) {
        std::string deployMode = Configure::Singleton()->schedulerConfig["deploy_mode"]; // 部署模式
        auto route = reqInfo->GetRoute();
        auto prefillIp = instancesRecord->GetIp(route[0]);
        auto decodeIp = instancesRecord->GetIp(route[1]);
        auto prefillPort = instancesRecord->GetPort(route[0]);
        auto decodePort = instancesRecord->GetPort(route[1]);
        // route两实例相同认为是FLEX自身转发场景，走SingleHandler方案
        if ((deployMode == "pd_separate" || deployMode == "pd_disaggregation" ||
             deployMode == "pd_disaggregation_single_container") &&
            !(prefillIp == decodeIp && prefillPort == decodePort)) {
            LOG_I("[UserDisConnHandler] Request %s stop inference with PD separate mode.", reqId.c_str());
            ReqPDStopInferHandler(reqId, false);
        } else {
            LOG_I("[UserDisConnHandler] Request %s stop inference with Mixed mode.", reqId.c_str());
            ReqMixStopInferHandler(reqId);
        }
    } else {
        LOG_I("[UserDisConnHandler] Request updating state to EXCEPTION.", reqId.c_str());
        reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    }
}

MINDIE::MS::Request RequestRepeater::GetStopInfoReq(const std::string &reqId, std::string modelName) const
{
    std::string target = "/v2/models/" + modelName + "/stopInfer";
    std::map <boost::beast::http::field, std::string> header;
    header[boost::beast::http::field::accept] = "*/*";
    header[boost::beast::http::field::content_type] = "application/json";
    nlohmann::json bodyJson;
    bodyJson["id"] = reqId;
    MINDIE::MS::Request req = {target, boost::beast::http::verb::post, header, bodyJson.dump()};
    return req;
}

void RequestRepeater::ReqPDStopInferHandler(const std::string &reqId, bool skipDecode)
{
    LOG_D("[RequestRepeater] Handling PD stop requests, request ID %s.", reqId.c_str());
    auto routeIp = reqManage->GetRouteIp(reqId);
    auto modelName = reqManage->GetModelName(reqId);
    reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    auto pIp = routeIp[0];
    auto pPort = routeIp[1];
    auto dIp = routeIp[2];
    auto dPort = routeIp[3];

    // 检查PD实例是否还存在，不存在则直接返回
    if (!reqManage->ArePDInstancesValid(reqId, skipDecode)) {
        LOG_W("[RequestRepeater] PD instances not valid for reqId: %s, skip stop request",
              reqId.c_str());
        return;
    }
    
    auto req = GetStopInfoReq(reqId, modelName);
    HttpClient syncClient;
    if (syncClient.Init("", "", Configure::Singleton()->mindieClientTlsItems) != 0) {
        return;
    }
    std::string response;
    int32_t code;
    int32_t ret;
    bool stopSuccess = false;
    for (size_t i = 0; i < Configure::Singleton()->exceptionConfig.maxRetry; ++i) {
        syncClient.SetHostAndPort(pIp, pPort);
        LOG_I("[RequestRepeater] Handling PD stop requests, send request ID %s stop infer to prefill node: %s:%s.",
            reqId.c_str(), pIp.c_str(), pPort.c_str());
        ret = syncClient.SendRequest(req, Configure::Singleton()->httpConfig.httpTimeoutS,
        Configure::Singleton()->exceptionConfig.maxRetry, response, code);
        if (ret == 0 && code == 200) { // 200表示http正常返回
            stopSuccess = true;
        }
        if (!skipDecode) {
            syncClient.SetHostAndPort(dIp, dPort);
            LOG_I("[RequestRepeater] Handling PD stop requests. send reqId %s stop infer to decode node: %s:%s.",
                  reqId.c_str(), dIp.c_str(), dPort.c_str());
            ret = syncClient.SendRequest(req, Configure::Singleton()->httpConfig.httpTimeoutS,
                                         Configure::Singleton()->exceptionConfig.maxRetry, response, code);
            if (ret == 0 && code == 200) { // 200表示http正常返回
                stopSuccess = true;
            }
        }
        if (stopSuccess) {
            LOG_D("[RequestRepeater] Successfully stopped inference for request %s", reqId.c_str());
            break;
        }
    }
    if (!stopSuccess) {
        LOG_W("[RequestRepeater] Failed to stop inference for request %s after %zu retries", reqId.c_str(),
              Configure::Singleton()->exceptionConfig.maxRetry);
    }
}

void RequestRepeater::ReqMixStopInferHandler(const std::string &reqId)
{
    LOG_D("[RequestRepeater] Handling mix stop request, request id is %s.", reqId.c_str());
    auto route = reqManage->GetRoute(reqId);
    reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    auto ip = instancesRecord->GetIp(route[0]);
    auto port = instancesRecord->GetPort(route[0]);
    auto modelName = reqManage->GetModelName(reqId);
    auto req = GetStopInfoReq(reqId, modelName);
    std::string response;
    int32_t code;
    int32_t ret;
    HttpClient syncClient;
    if (syncClient.Init("", "", Configure::Singleton()->mindieClientTlsItems) != 0) {
        return;
    }
    for (size_t i = 0; i < Configure::Singleton()->exceptionConfig.maxRetry; ++i) {
        syncClient.SetHostAndPort(ip, port);
        LOG_D("[RequestRepeater] Handling mix stop requests, send request ID %s stop infer to instance: %s:%s",
            reqId.c_str(), ip.c_str(), port.c_str());
        ret = syncClient.SendRequest(req, Configure::Singleton()->httpConfig.httpTimeoutS,
        Configure::Singleton()->exceptionConfig.maxRetry, response, code);
        if (ret == 0 && code == 200) { // 200表示http正常返回
            break;
        }
    }
}

}