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
#include "Util.h"

namespace MINDIE::MS {
// 向P发送请求成功的回调函数
void RequestRepeater::PSendHandler(std::shared_ptr<ClientConnection> connection)
{
    auto reqId = connection->GetReqId();
    if (reqId.empty()) {
        return;
    }
    reqManage->UpdateState(reqId, ReqState::REPEATED);
}

// 向P发送请求异常的回调函数
void RequestRepeater::PSendErrorHandler(std::shared_ptr<ClientConnection> connection)
{
    auto reqId = connection->GetReqId();
    if (reqId.empty()) {
        return;
    }
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_W("[%s] [RequestRepeater] Get request %s failed.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::P_REQUESTREPEATER).c_str(), reqId.c_str());
        return;
    }
    if (reqInfo->GetRetry() > Configure::Singleton()->exceptionConfig.maxRetry) {
        auto insId = reqInfo->GetRoute()[0];
        auto ip = instancesRecord->GetIp(insId);
        auto port = instancesRecord->GetPort(insId);
        LOG_E("[%s] [RequestRepeater] Send request %s to instance %s:%s failed.",
            GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
            reqId.c_str(), ip.c_str(), port.c_str());
        SendErrorRes(reqInfo->GetConnection(), boost::beast::http::status::internal_server_error,
            "Send messges to p instance failed\r\n");
        reqManage->UpdateState(reqId, ReqState::EXCEPTION);
        LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, false);
        return;
    }
    exceptionMonitor->PushReqException(ReqExceptionType::SEND_P_ERR, reqId);
}

// 向P读取应答异常的时候的回调函数
void RequestRepeater::PResErrorHandler(std::shared_ptr<ClientConnection> connection)
{
    auto reqId = connection->GetReqId();
    if (reqId.empty()) {
        return;
    }
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_W("[%s] [RequestRepeater] Get request with id %s information failed.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::P_REQUESTREPEATER).c_str(), reqId.c_str());
        return;
    }
    LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, false);
    // 请求已终止，不必往下走
    if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION) ||
        reqInfo->HasState(ReqState::TIMEOUT)) {
        LOG_W("[%s] [Request_Trace] Request with ID %s is already ended.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::P_REQUESTREPEATER).c_str(), reqId.c_str());
        return;
    }
    LOG_D("[Request_Trace] The response has been sent to the user, request ID is %s", reqId.c_str());
    auto serverConn = reqInfo->GetConnection();
    SendErrorRes(serverConn, boost::beast::http::status::internal_server_error,
        "Receive messages from p instance failed\r\n");
    reqManage->UpdateState(reqId, ReqState::EXCEPTION);
}

void RequestRepeater::DealPResError(std::shared_ptr<ReqAgent> reqInfo, std::shared_ptr<ClientConnection> connection,
    const std::string &reqId, const std::string &body)
{
    LOG_E("[%s] [RequestRepeater] Request ID %s prefill failed, error detail: %s.",
        GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
        reqId.c_str(), body.c_str());
        
    LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, false);
    if (reqInfo->HasState(ReqState::RETRY)) {
        // 请求触发过重计算，并且返回重复reqId失败，重新下发这条请求
        LOG_W("[%s] [RequestRepeater] Request %s retry failed, try again...",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::P_REQUESTREPEATER).c_str(), reqId.c_str());
        connection->SetAvailable(true);
        return exceptionMonitor->PushReqException(ReqExceptionType::RETRY_DUPLICATE_REQID, reqId);
    }
    auto &res = connection->GetResMessage();
    ServerRes serverRes;
    serverRes.body = body;
    serverRes.state = res.result();
    auto serverConn = reqInfo->GetConnection();
    serverConn->SendRes(serverRes);
    PResFinish(connection);
    reqManage->UpdateState(reqId, ReqState::EXCEPTION);
}

