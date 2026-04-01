/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2024-2026. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#include "RequestListener.h"
#include <string>
#include "boost/uuid/uuid_io.hpp"
#include "Logger.h"
#include "nlohmann/json.hpp"
#include "Configure.h"
#include "Communication.h"
#include "Util.h"
#include "HealthMonitor.h"
#include "MemoryUtil.h"

namespace MINDIE::MS {

constexpr int WAIT_TIME = 1; // ms
thread_local boost::uuids::random_generator RequestListener::reqIdGen;
std::atomic<uint64_t> RequestListener::sReqCounter;
RequestListener::RequestListener(std::unique_ptr<MINDIE::MS::DIGSScheduler> &schedulerInit,
                                 std::unique_ptr<ReqManage> &reqManager, std::unique_ptr<ClusterNodes> &instancesRec,
                                 std::unique_ptr<ExceptionMonitor> &exceptionMonitorInit,
                                 std::unique_ptr<RequestRepeater> &requestRepeaterInit)
    : scheduler(schedulerInit), reqManage(reqManager), instancesRecord(instancesRec),
      exceptionMonitor(exceptionMonitorInit), requestRepeater(requestRepeaterInit)
{
    LOG_I("[RequestListener] Health monitor initialization started");
    if (reqManage) {
        bool success = InitHealthMonitor(reqManage.get());
        if (!success) {
            LOG_W("[RequestListener] Memory-based request interception is disabled");
        }
    } else {
        LOG_W("[RequestListener] reqManage is null, health monitor initialization skipped");
    }
}

int RequestListener::Init() const
{
    return 0;
}

std::string RequestListener::GenReqestId()
{
    auto uuid = reqIdGen(); // 不支持const函数
    uint64_t counter = sReqCounter++;
    uint64_t reqId = 0;
    boost::hash_combine(reqId, uuid);
    boost::hash_combine(reqId, counter);
    return std::to_string(reqId);
}

void RequestListener::TritonReqHandler(std::shared_ptr<ServerConnection> connection)
{
    reqManage->SetReqArriveNum();
    if (!instancesRecord->IsAvailable()) {
        SendErrorRes(connection, boost::beast::http::status::service_unavailable,
            "MindIE-MS Coordinator is not ready\r\n");
        return;
    }
    reqManage->CheckAndHandleReqCongestionAlarm();
    if (reqManage->GetReqNum() >= Configure::Singleton()->reqLimit.maxReqs) {
        SendErrorRes(connection, boost::beast::http::status::too_many_requests,
            "Too many requests\r\n");
        return;
    }
    if (!PreRequestCheck("Triton", connection)) {
        return;
    }
    if (Configure::Singleton()->IsAbnormal() && !Configure::Singleton()->IsMaster()) { // 异常且为备节点则不做处理
        return;
    }
    CheckMasterAndCreateLinkWithDNode();
    auto &req = connection->GetReq();
    auto body = boost::beast::buffers_to_string(req.body().data());
    try {
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        std::string reqIdStr = GenReqestId();
        auto ret = reqManage->AddReq(reqIdStr, ReqInferType::TRITON, connection, req);
        if (!ret) {
            LOG_E("[%s] [RequestListener] Duplicate request detected while handling Triton request, request ID is %s.",
                GetErrorCode(ErrorType::OPERATION_REPEAT, CoordinatorFeature::REQUEST_LISTENER).c_str(),
                reqIdStr.c_str());
            SendErrorRes(connection, boost::beast::http::status::bad_request, "Duplicate request id\r\n");
            return;
        }
        reqManage->UpdateState(reqIdStr, ReqState::ARRIVE);
        if (DealTritonReq(reqManage->GetReqInfo(reqIdStr)) != 0) {
            reqManage->UpdateState(reqIdStr, ReqState::EXCEPTION);
        }
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestListener] Exception while handling Triton request, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str(),
            e.what());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
    }
}

