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
#include "RequestMgr.h"
#include <stdexcept>
#include <iostream>
#include <map>
#include <string>
#include "Logger.h"
#include "Configure.h"
#include "AlarmRequestHandler.h"

using namespace MINDIE::MS;
static ReqManage* g_reqManageInstance = nullptr;
constexpr uint64_t UNKNOWN_ID = std::numeric_limits<uint64_t>::max();
ReqManage::ReqManage(std::unique_ptr<MINDIE::MS::DIGSScheduler>& schedulerInit, std::unique_ptr<PerfMonitor> &perf,
    std::unique_ptr<ClusterNodes>& instancesRecordInit) : scheduler(schedulerInit), perfMonitor(perf),
    instancesRecord(instancesRecordInit), dRelease(0), count(0), recvs(0), pRecvs(0), pSends(0), pRelease(0),
    dRecvs(0)
{
    g_reqManageInstance = this;
    LOG_I("ReqManage instance created and set to global");
}

ReqManage& ReqManage::GetInstance()
{
    if (g_reqManageInstance == nullptr) {
        throw std::runtime_error("ReqManage instance not created yet");
    }
    return *g_reqManageInstance;
}

size_t ReqManage::GetReqNum()
{
    std::shared_lock<std::shared_mutex> lock(reqIdMutex);
    return reqIdMap.size();
}

int32_t ReqManage::GetReqArriveNum()
{
    int recvInThisPeriod = recvs - preRecvs;
    preRecvs = recvs;
    return recvInThisPeriod;
}

void ReqManage::SetReqArriveNum()
{
    recvs += 1;
}

bool ReqManage::AddReq(boost::beast::string_view reqId, ReqInferType type, std::shared_ptr<ServerConnection> connection,
    boost::beast::http::request<boost::beast::http::dynamic_body> &req)
{
    LOG_D("[ReqManage] Adding Reqest. Request ID is %s, server conenction is: %u",
        reqId.data(), connection->GetConnectionId());
    std::unique_lock<std::shared_mutex> lock(reqIdMutex);
    auto iter = reqIdMap.find(reqId);
    if (iter != reqIdMap.end()) {
        LOG_E("[%s] [ReqManage] Add request ID %s failed, request ID already exists.",
            GetErrorCode(ErrorType::OPERATION_REPEAT, CoordinatorFeature::REQMANAGE).c_str(), reqId.data());
        return false;
    }
    try {
        auto reqInfo = std::make_shared<ReqAgent>(reqId, type, connection, req);
        reqIdMap.emplace(reqId, reqInfo);
        numAllRequests++;
    } catch (const std::exception& e) {
        LOG_E("[%s] [ReqManage] Add request failed, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQMANAGE).c_str(), e.what());
        return false;
    }
    connection->SetReqId(reqId);
    return true;
}

std::shared_ptr<ReqAgent> ReqManage::GetReqInfo(boost::beast::string_view reqId)
{
    std::shared_lock<std::shared_mutex> lock(reqIdMutex);
    auto iter = reqIdMap.find(reqId);
    if (iter != reqIdMap.end()) {
        return iter->second;
    }
    return nullptr;
}

void ReqManage::ReleaseFinishedRequest()
{
    if (GetReqNum() == 0) {
        return;
    }
    std::unique_lock<std::shared_mutex> lock(reqIdMutex);
    for (auto iter = reqIdMap.begin(); iter != reqIdMap.end();) {
        if (iter->second->HasState(ReqState::FINISH) || iter->second->HasState(ReqState::EXCEPTION)) {
            auto clientConn = iter->second->GetClientConn();
            if (clientConn != nullptr) {
                clientConn->GraceClose();
            }
            iter->second->ClearLargeMembers();
            LOG_I("[ReqManage] Release finished request. Remove request id %s.", iter->first.c_str());
            iter = reqIdMap.erase(iter);
        } else {
            iter++;
        }
    }
    lock.unlock(); // 防止死锁
    LOG_I("Number of remaining requests is %lu.", GetReqNum()); // 剩余正在处理的请求数
    CheckAndHandleReqCongestionAlarm();
}

std::vector<std::string> ReqManage::GetReqId(std::shared_ptr<ServerConnection> connection)
{
    std::vector<std::string> ret;
    auto p = connection.get();
    std::shared_lock<std::shared_mutex> lock(reqIdMutex);
    for (auto &it : std::as_const(reqIdMap)) {
        auto tmp = it.second->GetConnection().get();
        if (p == tmp) {
            ret.emplace_back(it.first);
        }
    }
    return ret;
}

void ReqManage::SetRoute(boost::beast::string_view reqId, const std::array<uint64_t, MEMBER_NUM> &route)
{
    LOG_D("[ReqManage] Setting route for request ID %s.", reqId.data());
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return;
    }
    auto pId = route[0];
    auto dId = route[1];
    {
        std::unique_lock<std::shared_mutex> lock(insIdToReqMapMutex);
        insIdToReqMap[pId].insert(reqInfo->GetReqId());
        insIdToReqMap[dId].insert(reqInfo->GetReqId());
    }
    reqInfo->SetRoute(route);
}

