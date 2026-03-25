/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#include <algorithm>
#include <chrono>
#include <string>
#include <thread>
#include <future>
#include <atomic>
#include <set>
#include "Logger.h"
#include "Util.h"
#include "file_lock_guard.h"
#include "ControllerConfig.h"
#include "node_manager_sender/NodeManagerRequestSender.h"
#include "ControllerLeaderAgent.h"
#include "AlarmManager.h"
#include "AlarmRequestHandler.h"
#include "AlarmManager.h"
#include "AlarmRequestHandler.h"
#include "NodeScheduler.h"
#include "NPURecoveryManager.h"

namespace MINDIE {
namespace MS {

constexpr int NPU_STATUS_POLL_INTERVAL_SECONDS = 1;
constexpr int NPU_STATUS_POLL_TIMEOUT_SECONDS = 60;
constexpr int NPU_RECOVERY_TIMER_SECONDS = 52;
constexpr int64_t FAULT_TIMESTAMP_THRESHOLD_MILLISECONDS = 5000;
constexpr int STALE_FAULT_THRESHOLD_MILLISECONDS = 52000;
constexpr uint64_t INVALID_ID = UINT64_MAX;
constexpr int PREFILL_ISOLATION_TIMEOUT_SECONDS = 52; // PREFILL实例隔离恢复时间
// 虚推失败故障码(health_checker上报的071106)，与Python侧heartbeat_mng保持一致
constexpr const char* SIMULATE_FAILED_ERROR_CODE_PREFIX = "[MIE04E071106] ";

int32_t NPURecoveryManager::Init(std::shared_ptr<NodeStatus> nodeStatus,
                                 std::shared_ptr<NodeScheduler> nodeScheduler)
{
    mNodeStatus = nodeStatus;
    mNodeScheduler = nodeScheduler;
    mGlobalNPUPollTimer = std::make_shared<InstanceRecoveryTimer>();

    mNodeManagerClient = std::make_shared<HttpClient>();
    if (mNodeManagerClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems())) {
        LOG_E("[NPURecoveryManager] Initialize NodeManager client failed!");
        return -1;
    }
    // 初始化NodeManagerSender
    mNodeManagerSender = std::make_shared<NodeManagerRequestSender>();
    mNodeManagerSender->Init(mNodeStatus);
    
