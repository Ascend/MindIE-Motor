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
#include "metrics/Metrics.h"

namespace MINDIE::MS {
std::map<int, std::map<int, int>> token_distribution;
constexpr int MAX_TOKEN_RANGE = 128000;
const std::vector<int> token_ranges = {10, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 16000, 20000, 30000, 50000, 64000, MAX_TOKEN_RANGE};
static size_t StrFindRepeat(boost::beast::string_view str, char c, size_t i)
{
    size_t j = i + 1;
    for (; j < str.size(); ++j) { // 确认后面有没有重复的
        if (str[j] != c) {
            return j - 1;
        }
    }
    return j - 1;
}

// 找的是最后一个字符的位置，比如: "aaaabbbaaa",搜索a，返回的是3； "abbbba",搜索a,返回0
static size_t StrFindLast(boost::beast::string_view str, char c)
{
    for (size_t i = 0; i < str.size(); ++i) {
        if (str[i] == c) {
            return StrFindRepeat(str, c, i);
        }
    }
    return std::string::npos;
}

// 向D发起长链接，并发送，查询结果的请求
int RequestRepeater::LinkWithDNode(const std::string &ip, const std::string &port)
{
    boost::beast::http::request<boost::beast::http::dynamic_body> req;
    req.version(HTTP_VERSION_11);
    req.method(boost::beast::http::verb::get);
    req.target("/dresult");
    req.set(boost::beast::http::field::user_agent, Configure::Singleton()->httpConfig.userAgent);
    req.keep_alive(true);
    auto conn = connectionPool->ApplyConn(ip, port, dHandler, 30);
    if (conn == nullptr) {
        LOG_E("[%s] [RequestRepeater] Failed to apply connection to decode node at IP: %s and port: %s.",
            GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::D_REQUESTREPEATER).c_str(),
            ip.c_str(), port.c_str());
        return -1;
    }
    LOG_D("[Request_Trace] Connection established with node at IP %s and port %s. "
        "GET request for results has been sent.", ip.c_str(), port.c_str());
    conn->SendReq(req);
    return 0;
}

bool RequestRepeater::CheckLinkWithDNode(boost::beast::string_view ip, boost::beast::string_view port)
{
    auto connIds = httpClient.FindId(std::string(ip), std::string(port));
    for (auto &connId : connIds) {
        std::shared_ptr<ClientConnection> conn = httpClient.GetConnection(connId);
        if (conn == nullptr) {
            LOG_D("[RequestRepeater] Connection ID %u is null, continuing to check next connection", connId);
            continue;
        } else {
            LOG_D("[RequestRepeater] Found valid connection ID %u for %s:%s",
                connId, std::string(ip).c_str(), std::string(port).c_str());
            return true;
        }
    }
    LOG_D("[RequestRepeater] No valid connections found for decode node at %s:%s",
        std::string(ip).c_str(), std::string(port).c_str());
    return false;
}

HttpClientAsync &RequestRepeater::GetHttpClientAsync()
{
    return httpClient;
}

// 向D发送请求的时候，出现错误
void RequestRepeater::DSendErrorHandler(std::shared_ptr<ClientConnection> connection)
{
    auto ip = connection->GetIp();
    auto port = connection->GetPort();
    auto insId = instancesRecord->GetId(ip, port);
    if (instancesRecord->GetRetry(insId) > Configure::Singleton()->exceptionConfig.maxRetry) {
        scheduler->RemoveInstance({insId});
        instancesRecord->RemoveInstance(insId);
        return;
    }
    instancesRecord->AddRetry(insId);
    exceptionMonitor->PushInsException(InsExceptionType::CONN_D_ERR, insId);
}

// 读取D的响应，出现错误
void RequestRepeater::DResErrorHandler(std::shared_ptr<ClientConnection> connection)
{
    auto ip = connection->GetIp();
    auto port = connection->GetPort();
    auto insId = instancesRecord->GetId(ip, port);
    exceptionMonitor->PushInsException(InsExceptionType::CONN_D_ERR, insId);
}

static std::string ParseKeyWord(boost::beast::string_view body, size_t pos)
{
    size_t i = pos;
    for (; i > 0; --i) {
        if (body[i - 1] == '\0') {
            break;
        }
    }
    return body.substr(i, pos - i);
}