void ReqManage::SetRouteIp(boost::beast::string_view reqId, const std::array<std::string, IP_INFO_MEMBER_NUM> &routeIp)
{
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return;
    }
    reqInfo->SetRouteIp(routeIp);
}

void ReqManage::SetModelName(boost::beast::string_view reqId, std::string modelName)
{
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return;
    }
    reqInfo->SetModelName(modelName);
}

std::string ReqManage::GetModelName(boost::beast::string_view reqId)
{
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return "";
    }
    return reqInfo->GetModelName();
}

const std::unordered_set<std::string> ReqManage::GetInsRequests(uint64_t insId)
{
    std::shared_lock<std::shared_mutex> lock(insIdToReqMapMutex);
    const std::unordered_set<std::string> emptySet;
    auto it = insIdToReqMap.find(insId);
    if (it != insIdToReqMap.end()) {
        return it->second;
    }
    return emptySet;
}

std::array<uint64_t, MEMBER_NUM> ReqManage::GetRoute(boost::beast::string_view reqId)
{
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return {UNKNOWN_ID, UNKNOWN_ID};
    }
    auto reqRoute = reqInfo->GetRoute();
    if (reqRoute.empty()) {
        throw std::runtime_error("[ReqManage] Route is empty.");
    }
    return reqRoute;
}

bool ReqManage::ArePDInstancesValid(boost::beast::string_view reqId, bool skipDecode)
{
    auto route = GetRoute(reqId);
    // 检查Prefill实例是否存在
    if (route[0] == UNKNOWN_ID || !instancesRecord->HasInstance(route[0])) {
        LOG_W("[ReqManage] Prefill instance %lu not found for request %s",
              route[0], reqId.data());
        return false;
    }
    // 检查Decode实例是否存在
    if (!skipDecode && (route[1] == UNKNOWN_ID || !instancesRecord->HasInstance(route[1]))) {
        LOG_W("[ReqManage] Decode instance %lu not found for request %s",
              route[1], reqId.data());
        return false;
    }
    return true;
}

std::array<std::string, IP_INFO_MEMBER_NUM> ReqManage::GetRouteIp(boost::beast::string_view reqId)
{
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return {};
    }
    return reqInfo->GetRouteIp();
}

void ReqManage::AddOutputNum(boost::beast::string_view reqId, size_t num)
{
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return;
    }
    reqInfo->AddOutputNum(num);
}

size_t ReqManage::GetOutputNum(boost::beast::string_view reqId)
{
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return 0;
    }
    return reqInfo->GetOutputNum();
}