void RequestRepeater::DealPRes(std::shared_ptr<ReqAgent> reqInfo, std::shared_ptr<ClientConnection> connection,
    const std::string &reqId, const std::string &body)
{
    auto serverConn = reqInfo->GetConnection();
    if (serverConn == nullptr) {
        LOG_E("[%s] [Request_Trace] Get user connection failed, request id is %s.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::P_REQUESTREPEATER).c_str(), reqId.c_str());
        return PResFinish(connection);
    }
    if (reqInfo->HasState(ReqState::RETRY)) {
        LOG_D("[RequestRepeater] Clearing retry state for request ID %s.", reqId.c_str());
        reqInfo->ClearRetry();
    }
    try {
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        auto rcvReqId = bodyJson.at("reqId").template get<std::string>();
        if (reqId != rcvReqId) {
            LOG_E("[%s] [RequestRepeater] Mismatched request ID, received %s, expected %s.", GetErrorCode(ErrorType::
                UNREACHABLE, CoordinatorFeature::P_REQUESTREPEATER).c_str(), rcvReqId.c_str(), reqId.c_str());
            return;
        }
        auto isStream = reqInfo->GetIsStream();
        LOG_D("[RequestRepeater] DealPRes:  Request ID %s stream status is %d.", reqId.c_str(), isStream);
        if (isStream) {
            PResStreamHandler(connection, serverConn, bodyJson);
        } else {
            PResNotStreamHandler(connection, serverConn, bodyJson, reqInfo);
        }
    } catch (const nlohmann::json::exception& e) {
        std::string bodyStr;
        bodyStr.assign(body.data(), body.size());
        LOG_E("[%s] [RequestRepeater] Failed to parse request body: %s, json parse error is %s.", GetErrorCode(
            ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(), bodyStr.c_str(), e.what());
        SendErrorRes(serverConn, boost::beast::http::status::internal_server_error, "P instance error\r\n");
        reqManage->UpdateState(reqId, ReqState::EXCEPTION);
        LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, false);
        return;
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestRepeater] Exception while processing response for request ID %s: %s.", GetErrorCode(
            ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(), reqId.c_str(), e.what());
        SendErrorRes(serverConn, boost::beast::http::status::internal_server_error, "P instance error\r\n");
        reqManage->UpdateState(reqId, ReqState::EXCEPTION);
        LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, false);
        return;
    }
}

// 向P读取应答成功的时候的回调函数
void RequestRepeater::PResHandler(std::shared_ptr<ClientConnection> connection)
{
    auto reqId = connection->GetReqId();
     // 判断请求不存在不再处理
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_W("[%s] [RequestRepeater] Get request id %s information failed.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::P_REQUESTREPEATER).c_str(), reqId.c_str());
        return;
    }
    // 请求已终止，不必往下走
    if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION) ||
        reqInfo->HasState(ReqState::TIMEOUT)) {
        LOG_W("[%s] [Request_Trace] Request with ID %s is already ended.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::P_REQUESTREPEATER).c_str(), reqId.c_str());
        return;
    }
    reqManage->UpdateState(reqId, ReqState::RECV_TOKENS_FROM_INS);
    auto &res = connection->GetResMessage();
    auto body = boost::beast::buffers_to_string(res.body().data());
    if (res.result() != boost::beast::http::status::ok) {
        LOG_E("[%s] [Request_Trace] PResHandler receive error code: %u",
            GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::P_REQUESTREPEATER).c_str(), res.result());
        return DealPResError(reqInfo, connection, reqId, body);
    } else {
        PROF(INFO, Domain("Coordinator").Resource(reqId).Attr("Phase", "prefill").Event("GenerateToken"));
        return DealPRes(reqInfo, connection, reqId, body);
    }
}