    LOG_I("[NPURecoveryManager] NPU Recovery Manager initialized");
    return 0;
}

bool NPURecoveryManager::HasCriticalFaultLevel(const fault::NodeFaultInfo& nodeInfo)
{
    if (!IsInstanceRecoveryInProgress()) {
        return true;
    }
    if (nodeInfo.faultdevice().empty()) {
        LOG_W("[NPURecoveryManager] The fault device is empty, the hardware is recoverd directly.");
        return true;
    }
    for (const auto& device : nodeInfo.faultdevice()) {
        if (device.faultlevel() != "UnHealthy") {
            continue;
        }
        // 获取故障码和故障级别列表（一一对应）
        const auto& faultCodes = device.faultcodes();
        const auto& faultLevels = device.faultlevels();
        if (faultCodes.size() != faultLevels.size() || faultCodes.empty() || faultLevels.empty()) {
            LOG_W("[NPURecoveryManager] The fault code reported by ClusterD is incorrect or empty,"
                  "The hardware is recoverd directly.");
            return true;
        }
        for (int i = 0; i < faultCodes.size(); i++) {
            if (faultLevels[i] == "NotHandleFault") {
                continue;
            }
            // 对于非L1级别的故障码，检查是否在白名单中
            // 如果不在白名单中，说明存在关键故障
            if (mFaultRecoveringCodeWhitelist.find(faultCodes[i]) == mFaultRecoveringCodeWhitelist.end()) {
                return true;
            }
        }
    }
    return false;
}

/*************************以下是OOM故障快恢相关方法*******************************/
// LLMEngine alarm处理主入口
void NPURecoveryManager::ProcessLLMEngineAlarm(const nlohmann::json& alarmJson)
{
    // 只有coordinator is ready后, 才对LLMEngine alarm中携带的可恢复故障码进行处理
    if (!IsFirstCoordinatorReady()) {
        return;
    }

    // AlarmListener中已对alarmInfoJson的格式进行了校验
    auto errorInfo = alarmJson["alarm_info"];
    std::string nodeManagerIP = alarmJson["node_manager_ip"].get<std::string>();

    // 根据Pod IP获取实例Id
    uint64_t instanceId = GetInstanceIdByPodIP(nodeManagerIP);
    if (instanceId == INVALID_ID) {
        LOG_I("[NPURecoveryManager] Cannot find instance including Pod IP: %s", nodeManagerIP.c_str());
        return;
    }

    // 如果errorInfo包含无法进行恢复的故障码, 不处理, nodeManager会进行实例重配置
    if (IsNonRecoverableErrCodeExists(errorInfo)) {
        LOG_I("[NPURecoveryManager] Some errors in instance %lu are not recoverable, skip handling.", instanceId);
        for (const auto& errCode : GetNonRecoverableErrCodes(errorInfo)) {
            LLMEngineFaultReason faultReason;
            if (errCode.find(SIMULATE_FAILED_ERROR_CODE_PREFIX) != std::string::npos) {
                faultReason = LLMEngineFaultReason::SIMULATE_FAILED;
                LOG_I("[NPURecoveryManager] Simulate failed error code %s in instance %lu, "
                    "fault reason: simulate failed", errCode.c_str(), instanceId);
            } else {
                faultReason = LLMEngineFaultReason::NON_RECOVERABLE;
                LOG_I("[NPURecoveryManager] Non-recoverable error code %s in instance %lu, "
                    "fault reason: for unknown reason", errCode.c_str(), instanceId);
            }
            ReportLLMEngineAlarm(GetErrorLocationStr(errorInfo, errCode), faultReason);
        }
        return;
    }

    // 执行故障码对应的快恢流程
    // 故障码为全量上报, 按vector顺序决定优先级(靠前优先), 只执行第一个匹配的快恢流程函数
    for (const auto& processor : mErrCodeProcessors) {
        if (IsErrCodeExists(errorInfo, processor.errCode)) {
            if (!ControllerConfig::GetInstance()->GetFaultRecoveryEnableByConfigKey(processor.recoveryFuncKey)) {
                LOG_I("[NPURecoveryManager] Find error code %s in instance %lu, "
                    "but do not enable %s recovery, just send STOP_ENGINE.",
                    processor.errCode.c_str(), instanceId, processor.recoveryFuncKey.c_str());
                    (void)SendNodeManagerCommandToPodsParallel(GetAllPodIPsInInstance(instanceId),
                        instanceId, NodeManagerCmd::STOP_ENGINE);
                return;
            }
            
            LOG_I("[NPURecoveryManager] Start %s recovery handler for error code %s in instance %lu",
                processor.recoveryFuncKey.c_str(), processor.errCode.c_str(), instanceId);

            // 根据 processor.faultReason上报故障告警
            if (mErrCodeAlarmExisted.Insert(instanceId)) {
                ReportLLMEngineAlarm(GetErrorLocationStr(errorInfo, processor.errCode), processor.faultReason);
            }
            processor.handler(instanceId);
            return;
        }
    }

    LOG_D("[NPURecoveryManager] No recoverable error codes found, skip processing LLMEngine alarm.");
}

bool NPURecoveryManager::IsNonRecoverableErrCodeExists(const nlohmann::json& errorInfo)
{
    return !GetNonRecoverableErrCodes(errorInfo).empty();
}

std::set<std::string> NPURecoveryManager::GetNonRecoverableErrCodes(const nlohmann::json& errorInfo)
{
    std::set<std::string> nonRecoverableErrCodes;
    for (const auto& epErrors : errorInfo) {
        for (const auto& entry : epErrors) {
            if (!entry.contains("errCode")) {
                continue;
            }
            std::string errCode = entry["errCode"];
            auto it = std::find_if(mErrCodeProcessors.begin(), mErrCodeProcessors.end(),
                [&errCode](const auto& processor) { return processor.errCode == errCode; });
            if (it == mErrCodeProcessors.end()) {
                nonRecoverableErrCodes.insert(errCode);
            }
        }
    }
    return nonRecoverableErrCodes;
}

void NPURecoveryManager::ReportLLMEngineAlarm(const std::string& errorLocationStr,
    LLMEngineFaultReason faultReason)
{
    std::string alarmsString = AlarmRequestHandler::GetInstance()->FillLLMEngineAlarmInfo(
        AlarmCategory::ALARM_CATEGORY_EVENT,
        faultReason,
        errorLocationStr);
    AlarmManager::GetInstance()->AlarmAdded(alarmsString);
    LOG_M("[NPURecoveryManager] Add LLMEngine alarm. Contents: %s", alarmsString.c_str());
}

bool NPURecoveryManager::IsErrCodeExists(const nlohmann::json& errorInfo, const std::string& errCode)
{
    for (const auto& epErrors : errorInfo) {
        for (const auto& entry : epErrors) {
            if (!entry.contains("errCode")) {
                continue;
            }
            if (entry["errCode"] == errCode) {
                return true;
            }
        }
    }
    return false;
}

std::string NPURecoveryManager::GetErrorLocationStr(const nlohmann::json& errorInfo, const std::string& errCode)
{
    std::set<std::string> errorLocations;
    for (const auto& epErrors : errorInfo) {
        for (const auto& entry : epErrors) {
            if (!entry.contains("errCode") || !entry.contains("errorLocation")) {
                continue;
            }
            if (entry["errCode"] == errCode) {
                errorLocations.insert(entry["errorLocation"].get<std::string>());
            }
        }
    }

    std::string errorLocationStr = "";
    bool isFirst = true;
    for (const auto& location : errorLocations) {
        if (!isFirst) {
            errorLocationStr += ", ";
        }
        errorLocationStr += location;
        isFirst = false;
    }
    return errorLocationStr;
}

void NPURecoveryManager::OOMRecoveryHandler(uint64_t instanceId)
{
    // 存在OOM故障码, 进入FullNPURecovery流程
    std::vector<uint64_t> instanceNodeIds = GetAllNodesInInstance(instanceId);
    std::unordered_set<std::string> instancePodIps = GetAllPodIPsInInstance(instanceId);
    std::vector<FaultNodeInfo> faultNodes = std::vector<FaultNodeInfo>(instancePodIps.size());

    LOG_I("[NPURecoveryManager] Enable full NPU recovery strategy to process OOM errors in instance %lu.", instanceId);
    
    ProcessFullNPURecovery(instanceId, faultNodes, instanceNodeIds, instancePodIps);
}

/******************以下是Roce故障恢复相关方法********************************/
void NPURecoveryManager::ProcessCQEFault(const fault::FaultMsgSignal& faultMsg)
{
    std::unordered_set<uint64_t> cqeInstanceIds = GetCQEInstanceIdsFromFaultMessage(faultMsg);
    if (cqeInstanceIds.empty()) {
        LOG_D("[NPURecoveryManager] No CQE instances found, skipping");
        return;
    }
    LOG_I("[NPURecoveryManager] Found %zu CQE instances", cqeInstanceIds.size());

    static const std::vector<FaultNodeInfo> kEmptyFaultNodes;

    for (uint64_t instanceId : cqeInstanceIds) {
        if (mInstanceRecoveryInfo.Count(instanceId) > 0) {
            LOG_I("[NPURecoveryManager] Instance %lu is already in recovery, skipping CQE", instanceId);
            continue;
        }
        std::vector<uint64_t> instanceNodeIds = GetAllNodesInInstance(instanceId);
        std::unordered_set<std::string> instancePodIPs = GetAllPodIPsInInstance(instanceId);
        if (instanceNodeIds.empty() || instancePodIPs.empty()) {
            LOG_D("[NPURecoveryManager] No nodes or pods found for instance %lu, skipping CQE", instanceId);
            continue;
        }
        // ProcessRoCERecovery 前上报 CQE/RoCE 故障事件告警
        std::string errorLocationStr = GetErrorLocationStrFromFaultMsg(faultMsg, instanceId, "4C1F8608");
        ReportLLMEngineAlarm(errorLocationStr, LLMEngineFaultReason::TEXT_GENERATOR_PD_PULL_KV_ERROR);
        LOG_I("[NPURecoveryManager] CQE instance %lu, starting RoCE recovery process", instanceId);
        auto future = std::async(std::launch::async, [this, instanceId, instanceNodeIds, instancePodIPs]() {
            ProcessRoCERecovery(instanceId, kEmptyFaultNodes, instanceNodeIds, instancePodIPs);
        });
        PruneCompletedRoCEFutures();
        mRoCERecoveryFutures.push_back(std::move(future));
    }
}

void NPURecoveryManager::PruneCompletedRoCEFutures()
{
    auto it = mRoCERecoveryFutures.begin();
    while (it != mRoCERecoveryFutures.end()) {
        if (it->wait_for(std::chrono::seconds(0)) == std::future_status::ready) {
            it = mRoCERecoveryFutures.erase(it);
            LOG_D("[NPURecoveryManager] RoCE recovery future erased.");
        } else {
            ++it;
        }
    }
}

// CQE(4C1F8608)故障检测：仅读原始故障码识别含 CQE 的实例。
std::unordered_set<uint64_t> NPURecoveryManager::GetCQEInstanceIdsFromFaultMessage(
    const fault::FaultMsgSignal& faultMsg)
{
    std::unordered_set<uint64_t> cqeInstanceIds;
    for (const auto& nodeInfo : faultMsg.nodefaultinfo()) {
        uint64_t instanceId = GetInstanceIdByNodeIP(nodeInfo.nodeip());
        if (instanceId == INVALID_ID) {
            continue;
        }
        bool foundCQE = false;
        for (const auto& device : nodeInfo.faultdevice()) {
            for (const auto& code : device.faultcodes()) {
                if (mDeviceFaultCQECodesWhitelist.count(code)) {
                    cqeInstanceIds.insert(instanceId);
                    foundCQE = true;
                    break;
                }
            }
            if (foundCQE) {
                break;
            }
        }
    }
    return cqeInstanceIds;
}

std::string NPURecoveryManager::GetErrorLocationStrFromFaultMsg(const fault::FaultMsgSignal& faultMsg,
    uint64_t instanceId, const std::string& targetErrCode)
{
    std::set<std::string> locations;
    for (const auto& nodeInfo : faultMsg.nodefaultinfo()) {
        if (GetInstanceIdByNodeIP(nodeInfo.nodeip()) != instanceId) {
            continue;
        }
        for (const auto& device : nodeInfo.faultdevice()) {
            for (const auto& code : device.faultcodes()) {
                if (code == targetErrCode) {
                    locations.insert(nodeInfo.nodeip());
                }
            }
            for (const auto& sfi : device.switchfaultinfos()) {
                if (sfi.faultcode() == targetErrCode) {
                    locations.insert(nodeInfo.nodeip());
                }
            }
        }
    }
    std::string result;
    bool isFirst = true;
    for (const auto& loc : locations) {
        if (!isFirst) {
            result += ", ";
        }
        result += loc;
        isFirst = false;
    }
    return result;
}

void NPURecoveryManager::ProcessRoCERecovery(uint64_t instanceId,
                                             const std::vector<FaultNodeInfo>& faultNodes,
                                             const std::vector<uint64_t>& instanceNodeIds,
                                             const std::unordered_set<std::string>& instancePodIPs)
{
    // 设置实例恢复信息
    if (!mInstanceRecoveryInfo.Insert(instanceId, InstanceRecoveryInfo(faultNodes, instancePodIPs))) {
        LOG_I("[NPURecoveryManager] Instance %lu is already in recovery, skipping", instanceId);
        return;
    }

    LOG_I("[NPURecoveryManager] Processing instance %lu with ROCE recovery", instanceId);

    // 将该实例的所有节点设置为不可用
    for (uint64_t nodeId : instanceNodeIds) {
        SetNodeUnavailable(nodeId);
    }

    // 向该实例中的所有 pod 发送 PAUSE_ENGINE_ROCE,若失败则发送STOP_ENGINE停止服务
    NodeManagerCmd cmd = NodeManagerCmd::PAUSE_ENGINE_ROCE;
    if (!SendNodeManagerCommandToInstancePods(instanceId, cmd)) {
        LOG_I("[NPURecoveryManager] PAUSE_ENGINE_ROCE failed for instance %lu, aborting recovery process", instanceId);
        (void)SendNodeManagerCommandToPodsParallel(instancePodIPs, instanceId, NodeManagerCmd::STOP_ENGINE);
        mInstanceRecoveryInfo.Erase(instanceId);
        LOG_I("[NPURecoveryManager] Removed instance %lu from recovery queue (PAUSE_ENGINE_ROCE failed)", instanceId);
        return;
    }

    // ROCE网络下，先PD Unlink，然后重新link
    if (mNodeScheduler == nullptr) {
        LOG_E("[NPURecoveryManager] NodeScheduler is null, send STOP_ENGINE to instance %lu", instanceId);
        (void)SendNodeManagerCommandToPodsParallel(instancePodIPs, instanceId, NodeManagerCmd::STOP_ENGINE);
        mInstanceRecoveryInfo.Erase(instanceId);
        LOG_I("[NPURecoveryManager] Removed instance %lu from recovery queue (NodeScheduler not set)", instanceId);
        return;
    }

    // 等待 LLM 侧 PAUSE_ENGINE_ROCE 生效后再执行 unlink/link，避免过早下发 role 导致异常
    constexpr uint32_t extraWaitSeconds = 5;
    LOG_I("[NPURecoveryManager] ProcessRoCERecovery: wait %u seconds before BatchUnlinkAndLink.", extraWaitSeconds);
    std::this_thread::sleep_for(std::chrono::seconds(extraWaitSeconds));

    LOG_I("[NPURecoveryManager] PD link reestablishment for instance %lu", instanceId);
    if (!mNodeScheduler->ProcessBatchUnlinkAndLink(instanceNodeIds)) {
        LOG_E("[NPURecoveryManager] ProcessBatchUnlinkAndLink failed for node %lu, instance %lu, sending STOP_ENGINE",
            instanceNodeIds.size(), instanceId);
        (void)SendNodeManagerCommandToPodsParallel(instancePodIPs, instanceId, NodeManagerCmd::STOP_ENGINE);
        mInstanceRecoveryInfo.Erase(instanceId);
        LOG_I("[NPURecoveryManager] Removed instance %lu from recovery queue (UnlinkAndLink failed)", instanceId);
        return;
    }

    // RoCE 路径仅做 PAUSE + Unlink/Link，未对 NPU 做 REINIT，无需轮询 NPU 就绪，直接下发 START_ENGINE
    LOG_I("[NPURecoveryManager] Instance %lu in ROCE recovery, START_ENGINE sent - %zu nodes, %zu pods",
        instanceId, instanceNodeIds.size(), instancePodIPs.size());
    RecoverInstanceServiceWithPodIPs(instanceId, instancePodIPs);
    mInstanceRecoveryInfo.Erase(instanceId);
}

/******************以下是灵衢故障恢复相关方法********************************/

// 故障消息处理主入口
void NPURecoveryManager::ProcessFaultMessage(const fault::FaultMsgSignal &faultMsg)
{
    bool lingquLinkEnabled = ControllerConfig::GetInstance()->GetFaultRecoveryEnableByConfigKey("lingqu_link");
    bool roceEnabled = ControllerConfig::GetInstance()->GetFaultRecoveryEnableByConfigKey("roce");
    if (!lingquLinkEnabled && !roceEnabled) {
        LOG_I("[NPURecoveryManager] Skip processing fault message (lingqu_link and roce both disabled)");
        return;
    }
    LOG_D("[NPURecoveryManager] Entry point: Processing fault message");
    // 只有coordinator is ready后才真正开始后面的流程
    if (!IsFirstCoordinatorReady()) {
        // coordinator未ready时，记录所有白名单故障的完整ID到并发队列
        for (const auto& nodeInfo : faultMsg.nodefaultinfo()) {
            for (const auto& device : nodeInfo.faultdevice()) {
                for (const auto& sfi : device.switchfaultinfos()) {
                    if (!sfi.faultcode().empty() && IsFaultCodeInWhitelist(sfi.faultcode())) {
                        std::string faultId = sfi.faultcode() + "|" + sfi.switchchipid() + "|" +
                                            sfi.switchportid() + "|" + sfi.faulttime();
                        mCoordinatorNotReadyFaults.Insert(faultId);
                    }
                }
            }
        }
        return;
    }
    
    LOG_I("[NPURecoveryManager] Processing fault message, node count: %d", faultMsg.nodefaultinfo_size());
    // 筛选出需要进行故障恢复的实例
    bool isNeedToRestart = false;
    auto faultyInstances = FindFaultyInstances(faultMsg, isNeedToRestart);

    LOG_D("[NPURecoveryManager] Found %zu instances requiring fault recovery", faultyInstances.size());
    if (isNeedToRestart) {
        LOG_I("[NPURecoveryManager] Need to restart instance, skip fault recovery");
        // 提取所有需要重启的实例ID
        std::unordered_set<uint64_t> faultyInstanceIds;
        for (const auto& pair : faultyInstances) {
            faultyInstanceIds.insert(pair.first);
        }
        RestartInstance(faultyInstanceIds);
        return;
    }
    
    if (roceEnabled) {
        LOG_I("[NPURecoveryManager] Processing CQE fault message");
        ProcessCQEFault(faultMsg);
    }
    if (lingquLinkEnabled) {
        if (faultyInstances.empty()) {
            LOG_D("[NPURecoveryManager] No instances require lingqu fault recovery");
            return;
        }
        LOG_D("[NPURecoveryManager] Found %zu instances requiring lingqu fault recovery", faultyInstances.size());
        ProcessInstanceFaults(faultyInstances);
    }
}

std::unordered_map<uint64_t, std::vector<FaultNodeInfo>> NPURecoveryManager::FindFaultyInstances(
    const fault::FaultMsgSignal &faultMsg, bool &isNeedToRestart)
{
    std::unordered_map<uint64_t, std::vector<FaultNodeInfo>> faultyInstances;
    std::unordered_set<uint64_t> blacklistedInstances; // 记录有不健康节点的实例
    std::vector<ProcessedSwitchFaultInfo> newProcessedFaults;
    
    for (const auto& nodeInfo : faultMsg.nodefaultinfo()) {
        // 1）获取实例ID
        uint64_t instanceId = GetInstanceIdByNodeIP(nodeInfo.nodeip());
        if (instanceId == INVALID_ID) {
            LOG_I("[NPURecoveryManager] Cannot find instance for node IP: %s", nodeInfo.nodeip().c_str());
            continue;
        }
        
        // 2）检查节点是否健康 - 如果不健康，将整个实例加入blacklistedInstances
        // 2）和3）作用是同一个实例发生硬件故障或软件故障时优先级高于灵衢故障
        if (nodeInfo.faultlevel() == "UnHealthy" && HasCriticalFaultLevel(nodeInfo)) {
            LOG_D("[NPURecoveryManager] Node %s faultlevel: %s, blacklisting instance %lu",
                  nodeInfo.nodeip().c_str(), nodeInfo.faultlevel().c_str(), instanceId);
            blacklistedInstances.insert(instanceId);
            // 如果该实例已经在故障列表中，移除它
            faultyInstances.erase(instanceId);
            continue;
        }
        
        // 3）检查实例是否已被黑名单 - 如果是，跳过处理
        if (blacklistedInstances.find(instanceId) != blacklistedInstances.end()) {
            LOG_D("[NPURecoveryManager] Instance %lu is blacklisted, skipping node %s",
                  instanceId, nodeInfo.nodeip().c_str());
            continue;
        }
        
        // 4）检查是否有需要处理的switchFaultInfo
        auto [hasValidFaults, extractedFaults] = ExtractValidSwitchFaults(nodeInfo);
        
        if (hasValidFaults) {
            // 记录需要故障恢复的实例
            FaultNodeInfo faultNodeInfo = ConvertToFaultNodeInfo(nodeInfo);
            faultyInstances[instanceId].push_back(faultNodeInfo);
            
            // 收集新处理的故障信息
            newProcessedFaults.insert(newProcessedFaults.end(), extractedFaults.begin(), extractedFaults.end());
            
            LOG_I("[NPURecoveryManager] Added fault node %s to instance %lu for NPU recovery",
                  nodeInfo.nodeip().c_str(), instanceId);
        }
    }
    
    // 更新已处理的故障记录
    if (!faultyInstances.empty() && !newProcessedFaults.empty()) {
        UpdateProcessedSwitchFaults(newProcessedFaults);
    }
    
    LOG_I("[NPURecoveryManager] Final result: %zu instances qualified for NPU recovery, %zu instances blacklisted",
          faultyInstances.size(), blacklistedInstances.size());

    if (!blacklistedInstances.empty()) {
        isNeedToRestart = true;
    }
    
    return faultyInstances;
}

// 提取有效的switchFaultInfo（未处理且在白名单中且时间戳符合要求）
std::pair<bool, std::vector<ProcessedSwitchFaultInfo>> NPURecoveryManager::ExtractValidSwitchFaults(
    const fault::NodeFaultInfo& nodeInfo)
{
    std::vector<ProcessedSwitchFaultInfo> validFaults;
    bool hasValidFault = false;
    
    for (const auto& device : nodeInfo.faultdevice()) {
        for (const auto& sfi : device.switchfaultinfos()) {
            // 检查故障码是否在白名单中
            if (sfi.faultcode().empty() || !IsFaultCodeInWhitelist(sfi.faultcode())) {
                continue;
            }
            
            // 创建故障信息
            ProcessedSwitchFaultInfo switchFault;
            switchFault.faultCode = sfi.faultcode();
            switchFault.switchChipId = sfi.switchchipid();
            switchFault.switchPortId = sfi.switchportid();
            switchFault.faultTime = sfi.faulttime();
            switchFault.processTime = std::chrono::steady_clock::now();
            
            std::string uniqueId = switchFault.GetUniqueId();
            // 检查是否是coordinator未ready时的故障
            if (mCoordinatorNotReadyFaults.Contains(uniqueId)) {
                LOG_D("[NPURecoveryManager] Skip previously recorded fault during coordinator not ready: %s",
                      uniqueId.c_str());
                continue;
            }
            // 检查是否已处理过
            if (mProcessedSwitchFaults.Contains(uniqueId)) {
                continue;
            }
            
            validFaults.push_back(switchFault);
            hasValidFault = true;
        }
    }
    return {hasValidFault, validFaults};
}

int32_t NPURecoveryManager::SaveProcessedSwitchFaults(const std::vector<std::string>& processedSwitchFaults) const
{
    try {
        if (ControllerConfig::GetInstance()->GetCtrlBackUpConfig().funSw &&
            ControllerConfig::GetInstance()->IsLeader()) {
            nlohmann::json faultsData;
            faultsData["processed_switch_faults"] = nlohmann::json::array();
            for (const auto& faultId : processedSwitchFaults) {
                faultsData["processed_switch_faults"].push_back(faultId);
            }
            if (!ControllerLeaderAgent::GetInstance()->WriteFaultsValue(faultsData.dump(4))) { // 4表示缩进4个空格
                LOG_E("[NPURecoveryManager] Save processed switch faults to etcd failed");
                return -1;
            }
            LOG_I("[NPURecoveryManager] Save processed switch faults to etcd successfully, count: %zu",
                  processedSwitchFaults.size());
            return 0;
        }
        auto path = ControllerConfig::GetInstance()->GetProcessManagerConfig().filePath;
        if (!PathCheckForCreate(path)) {
            LOG_E("[NPURecoveryManager] Invalid path of node status file and faults file: %s", path.c_str());
            return -1;
        }

        // 使用 RAII 文件锁，自动管理锁的获取和释放
        std::string lockPath = path + ".lock";
        FileLockGuard lockGuard(lockPath);

        if (!lockGuard.IsLocked()) {
            LOG_E("[NPURecoveryManager] Failed to acquire file lock for %s", path.c_str());
            return -1;
        }

        // 在锁保护下执行读取-合并-写入
        return SaveFaultsWithLock(path, processedSwitchFaults);
    }  catch (const std::exception& e) {
        LOG_E("[NPURecoveryManager] Save status to file failed, error is %s.", e.what());
        return -1;
    }
}

int32_t NPURecoveryManager::SaveFaultsWithLock(const std::string& path,
                                               const std::vector<std::string>& newFaultsToAdd) const
{
    // 读取现有文件内容
    bool checkProcessFile = ControllerConfig::GetInstance()->GetCheckMountedFiles();
    uint32_t mode = checkProcessFile ? 0640 : 0777; // 校验权限是0640, 不校验是0777
    nlohmann::json existingData = FileToJsonObj(path, mode);
    // 合并故障信息
    nlohmann::json::array_t faultArray;
    // 先添加现有的故障信息
    if (existingData.contains("processed_switch_faults")) {
        faultArray = existingData["processed_switch_faults"];
    }
    // 直接添加新的故障信息（故障ID唯一，不需要检查重复）
    for (const auto& faultId : newFaultsToAdd) {
        faultArray.push_back(faultId);
    }
    existingData["processed_switch_faults"] = faultArray;
    // 写入临时文件
    std::string tmpPath = path + ".tmp";
    if (DumpStringToFile(tmpPath, existingData.dump(4)) != 0) { // 4 json格式行缩进为4
        LOG_E("[NPURecoveryManager] Failed to write temporary file");
        return -1;
    }
    // 原子替换
    if (rename(tmpPath.c_str(), path.c_str()) != 0) {
        LOG_E("[NPURecoveryManager] Rename tmp file failed");
        return -1;
    }
    LOG_I("[NPURecoveryManager] Save controller file successfully, total faults count: %zu",
          faultArray.size());
    return 0;
}
// 更新已处理的switchFaultInfo记录
void NPURecoveryManager::UpdateProcessedSwitchFaults(const std::vector<ProcessedSwitchFaultInfo>& newFaults)
{
    for (const auto& switchFault : newFaults) {
        std::string uniqueId = switchFault.GetUniqueId();
        mProcessedSwitchFaults.Insert(uniqueId);
        LOG_D("[NPURecoveryManager] Recorded processed switch fault: %s", uniqueId.c_str());
    }
    LOG_D("[NPURecoveryManager] Start saving processed information.");
    
    // 获取所有故障 ID 用于保存
    auto faultIds = mProcessedSwitchFaults.ToVector();
    SaveProcessedSwitchFaults(faultIds);
    LOG_D("[NPURecoveryManager] Updated processed switch faults, total count: %zu", faultIds.size());
}

nlohmann::json NPURecoveryManager::LoadProcessedSwitchFaults() const
{
    LOG_I("[NPURecoveryManager] Start to load process file.");
    nlohmann::json processFile;
    if (ControllerConfig::GetInstance()->GetCtrlBackUpConfig().funSw &&
        ControllerConfig::GetInstance()->IsLeader()) {
        if (!ControllerLeaderAgent::GetInstance()->ReadFaultsValue(processFile)) {
            LOG_E("[NPURecoveryManager] load faults from etcd failed");
            return {};
        }
        LOG_I("[NPURecoveryManager] load faults from etcd successfully.");
    } else {
        if (!ControllerConfig::GetInstance()->GetProcessManagerConfig().toFile) {
            LOG_W("[NPURecoveryManager] load file path failed");
            return {};
        }
        bool checkProcessFile = ControllerConfig::GetInstance()->GetCheckMountedFiles();
        uint32_t mode = checkProcessFile ? 0640 : 0777;
        processFile = FileToJsonObj(ControllerConfig::GetInstance()->GetProcessManagerConfig().filePath,
            mode);
        LOG_I("[NPURecoveryManager] load faults from local file successfully.");
    }
    try {
        nlohmann::json faultsData;
        if (processFile.contains("processed_switch_faults")) {
            faultsData["processed_switch_faults"] = processFile["processed_switch_faults"];
            LOG_I("[NPURecoveryManager] Loaded processed switch faults, count: %zu",
                  processFile["processed_switch_faults"].size());
        } else {
            LOG_W("[NPURecoveryManager] No processed switch faults found in file");
            faultsData["processed_switch_faults"] = nlohmann::json::array();
        }
        return faultsData;
    } catch (const std::exception &e) {
        LOG_E("[%s] [NPURecoveryManager] Load process faults error : %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::PROCESS_MANAGER).c_str(),
            e.what());
        return {};
    }
}

void NPURecoveryManager::SetNodeUnavailable(uint64_t nodeId)
{
    auto node = mNodeStatus->GetNode(nodeId);
    if (node != nullptr) {
        // 设置节点为不可用
        mNodeStatus->UpdateInferenceType(nodeId, InferenceType::UNAVAILABLE);
        LOG_I("[NPURecoveryManager] Node %lu set to unavailable", nodeId);
    }
}

void NPURecoveryManager::SetNodeAvailable(uint64_t nodeId)
{
    auto node = mNodeStatus->GetNode(nodeId);
    if (node != nullptr) {
        mNodeStatus->UpdateInferenceType(nodeId, InferenceType::AVAILABLE);
        LOG_I("[NPURecoveryManager] Node %lu set to available", nodeId);
    }
}

void NPURecoveryManager::RestartInstance(const std::unordered_set<uint64_t>& faultyInstanceIds)
{
    for (uint64_t instanceId : faultyInstanceIds) {
        auto instanceNodeIds = GetAllNodesInInstance(instanceId);
        if (instanceNodeIds.empty()) {
            LOG_D("[NPURecoveryManager] No nodes found for instance %lu", instanceId);
            continue;
        }
        for (uint64_t nodeId : instanceNodeIds) {
            SetNodeUnavailable(nodeId);
        }
        // RestartInstance 用于硬件/软件故障，实例不在 mInstanceRecoveryInfo 中
        // 直接获取 podIPs 并使用公共函数发送命令
        auto instancePodIPs = GetAllPodIPsInInstance(instanceId);
        if (instancePodIPs.empty()) {
            LOG_D("[NPURecoveryManager] No pod IPs found for instance %lu", instanceId);
            continue;
        }
        std::string cmdName = mNodeManagerSender->NodeManagerCmdToString(NodeManagerCmd::STOP_ENGINE);
        LOG_I("[NPURecoveryManager] Sending %s to all %zu pods in instance %lu (parallel)",
              cmdName.c_str(), instancePodIPs.size(), instanceId);
        if (!SendNodeManagerCommandToPodsParallel(instancePodIPs, instanceId, NodeManagerCmd::STOP_ENGINE)) {
            LOG_I("[NPURecoveryManager] STOP_ENGINE failed for instance %lu, maybe it is not running", instanceId);
        }
    }
}

void NPURecoveryManager::ProcessInstanceFaults(const std::unordered_map<uint64_t,
                                               std::vector<FaultNodeInfo>>& instanceFaultMap)
{
    LOG_D("[NPURecoveryManager] Processing %zu instances with faults", instanceFaultMap.size());
    
    for (const auto& [instanceId, faultNodes] : instanceFaultMap) {
        // 获取节点信息
        std::vector<uint64_t> instanceNodeIds = GetAllNodesInInstance(instanceId);
        std::unordered_set<std::string> instancePodIPs = GetAllPodIPsInInstance(instanceId);
        if (instanceNodeIds.empty() || instancePodIPs.empty()) {
            LOG_D("[NPURecoveryManager] No nodes found for instance %lu", instanceId);
            continue;
        }

        // 检查该实例是否已经在恢复中，避免重复处理
        if (mInstanceRecoveryInfo.Count(instanceId) > 0) {
            LOG_W("[NPURecoveryManager] Instance %lu is already in recovery, stop engine and abort recovery",
                  instanceId);
            (void)SendNodeManagerCommandToPodsParallel(instancePodIPs, instanceId, NodeManagerCmd::STOP_ENGINE);
            // 清理恢复信息，避免资源泄漏
            mInstanceRecoveryInfo.Erase(instanceId);
            mErrCodeAlarmExisted.Erase(instanceId);
            LOG_I("[NPURecoveryManager] Removed instance %lu from recovery queue (PAUSE_ENGINE failed)", instanceId);
            return;
        }

        // 判断实例中故障节点的角色，决定使用哪种恢复策略
        bool isPrefillInstance = CheckIfPrefillInstance(faultNodes);
        if (isPrefillInstance) {
            // 策略1: PREFILL实例 - 简单隔离策略
            LOG_I("[NPURecoveryManager] Instance %lu has PREFILL role, using isolation recovery strategy", instanceId);
            ProcessPrefillInstanceFaults(instanceId, faultNodes, instanceNodeIds);
        } else {
            // 策略2: 其他角色实例 - 完整NPU恢复流程
            LOG_I("[NPURecoveryManager] Instance %lu using full NPU recovery strategy", instanceId);
            ProcessFullNPURecovery(instanceId, faultNodes, instanceNodeIds, instancePodIPs);
        }
    }
}


// 检查故障节点是否属于PREFILL实例
bool NPURecoveryManager::CheckIfPrefillInstance(const std::vector<FaultNodeInfo>& faultNodes)
{
    if (faultNodes.empty()) {
        return false;
    }
    // 遍历所有故障节点，检查它们的currentRole
    for (const auto& faultNode : faultNodes) {
        uint64_t nodeId = FindNodeIdByIP(faultNode.nodeIP);
        if (nodeId == INVALID_ID) {
            LOG_W("[NPURecoveryManager] Cannot find node ID for IP: %s", faultNode.nodeIP.c_str());
            continue;
        }

        auto node = mNodeStatus->GetNode(nodeId);
        if (node != nullptr) {
            // 只要有一个故障节点的currentRole是PREFILL_INSTANCE，就使用PREFILL恢复策略
            if (node->currentRole == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE && node->isSingleNode) {
                LOG_D("[NPURecoveryManager] Detected PREFILL role for node %s (ID: %lu)",
                      faultNode.nodeIP.c_str(), nodeId);
                return true;
            }
        }
    }
    return false;
}

// 处理PREFILL实例故障 - 简单隔离策略
void NPURecoveryManager::ProcessPrefillInstanceFaults(uint64_t instanceId,
                                                      const std::vector<FaultNodeInfo>& faultNodes,
                                                      const std::vector<uint64_t>& instanceNodeIds)
{
    (void)instanceNodeIds; // 参数保留用于接口一致性，当前策略只隔离故障节点
    LOG_I("[NPURecoveryManager] Processing PREFILL instance %lu with isolation strategy, %zu fault nodes",
          instanceId, faultNodes.size());

    // 只设置故障节点为不可用（不是所有节点）
    for (const auto& faultNode : faultNodes) {
        uint64_t nodeId = FindNodeIdByIP(faultNode.nodeIP);
        if (nodeId != INVALID_ID) {
            SetNodeUnavailable(nodeId);
            LOG_I("[NPURecoveryManager] Set PREFILL fault node %lu (%s) to unavailable",
                  nodeId, faultNode.nodeIP.c_str());
        }
    }

    // 创建定时器，60秒后恢复节点可用状态
    auto isolationTimer = std::make_shared<InstanceRecoveryTimer>();
    int32_t ret = isolationTimer->Start(PREFILL_ISOLATION_TIMEOUT_SECONDS,
                                        [this, instanceId, faultNodes]() {
                                            this->OnPrefillIsolationTimerExpired(instanceId, faultNodes);
                                        });
    if (ret != 0) {
        LOG_E("[NPURecoveryManager] Failed to start isolation timer for PREFILL instance %lu", instanceId);
        // 定时器启动失败，立即恢复节点状态
        for (const auto& faultNode : faultNodes) {
            uint64_t nodeId = FindNodeIdByIP(faultNode.nodeIP);
            if (nodeId != INVALID_ID) {
                SetNodeAvailable(nodeId);
            }
        }
    } else {
        // 保存定时器到map中，以便后续可以取消
        mInstanceRecoveryTimers.Set(instanceId, isolationTimer);
        LOG_I("[NPURecoveryManager] Started %d seconds isolation timer for PREFILL instance %lu",
              PREFILL_ISOLATION_TIMEOUT_SECONDS, instanceId);
    }
}

// PREFILL实例隔离定时器到期回调
void NPURecoveryManager::OnPrefillIsolationTimerExpired(uint64_t instanceId,
                                                        const std::vector<FaultNodeInfo>& faultNodes)
{
    LOG_I("[NPURecoveryManager] Isolation timeout expired for PREFILL instance %lu, restoring nodes", instanceId);

    // 恢复故障节点为可用状态
    for (const auto& faultNode : faultNodes) {
        uint64_t nodeId = FindNodeIdByIP(faultNode.nodeIP);
        if (nodeId != INVALID_ID) {
            SetNodeAvailable(nodeId);
            LOG_I("[NPURecoveryManager] Restored PREFILL node %lu (%s) to available",
                  nodeId, faultNode.nodeIP.c_str());
        }
    }
    // 清理定时器
    auto timer = mInstanceRecoveryTimers.Get(instanceId);
    if (timer.has_value()) {
        mInstanceRecoveryTimers.Erase(instanceId);
    }
    LOG_I("[NPURecoveryManager] PREFILL instance %lu isolation recovery completed", instanceId);
}

// 处理完整NPU恢复流程（原有逻辑）
void NPURecoveryManager::ProcessFullNPURecovery(uint64_t instanceId,
                                                const std::vector<FaultNodeInfo>& faultNodes,
                                                const std::vector<uint64_t>& instanceNodeIds,
                                                const std::unordered_set<std::string>& instancePodIPs)
{
    // 设置实例恢复信息
    if (!mInstanceRecoveryInfo.Insert(instanceId, InstanceRecoveryInfo(faultNodes, instancePodIPs))) {
        LOG_D("[NPURecoveryManager] Instance %lu is already in recovery, skipping", instanceId);
        return;
    }

    LOG_I("[NPURecoveryManager] Processing instance %lu with full NPU recovery, %zu fault nodes",
          instanceId, faultNodes.size());

    // 将该实例的所有节点设置为不可用
    for (uint64_t nodeId : instanceNodeIds) {
        SetNodeUnavailable(nodeId);
    }

    // 向该实例中的所有 pod 发送 PAUSE_ENGINE,若失败则发送STOP_ENGINE停止服务
    if (!SendNodeManagerCommandToInstancePods(instanceId, NodeManagerCmd::PAUSE_ENGINE)) {
        LOG_D("[NPURecoveryManager] PAUSE_ENGINE failed for instance %lu, aborting recovery process", instanceId);
        (void)SendNodeManagerCommandToPodsParallel(instancePodIPs, instanceId, NodeManagerCmd::STOP_ENGINE);
        // 清理恢复信息，避免资源泄漏
        mInstanceRecoveryInfo.Erase(instanceId);
        mErrCodeAlarmExisted.Erase(instanceId);
        LOG_I("[NPURecoveryManager] Removed instance %lu from recovery queue (PAUSE_ENGINE failed)", instanceId);
        return;
    }

    // PAUSE_ENGINE成功后，直接执行故障恢复操作
    LOG_D("[NPURecoveryManager] Starting NPU recovery for instance %lu with %zu fault nodes",
          instanceId, faultNodes.size());

    // 发送REINIT_NPU到实例的所有pod,若失败则发送STOP_ENGINE停止服务
    if (!SendNodeManagerCommandToInstancePods(instanceId, NodeManagerCmd::REINIT_NPU)) {
        LOG_I("[NPURecoveryManager] REINIT_NPU failed for instance %lu, aborting NPU recovery", instanceId);
        (void)SendNodeManagerCommandToPodsParallel(instancePodIPs, instanceId, NodeManagerCmd::STOP_ENGINE);
        // 清理恢复信息，避免资源泄漏
        mInstanceRecoveryInfo.Erase(instanceId);
        mErrCodeAlarmExisted.Erase(instanceId);
        LOG_I("[NPURecoveryManager] Removed instance %lu from recovery queue (REINIT_NPU failed)", instanceId);
        return;
    }
    
    // 更新恢复开始时间
    auto recoveryInfo = mInstanceRecoveryInfo.Get(instanceId);
    if (recoveryInfo.has_value()) {
        auto updatedInfo = recoveryInfo.value();
        updatedInfo.UpdateTimestamp(); // 更新恢复开始时间（REINIT_NPU成功后才真正开始恢复）
        mInstanceRecoveryInfo.Set(instanceId, updatedInfo);
    } else {
        LOG_I("[NPURecoveryManager] Instance %lu was removed during command execution, skipping", instanceId);
        return;
    }
    
    // 轮询NPU状态
    StartGlobalNPUPolling();
    LOG_I("[NPURecoveryManager] Instance %lu fault processing initiated - %zu nodes, %zu pods",
          instanceId, instanceNodeIds.size(), instancePodIPs.size());
}

uint64_t NPURecoveryManager::GetInstanceIdByPodIP(const std::string& podIP)
{
    uint64_t nodeId = FindFirstNodeIdByPodIP(podIP);
    if (nodeId == INVALID_ID) {
        return INVALID_ID;
    }
    return GetInstanceIdByNodeId(nodeId);
}

uint64_t NPURecoveryManager::GetInstanceIdByNodeIP(const std::string& nodeIP)
{
    uint64_t nodeId = FindNodeIdByIP(nodeIP);
    if (nodeId == INVALID_ID) {
        return INVALID_ID;
    }
    return GetInstanceIdByNodeId(nodeId);
}

uint64_t NPURecoveryManager::GetInstanceIdByNodeId(uint64_t nodeId)
{
    auto node = mNodeStatus->GetNode(nodeId);
    if (node == nullptr) {
        return INVALID_ID;
    }
    
    // 使用 dpGroupPeers 来确定实例归属
    if (!node->dpGroupPeers.empty()) {
        // 使用最小的 peer ID 作为实例标识
        uint64_t minPeerId = *std::min_element(node->dpGroupPeers.begin(), node->dpGroupPeers.end());
        return minPeerId;
    } else {
        // 如果没有 dpGroupPeers，则用nodeId作为实例id
        return nodeId;
    }
}

std::vector<uint64_t> NPURecoveryManager::GetAllNodesInInstance(uint64_t instanceId)
{
    std::vector<uint64_t> instanceNodes;
    std::vector<uint64_t> allNodeIds = mNodeStatus->GetAllNodeIds();
    
    for (auto nodeId : allNodeIds) {
        auto node = mNodeStatus->GetNode(nodeId);
        if (node == nullptr) {
            continue;
        }
        
        // 判断该节点是否属于该实例
        if (!node->dpGroupPeers.empty()) {
            uint64_t nodeInstanceId = *std::min_element(node->dpGroupPeers.begin(), node->dpGroupPeers.end());
            if (nodeInstanceId == instanceId) {
                instanceNodes.push_back(nodeId);
            }
        } else if (nodeId == instanceId) {
            instanceNodes.push_back(nodeId);
        }
    }
    
    return instanceNodes;
}

std::unordered_set<std::string> NPURecoveryManager::GetAllPodIPsInInstance(uint64_t instanceId)
{
    std::unordered_set<std::string> podIPs;
    std::vector<uint64_t> instanceNodes = GetAllNodesInInstance(instanceId);
    
    for (uint64_t nodeId : instanceNodes) {
        auto node = mNodeStatus->GetNode(nodeId);
        if (node != nullptr && !node->ip.empty()) {
            podIPs.insert(node->ip);
        }
    }
    
    return podIPs;
}

FaultNodeInfo NPURecoveryManager::ConvertToFaultNodeInfo(const fault::NodeFaultInfo& nodeInfo)
{
    FaultNodeInfo faultNodeInfo;
    faultNodeInfo.nodeIP = nodeInfo.nodeip();
    faultNodeInfo.nodeSN = nodeInfo.nodesn();
    faultNodeInfo.faultLevel = nodeInfo.faultlevel();
    return faultNodeInfo;
}

uint64_t NPURecoveryManager::FindFirstNodeIdByPodIP(const std::string& podIP)
{
    std::vector<uint64_t> nodeIds = mNodeStatus->GetAllNodeIds();
    if (nodeIds.empty()) {
        LOG_D("[NPURecoveryManager] No node IDs found when looking for nodes with pod IP: %s", podIP.c_str());
        return INVALID_ID;
    }
    
    for (auto id : nodeIds) {
        auto node = mNodeStatus->GetNode(id);
        if (node == nullptr) {
            continue;
        }
        // 检查NodeInfo的ip
        if (node->ip == podIP) {
            return id;
        }
    }
    LOG_D("[NPURecoveryManager] Cannot find node with pod IP: %s", podIP.c_str());
    return INVALID_ID;
}

uint64_t NPURecoveryManager::FindNodeIdByIP(const std::string& nodeIP)
{
    std::vector<uint64_t> nodeIds = mNodeStatus->GetAllNodeIds();
    if (nodeIds.empty()) {
        LOG_D("[NPURecoveryManager] No node IDs found when looking for IP: %s", nodeIP.c_str());
        return INVALID_ID;
    }
    
    for (auto id : nodeIds) {
        auto node = mNodeStatus->GetNode(id);
        if (node == nullptr) {
            continue;
        }
        // 检查NodeInfo的hostId
        if (node->hostId == nodeIP) {
            return id;
        }
    }
    LOG_D("[NPURecoveryManager] Node not found for IP: %s", nodeIP.c_str());
    return INVALID_ID;
}

bool NPURecoveryManager::IsFirstCoordinatorReady()
{
    if (mCoordinatorReadyChecked.load()) {
        return true;
    }

    if (mNodeStatus == nullptr) {
        LOG_D("[NPURecoveryManager] Node status is null when checking coordinator readiness");
        return false;
    }

    std::vector<uint64_t> nodeIds = mNodeStatus->GetAllNodeIds();
    if (nodeIds.empty()) {
        LOG_D("[NPURecoveryManager] No node IDs found when checking coordinator readiness");
        return false;
    }
    bool pReady = false;
    bool dReady = false;
    for (auto id : nodeIds) {
        auto node = mNodeStatus->GetNode(id);
        if (node == nullptr) {
            continue;
        }
        if (node->currentRole == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE &&
            node->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::READY)) {
                pReady = true;
        }
        if (node->currentRole == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE &&
            node->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::READY)) {
                dReady = true;
        }
        if (pReady && dReady) {
            mCoordinatorReadyChecked.store(true);
            LOG_I("[NPURecoveryManager] Coordinator is ready, proceeding with fault processing");
            return true;
        }
    }
    return false;
}