void ReqManage::UpdateState(boost::beast::string_view reqId, ReqState state)
{
    LOG_D("[ReqManage] Updating state for request ID : %s to state %d.", reqId.data(), state);
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return;
    }
    auto time = GetTimeNow();
    switch (state) {
        case ReqState::ARRIVE:
            ReqArrive(reqId);
            break;
        case ReqState::SCHEDULED:
            ReqScheduled(reqId);
            break;
        case ReqState::REPEATED:
            ReqRepeated(reqId);
            break;
        case ReqState::FIRST_TOKEN_FINISH:
            ReqFinishFirstToken(reqId, time);
            break;
        case ReqState::FINISH:
        case ReqState::EXCEPTION:
            ClearReq(reqId, state, time);
            break;
            // case ReqState::TIMEOUT: // timeout状态，并不是终点
        case ReqState::RETRY:
            ReqRetry(reqId, time);
            break;
        default:
            break;
    }
    reqInfo->UpdateState(state, time);
}

std::map<std::string, uint64_t> ReqManage::StatRequest() const
{
    std::map<std::string, uint64_t> stats = {
        {"numAllRequests", numAllRequests.load()},
        {"numFailRequests", numFailRequests.load()},
        {"numSuccessRequests", numSuccessRequests.load()}
    };
    return stats;
}

const std::vector<uint64_t> ReqManage::GetStateTime(boost::beast::string_view reqId, ReqState state)
{
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return {0};
    }
    return reqInfo->GetStateTime(state);
}

std::map<std::string, std::shared_ptr<ReqAgent>> ReqManage::GetAllReqs()
{
    std::shared_lock<std::shared_mutex> lock(reqIdMutex);
    return reqIdMap;
}

bool ReqManage::HasState(boost::beast::string_view reqId, ReqState state)
{
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return false;
    }
    return reqInfo->HasState(state);
}

uint64_t ReqManage::GetTimeNow() const
{
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

void ReqManage::ReqArrive(boost::beast::string_view reqId)
{
    LOG_I("[ReqManage] Request arrived, reqest ID is %s.", reqId.data());
    count++;
    LOG_D("Number of total request arrived is %lu.", count.load());
}

void ReqManage::ReqScheduled(boost::beast::string_view reqId) const
{
    LOG_I("[ReqManage] Request scheduled, reqest ID is %s.", reqId.data());
}

void ReqManage::ReqRepeated(boost::beast::string_view reqId)
{
    LOG_I("[ReqManage] Request repeated, reqest ID is %s.", reqId.data());
    pSends++;
    LOG_D("Number of total request repeated is %lu.", pSends.load());
    // 请求转发成功，对应的转发的实例任务数+1
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return; // 请求已经不存在了
    }
    auto route = reqInfo->GetRoute();
    auto pId = route[0];
    instancesRecord->AddTask(pId, reqId);
}

void ReqManage::ReqFinishFirstToken(boost::beast::string_view reqId, uint64_t time)
{
    std::string deployMode = Configure::Singleton()->schedulerConfig["deploy_mode"];  // 部署模式
    if (deployMode != "pd_separate" && deployMode != "pd_disaggregation" &&
        deployMode != "pd_disaggregation_single_container") {
        return;
    }
    LOG_I("[ReqManage] First prefill finished for request ID %s.", reqId.data());
    pRecvs++;
    LOG_D("Total number of requests received for prefill is %lu.", pRecvs.load());
    pRelease++;
    LOG_D("Scheduler update request for prefill. Times : %lu, request: %s.", pRelease.load(), reqId.data());
    scheduler->UpdateReq(reqId, MINDIE::MS::DIGSReqStage::PREFILL, time, 0);
    auto reqInfo = GetReqInfo(reqId);
    if (reqInfo == nullptr) {
        return;  // 请求已经不存在了
    }
    auto route = reqInfo->GetRoute();
    auto pId = route[0];
    auto dId = route[1];
    auto prefillIp = instancesRecord->GetIp(pId);
    auto decodeIp = instancesRecord->GetIp(dId);
    auto prefillPort = instancesRecord->GetPort(pId);
    auto decodePort = instancesRecord->GetPort(dId);
    instancesRecord->DecreaseTask(pId, reqId);  // P任务完成，任务数-1
    instancesRecord->AddTask(dId, reqId);       // D任务数+1
    {
        std::unique_lock<std::shared_mutex> lock(insIdToReqMapMutex);
        insIdToReqMap[pId].erase(reqInfo->GetReqId());
    }
    auto clientConn = reqInfo->GetClientConn();
    // PD分离部署下P和D实例IP相同时，认为是Flex转发自身场景，此时需要等待所有token返回后才能关链接，这里不可以关
    if (clientConn != nullptr && !(prefillIp == decodeIp && prefillPort == decodePort)) {
        clientConn->SetAvailable(true);
        reqInfo->SetClientConn(nullptr);
    }
}