void RequestRepeater::SendPResStream(std::shared_ptr<ClientConnection> pConn,
    std::shared_ptr<ServerConnection> userConn, const std::string reqId, const std::string data, const bool isLastResp)
{
    ServerRes res;
    res.body = data;
    res.contentType = "text/event-stream";
    res.isFinish = isLastResp;
    LOG_D("[Request_Trace] Sent streaming response for request ID %s. %s.",
          reqId.c_str(), isLastResp ? "This reponse marks the end of the stream" : "Additional reponse are expected.");
    // 如果first token即last token， 则直接刷新为终止。
    if (isLastResp) {
        LOG_D("[Request_Trace] Final reponse received for request ID %s. Finishing the request.",
              reqId.c_str());
        userConn->SendRes(res);
        reqManage->AddOutputNum(reqId, 1);
        PResFinish(pConn);
        LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, true);
        return reqManage->UpdateState(reqId, ReqState::FINISH);
    }

    auto reqAgent = reqManage->GetReqInfo(reqId);
    if (reqAgent == nullptr) {
        LOG_E("[%s] [RequestRepeater] Failed to get information for request ID %s.",
              GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
              reqId.data());
        return;
    }

    PResFinish(pConn);
    LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, true);

    bool finshFlag = false;
    reqAgent->RepeatPStreamToken(res, finshFlag);
    if (finshFlag) {
        reqManage->UpdateState(reqId, ReqState::FINISH);
    }
}

// 触发读取P的流式应答
void RequestRepeater::PResStreamHandler(std::shared_ptr<ClientConnection> pConn,
    std::shared_ptr<ServerConnection> userConn, const nlohmann::json &bodyJson)
{
    std::string reqId;
    std::string data;
    bool isLastResp;
    try {
        reqId = bodyJson.at("reqId").template get<std::string>();
        data = bodyJson.at("output").template get<std::string>();
        isLastResp = bodyJson.at("isLastResp").template get<bool>();
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [RequestRepeater] Failed to read request body: %s, json parse error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
              bodyJson.dump().c_str(), e.what());
        SendErrorRes(userConn, boost::beast::http::status::internal_server_error, "P instance error\r\n");
        PResFinish(pConn);
        LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, false);
        return reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestRepeater] Error parsing streaming response for request ID %s is %s.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
              reqId.c_str(), e.what());
        SendErrorRes(userConn, boost::beast::http::status::internal_server_error, "P instance error\r\n");
        PResFinish(pConn);
        LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, false);
        return reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    }
    SendPResStream(pConn, userConn, reqId, data, isLastResp);
}

void RequestRepeater::NotStreamSetOutputNum(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const
{
    auto inferType = reqInfo->GetType();
    if (inferType == ReqInferType::TGI || inferType == ReqInferType::MINDIE) {
        SetOutputNumTGIOrMindIE(data, reqInfo);
    } else if (inferType == ReqInferType::VLLM) {
        SetOutputNumVLLM(data, reqInfo);
    } else if (inferType == ReqInferType::OPENAI) {
        SetOutputNumOpenAI(data, reqInfo);
    } else if (inferType == ReqInferType::TRITON) {
        SetOutputNumTriton(data, reqInfo);
    }
}

void RequestRepeater::SetOutputNumTriton(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const
{
    try {
        auto dataJson = nlohmann::json::parse(data, CheckJsonDepthCallBack);
        if (dataJson.contains("text_output")) {
            auto textOutput = dataJson.at("text_output").template get<std::string>();
            reqInfo->AddOutputNum(
                static_cast<size_t>(static_cast<float>(textOutput.size()) / Configure::Singleton()->strTokenRate));
        } else {
            auto outputs = dataJson.at("outputs");
            for (auto it = outputs.begin(); it != outputs.end(); ++it) {
                auto tokens = it->at("data").template get<std::vector<uint32_t>>();
                reqInfo->AddOutputNum(tokens.size());
            }
        }
    } catch (const nlohmann::json::exception& e) {
        std::string dataStr;
        dataStr.assign(data.data(), data.size());
        LOG_E("[%s] [RequestRepeater] Failed to parse request body: %s, json parse error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
              dataStr.c_str(), e.what());
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestRepeater] Failed to set Triton output number, exception is %s.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(), e.what());
    }
}

void RequestRepeater::SetOutputNumOpenAI(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const
{
    try {
        auto dataJson = nlohmann::json::parse(data, CheckJsonDepthCallBack);
        auto choices = dataJson.at("choices");
        for (auto it = choices.begin(); it != choices.end(); ++it) {
            auto message = it->at("message");
            auto output = message.dump();
            reqInfo->AddOutputNum(
                static_cast<size_t>(static_cast<float>(output.size()) / Configure::Singleton()->strTokenRate));
        }
    } catch (const nlohmann::json::exception& e) {
        std::string dataStr;
        dataStr.assign(data.data(), data.size());
        LOG_E("[%s] [RequestRepeater] Failed to parse request body: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
            dataStr.c_str(), e.what());
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestRepeater] Failed to set OpenAI output number, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(), e.what());
    }
}

