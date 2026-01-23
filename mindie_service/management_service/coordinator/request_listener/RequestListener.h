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
#ifndef MINDIE_MS_COORDINATOR_REQUEST_RECEIVER_H
#define MINDIE_MS_COORDINATOR_REQUEST_RECEIVER_H

#include <memory>
#include <atomic>
#include <boost/random/mersenne_twister.hpp>
#include "ServerConnection.h"
#include "BaseScheduler.h"
#include "boost/uuid/uuid_generators.hpp"
#include "HttpClientAsync.h"
#include "ConnectionPool.h"
#include "ClusterNodes.h"
#include "RequestMgr.h"
#include "ExceptionMonitor.h"
#include "PerfMonitor.h"
#include "RequestRepeater.h"
#include "ReqAgent.h"
#include "Logger.h"
#include "RequestMgr.h"
#include "FrequencyLogger.h"
#include "HealthMonitor.h"

namespace MINDIE::MS {

/// The class that define interface of prediction port.
///
/// The class that define interface of prediction port.
class RequestListener {
public:
    RequestListener(std::unique_ptr<MINDIE::MS::DIGSScheduler>& schedulerInit, std::unique_ptr<ReqManage>& reqManager,
        std::unique_ptr<ClusterNodes>& instancesRec,
        std::unique_ptr<ExceptionMonitor> &exceptionMonitorInit, std::unique_ptr<RequestRepeater> &requestRepeaterInit);
    int Init() const;
    // http server回调：收到Triton推理请求
    /// Triton infer request handler.
    ///
    /// If coordinator receive the triton infer request, this function will be called.
    /// The following APIs are supported
    /// POST /v2/models/{MODEL_NAME}/generate_stream
    /// POST /v2/models/{MODEL_NAME}/generate
    /// POST /v2/models/{MODEL_NAME}/infer
    ///
    /// \param connection Request tcp connection.
    void TritonReqHandler(std::shared_ptr<ServerConnection> connection);
    // http server回调：收到TGI流式推理请求
    /// TGI stream infer request handler.
    ///
    /// If coordinator receive the TGI stream infer request, this function will be called.
    /// The following APIs are supported
    /// POST /generate_stream
    /// POST /
    ///
    /// \param connection Request tcp connection.
    void TGIStreamReqHandler(std::shared_ptr<ServerConnection> connection);
    // http server回调：收到TGI非流式或vLLM推理请求
    /// TGI infer request or vLLM infer request handler.
    ///
    /// If coordinator receive the TGI infer request or vLLM infer request, this function will be called.
    /// The following APIs are supported
    /// POST /generate
    ///
    /// \param connection Request tcp connection.
    void TGIOrVLLMReqHandler(std::shared_ptr<ServerConnection> connection);
    // http server回调：收到OpenAI推理请求
    /// OpenAI infer request handler.
    ///
    /// If coordinator receive the OpenAI infer request, this function will be called.
    /// The following APIs are supported
    /// POST /v1/chat/completions
    ///
    /// \param connection Request tcp connection.
    void OpenAIReqHandler(std::shared_ptr<ServerConnection> connection);
    // http server回调：收到MindIE自研推理请求
    /// MindIE infer request handler.
    ///
    /// If coordinator receive the MindIE infer request, this function will be called.
    /// The following APIs are supported
    /// POST /infer
    ///
    /// \param connection Request tcp connection.
    void MindIEReqHandler(std::shared_ptr<ServerConnection> connection);
    // http server回调：向外部用户发送chunk消息成功
    void ServerResChunkHandler(std::shared_ptr<ServerConnection> connection);
    // http server回调：与外部用户连接异常中断
    void ServerExceptionCloseHandler(std::shared_ptr<ServerConnection> connection);
    // 异常处理回调：推理请求重计算
    void ReqRetryHandler(const std::string &reqId);
    // http server回调：收到计算token请求
    /// Tokenizer request handler.
    ///
    /// If coordinator receive the tokenizer request, this function will be called.
    /// The following APIs are supported
    /// POST /v1/tokenizer
    ///
    /// \param connection Request tcp connection.
    void TokenizerReqHandler(std::shared_ptr<ServerConnection> connection);

    void CreateLinkWithDNode();

    /**
    * @brief Initialize the health monitor
    * @param reqManage Pointer to request manager
    * @return true if initialization successful
    */
    bool InitHealthMonitor(ReqManage* reqManage);

    /**
    * @brief Check and intercept request based on memory pressure
    * @param requestType Type of request for logging
    * @return true if request should be intercepted
    */
    bool ShouldIntercept(const std::string& requestType);
    
    /**
    * @brief Get intercept statistics
    */
    struct InterceptStats {
        bool healthMonitorValid = false;       ///< Whether health monitor is valid
        double currentMemoryUsage = 0.0;       ///< Memory usage (0.0-1.0)
        int64_t currentMemoryBytes = 0;        ///< Current memory usage (bytes)
        int64_t memoryLimit = 0;               ///< Memory limit (bytes)
        int totalIntercepts = 0;               ///< Total intercept count
        int consecutiveIntercepts = 0;         ///< Consecutive intercept count
        int activeRequests = 0;                ///< Active request count
    };
    
    InterceptStats GetInterceptStats() const;

private:
    std::unique_ptr<MINDIE::MS::DIGSScheduler>& scheduler;
    std::unique_ptr<ReqManage>& reqManage;
    std::unique_ptr<ClusterNodes>& instancesRecord;
    std::unique_ptr<ExceptionMonitor> &exceptionMonitor;
    std::unique_ptr<RequestRepeater> &requestRepeater;

    thread_local static boost::uuids::random_generator reqIdGen;
    static std::atomic<uint64_t> sReqCounter;
    std::mutex linkMutex;

    std::string GenReqestId();

    int DealTritonReq(std::shared_ptr<ReqAgent> reqInfo);
    int DealTGIReq(std::shared_ptr<ReqAgent> reqInfo);
    int DealVLLMReq(std::shared_ptr<ReqAgent> reqInfo);
    int DealOpenAIReq(std::shared_ptr<ReqAgent> reqInfo);
    int DealMindIEReq(std::shared_ptr<ReqAgent> reqInfo);

    bool TritonIsStream(const std::string &url) const;
    bool TGIIsStream(const std::string &url, const nlohmann::json &bodyJson) const;
    bool VLLMIsStream(const nlohmann::json &bodyJson) const;
    bool OpenAIIsStream(const nlohmann::json &bodyJson) const;
    bool MindIEIsStream(const nlohmann::json &bodyJson) const;
    
    void CheckMasterAndCreateLinkWithDNode();
    int32_t LinkWithDNodeInMaxRetry(const std::string &ip, const std::string &port);
    /**
    * @brief Pre-request check including memory pressure
    */
    bool PreRequestCheck(const std::string& requestType,
                        std::shared_ptr<ServerConnection> connection);

    /**
    * @brief Send memory limit error response
    */
    void SendMemoryLimitError(std::shared_ptr<ServerConnection> connection,
                            const std::string& requestType);

    /**
    * @brief Log intercept event with rate limiting
    */
    void LogIntercept(const std::string& requestType, double memoryUsage);

private:
    // Local intercept counter for this listener (for logging purposes)
    std::atomic<int> localInterceptCount_{0};
    
    // Log interval (30 seconds)
    static constexpr uint64_t logPrintIntervalMs = 30 * 1000;
    FrequencyLogger freqLogger_{logPrintIntervalMs};
};
}
#endif