void ReqManage::ReqFinish(std::shared_ptr<ReqAgent> reqInfo)
{
    dRecvs++;
    LOG_D("Total number of request finished is %lu.", dRecvs.load());
    if (reqInfo->GetType() == ReqInferType::TOKENIZER) {
        return;
    }
    perfMonitor->PushReq(reqInfo);
    std::string deployMode = Configure::Singleton()->schedulerConfig["deploy_mode"];
    auto route = reqInfo->GetRoute();
    auto prefillIp = instancesRecord->GetIp(route[0]);
    auto decodeIp = instancesRecord->GetIp(route[1]);
    auto prefillPort = instancesRecord->GetPort(route[0]);
    auto decodePort = instancesRecord->GetPort(route[1]);
    // PD分离部署下P和D的IP+port相同时，认为是Flex转发自身场景，此处请求已全部处理完，需要关闭链接
    if (deployMode == "single_node" ||
        (deployMode == "pd_separate" && (prefillIp == decodeIp && prefillPort == decodePort))) {
        auto clientConn = reqInfo->GetClientConn();
        if (clientConn != nullptr) {
            clientConn->SetAvailable(true);
            reqInfo->SetClientConn(nullptr);
        }
    }
}

void ReqManage::ClearReq(boost::beast::string_view reqId, ReqState state, uint64_t time)
{
    auto reqInfo = GetReqInfo(reqId);  // 尽量减少find的次数，把find的动作提到前面
    if (reqInfo == nullptr) {
        // 防止没有释放资源, Finish的时候，必须确保调度器释放资源
        LOG_D("[RequestMgr] Request %s not found in ClearReq, skipping.",
            std::string(reqId).c_str());
        scheduler->UpdateReq(reqId, MINDIE::MS::DIGSReqStage::DECODE, 0, 0);
        return;  // 请求已经不存在了
    }
    if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION)) {
        return;
    }
    dRelease++;
    LOG_D("Scheduler update requet for DECODE. Times is %lu, request ID is %s.", dRelease.load(), reqId.data());
    scheduler->UpdateReq(reqId, MINDIE::MS::DIGSReqStage::DECODE, 0, time, reqInfo->GetOutputNum());
    if (state == ReqState::FINISH) {
        LOG_I("[ReqManage] Request finished, request id is %s.", reqId.data());
        ReqFinish(reqInfo);
        numSuccessRequests++;
    } else if (state == ReqState::EXCEPTION) {
        LOG_I("[ReqManage] Request end with exception, request id is %s, state is %d.", reqId.data(), state);
        numFailRequests++;
    } else {
        numFailRequests++;
    }
    if (reqInfo->HasState(ReqState::REPEATED)) {  // 请求已经转发出去了
        auto route = reqInfo->GetRoute();
        auto pId = route[0];
        auto dId = route[1];
        auto prefillIp = instancesRecord->GetIp(pId);
        auto decodeIp = instancesRecord->GetIp(dId);
        auto prefillPort = instancesRecord->GetPort(pId);
        auto decodePort = instancesRecord->GetPort(dId);
        std::string deployMode = Configure::Singleton()->schedulerConfig["deploy_mode"];  // 部署模式
        if ((deployMode == "pd_separate" || deployMode == "pd_disaggregation" ||
             deployMode == "pd_disaggregation_single_container") &&
            !(prefillIp == decodeIp && prefillPort == decodePort)) {
            if (reqInfo->HasState(ReqState::FIRST_TOKEN_FINISH)) {
                instancesRecord->DecreaseTask(dId, reqId);  // 已经在ReqFinishFirstToken减掉P了，这里只需要减D的任务数
            } else {
                // 首token还没收到，属于异常场景，已经取消推理了，PD的任务数都要减
                instancesRecord->DecreaseTask(pId, reqId);
                instancesRecord->DecreaseTask(dId, reqId);
            }
        } else {
            instancesRecord->DecreaseTask(pId, reqId);  // 单机场景，复用的pId，任务-1
        }
        {
            std::unique_lock<std::shared_mutex> lock(insIdToReqMapMutex);
            insIdToReqMap[dId].erase(reqInfo->GetReqId());
        }
    }
}

