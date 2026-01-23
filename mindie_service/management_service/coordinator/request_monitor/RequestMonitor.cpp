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
#include <chrono>
#include "Configure.h"
#include "Logger.h"
#include "RequestMonitor.h"

using namespace MINDIE::MS;

namespace {
constexpr size_t SECOND = 1000000000;
}

RequestMonitor::RequestMonitor(std::unique_ptr<ReqManage>& reqManageInit,
    std::unique_ptr<ExceptionMonitor> &exceptionMonitorInit)
    : reqManage(reqManageInit), exceptionMonitor(exceptionMonitorInit), running(true) {}

RequestMonitor::~RequestMonitor()
{
    Stop();
}

void RequestMonitor::Start()
{
    t = std::thread([this] {
        while (running) {
            OnWork();
            sleep(1);
        }
    });
}

void RequestMonitor::Stop()
{
    running = false;
    if (t.joinable()) {
        t.join();
    }
}

void RequestMonitor::OnWork()
{
    // 定期删除已完成的请求
    reqManage->ReleaseFinishedRequest();

    auto reqs = reqManage->GetAllReqs();
    if (reqs.empty()) {
        return;
    }
    for (auto& it : std::as_const(reqs)) {
        auto &reqInfo = it.second;
        if (reqInfo->HasState(ReqState::EXCEPTION)) {
            continue;
        }

        if (reqInfo->HasState(ReqState::FINISH)) {
            continue;
        }

        // 如果已经超时，无需再做检测
        if (reqInfo->HasState(ReqState::TIMEOUT)) {
            continue;
        }

        // 若尚无arrive起点时间，则暂不观测超时
        if (!reqInfo->HasState(ReqState::ARRIVE)) {
            continue;
        }

        // 调度超时判断逻辑：
        // 若请求已进入，但是一直未到达SCHEDULED， 判断是否调度超时
        auto scheduledFlag = reqInfo->HasState(ReqState::SCHEDULED);
        if (!scheduledFlag) {
            if (ScheduleTimeOut(reqInfo)) {
                LOG_W("[RequestMonitor] Request %s exceeded schedule timeout.", it.first.c_str());
                exceptionMonitor->PushReqException(ReqExceptionType::SCHEDULE_TIMEOUT, it.first);
                continue;
            }
        }

        // 如果请求是Tokenizer类型，则判断Tokenizer是否超时
        if (reqInfo->GetType() == ReqInferType::TOKENIZER) {
            if (TokenizerTimeout(reqInfo)) {
                LOG_W("[RequestMonitor] Request %s exceeded tokenizer timeout.", it.first.c_str());
                exceptionMonitor->PushReqException(ReqExceptionType::TOKENIZER_TIMEOUT, it.first);
            }
            continue;
        }
        // 首token超时判断逻辑：
        // 若请求已进入，但是一直未到达回复首token， 判断是否首token调度超时
        if (!HandleFirstTokenTimeout(it)) {
            continue;
        }
        // 端到端推理完成超时判断逻辑
        if (InferTimeOut(reqInfo)) {
            // 检查分配的PD实例是否还存在
            if (!reqManage->ArePDInstancesValid(it.first)) {
                // PD实例已经不存在，直接更新状态为EXCEPTION
                LOG_W("[RequestMonitor] PD instances not exist, "
                      "update request %s to EXCEPTION state", it.first.c_str());
                reqManage->UpdateState(it.first, ReqState::EXCEPTION);
                continue;
            }
            LOG_I("[RequestMonitor] Request %s exceeded inference timeout.", it.first.c_str());
            exceptionMonitor->PushReqException(ReqExceptionType::INFER_TIMEOUT, it.first);
            continue;
        }
    }
}

bool RequestMonitor::HandleFirstTokenTimeout(const std::pair<const std::string, std::shared_ptr<ReqAgent>>& reqPair)
{
    auto finishPrefillFlag = reqPair.second->HasState(ReqState::FIRST_TOKEN_FINISH);
    if (!finishPrefillFlag) {
        if (FirstTokenTimeOut(reqPair.second)) {
            // 检查分配的PD实例是否还存在
            if (!reqManage->ArePDInstancesValid(reqPair.first)) {
                // PD实例已经不存在，直接更新状态为EXCEPTION
                LOG_I("[RequestMonitor] PD instances not exist, "
                      "update request %s to EXCEPTION state", reqPair.first.c_str());
                reqManage->UpdateState(reqPair.first, ReqState::EXCEPTION);
                return false;
            }
            LOG_W("[RequestMonitor] Request %s exceeded first token timeout.", reqPair.first.c_str());
            exceptionMonitor->PushReqException(ReqExceptionType::FIRST_TOKEN_TIMEOUT, reqPair.first);
            return false;
        }
    }
    return true;
}

