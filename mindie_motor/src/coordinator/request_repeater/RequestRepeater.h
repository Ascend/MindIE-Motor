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
#ifndef MINDIE_MS_COORDINATOR_REQUEST_REPEATER_H
#define MINDIE_MS_COORDINATOR_REQUEST_REPEATER_H

#include <mutex>
#include <shared_mutex>
#include <memory>
#include "ServerConnection.h"
#include "BaseScheduler.h"
#include "boost/uuid/uuid_generators.hpp"
#include "HttpClientAsync.h"
#include "ConnectionPool.h"
#include "ClusterNodes.h"
#include "RequestMgr.h"
#include "ExceptionMonitor.h"
#include "HttpClient.h"
#include "Timer.h"

#ifdef WITH_PROF
#include "msServiceProfiler/msServiceProfiler.h"
#else
constexpr int PROF = 0;
#endif

namespace MINDIE::MS {

enum class RequestPhase {
    PREFILL,
    DECODE
};

class RequestRepeater {
public:
    RequestRepeater(std::unique_ptr<MINDIE::MS::DIGSScheduler>& schedulerInit, std::unique_ptr<ReqManage>& reqManager,
        std::unique_ptr<ClusterNodes>& instancesRec, std::unique_ptr<ExceptionMonitor> &exceptionMonitorInit);
    int Init();
    // PD分离部署形态下的请求转发
    int32_t PDRouteHandler(std::string reqId, uint64_t prefill, uint64_t decode);
    // 给D实例建立长连接，并发送查询结果的请求
    int LinkWithDNode(const std::string &ip, const std::string &port);
    // 返回HttpClientAsync
    HttpClientAsync &GetHttpClientAsync();
    // 检测是否连接成功
    bool CheckLinkWithDNode(boost::beast::string_view ip, boost::beast::string_view port);
    // 单机部署形态下的请求转发
    int32_t SingleNodeHandler(std::string reqId, uint64_t nodeIndex);