void ReqManage::ReqRetry(boost::beast::string_view reqId, uint64_t time)
{
    auto reqInfo = GetReqInfo(reqId); // 尽量减少find的次数，把find的动作提到前面
    if (reqInfo == nullptr) {
        return; // 请求已经不存在了
    }
    LOG_I("[ReqManage] Request retrying, request id is %s.", reqId.data());
    // 重计算触发的时候，需要先让调度器取消当前请求，才能重新添加调度
    ClearReq(reqId, ReqState::RETRY, time);
    reqInfo->ClearOutputNum(); // 清理掉已经计算好的输出长度
}

void ReqManage::CheckAndHandleReqCongestionAlarm()
{
    // Get request congestion trigger and clear threshold
    size_t maxResNum = Configure::Singleton()->reqLimit.maxReqs;
    const double reqCongestionTriggerRatio = DEFAULT_REQ_CONGESTION_TRIGGER_RATIO;
    const double reqCongestionClearRatio = DEFAULT_REQ_CONGESTION_CLEAR_RATIO;
    const size_t reqCongestionTriggerThreshold = maxResNum * reqCongestionTriggerRatio;
    const size_t reqCongestionClearThreshold = maxResNum * reqCongestionClearRatio;

    size_t curResNum = GetReqNum();
    std::string additionalInformation;

    if (curResNum >= reqCongestionTriggerThreshold && !inReqCongestionAlarmState.load(std::memory_order_relaxed)) {
        // Alarm trigger
        additionalInformation = "当前系统中推理请求数量为" + std::to_string(curResNum) +
            ", 已经大于等于配置的最大请求数" + std::to_string(maxResNum) + "(用户可配置) * 85%";
        inReqCongestionAlarmState.store(true, std::memory_order_release);
    } else if (curResNum < reqCongestionClearThreshold && inReqCongestionAlarmState.load(std::memory_order_relaxed)) {
        // Alarm clear
        additionalInformation = "当前系统中推理请求数量为" + std::to_string(curResNum) +
            ", 已经小于配置的最大请求数" + std::to_string(maxResNum) + "(用户可配置) * 75%";
        inReqCongestionAlarmState.store(false, std::memory_order_release);
    } else {
        return;
    }

    std::string alarmMsg = AlarmRequestHandler::GetInstance()->FillCoordinatorReqCongestionAlarmInfo(
        RequestCongestionReason::DEALING_WITH_CONGESTION, additionalInformation);
    if (AlarmRequestHandler::GetInstance()->SendAlarmToAlarmManager(alarmMsg) != 0) {
        LOG_E("[%s] [RequestListener] Send congestion alarm failed.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::REQUEST_LISTENER).c_str());
    }
}

size_t ReqManage::GetReqCount()
{
    return count.load();
}