void RequestListener::TGIStreamReqHandler(std::shared_ptr<ServerConnection> connection)
{
    reqManage->SetReqArriveNum();
    if (!instancesRecord->IsAvailable()) {
        SendErrorRes(connection, boost::beast::http::status::service_unavailable,
            "MindIE-MS Coordinator is not ready\r\n");
        return;
    }
    reqManage->CheckAndHandleReqCongestionAlarm();
    if (reqManage->GetReqNum() >= Configure::Singleton()->reqLimit.maxReqs) {
        SendErrorRes(connection, boost::beast::http::status::too_many_requests,
            "Too many requests\r\n");
        return;
    }
    if (!PreRequestCheck("TGIStream", connection)) {
        return;
    }
    if (Configure::Singleton()->IsAbnormal() && !Configure::Singleton()->IsMaster()) { // 异常且为备节点则不做处理
        return;
    }
    CheckMasterAndCreateLinkWithDNode();
    auto &req = connection->GetReq();
    std::string reqIdStr = GenReqestId();
    auto ret = reqManage->AddReq(reqIdStr, ReqInferType::TGI, connection, req);
    if (!ret) {
        LOG_E("[%s] [RequestListener] Duplicate request detected while handling TGI stream request, request ID is %s.",
            GetErrorCode(ErrorType::OPERATION_REPEAT, CoordinatorFeature::REQUEST_LISTENER).c_str(),
            reqIdStr.c_str());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Duplicate request id\r\n");
        return;
    }
    reqManage->UpdateState(reqIdStr, ReqState::ARRIVE);
    if (DealTGIReq(reqManage->GetReqInfo(reqIdStr)) != 0) {
        reqManage->UpdateState(reqIdStr, ReqState::EXCEPTION);
    }
}

void RequestListener::TGIOrVLLMReqHandler(std::shared_ptr<ServerConnection> connection)
{
    reqManage->SetReqArriveNum();
    if (!instancesRecord->IsAvailable()) {
        return SendErrorRes(connection, boost::beast::http::status::service_unavailable,
            "MindIE-MS Coordinator is not ready\r\n");
    }
    reqManage->CheckAndHandleReqCongestionAlarm();
    if (reqManage->GetReqNum() >= Configure::Singleton()->reqLimit.maxReqs) {
        return SendErrorRes(connection, boost::beast::http::status::too_many_requests, "Too many requests\r\n");
    }
    if (!PreRequestCheck("TGIOrVLLM", connection)) {
        return;
    }
    if (Configure::Singleton()->IsAbnormal() && !Configure::Singleton()->IsMaster()) { return; }
    CheckMasterAndCreateLinkWithDNode();
    auto &req = connection->GetReq();
    try {
        std::string bodyStr = boost::beast::buffers_to_string(req.body().data());
        auto bodyJson = nlohmann::json::parse(bodyStr, CheckJsonDepthCallBack);
        std::string reqIdStr = GenReqestId();
        int ret = 0;
        if (bodyJson.contains("inputs")) {
            if (!reqManage->AddReq(reqIdStr, ReqInferType::TGI, connection, req)) {
                LOG_E("[%s] [RequestListener] Duplicate TGI/VLLM request, ID is %s.", GetErrorCode(
                    ErrorType::OPERATION_REPEAT, CoordinatorFeature::REQUEST_LISTENER).c_str(), reqIdStr.c_str());
                SendErrorRes(connection, boost::beast::http::status::bad_request, "Duplicate request id\r\n");
                return;
            }
            reqManage->UpdateState(reqIdStr, ReqState::ARRIVE);
            ret = DealTGIReq(reqManage->GetReqInfo(reqIdStr));
        } else if (bodyJson.contains("prompt")) {
            if (!reqManage->AddReq(reqIdStr, ReqInferType::VLLM, connection, req)) {
                LOG_E("[%s] [RequestListener] Duplicate TGI/VLLM request, ID is %s.", GetErrorCode(ErrorType::
                    OPERATION_REPEAT, CoordinatorFeature::REQUEST_LISTENER).c_str(), reqIdStr.c_str());
                SendErrorRes(connection, boost::beast::http::status::bad_request, "Duplicate request id\r\n");
                return;
            }
            reqManage->UpdateState(reqIdStr, ReqState::ARRIVE);
            ret = DealVLLMReq(reqManage->GetReqInfo(reqIdStr));
        } else {
            LOG_E("[%s] [RequestListener] TGI or VLLM request is invalid.\n",
                GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::REQUEST_LISTENER).c_str());
            SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
            return;
        }
        if (ret != 0) {
            reqManage->UpdateState(reqIdStr, ReqState::EXCEPTION);
        }
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestListener] Exception during handling TGI or VLLM request: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str(), e.what());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
        return;
    }
}