bool NPURecoveryManager::IsInstanceRecoveryInProgress()
{
    return mInstanceRecoveryInfo.Size() > 0;
}

void NPURecoveryManager::AbortInstanceNPURecovery(uint64_t instanceId)
{
    bool hasTimer = false;
    bool hasRecovery = false;
    
    // 检查该实例是否正在进行灵衢恢复（使用定时器）
    auto timer = mInstanceRecoveryTimers.Get(instanceId);
    if (timer.has_value()) {
        hasTimer = true;
        // 停止恢复定时器
        if (timer.value()->IsActive()) {
            timer.value()->Stop();
        }
        // 清理定时器
        mInstanceRecoveryTimers.Erase(instanceId);
    }

    // 检查该实例是否在恢复队列中
    hasRecovery = (mInstanceRecoveryInfo.Count(instanceId) > 0);
    if (hasRecovery) {
        LOG_D("[NPURecoveryManager] Hardware fault detected for instance %lu, aborting NPU recovery", instanceId);
        AbortInstanceRecovery(instanceId);
    }
    
    if (hasTimer || hasRecovery) {
        LOG_I("[NPURecoveryManager] NPU recovery aborted for instance %lu due to hardware fault", instanceId);
    }
}

// 并发发送NodeManager命令到多个pod的公共函数
bool NPURecoveryManager::SendNodeManagerCommandToPodsParallel(const std::unordered_set<std::string>& podIPs,
                                                              uint64_t instanceId,
                                                              NodeManagerCmd cmd)
{
    if (podIPs.empty()) {
        LOG_D("[NPURecoveryManager] No pods to send command to for instance %lu", instanceId);
        return true;
    }

    std::string cmdName = mNodeManagerSender->NodeManagerCmdToString(cmd);
    
    // 使用原子变量来跟踪是否有失败
    std::atomic<bool> hasFailure(false);
    
    // HttpClient不是线程安全的，为每个线程创建独立的实例以避免数据竞争
    std::vector<std::future<void>> futures;
    futures.reserve(podIPs.size());

    // 并行发送命令到所有pod
    for (const auto& podIP : podIPs) {
        futures.push_back(std::async(std::launch::async, [this, instanceId, podIP, cmd, cmdName, &hasFailure]() {
            // 为每个线程创建独立的HttpClient实例，避免数据竞争
            auto threadLocalClient = std::make_shared<HttpClient>();
            if (threadLocalClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0) {
                LOG_E("[NPURecoveryManager] Failed to initialize HttpClient for pod %s in instance %lu",
                      podIP.c_str(), instanceId);
                hasFailure.store(true, std::memory_order_relaxed);
                return;
            }
            
            int32_t ret = mNodeManagerSender->SendCommandToNodeManager(*threadLocalClient, podIP, cmd);
            if (ret != 0) {
                LOG_E("[NPURecoveryManager] Failed to send %s to pod %s in instance %lu",
                      cmdName.c_str(), podIP.c_str(), instanceId);
                hasFailure.store(true, std::memory_order_relaxed);
            } else {
                LOG_I("[NPURecoveryManager] Successfully sent %s to pod %s in instance %lu",
                      cmdName.c_str(), podIP.c_str(), instanceId);
            }
        }));
    }

    // 等待所有任务完成
    for (auto& future : futures) {
        future.wait();
    }

    // 检查是否有失败
    return !hasFailure.load(std::memory_order_relaxed);
}