// 收到来自D的正常应答
void RequestRepeater::DResChunkHandler(std::shared_ptr<ClientConnection> connection)
{
    boost::beast::string_view body = connection->GetResChunkedBody();
    std::string reqId = "";
    while (1) { // 拆包流程：先找 \0 , 将\0中间的内容拆成一个消息包，再解析 ':' 前面的关键字，按照关键字类型，进入不同的分支处理
        if (body.empty()) {
            return;
        }
        auto pos = StrFindLast(body, '\0');
        if (pos == std::string::npos) {
            // 暂不支持不带\0的消息，需要先保证server每次chunk都是带\0的
            LOG_W("[%s] [RequestRepeater] Received a message chunk without the required delimiter ('\\0').",
                GetWarnCode(ErrorType::WARNING, CoordinatorFeature::D_REQUESTREPEATER).c_str(), body.data());
            return;
        }
        std::string oneMessage = body.substr(0, pos); // 拆包，取出一条消息
        if (pos < body.size()) {
            body = body.substr(pos + 1); // 拆掉已经取出的消息
        } else {
            body = std::string("");
        }
        // 先解析报文中的各种关键字
        auto pos1 = StrFindLast(oneMessage, ':'); // 找到关键字符 :
        if (pos1 == std::string::npos) {
            continue; // 可能是空字符串，或者其他未知的内容，忽略
        }
        if (pos1 + 1 == oneMessage.size()) {
            continue; // :后面已经没有内容了，不需要再去解析
        }
        auto keyWord = ParseKeyWord(oneMessage, pos1);
        oneMessage = oneMessage.substr(pos1 + 1); // 取出 : 后面的消息体
        if (keyWord == "reqId") { // 解析reqId
            reqId = oneMessage;
        } else if (keyWord == "data") { // 普通token（流式时最后一条可能带 usage，需参与二维表统计）
            TryUpdateTokenDistributionFromUsage(std::string(oneMessage.data(), oneMessage.size()));
            DResultNormalToken(reqId, oneMessage);
        } else if (keyWord == "lastData") { // 最后一个token
            TryUpdateTokenDistributionFromUsage(std::string(oneMessage.data(), oneMessage.size()));
            DResultLast(reqId, oneMessage);
        } else if (keyWord == "error") { // 错误消息
            DResultError(reqId, oneMessage);
        } else if (keyWord == "retry") { // 重计算
            DResultRetry(reqId, oneMessage);
        } else if (keyWord == "ka") { // 心跳包
            LOG_D("[RequestRepeater] Received keep-alive heartbeat from D node, address: %s.",
                connection->GetAddress().c_str());
        } else if (keyWord == "close") { // 连接关闭
            LOG_D("[RequestRepeater] Received close message from D node, closing connection at address: %s.",
                connection->GetAddress().c_str());
            connection->DoClose();
        } else {
            // 其他的关键字，暂不支持，忽略
            LOG_W("[RequestRepeater] Received unknown keyword: %s, from message: %s.",
                keyWord.c_str(), oneMessage.c_str());
        }
    }
}