void RequestListener::OpenAIReqHandler(std::shared_ptr<ServerConnection> connection)
{
    reqManage->SetReqArriveNum();
    if (!instancesRecord->IsAvailable()) {
        SendErrorRes(connection, boost::beast::http::status::service_unavailable,
            "MindIE-MS Coordinator is not ready\r\n");
        return;
    }
    reqManage->CheckAndHandleReqCongestionAlarm();
    if (reqManage->GetReqNum() >= Configure::Singleton()->reqLimit.maxReqs) {
        SendErrorRes(connection, boost::beast::http::status::too_many_requests,
            "Too many requests\r\n");
        return;
    }
    if (!PreRequestCheck("OpenAI", connection)) {
        return;
    }
    if (Configure::Singleton()->IsAbnormal() && !Configure::Singleton()->IsMaster()) { // 异常且为备节点则不做处理
        return;
    }
    CheckMasterAndCreateLinkWithDNode();
    auto &req = connection->GetReq();
    std::string reqIdStr = GenReqestId();
    auto ret = reqManage->AddReq(reqIdStr, ReqInferType::OPENAI, connection, req);
    if (!ret) {
        LOG_E("[%s] [RequestListener]  Duplicate request detected while handling OpenAI request, request ID is %s.",
            GetErrorCode(ErrorType::OPERATION_REPEAT, CoordinatorFeature::REQUEST_LISTENER).c_str(),
            reqIdStr.c_str());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Duplicate request id\r\n");
        return;
    }
    reqManage->UpdateState(reqIdStr, ReqState::ARRIVE);
    if (DealOpenAIReq(reqManage->GetReqInfo(reqIdStr)) != 0) {
        reqManage->UpdateState(reqIdStr, ReqState::EXCEPTION);
    }
}

void RequestListener::MindIEReqHandler(std::shared_ptr<ServerConnection> connection)
{
    reqManage->SetReqArriveNum();
    if (!instancesRecord->IsAvailable()) {
        SendErrorRes(connection, boost::beast::http::status::service_unavailable,
            "MindIE-MS Coordinator is not ready\r\n");
        return;
    }
    reqManage->CheckAndHandleReqCongestionAlarm();
    if (reqManage->GetReqNum() >= Configure::Singleton()->reqLimit.maxReqs) {
        SendErrorRes(connection, boost::beast::http::status::too_many_requests,
            "Too many requests\r\n");
        return;
    }
    if (!PreRequestCheck("MindIE", connection)) {
        return;
    }
    if (Configure::Singleton()->IsAbnormal() && !Configure::Singleton()->IsMaster()) { // 异常且为备节点则不做处理
        return;
    }
    CheckMasterAndCreateLinkWithDNode();
    auto &req = connection->GetReq();
    std::string reqIdStr = GenReqestId();
    auto ret = reqManage->AddReq(reqIdStr, ReqInferType::MINDIE, connection, req);
    if (!ret) {
        LOG_E("[%s] [RequestListener] Duplicate request detected while handling MindIE request, request ID is %s.",
            GetErrorCode(ErrorType::OPERATION_REPEAT, CoordinatorFeature::REQUEST_LISTENER).c_str(),
                reqIdStr.c_str());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Duplicate request id\r\n");
        return;
    }
    reqManage->UpdateState(reqIdStr, ReqState::ARRIVE);
    if (DealMindIEReq(reqManage->GetReqInfo(reqIdStr)) != 0) {
        reqManage->UpdateState(reqIdStr, ReqState::EXCEPTION);
    }
}

void RequestListener::ReqRetryHandler(const std::string &reqId)
{
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_E("[%s] [RequestListener] Get request %s failed when handling retry request.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::REQUEST_LISTENER).c_str(),
            reqId.c_str());
        return;
    }
    int ret;
    switch (reqInfo->GetType()) {
        case ReqInferType::TGI:
            ret = DealTGIReq(reqInfo);
            break;
        case ReqInferType::VLLM:
            ret = DealVLLMReq(reqInfo);
            break;
        case ReqInferType::OPENAI:
            ret = DealOpenAIReq(reqInfo);
            break;
        case ReqInferType::TRITON:
            ret = DealTritonReq(reqInfo);
            break;
        case ReqInferType::MINDIE:
            ret = DealMindIEReq(reqInfo);
            break;
        default:
            ret = -1;
            break;
    }
    if (ret != 0) {
        reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    }
}