// 向实例所有Pod发送NodeManager命令
bool NPURecoveryManager::SendNodeManagerCommandToInstancePods(uint64_t instanceId, NodeManagerCmd cmd)
{
    std::string cmdName = mNodeManagerSender->NodeManagerCmdToString(cmd);
    
    // 从 mInstanceRecoveryInfo 中获取 podIPs
    auto recoveryInfo = mInstanceRecoveryInfo.Get(instanceId);
    // 如果找不到实例或podIPs为空，记录警告
    if (!recoveryInfo.has_value() || recoveryInfo.value().podIPs.empty()) {
        LOG_W("[NPURecoveryManager] Instance %lu not found in recovery info or has no pods, cannot send %s",
              instanceId, cmdName.c_str());
        return false;
    }
    
    const auto& podIPs = recoveryInfo.value().podIPs;
    LOG_I("[NPURecoveryManager] Sending %s to all %zu pods in instance %lu (parallel)",
          cmdName.c_str(), podIPs.size(), instanceId);

    return SendNodeManagerCommandToPodsParallel(podIPs, instanceId, cmd);
}

void NPURecoveryManager::StartGlobalNPUPolling()
{
    if (mGlobalNPUPollTimer == nullptr) {
        LOG_E("[NPURecoveryManager] Global NPU polling timer is null");
        return;
    }
    
    if (mGlobalNPUPollTimer->IsActive()) {
        LOG_I("[NPURecoveryManager] Global NPU polling already active");
        return;
    }
    
    // 启动全局NPU轮询定时器，每1秒检查一次
    int32_t ret = mGlobalNPUPollTimer->Start(NPU_STATUS_POLL_INTERVAL_SECONDS,
                                             [this]() {this->OnGlobalNPUPollTimerExpired();});
    if (ret != 0) {
        LOG_E("[NPURecoveryManager] Failed to start global NPU polling timer");
    } else {
        LOG_I("[NPURecoveryManager] Started global NPU polling timer (interval: %d seconds)",
              NPU_STATUS_POLL_INTERVAL_SECONDS);
    }
}

