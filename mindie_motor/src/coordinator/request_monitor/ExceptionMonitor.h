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
#ifndef MINDIE_MS_COORDINATOR_EXCEPTION_MONITOR_H
#define MINDIE_MS_COORDINATOR_EXCEPTION_MONITOR_H

#include <map>
#include <functional>
#include <mutex>
#include <thread>
#include <atomic>
#include <condition_variable>
#include "WriteDeque.h"
#include "ServerConnection.h"

namespace MINDIE::MS {

// 集群实例异常
enum class InsExceptionType {
    CONN_P_ERR, // 与P实例连接失败
    CONN_D_ERR, // 与D实例连接失败
    CONN_MIX_ERR, // 与混合实例连接失败（prefix-cache单机场景）
    CONN_TOKEN_ERR, // 计算token时连接实例失败
};

// 请求异常
enum class ReqExceptionType {
    SEND_P_ERR, // 请求转发P失败
    RETRY, // 请求重调度
    SEND_MIX_ERR, // 请求转发混合实例失败
    USER_DIS_CONN, // 用户连接异常断开
    INFER_TIMEOUT, // 推理超时
    FIRST_TOKEN_TIMEOUT, // 首token超时
    SCHEDULE_TIMEOUT, // 调度超时
    SEND_TOKEN_ERR, // 计算token时转发请求失败
    TOKENIZER_TIMEOUT, // 计算token超时
    RETRY_DUPLICATE_REQID, // 重计算失败，reqId重复
    DECODE_DIS_CONN, // Decode实例断开
};

inline const char *ReqExceptionTypeToStr(ReqExceptionType t)
{
    switch (t) {
        case ReqExceptionType::SEND_P_ERR: return "FAILED_TO_SEND_REQUEST_TO_PREFILL_INSTANCE";
        case ReqExceptionType::RETRY: return "REQUEST_RETRY_OR_RESCHEDULE";
        case ReqExceptionType::SEND_MIX_ERR: return "FAILED_TO_SEND_REQUEST_TO_MIXED_INSTANCE";
        case ReqExceptionType::USER_DIS_CONN: return "USER_CONNECTION_DISCONNECTED";
        case ReqExceptionType::INFER_TIMEOUT: return "INFERENCE_TIMEOUT";
        case ReqExceptionType::FIRST_TOKEN_TIMEOUT: return "FIRST_TOKEN_TIMEOUT";
        case ReqExceptionType::SCHEDULE_TIMEOUT: return "SCHEDULE_TIMEOUT";
        case ReqExceptionType::SEND_TOKEN_ERR: return "FAILED_TO_FORWARD_REQUEST_DURING_TOKEN_COMPUTATION";
        case ReqExceptionType::TOKENIZER_TIMEOUT: return "TOKENIZER_TIMEOUT";
        case ReqExceptionType::RETRY_DUPLICATE_REQID: return "RETRY_FAILED_DUE_TO_DUPLICATE_REQUEST_ID";
        case ReqExceptionType::DECODE_DIS_CONN: return "DECODE_INSTANCE_DISCONNECTED";
        default: return "UNKNOWN_EXCEPTION_TYPE";
    }
}

// 用户链接异常
enum class UserExceptionType {
    CONN_USER_ERR, // 与用户连接断开
};

// 集群实例异常处理函数
using InsExceptionFun = std::function<void(uint64_t)>; // 参数：异常实例id
// 请求异常处理函数
using ReqExceptionFun = std::function<void(const std::string &)>; // 参数：异常请求id
// 用户链接异常处理函数
using UserExceptionFun = std::function<void(std::shared_ptr<ServerConnection> connection)>; // 参数：异常用户链接

class ExceptionMonitor {
public:
    ExceptionMonitor();
    ExceptionMonitor(const ExceptionMonitor& other) = delete;
    ExceptionMonitor& operator=(const ExceptionMonitor& other) = delete;
    ExceptionMonitor(ExceptionMonitor&& other) = delete;
    ExceptionMonitor& operator=(ExceptionMonitor&& other) = delete;
    ~ExceptionMonitor();
    void Start();
    void Stop();
    // 注册集群实例异常处理函数
    void RegInsExceptionFun(InsExceptionType exceptionType, InsExceptionFun fun);
    // 注册请求异常处理函数
    void RegReqExceptionFun(ReqExceptionType exceptionType, ReqExceptionFun fun);
    // 注册用户链接异常处理函数
    void RegUserExceptionFun(UserExceptionType exceptionType, UserExceptionFun fun);
    // 添加集群实例异常
    void PushInsException(InsExceptionType exceptionType, uint64_t insId);
    // 添加请求异常
    void PushReqException(ReqExceptionType exceptionType, const std::string &reqId);
    // 添加用户链接异常
    void PushUserException(UserExceptionType exceptionType, std::shared_ptr<ServerConnection> connection);

private:
    std::mutex mtx;
    std::condition_variable cv;
    std::thread t;
    std::atomic<bool> running;
    ThreadSafeMap<InsExceptionType, InsExceptionFun> insExceptionHandlers;
    ThreadSafeMap<ReqExceptionType, ReqExceptionFun> reqExceptionHandlers;
    ThreadSafeMap<UserExceptionType, UserExceptionFun> userExceptionHandlers;
    WriteDeque<std::pair<InsExceptionType, uint64_t>> insExceptionQueue;
    WriteDeque<std::pair<ReqExceptionType, std::string>> reqExceptionQueue;
    WriteDeque<std::pair<UserExceptionType, std::shared_ptr<ServerConnection>>> userExceptionQueue;
    void OnWork();
    void ExecuteAbnormalEvents();
};

}
#endif