static bool TritonTokenInferParse(const nlohmann::json &bodyJson, std::vector<uint32_t> &tokenList)
{
    try {
        auto inputs = bodyJson.at("inputs").at(0);
        auto dataType = inputs.at("datatype").template get<std::string>();
        if (dataType != "UINT32") {
            LOG_E("[%s] [RequestListener] Unsupported data type %s detected when parsing Triton token.",
                GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::REQUEST_LISTENER).c_str(),
                dataType.c_str());
            return false;
        }
        tokenList = inputs.at("data").template get<std::vector<uint32_t>>();
        return true;
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestListener] Caught exception when parsing Triton token,  error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str(),
            e.what());
        return false;
    }
}

static inline bool TritonInferParse(const nlohmann::json &bodyJson, std::string &prompt)
{
    try {
        prompt = bodyJson.at("text_input").template get<std::string>();
        return true;
    } catch (const std::exception &e) {
        LOG_W("[%s] [RequestListener] Caught exception when parsing Triton token, error is: %s",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::REQUEST_REPEATER).c_str(), e.what());
        return false;
    }
}

static bool TritonModelsIsEmpty(const std::string &url)
{
    auto pos1 = url.find("models/");
    if (pos1 == std::string::npos) {
        return true;
    } else {
        auto subUrl = url.substr(pos1 + 7); // 7是"models/"的字节数
        auto pos2 = subUrl.find("/");
        if (pos2 == std::string::npos) {
            return true;
        } else {
            auto modelName = subUrl.substr(0, pos2);
            if (modelName.empty()) {
                return true;
            }
            return false;
        }
    }
}

int RequestListener::ProcessTritonRequest(std::shared_ptr<ReqAgent> reqInfo,
    const boost::beast::http::request<boost::beast::http::dynamic_body>& req)
{
    auto connection = reqInfo->GetConnection();
    if (TritonModelsIsEmpty(req.target())) {
        LOG_E("[%s] Failed to process Triton request. No models specified for the request.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::REQUEST_LISTENER).c_str());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
        return -1;
    }

    reqInfo->SetIsStream(TritonIsStream(req.target()));
    auto body = boost::beast::buffers_to_string(req.body().data());

    try {
        return ProcessTritonRequestBody(reqInfo, body);
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [RequestListener] Failed to parse request body: %s, json parse error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str(),
              body.c_str(), e.what());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
        return -1;
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestListener] Failed to deal Triton request, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str(), e.what());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
        return -1;
    }
}

int RequestListener::ProcessTritonRequestBody(std::shared_ptr<ReqAgent> reqInfo,
    const std::string& body)
{
    auto connection = reqInfo->GetConnection();
    std::string errorCode = GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::REQUEST_LISTENER);
    auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
    std::string prompt;
    std::vector<uint32_t> tokenList;

    if (TritonInferParse(bodyJson, prompt)) {
        if (scheduler->ProcReq(reqInfo->GetReqId(), prompt, MINDIE::MS::ReqType::TRITON) != 0) {
            LOG_E("[%s] [RequestListener] Failed to deal Triton request. Scheduler process request failed.",
                errorCode.c_str());
            SendErrorRes(connection, boost::beast::http::status::internal_server_error,
                "Scheduler proc req failed\r\n");
            return -1;
        }
        return 0;
    }

    if (TritonTokenInferParse(bodyJson, tokenList)) {
        if (scheduler->ProcReq(reqInfo->GetReqId(), tokenList) != 0) {
            LOG_E("[%s] [RequestListener] Failed to deal Triton request. Scheduler process request failed.",
                errorCode.c_str());
            SendErrorRes(connection, boost::beast::http::status::internal_server_error,
                "Scheduler proc req failed\r\n");
            return -1;
        }
        return 0;
    }

    SendErrorRes(connection, boost::beast::http::status::bad_request,
        "Request format is invalid\r\n");
    return -1;
}

int RequestListener::DealTritonReq(std::shared_ptr<ReqAgent> reqInfo)
{
    std::string errorCode = GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::REQUEST_LISTENER);
    if (reqInfo == nullptr) {
        LOG_E("[%s] [RequestListener] Request info is nullptr detected when dealing Triton request.",
            errorCode.c_str());
        return -1;
    }

    const auto& req = reqInfo->GetReqRef();
    return ProcessTritonRequest(reqInfo, req);
}