    // 异常事件的处理函数：
    // 异常处理回调：连接P实例失败
    void ConnPErrHandler(uint64_t insId);
    // 异常处理回调：向P实例发送请求失败
    void ReqSendPErrHandler(const std::string &reqId);
    // 异常处理回调：连接D实例失败
    void ConnDErrHandler(uint64_t insId);
    // 异常处理回调：连接混合实例失败
    void ConnMixErrHandler(uint64_t insId);
    // 异常处理回调：向混合实例发送请求失败
    void ReqSendMixErrHandler(const std::string &reqId);
    // 异常处理回调：用户连接异常断开
    void UserDisConnHandler(const std::string &reqId);
    // 异常处理回调：用户连接异常断开
    void DecodeDisConnHandler(const std::string &reqId);
    // 异常处理回调：请求调度超时
    void ReqScheduleTimeoutHandler(const std::string &reqId);
    // 异常处理回调：推理首token超时
    void ReqFirstTokenTimeoutHandler(const std::string &reqId);
    // 异常处理回调：推理总时间超时
    void ReqInferTimeoutHandler(const std::string &reqId);
    // 异常处理回调：计算token时连接实例失败
    void TokenizerConnErrHandler(uint64_t insId);
    // 异常处理回调：计算token时向实例发送请求失败
    void TokenizerSendErrHandler(const std::string &reqId);
    // 异常处理回调：计算token超时
    void ReqTokenizerTimeoutHandler(const std::string &reqId);
    // 异常处理回调：请求重计算P实例返回reqId重复错误
    void RetryDuplicateReqIdHandler(const std::string &reqId);
    void LogRequestCompletionStatus(std::string reqId, RequestPhase phase, bool isSuccess);

private:
    std::unique_ptr<MINDIE::MS::DIGSScheduler>& scheduler;
    std::unique_ptr<ReqManage>& reqManage;
    std::unique_ptr<ClusterNodes>& instancesRecord;
    std::unique_ptr<ExceptionMonitor> &exceptionMonitor;
    std::unique_ptr<ConnectionPool> connectionPool;
    ClientHandler pHandler;
    ClientHandler dHandler;
    ClientHandler singleHandler;
    ClientHandler tokenizerHandler;
    HttpClientAsync httpClient;
    Timer timer;
    void PResHandler(std::shared_ptr<ClientConnection> connection);
    void DealPResError(std::shared_ptr<ReqAgent> reqInfo, std::shared_ptr<ClientConnection> connection,
        const std::string &reqId, const std::string &body);
    void DealPRes(std::shared_ptr<ReqAgent> reqInfo, std::shared_ptr<ClientConnection> connection,
        const std::string &reqId, const std::string &body);
    void SendPResStream(std::shared_ptr<ClientConnection> pConn, std::shared_ptr<ServerConnection> userConn,
        const std::string reqId, const std::string data, const bool isLastResp);
    void PResStreamHandler(std::shared_ptr<ClientConnection> pConn, std::shared_ptr<ServerConnection> userConn,
        const nlohmann::json &bodyJson);
    void PResNotStreamHandler(std::shared_ptr<ClientConnection> pConn, std::shared_ptr<ServerConnection> userConn,
        const nlohmann::json &bodyJson, std::shared_ptr<ReqAgent> reqInfo);
    void DResChunkHandler(std::shared_ptr<ClientConnection> connection);
    void PResFinish(std::shared_ptr<ClientConnection> connection);
    void PResErrorHandler(std::shared_ptr<ClientConnection> connection);
    void DResErrorHandler(std::shared_ptr<ClientConnection> connection);
    void PSendHandler(std::shared_ptr<ClientConnection> connection);
    void PSendErrorHandler(std::shared_ptr<ClientConnection> connection);
    void DSendErrorHandler(std::shared_ptr<ClientConnection> connection);
    int32_t ReportAbnormalNodeToController(std::string nodeInfo);
    void SendAlarm(std::string additionalInfo);
    void AddToFaulty(uint64_t insId);
    
    void SingleResHandler(std::shared_ptr<ClientConnection> connection);
    void SingleSendHandler(std::shared_ptr<ClientConnection> connection);
    void SingleResChunkHandler(std::shared_ptr<ClientConnection> connection);
    void SingleResFinish(std::shared_ptr<ClientConnection> connection, ReqState state);
    void SingleResErrorHandler(std::shared_ptr<ClientConnection> connection);
    void SingleSendErrorHandler(std::shared_ptr<ClientConnection> connection);

    void DResultNormalToken(boost::beast::string_view reqId, boost::beast::string_view body);
    void DResultError(boost::beast::string_view reqId, boost::beast::string_view body);
    void DResultRetry(boost::beast::string_view reqId, boost::beast::string_view body);
    void DResultLast(boost::beast::string_view reqId, boost::beast::string_view body);
    MINDIE::MS::Request GetStopInfoReq(const std::string &reqId, std::string modelName) const;
    // 异常处理回调：PD分离取消推理
    void ReqPDStopInferHandler(const std::string &reqId, bool skipDecode);
    // 异常处理回调：PD混合取消推理
    void ReqMixStopInferHandler(const std::string &reqId);
    // Tokenizer相关的http回调函数
    void TokenizerReqHandler(std::shared_ptr<ClientConnection> connection);
    void TokenizerReqErrHandler(std::shared_ptr<ClientConnection> connection);
    void TokenizerResHandler(std::shared_ptr<ClientConnection> connection);
    void TokenizerResErrHandler(std::shared_ptr<ClientConnection> connection);

    // 解析推理输出token数
    void NotStreamSetOutputNum(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const;
    void SetOutputNumTriton(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const;
    void SetOutputNumOpenAI(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const;
    void SetOutputNumVLLM(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const;
    void SetOutputNumTGIOrMindIE(boost::beast::string_view data, std::shared_ptr<ReqAgent> reqInfo) const;
};

}
#endif