bool RequestMonitor::InferTimeOut(std::shared_ptr<ReqAgent> reqInfo) const
{
    if (!reqInfo) {
        LOG_E("[RequestMonitor] Invalid null reqInfo in InferTimeOut.");
        return false;
    }

    if (Configure::Singleton()->exceptionConfig.inferTimeout == 0) {
        return false;
    }

    // 此处有bug，
    // arriveTime[0] 的含义是 尚未来临的请求，尚未arrive的请求，不应该算超时
    // 对应异常现象，一启动上来就回复超时应答
    auto &arriveTime = reqInfo->GetStateTime(ReqState::ARRIVE);
    auto timeNs = GetTimeNow() - arriveTime[0];
    auto timeoutFlag = (timeNs > (Configure::Singleton()->exceptionConfig.inferTimeout * SECOND));
    if (timeoutFlag) {
        // 若判断超时，则将其状态设置为超时，避免被重复检测超时
        reqManage->UpdateState(reqInfo->GetReqId(), ReqState::TIMEOUT);
    }
 
    return timeoutFlag;
}

bool RequestMonitor::FirstTokenTimeOut(std::shared_ptr<ReqAgent> reqInfo) const
{
    if (!reqInfo) {
        LOG_E("[RequestMonitor] Invalid null reqInfo in FirstTokenTimeOut.");
        return false;
    }

    if (Configure::Singleton()->exceptionConfig.firstTokenTimeout == 0) {
        return false;
    }
    auto arriveTime = reqInfo->GetStateTime(ReqState::ARRIVE);

    // 此处有bug，
    // arriveTime[0] 的含义是 尚未来临的请求，尚未arrive的请求，不应该算超时
    auto timeNs = GetTimeNow() - arriveTime[0];
    auto timeoutFlag = (timeNs > (Configure::Singleton()->exceptionConfig.firstTokenTimeout * SECOND));
    if (timeoutFlag) {
        // 若判断超时，则将其状态设置为超时，避免被重复检测超时
        reqManage->UpdateState(reqInfo->GetReqId(), ReqState::TIMEOUT);
    }

    return timeoutFlag;
}

bool RequestMonitor::ScheduleTimeOut(std::shared_ptr<ReqAgent> reqInfo) const
{
    if (!reqInfo) {
        LOG_E("[RequestMonitor] Invalid null reqInfo in ScheduleTimeOut.");
        return false;
    }
    
    if (Configure::Singleton()->exceptionConfig.scheduleTimeout == 0) {
        return false;
    }

    auto arriveTime = reqInfo->GetStateTime(ReqState::ARRIVE);

    // 此处有bug，
    // arriveTime[0] 的含义是 尚未来临的请求，尚未arrive的请求，不应该算超时
    auto timeNs = GetTimeNow() - arriveTime[0];
    auto timeoutFlag = timeNs > (Configure::Singleton()->exceptionConfig.scheduleTimeout * SECOND);
    if (timeoutFlag) {
        // 若判断超时，则将其状态设置为超时，避免被重复检测超时
        reqManage->UpdateState(reqInfo->GetReqId(), ReqState::TIMEOUT);
    }

    return timeoutFlag;
}

bool RequestMonitor::TokenizerTimeout(std::shared_ptr<ReqAgent> reqInfo) const
{
    if (!reqInfo) {
        LOG_E("[RequestMonitor] Invalid null reqInfo in TokenizerTimeout.");
        return false;
    }

    if (Configure::Singleton()->exceptionConfig.tokenizerTimeout == 0) {
        return false;
    }
    auto arriveTime = reqInfo->GetStateTime(ReqState::ARRIVE);
    auto timeNs = GetTimeNow() - arriveTime[0];
    auto timeoutFlag = timeNs > (Configure::Singleton()->exceptionConfig.tokenizerTimeout * SECOND);
    if (timeoutFlag) {
        // 若判断超时，则将其状态设置为超时，避免被重复检测超时
        reqManage->UpdateState(reqInfo->GetReqId(), ReqState::TIMEOUT);
    }

    return timeoutFlag;
}

uint64_t RequestMonitor::GetTimeNow() const
{
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}