int RequestListener::DealTGIReq(std::shared_ptr<ReqAgent> reqInfo)
{
    if (reqInfo == nullptr) {
        LOG_E("[%s] [RequestListener] Failed to deal TGI request, request info is nullptr.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::REQUEST_LISTENER).c_str());
        return -1;
    }
    const auto& req = reqInfo->GetReqRef();
    auto connection = reqInfo->GetConnection();
    auto body = boost::beast::buffers_to_string(req.body().data());
    try {
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        reqInfo->SetIsStream(TGIIsStream(req.target(), bodyJson));
        std::string inputs = bodyJson.at("inputs").template get<std::string>();
        if (scheduler->ProcReq(reqInfo->GetReqId(), inputs, MINDIE::MS::ReqType::TGI) != 0) {
            LOG_E("[%s] [RequestListener] Failed to deal TGI request, scheduler process request failed.",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::REQUEST_LISTENER).c_str());
            SendErrorRes(connection, boost::beast::http::status::internal_server_error,
                "Scheduler proc req failed\r\n");
            return -1;
        }
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestListener] Failed to deal TGI request, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str(),
            e.what());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
        return -1;
    }
    return 0;
}

int RequestListener::DealVLLMReq(std::shared_ptr<ReqAgent> reqInfo)
{
    if (reqInfo == nullptr) {
        LOG_E("[%s] [RequestListener] Failed to deal VLLM request, request info is nullptr.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::REQUEST_LISTENER).c_str());
        return -1;
    }
    const auto& req = reqInfo->GetReqRef();
    auto connection = reqInfo->GetConnection();
    auto body = boost::beast::buffers_to_string(req.body().data());
    try {
        std::string inputs;
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        reqInfo->SetIsStream(VLLMIsStream(bodyJson));
        inputs = bodyJson.at("prompt").template get<std::string>();
        if (scheduler->ProcReq(reqInfo->GetReqId(), inputs, MINDIE::MS::ReqType::VLLM) != 0) {
            LOG_E("[%s] [RequestListener] Failed to deal VLLM request, scheduler process request failed.",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::REQUEST_LISTENER).c_str());
            SendErrorRes(connection, boost::beast::http::status::internal_server_error,
                "Scheduler proc req failed\r\n");
            return -1;
        }
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestListener] Failed to deal VLLM, request, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str(), e.what());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
        return -1;
    }
    return 0;
}

int RequestListener::DealOpenAIReq(std::shared_ptr<ReqAgent> reqInfo)
{
    if (reqInfo == nullptr) {
        LOG_E("[%s] [RequestListener] Failed to deal OpenAI request, request info is nullptr.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::REQUEST_LISTENER).c_str());
        return -1;
    }
    const auto& req = reqInfo->GetReqRef();
    auto connection = reqInfo->GetConnection();
    auto body = boost::beast::buffers_to_string(req.body().data());
    try {
        std::string inputs;
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        reqInfo->SetIsStream(OpenAIIsStream(bodyJson));
        if (bodyJson.find("prompt") != bodyJson.end()) {
            inputs = bodyJson.at("prompt").dump();
        } else if (bodyJson.find("messages") != bodyJson.end()) {
            inputs = bodyJson.at("messages").dump();
        } else {
            LOG_E("[%s] [RequestListener] Failed to deal OpenAI request. Invalid request format, "
                "missing both 'prompt' or 'messages'",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CoordinatorFeature::REQUEST_LISTENER).c_str());
            SendErrorRes(connection, boost::beast::http::status::bad_request,
                "Invalid request format: Missing both 'prompt' or 'messages'\r\n");
            return -1;
        }
        if (scheduler->ProcReq(reqInfo->GetReqId(), inputs, MINDIE::MS::ReqType::OPENAI) != 0) {
            LOG_E("[%s] [RequestListener] Failed to deal OpenAI request, scheduler process request failed.",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::REQUEST_LISTENER).c_str());
            SendErrorRes(connection, boost::beast::http::status::internal_server_error,
                "Scheduler proc req failed\r\n");
            return -1;
        }
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestListener] Failed to deal OpenAI request, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str(), e.what());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
        return -1;
    }
    return 0;
}

