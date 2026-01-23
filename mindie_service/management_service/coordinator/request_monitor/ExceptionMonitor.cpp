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
#include "ExceptionMonitor.h"
#include "Logger.h"

using namespace MINDIE::MS;

ExceptionMonitor::ExceptionMonitor() : running(true) {}

ExceptionMonitor::~ExceptionMonitor()
{
    Stop();
}

void ExceptionMonitor::Start()
{
    t = std::thread([this] {
        while (running) {
            OnWork();
        }
    });
}

void ExceptionMonitor::Stop()
{
    {
        std::unique_lock<std::mutex> lock(mtx);
        running = false;
    }
    cv.notify_all();
    if (t.joinable()) {
        t.join();
    }
}

void ExceptionMonitor::RegInsExceptionFun(InsExceptionType exceptionType, InsExceptionFun fun)
{
    insExceptionHandlers.Emplace(exceptionType, fun);
}

void ExceptionMonitor::RegReqExceptionFun(ReqExceptionType exceptionType, ReqExceptionFun fun)
{
    reqExceptionHandlers.Emplace(exceptionType, fun);
}

void ExceptionMonitor::RegUserExceptionFun(UserExceptionType exceptionType, UserExceptionFun fun)
{
    userExceptionHandlers.Emplace(exceptionType, fun);
}

void ExceptionMonitor::PushInsException(InsExceptionType exceptionType, uint64_t insId)
{
    LOG_I("[ExceptionMonitor] Push instance exception type:%d, instance Id %lu.", exceptionType, insId);
    insExceptionQueue.PushBack(std::make_pair(exceptionType, insId));
    cv.notify_one();
}

void ExceptionMonitor::PushReqException(ReqExceptionType exceptionType, const std::string &reqId)
{
    LOG_I("[ExceptionMonitor] Push req exception type:%d, request ID %s.", exceptionType, reqId.data());
    reqExceptionQueue.PushBack(std::make_pair(exceptionType, reqId));
    cv.notify_one();
}

void ExceptionMonitor::PushUserException(UserExceptionType exceptionType, std::shared_ptr<ServerConnection> connection)
{
    LOG_I("[ExceptionMonitor] Push user exception type:%d, connection %p.", exceptionType, connection.get());
    userExceptionQueue.PushBack(std::make_pair(exceptionType, connection));
    cv.notify_one();
}

void ExceptionMonitor::ExecuteAbnormalEvents()
{
    if (!reqExceptionQueue.Empty()) {
        auto &message = reqExceptionQueue.Front();
        auto exceptionType = message.first;
        auto reqId = message.second;
        reqExceptionQueue.PopFront();
        auto fun = reqExceptionHandlers.Get(exceptionType);
        if (fun != nullptr) {
            LOG_I("[ExceptionMonitor] Execute abnormal events for request, abnormal type is %d, ID is %s.",
                exceptionType, reqId.c_str());
            fun(reqId);
        }
    }
    LOG_D("[ExceptionMonitor] After processing request exceptions, remaining: %zu", reqExceptionQueue.Size());

    if (!insExceptionQueue.Empty()) {
        auto &message = insExceptionQueue.Front();
        auto exceptionType = message.first;
        auto insId = message.second;
        insExceptionQueue.PopFront();
        auto fun = insExceptionHandlers.Get(exceptionType);
        if (fun != nullptr) {
            LOG_I("[ExceptionMonitor] Execute abnormal events for instance, abnormal type is %d, instance ID is %lu",
                exceptionType, insId);
            fun(insId);
        }
    }
    LOG_D("[ExceptionMonitor] After processing instance exceptions, remaining: %zu", insExceptionQueue.Size());

    if (!userExceptionQueue.Empty()) {
        auto &message = userExceptionQueue.Front();
        auto exceptionType = message.first;
        auto conn = message.second;
        userExceptionQueue.PopFront();
        auto fun = userExceptionHandlers.Get(exceptionType);
        if (fun != nullptr) {
            LOG_I("[ExceptionMonitor] Execute abnormal events for user, abnormal type is %d, Connection ID is %u.",
                exceptionType, conn->GetConnectionId());
            fun(conn);
        }
    }
    LOG_D("[ExceptionMonitor] After processing user exceptions, remaining: %zu", userExceptionQueue.Size());
}

void ExceptionMonitor::OnWork()
{
    {
        std::unique_lock<std::mutex> lock(mtx);
        cv.wait(lock, [this] {
            return !running || !insExceptionQueue.Empty() || !reqExceptionQueue.Empty() || !userExceptionQueue.Empty();
        });
    }
 
    if (!running) {
        LOG_D("[ExceptionMonitor] ExceptionMonitor is not running, returning from OnWork.");
        return;
    }

    while (running) {
        if (reqExceptionQueue.Empty() && insExceptionQueue.Empty() && userExceptionQueue.Empty()) {
            LOG_D("[ExceptionMonitor] All exception queues are empty, returning from OnWork.");
            return;
        } else {
            LOG_D("[ExceptionMonitor] Processing abnormal events - req:%zu, ins:%zu, user:%zu",
                  reqExceptionQueue.Size(), insExceptionQueue.Size(), userExceptionQueue.Size());
            ExecuteAbnormalEvents();
        }
        // 是否需要加sleep
    }
}