void NPURecoveryManager::OnGlobalNPUPollTimerExpired()
{
    InstanceRecoveryData data;
    bool shouldStopPolling = false;
    
    // 收集需要检查的实例数据
    if (!CollectInstanceRecoveryData(data)) {
        // 没有实例在恢复中，停止轮询
        StopGlobalNPUPolling();
        return;
    }
    
    // 检查每个实例的恢复状态
    std::vector<uint64_t> recoveredInstances;
    std::vector<uint64_t> timeoutInstances;
    CheckInstancesRecoveryStatus(data, recoveredInstances, timeoutInstances);
    
    // 处理已恢复和超时的实例
    ProcessRecoveredAndTimeoutInstances(recoveredInstances, timeoutInstances, shouldStopPolling);
    
    // 停止轮询（如果需要）
    if (shouldStopPolling) {
        LOG_I("[NPURecoveryManager] Stopping global NPU polling timer");
        StopGlobalNPUPolling();
    }
}

bool NPURecoveryManager::CollectInstanceRecoveryData(InstanceRecoveryData& data)
{
    // 检查是否有实例在恢复中
    if (mInstanceRecoveryInfo.Size() == 0) {
        LOG_I("[NPURecoveryManager] No instances in recovery, will stop polling");
        return false;
    }
    
    LOG_D("[NPURecoveryManager] Checking recovery status for %zu instances", mInstanceRecoveryInfo.Size());
    
    // 收集需要检查的实例ID、podIPs和开始时间
    auto instanceIds = mInstanceRecoveryInfo.KeySet();
    for (uint64_t instanceId : instanceIds) {
        auto recoveryInfo = mInstanceRecoveryInfo.Get(instanceId);
        if (recoveryInfo.has_value()) {
            data.instanceIds.push_back(instanceId);
            data.podIPsMap[instanceId] = recoveryInfo.value().podIPs;
            data.startTimes[instanceId] = recoveryInfo.value().startTime;
        }
    }
    
    return !data.instanceIds.empty();
}