int RequestListener::DealMindIEReq(std::shared_ptr<ReqAgent> reqInfo)
{
    if (reqInfo == nullptr) {
        LOG_E("[%s] [RequestListener] Failed to deal MindIE request, request info is nullptr.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::REQUEST_LISTENER).c_str());
        return -1;
    }
    const auto& req = reqInfo->GetReqRef();
    auto connection = reqInfo->GetConnection();
    auto body = boost::beast::buffers_to_string(req.body().data());
    try {
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        reqInfo->SetIsStream(MindIEIsStream(bodyJson));
        // inputs 和 input_id 不能同时存在
        if (bodyJson.contains("inputs") && bodyJson.contains("input_id")) {
            LOG_E("[%s] [RequestListener] The body contains both input_id and inputs",
                GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str());
            SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
            return -1;
        }
        size_t inputLen = 0;
        if (bodyJson.contains("inputs")) {
            std::string inputs = bodyJson.at("inputs").template get<std::string>();
            inputLen = inputs.size();
        } else if (bodyJson.contains("input_id")) {
            std::vector<uint32_t> input_id = bodyJson.at("input_id").template get<std::vector<uint32_t>>();
            inputLen = input_id.size();
        } else {
            LOG_E("[%s] [RequestListener] The body does not contains input_id or inputs.",
                GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str());
            SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
            return -1;
        }
        if (scheduler->ProcReq(reqInfo->GetReqId(), inputLen, MINDIE::MS::ReqType::MINDIE) != 0) {
            LOG_E("[%s] [RequestListener] Failed to deal MindIE request, scheduler process request failed.",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::REQUEST_LISTENER).c_str());
            SendErrorRes(connection, boost::beast::http::status::internal_server_error,
                "Scheduler proc req failed\r\n");
            return -1;
        }
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestListener] Failed to deal MindIE request, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str(), e.what());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Request format is invalid\r\n");
        return -1;
    }
    return 0;
}

bool RequestListener::TritonIsStream(const std::string &url) const
{
    // 判断url
    if (url.find("infer") != std::string::npos) {
        return false; // /v2/models/${MODEL_NAME}[/versions/${MODEL_VERSION}]/infer
    }
    if (url.find("generate_stream") != std::string::npos) {
        return true; // /v2/models/${MODEL_NAME}[/versions/${MODEL_VERSION}]/generate_stream
    }
    return false; // /v2/models/${MODEL_NAME}[/versions/${MODEL_VERSION}]/generate
}

bool RequestListener::TGIIsStream(const std::string &url, const nlohmann::json &bodyJson) const
{
    if (url.find("generate_stream") != std::string::npos) {
        return true; // /generate_stream
    }
    if (url.find("generate") != std::string::npos) {
        return false;
    }
    if (bodyJson.contains("stream")) {
        return bodyJson.at("stream").template get<bool>();
    } else {
        return false; // 协议默认false
    }
}

bool RequestListener::VLLMIsStream(const nlohmann::json &bodyJson) const
{
    if (bodyJson.contains("stream")) {
        return bodyJson.at("stream").template get<bool>();
    } else {
        return false;
    }
}

bool RequestListener::OpenAIIsStream(const nlohmann::json &bodyJson) const
{
    if (bodyJson.contains("stream")) {
        return bodyJson.at("stream").template get<bool>();
    } else {
        return false;
    }
}

bool RequestListener::MindIEIsStream(const nlohmann::json &bodyJson) const
{
    if (bodyJson.contains("stream")) {
        return bodyJson.at("stream").template get<bool>();
    } else {
        return false;
    }
}

void RequestListener::ServerResChunkHandler(std::shared_ptr<ServerConnection> connection)
{
    auto reqId = connection->GetReqId();
    reqManage->UpdateState(reqId, ReqState::SEND_TOKENS_TO_USER);
}

void RequestListener::ServerExceptionCloseHandler(std::shared_ptr<ServerConnection> connection)
{
    auto reqId = connection->GetReqId();
    LOG_E("[%s] [RequestListener] Request %s close with exception.",
        GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::REQUEST_LISTENER).c_str(), reqId.c_str());
    exceptionMonitor->PushReqException(ReqExceptionType::USER_DIS_CONN, reqId);
}