void RequestRepeater::SetOutputNumVLLM(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const
{
    try {
        auto dataJson = nlohmann::json::parse(data, CheckJsonDepthCallBack);
        auto text = dataJson.at("text").template get<std::vector<std::string>>();
        for (auto &elem : std::as_const(text)) {
            reqInfo->AddOutputNum(
                static_cast<size_t>(static_cast<float>(elem.size()) / Configure::Singleton()->strTokenRate));
        }
    } catch (const nlohmann::json::exception& e) {
        std::string dataStr;
        dataStr.assign(data.data(), data.size());
        LOG_E("[%s] [RequestRepeater] Failed to parse request body: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
            dataStr.c_str(), e.what());
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestRepeater] Failed to set VLLM output number, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(), e.what());
    }
}

void RequestRepeater::SetOutputNumTGIOrMindIE(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const
{
    try {
        auto dataJson = nlohmann::json::parse(data, CheckJsonDepthCallBack);
        auto outputStr = dataJson.at("generated_text").template get<std::string>();
        reqInfo->AddOutputNum(
            static_cast<size_t>(static_cast<float>(outputStr.size()) / Configure::Singleton()->strTokenRate));
    } catch (const nlohmann::json::exception& e) {
        std::string dataStr;
        dataStr.assign(data.data(), data.size());
        LOG_E("[%s] [RequestRepeater] Failed to parse request body: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
            dataStr.c_str(), e.what());
    } catch (const std::exception &e) {
        LOG_E("[%s] [RequestRepeater] Failed to set TGI or MindIE output number, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(), e.what());
    }
}

// 触发读取P的非流式应答
void RequestRepeater::PResNotStreamHandler(std::shared_ptr<ClientConnection> pConn,
    std::shared_ptr<ServerConnection> userConn, const nlohmann::json &bodyJson, std::shared_ptr<ReqAgent> reqInfo)
{
    std::string reqId;
    bool isLastResp = false;
    std::string data;
    try {
        reqId = bodyJson.at("reqId").template get<std::string>();
        if (bodyJson.contains("isLastResp")) {
            isLastResp = bodyJson.at("isLastResp").template get<bool>();
        }
        if (isLastResp) {
            data = bodyJson.at("output").template get<std::string>();
        }
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [Request_Trace] Failed to read request body: %s, json parse error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
              bodyJson.dump().c_str(), e.what());
        SendErrorRes(userConn, boost::beast::http::status::internal_server_error, "P instance error\r\n");
        PResFinish(pConn);
        LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, false);
        return reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    } catch (const std::exception &e) {
        LOG_E("[%s] [Request_Trace] Error processing non-streaming response for request ID %s, error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::P_REQUESTREPEATER).c_str(),
              reqId.c_str(), e.what());
        SendErrorRes(userConn, boost::beast::http::status::internal_server_error, "P instance error\r\n");
        PResFinish(pConn);
        LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, false);
        return reqManage->UpdateState(reqId, ReqState::EXCEPTION);
    }
    if (isLastResp) {
        ServerRes res;
        res.body = data;
        res.contentType = "application/json";
        res.isFinish = isLastResp;
        LOG_D("[Request_Trace] Received the final token for request ID %s. Finish sending normal response back.",
            reqId.c_str());
        NotStreamSetOutputNum(data, reqInfo);
        userConn->SendRes(res);
        reqManage->UpdateState(reqId, ReqState::FINISH);
    }
    LogRequestCompletionStatus(reqId, RequestPhase::PREFILL, true);
    PResFinish(pConn); // 置位P状态
}

void RequestRepeater::PResFinish(std::shared_ptr<ClientConnection> connection)
{
    auto reqId = connection->GetReqId();
    reqManage->UpdateState(reqId, ReqState::FIRST_TOKEN_FINISH);
}
}