void NPURecoveryManager::CheckInstancesRecoveryStatus(const InstanceRecoveryData& data,
                                                      std::vector<uint64_t>& recoveredInstances,
                                                      std::vector<uint64_t>& timeoutInstances)
{
    // 在锁外进行网络调用和超时检查，避免长时间持有锁
    for (uint64_t instanceId : data.instanceIds) {
        // 检查超时
        bool isTimeout = false;
        auto startTimeIt = data.startTimes.find(instanceId);
        if (startTimeIt != data.startTimes.end()) {
            auto now = std::chrono::steady_clock::now();
            auto duration = std::chrono::duration_cast<std::chrono::seconds>(now - startTimeIt->second);
            isTimeout = (duration.count() >= NPU_STATUS_POLL_TIMEOUT_SECONDS);
        }
        
        if (isTimeout) {
            LOG_I("[NPURecoveryManager] Instance %lu recovery timeout", instanceId);
            timeoutInstances.push_back(instanceId);
        } else {
            // 检查恢复状态（网络调用）
            auto podIPsIt = data.podIPsMap.find(instanceId);
            if (podIPsIt != data.podIPsMap.end() &&
                CheckInstanceRecoveryWithPodIPs(instanceId, podIPsIt->second)) {
                LOG_I("[NPURecoveryManager] Instance %lu recovery completed", instanceId);
                recoveredInstances.push_back(instanceId);
            }
        }
    }
}