void RequestListener::TokenizerReqHandler(std::shared_ptr<ServerConnection> connection)
{
    reqManage->SetReqArriveNum();
    if (!instancesRecord->IsAvailable()) {
        SendErrorRes(connection, boost::beast::http::status::service_unavailable,
            "MindIE-MS Coordinator is not ready\r\n");
        return;
    }
    reqManage->CheckAndHandleReqCongestionAlarm();
    if (reqManage->GetReqNum() >= Configure::Singleton()->reqLimit.maxReqs) {
        SendErrorRes(connection, boost::beast::http::status::too_many_requests, "Too many requests\r\n");
        return;
    }
    if (!PreRequestCheck("Tokenizer", connection)) {
        return;
    }
    if (Configure::Singleton()->IsAbnormal() && !Configure::Singleton()->IsMaster()) { // 异常且为备节点则不做处理
        return;
    }
    CheckMasterAndCreateLinkWithDNode();
    auto &req = connection->GetReq();
    auto uuid = reqIdGen();
    uint64_t reqId = 0;
    boost::hash_combine(reqId, uuid);
    std::string reqIdStr = std::to_string(reqId);
    auto ret = reqManage->AddReq(reqIdStr, ReqInferType::TOKENIZER, connection, req);
    if (!ret) {
        LOG_E("[%s] [RequestListener] Tokenizer request handle failed, duplicate request ID %s detected.",
            GetErrorCode(ErrorType::OPERATION_REPEAT, CoordinatorFeature::REQUEST_LISTENER).c_str(),
            reqIdStr.c_str());
        SendErrorRes(connection, boost::beast::http::status::bad_request, "Duplicate request id\r\n");
        return;
    }
    reqManage->UpdateState(reqIdStr, ReqState::ARRIVE);
    auto insId = instancesRecord->GetTokenizerIns();
    (void)requestRepeater->SingleNodeHandler(reqIdStr, insId);
}

void RequestListener::CheckMasterAndCreateLinkWithDNode()
{
    if (!Configure::Singleton()->CheckBackup()) { // 若不开主备功能不用走这里的建链逻辑
        return;
    }
    if (Configure::Singleton()->IsMaster()) {
        LOG_D("[RequestListener] I'm master.");
        CreateLinkWithDNode();
    }
}


void RequestListener::CreateLinkWithDNode()
{
    const auto& instanceInfos = instancesRecord->GetInstanceInfos();
    int32_t ret = 0;
    for (auto it = instanceInfos.begin(); it != instanceInfos.end(); ++it) {
        if (it->second.role != MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            LOG_D("[RequestListener] CreateLinkWithDNode: skip instance id %llu, not a decode instance.",
                static_cast<unsigned long long>(it->first));
            continue;
        }
        const uint64_t insId = it->first;
        const std::string ip = instancesRecord->GetIp(insId);
        const std::string port = instancesRecord->GetPort(insId);
        if (ip.empty() || port.empty()) {
            LOG_D("[RequestListener] CreateLinkWithDNode: skip instance id %llu, empty ip or port.",
                static_cast<unsigned long long>(insId));
            continue;
        }
        {
            std::lock_guard<std::mutex> lock(linkMutex);
            if (!requestRepeater->CheckLinkWithDNode(ip, port)) {
                ret = LinkWithDNodeInMaxRetry(ip, port, insId);
            }
        }
        if (ret != 0) {
            LOG_E("[%s] [RequestListener] Add link with decode node failed",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::REQUEST_LISTENER).c_str());
        }
    }
}

int32_t RequestListener::LinkWithDNodeInMaxRetry(const std::string &ip, const std::string &port, uint64_t insId)
{
    for (size_t i = 0; i < Configure::Singleton()->exceptionConfig.maxRetry + 1; ++i) {
        if (!instancesRecord->HasInstance(insId)) {
            LOG_I("[RequestListener] LinkWithDNodeInMaxRetry: instance id %llu "
                "no longer in cluster, skip link for %s:%s.",
                static_cast<unsigned long long>(insId), ip.c_str(), port.c_str());
            return 0;
        }
        if (requestRepeater->LinkWithDNode(ip, port) != 0) {
            LOG_D("[RequestListener] LinkWithDNodeInMaxRetry failed add link with decode node at %s:%s, with %u times.",
                ip.c_str(), port.c_str(), i);
            continue;
        }
        LOG_I("[RequestListener] Successfully add link with decode node at %s:%s.", ip.c_str(), port.c_str());
        return 0;
    }
    LOG_E("[%s] [RequestListener] Failed to add link with decode node at %s:%s.",
        GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::REQUEST_LISTENER).c_str(),
        ip.c_str(), port.c_str());
    return -1;
}