// 收到D的应答，错误码为0，提示D正常应答
void RequestRepeater::DResultNormalToken(boost::beast::string_view reqId, boost::beast::string_view body)
{
    // 判断请求不存在不再处理
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_W("[%s] [RequestRepeater] Get request id %s info failed",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::D_REQUESTREPEATER).c_str(), reqId.data());
        return;
    }
    // 请求已终止，不必往下走
    if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION) ||
        reqInfo->HasState(ReqState::TIMEOUT)) {
        LogRequestCompletionStatus(reqId, RequestPhase::DECODE, false);
        LOG_W("[%s] [Request_Trace] Request with ID %s is already ended.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::D_REQUESTREPEATER).c_str(), reqId.data());
        return;
    }
    auto userConn = reqInfo->GetConnection();
    if (userConn == nullptr) {
        LOG_E("[%s] [RequestRepeater] User connection is nullptr.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::D_REQUESTREPEATER).c_str());
        return;
    }
    PROF(INFO, Domain("Coordinator").Resource(reqId.data()).Attr("Phase", "decode")
        .Attr("IsStream", reqInfo->GetIsStream()).Event("GenerateToken"));
    reqManage->UpdateState(reqId, ReqState::RECV_TOKENS_FROM_INS);
    ServerRes res;
    res.body = body;
    auto isStream = reqInfo->GetIsStream();
    res.contentType = isStream ? "text/event-stream" : "application/json";
    res.isFinish = false;
    LOG_D("[RequestRepeater] Send normal decode token to user, request ID is %s, body.size() is %zu",
        reqId.data(), body.size());
    bool isFinishFlag = false;
    reqInfo->RepeatDStreamToken(res, isFinishFlag);
    if (isFinishFlag) {
        reqManage->UpdateState(reqId, ReqState::FINISH);
    }
}

// 收到D的应答，错误码提示：推理错误
void RequestRepeater::DResultError(boost::beast::string_view reqId, boost::beast::string_view body)
{
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_W("[%s] [RequestRepeater] Get request %s info failed.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::D_REQUESTREPEATER).c_str(), reqId.data());
        return;
    }
    LogRequestCompletionStatus(reqId, RequestPhase::DECODE, false);
    // 请求已终止，不必往下走
    if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION) ||
        reqInfo->HasState(ReqState::TIMEOUT)) {
        LOG_W("[%s] [Request_Trace] Request with ID %s is already ended.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::D_REQUESTREPEATER).c_str(), reqId.data());
        return;
    }
    auto conn = reqInfo->GetConnection();
    if (conn == nullptr) {
        LOG_E("[%s] [RequestRepeater] Get user connection failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::D_REQUESTREPEATER).c_str());
        return;
    }
    ServerRes res;
    res.body = body;
    auto isStream = reqInfo->GetIsStream();
    res.contentType = isStream ? "text/event-stream" : "application/json";
    res.isFinish = true;
    LOG_D("[RequestRepeater] Send error response to the user, request ID is %s, body.size() is %zu",
        reqId.data(), body.size());
    conn->SendRes(res);
    reqManage->UpdateState(reqId, ReqState::EXCEPTION);
}

// 收到D的应答，错误码提示，需要触发重计算
void RequestRepeater::DResultRetry(boost::beast::string_view reqId, boost::beast::string_view body)
{
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_W("[%s] [RequestRepeater] Get request %s failed.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::D_REQUESTREPEATER).c_str(), reqId.data());
        return;
    }
    reqManage->UpdateState(reqId, ReqState::RETRY);
    LogRequestCompletionStatus(reqId, RequestPhase::DECODE, false);
    // 请求已终止，不必往下走
    if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION) ||
        reqInfo->HasState(ReqState::TIMEOUT)) {
        LOG_W("[%s] [Request_Trace] ReqId : %s already ended, no need to go ahead DResultRetry",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::D_REQUESTREPEATER).c_str(), reqId.data());
        return;
    }
    LOG_D("[RequestRepeater] Decode request retrying, request ID is %s, body.size() is %zu.",
        reqId.data(), body.size());
    auto req = reqInfo->GetReq();
    req.set("is-recompute", "true");
    req.body().clear();
    req.prepare_payload();
    boost::beast::ostream(req.body()) << body;
    req.prepare_payload();
    reqInfo->SetReq(req);
    exceptionMonitor->PushReqException(ReqExceptionType::RETRY, reqId);
}

// 请求的最后一个token
void RequestRepeater::DResultLast(boost::beast::string_view reqId, boost::beast::string_view body)
{
    // 判断请求不存在不再处理
    auto reqInfo = reqManage->GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        LOG_W("[%s] [RequestRepeater] Get request id %s information failed.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::D_REQUESTREPEATER).c_str(), reqId.data());
        return;
    }
    // 请求已终止，不必往下走
    if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION) ||
        reqInfo->HasState(ReqState::TIMEOUT)) {
        LogRequestCompletionStatus(reqId, RequestPhase::DECODE, false);
        LOG_W("[%s] [Request_Trace] Request with ID %s has already ended.",
            GetWarnCode(ErrorType::WARNING, CoordinatorFeature::D_REQUESTREPEATER).c_str(), reqId.data());
        return;
    }
    auto userConn = reqInfo->GetConnection();
    if (userConn == nullptr) {
        LogRequestCompletionStatus(reqId, RequestPhase::DECODE, false);
        LOG_E("[%s] [RequestRepeater] User connection is nullptr.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::D_REQUESTREPEATER).c_str());
        return;
    }
    PROF(INFO, Domain("Coordinator").Resource(reqId.data()).Attr("Phase", "decode")
        .Attr("IsStream", reqInfo->GetIsStream()).Attr("IsLast", "true").Event("GenerateToken"));
    reqManage->UpdateState(reqId, ReqState::RECV_TOKENS_FROM_INS);
    LogRequestCompletionStatus(reqId, RequestPhase::DECODE, true);
    ServerRes res;
    res.body = body;
    auto isStream = reqInfo->GetIsStream();
    res.contentType = isStream ? "text/event-stream" : "application/json";
    res.isFinish = true;

    if (!reqInfo->GetIsStream()) { // 非流式D token转发
        NotStreamSetOutputNum(body, reqInfo);
        LOG_D("[RequestRepeater] Send last decode token to user, id is %s, body.size() is %zu.",
            reqId.data(), body.size());
        userConn->SendRes(res);
        reqManage->UpdateState(reqId, ReqState::FINISH);
        return;
    }

    // 流式D token转发
    bool isFinishFlag = false;
    reqInfo->RepeatDStreamToken(res, isFinishFlag);
    if (isFinishFlag) {
        reqManage->UpdateState(reqId, ReqState::FINISH);
        PROF(INFO, Domain("Coordinator").Resource(reqId.data()).Attr("Phase", "decode").Event("ReqFinish"));
    }
}

}