void NPURecoveryManager::ProcessRecoveredAndTimeoutInstances(const std::vector<uint64_t>& recoveredInstances,
                                                             const std::vector<uint64_t>& timeoutInstances,
                                                             bool& shouldStopPolling)
{
    // 收集实例ID和podIPs，然后移除实例
    std::unordered_map<uint64_t, std::unordered_set<std::string>> timeoutPodIPs;
    std::unordered_map<uint64_t, std::unordered_set<std::string>> recoveredPodIPs;
    
    // 处理超时实例：先保存podIPs，再移除
    for (uint64_t instanceId : timeoutInstances) {
        auto recoveryInfo = mInstanceRecoveryInfo.Get(instanceId);
        if (recoveryInfo.has_value()) {
            timeoutPodIPs[instanceId] = recoveryInfo->podIPs;
            mInstanceRecoveryInfo.Erase(instanceId);
            mErrCodeAlarmExisted.Erase(instanceId);
        }
    }
    // 处理已恢复实例：先保存podIPs，再移除
    for (uint64_t instanceId : recoveredInstances) {
        auto recoveryInfo = mInstanceRecoveryInfo.Get(instanceId);
        if (recoveryInfo.has_value()) {
            recoveredPodIPs[instanceId] = recoveryInfo->podIPs;
            mInstanceRecoveryInfo.Erase(instanceId);
            mErrCodeAlarmExisted.Erase(instanceId);
        }
    }
    // 批量移除时统一记录日志
    if (!timeoutInstances.empty() || !recoveredInstances.empty()) {
        LOG_D("[NPURecoveryManager] Removed %zu timeout instances and %zu recovered instances from recovery queue",
              timeoutInstances.size(), recoveredInstances.size());
    }

    // 检查处理完成后是否还有实例在恢复中
    if (mInstanceRecoveryInfo.Size() == 0) {
        LOG_D("[NPURecoveryManager] All instances processed, will stop polling");
        shouldStopPolling = true;
    }
    
    // 使用保存的podIPs直接调用公共函数，避免从已移除的mInstanceRecoveryInfo中获取
    for (const auto& [instanceId, podIPs] : timeoutPodIPs) {
        LOG_I("[NPURecoveryManager] Stopping service for instance %lu with %zu pods due to recovery timeout",
              instanceId, podIPs.size());
        std::string cmdName = mNodeManagerSender->NodeManagerCmdToString(NodeManagerCmd::STOP_ENGINE);
        LOG_I("[NPURecoveryManager] Sending %s to all %zu pods in instance %lu (parallel)",
              cmdName, podIPs.size(), instanceId);
        (void)SendNodeManagerCommandToPodsParallel(podIPs, instanceId, NodeManagerCmd::STOP_ENGINE);
    }
    
    for (const auto& [instanceId, podIPs] : recoveredPodIPs) {
        RecoverInstanceServiceWithPodIPs(instanceId, podIPs);
    }
}