bool RequestListener::PreRequestCheck(const std::string& requestType,
    std::shared_ptr<ServerConnection> connection)
{
    if (ShouldIntercept(requestType)) {
        // Send memory limit error response
        SendMemoryLimitError(connection, requestType);
        return false;  // Request intercepted
    }

    return true;  // Request can proceed
}

void RequestListener::SendMemoryLimitError(std::shared_ptr<ServerConnection> connection,
    const std::string& requestType)
{
    std::string errorResponse;

    if (requestType.find("Triton") != std::string::npos) {
        errorResponse = R"({"error": "Memory limit exceeded", "code": 429})";
    } else if (requestType.find("OpenAI") != std::string::npos) {
        errorResponse = R"({"error": {"message": "Memory limit exceeded", "type": "server_error", "code": 429}})";
    } else if (requestType.find("TGI") != std::string::npos ||
            requestType.find("vLLM") != std::string::npos) {
        errorResponse = R"({"error": "Memory limit exceeded", "error_type": "overloaded"})";
    } else {
        errorResponse = R"({"error": "Memory limit exceeded", "code": 429})";
    }

    SendErrorRes(connection, boost::beast::http::status::too_many_requests, errorResponse);
}

bool RequestListener::InitHealthMonitor(ReqManage* reqManage)
{
    if (reqManage == nullptr) {
        LOG_E("[RequestListener] reqManage is null");
        return false;
    }
    auto& healthMonitor = HealthMonitor::GetInstance();
    LOG_I("[RequestListener] Initializing health monitor...");

    bool success = healthMonitor.Initialize(reqManage);
    if (!success) {
        LOG_W("[RequestListener] Health monitor initialization failed. "
            "Memory-based request interception will be disabled.");
    } else {
        auto stats = healthMonitor.GetStats();
        LOG_I("[RequestListener] Health monitor initialized successfully with memory limit: %s",
            MemoryUtil::FormatBytes(stats.memoryLimitBytes).c_str());
    }

    return success;
}

bool RequestListener::ShouldIntercept(const std::string& requestType)
{
    auto& healthMonitor = HealthMonitor::GetInstance();

    // If health monitor is invalid, do not intercept
    if (!healthMonitor.IsValid()) {
        return false;
    }

    // Check if memory usage exceeds threshold
    if (!healthMonitor.ShouldInterceptRequest()) {
        return false;
    }

    // Record intercept (this may trigger graceful shutdown if conditions are met)
    localInterceptCount_++;
    healthMonitor.RecordIntercept();

    // Get current memory usage for logging
    double currentUsage = healthMonitor.GetCurrentMemoryUsage();

    // Log with rate limiting
    LogIntercept(requestType, currentUsage);

    return true;
}

RequestListener::InterceptStats RequestListener::GetInterceptStats() const
{
    auto healthStats = HealthMonitor::GetInstance().GetStats();

    InterceptStats stats;
    stats.healthMonitorValid = healthStats.valid;
    stats.currentMemoryUsage = healthStats.currentMemoryUsage;
    stats.currentMemoryBytes = healthStats.currentMemoryBytes;
    stats.memoryLimit = healthStats.memoryLimitBytes;
    stats.totalIntercepts = healthStats.totalIntercepts;
    stats.consecutiveIntercepts = healthStats.consecutiveIntercepts;
    stats.activeRequests = healthStats.activeRequests;

    return stats;
}

void RequestListener::LogIntercept(const std::string& requestType, double memoryUsage)
{
    if (memoryUsage < 0) {
        freqLogger_.Warn("[RequestListener] Intercepted %s request due to memory pressure. "
            "Memory usage: N/A", requestType.c_str());
    } else {
        freqLogger_.Warn("[RequestListener] Intercepted %s request due to memory pressure. "
            "Memory usage: %.2f%%", requestType.c_str(), memoryUsage * 100); // 100：转换为百分比
    }
}

}