bool NPURecoveryManager::CheckInstanceRecoveryWithPodIPs(uint64_t instanceId,
                                                         const std::unordered_set<std::string>& podIPs)
{
    // 检查该实例下所有pod的NPU状态
    // 注意：此函数可能在多个线程中并发调用，需要为每个线程创建独立的HttpClient
    auto threadLocalClient = std::make_shared<HttpClient>();
    if (threadLocalClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0) {
        LOG_E("[NPURecoveryManager] Failed to initialize HttpClient for instance %lu recovery check", instanceId);
        return false;
    }
    
    for (const auto& podIP : podIPs) {
        NPUStatus status;
        int32_t ret = mNodeManagerSender->GetNodeManagerNodeStatus(*threadLocalClient, podIP, status);
        if (ret != 0 || status != NPUStatus::READY) {
            LOG_I("[NPURecoveryManager] Instance %lu, Pod %s NPU not ready yet, status: %d",
                  instanceId, podIP.c_str(), status);
            return false;
        }
    }
    
    LOG_D("[NPURecoveryManager] Instance %lu all pods ready", instanceId);
    return true;
}

void NPURecoveryManager::RecoverInstanceServiceWithPodIPs(uint64_t instanceId,
                                                          const std::unordered_set<std::string>& podIPs)
{
    LOG_D("[NPURecoveryManager] Recovering service for instance %lu with %zu pods", instanceId, podIPs.size());
    
    std::string cmdName = mNodeManagerSender->NodeManagerCmdToString(NodeManagerCmd::START_ENGINE);
    LOG_I("[NPURecoveryManager] Sending %s to all %zu pods in instance %lu (parallel)",
          cmdName.c_str(), podIPs.size(), instanceId);
    if (!SendNodeManagerCommandToPodsParallel(podIPs, instanceId, NodeManagerCmd::START_ENGINE)) {
        (void)SendNodeManagerCommandToPodsParallel(podIPs, instanceId, NodeManagerCmd::STOP_ENGINE);
        LOG_I("[NPURecoveryManager] START_ENGINE failed for instance %lu, stopping service", instanceId);
        return;
    }
    
    // 恢复该实例所有节点的可用状态
    RestoreInstanceNodeAvailability(instanceId);
    
    LOG_I("[NPURecoveryManager] Instance %lu service recovery completed", instanceId);
}


void NPURecoveryManager::RestoreInstanceNodeAvailability(uint64_t instanceId)
{
    std::vector<uint64_t> instanceNodes = GetAllNodesInInstance(instanceId);
    LOG_I("[NPURecoveryManager] RestoreInstanceNodeAvailability instance %lu: setting AVAILABLE for %zu nodes",
          instanceId, instanceNodes.size());
    for (uint64_t nodeId : instanceNodes) {
        SetNodeAvailable(nodeId);
    }
    LOG_I("[NPURecoveryManager] Instance %lu service recovered successfully", instanceId);
}

std::vector<uint64_t> NPURecoveryManager::GetInstancesInRecovery()
{
    std::vector<uint64_t> instanceIds = mInstanceRecoveryInfo.KeySet();
    return instanceIds;
}


void NPURecoveryManager::StopGlobalNPUPolling()
{
    if (mGlobalNPUPollTimer != nullptr && mGlobalNPUPollTimer->IsActive()) {
        mGlobalNPUPollTimer->Stop();
        LOG_I("[NPURecoveryManager] Stopped global NPU polling timer");
    }
}

void NPURecoveryManager::AbortInstanceRecovery(uint64_t instanceId)
{
    // 检查该实例是否在恢复中
    if (mInstanceRecoveryInfo.Count(instanceId) == 0) {
        LOG_I("[NPURecoveryManager] No active recovery found for instance %lu", instanceId);
        return;
    }

    LOG_I("[NPURecoveryManager] Aborting recovery for instance %lu due to hardware fault", instanceId);

    // 从恢复队列中移除该实例
    mInstanceRecoveryInfo.Erase(instanceId);
    mErrCodeAlarmExisted.Erase(instanceId);
    LOG_I("[NPURecoveryManager] Removed instance %lu from recovery queue (aborted)", instanceId);

    // 检查是否需要停止全局轮询
    bool shouldStopPolling = (mInstanceRecoveryInfo.Size() == 0);
    if (shouldStopPolling) {
        LOG_I("[NPURecoveryManager] No more instances in recovery, will stop global polling");
        LOG_I("[NPURecoveryManager] Stopping global polling after aborting instance %lu", instanceId);
        StopGlobalNPUPolling();
    }
    RestartInstance({instanceId});
    LOG_I("[NPURecoveryManager] Instance %lu recovery aborted, service restored", instanceId);
}

// 检查故障码是否在白名单中
bool NPURecoveryManager::IsFaultCodeInWhitelist(const std::string& faultCode)
{
    for (const auto& whitelistCode : mFaultCodeWhitelist) {
        if (faultCode.find(whitelistCode) != std::string::npos) {
            return true;
        }
    }
    return false;
}

std::vector<std::string> NPURecoveryManager::GetProcessedSwitchFaults() const
{
    return mProcessedSwitchFaults.ToVector();
}

std::vector<uint64_t> NPURecoveryManager::GetErrCodeAlarmExisted() const
{
    return mErrCodeAlarmExisted.ToVector();
}

void NPURecoveryManager::SetProcessedSwitchFaults(const std::vector<std::string>& newFaults)
{
    for (const auto& faultKey : newFaults) {
        mProcessedSwitchFaults.Insert(faultKey);
    }
}

} // namespace MS
} // namespace MINDIE