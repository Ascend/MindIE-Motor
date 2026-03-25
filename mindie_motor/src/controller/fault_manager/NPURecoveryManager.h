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
#ifndef MINDIE_MS_NPU_RECOVERY_MANAGER_H
#define MINDIE_MS_NPU_RECOVERY_MANAGER_H

#include <deque>
#include <future>
#include <memory>
#include <string>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <set>
#include <mutex>
#include <chrono>
#include <atomic>
#include "NodeStatus.h"
#include "InstanceRecoveryTimer.h"
#include "node_manager_sender/NodeManagerRequestSender.h"
#include "HttpClient.h"
#include "grpc_proto/cluster_fault.pb.h"
#include "grpc_proto/cluster_fault.grpc.pb.h"
#include "concurrent_map.h"
#include "concurrent_set.h"
#include "AlarmConfig.h"
#include "NodeScheduler.h"

namespace MINDIE {
namespace MS {

// 检查是否包含关键故障级别 L3,L5,L6
bool HasCriticalFaultLevel(const fault::NodeFaultInfo& nodeInfo);

// switchFaultInfo级别的处理记录，用于去重
struct ProcessedSwitchFaultInfo {
    std::string faultCode;
    std::string switchChipId;
    std::string switchPortId;
    std::string faultTime;
    std::chrono::steady_clock::time_point processTime;
    
    // 生成唯一标识符
    std::string GetUniqueId() const
    {
        return faultCode + "|" + switchChipId + "|" + switchPortId + "|" + faultTime;
    }
    
    bool operator==(const ProcessedSwitchFaultInfo& other) const
    {
        return faultCode == other.faultCode &&
               switchChipId == other.switchChipId &&
               switchPortId == other.switchPortId &&
               faultTime == other.faultTime;
    }
};
class NPURecoveryManager {
public:
    static NPURecoveryManager *GetInstance()
    {
        static NPURecoveryManager instance;
        return &instance;
    }
    NPURecoveryManager() = default;
    ~NPURecoveryManager() = default;
    
    NPURecoveryManager(const NPURecoveryManager&) = delete;
    NPURecoveryManager& operator=(const NPURecoveryManager&) = delete;
    NPURecoveryManager(NPURecoveryManager&&) = delete;
    NPURecoveryManager& operator=(NPURecoveryManager&&) = delete;
    
    int32_t Init(std::shared_ptr<NodeStatus> nodeStatus,
                 std::shared_ptr<NodeScheduler> nodeScheduler = nullptr);

    // 由NodeManager透传的LLMEngine故障码处理主入口
    void ProcessLLMEngineAlarm(const nlohmann::json& alarmJson);
    std::vector<uint64_t> GetErrCodeAlarmExisted() const;
    
    // 灵衢故障消息处理主入口
    void ProcessFaultMessage(const fault::FaultMsgSignal &faultMsg);
    
    // 获取实例中的所有节点ID
    std::vector<uint64_t> GetAllNodesInInstance(uint64_t instanceId);
    
    // 获取实例中的所有Pod IP
    std::unordered_set<std::string> GetAllPodIPsInInstance(uint64_t instanceId);
    
    // 获取当前处于恢复流程中的所有实例ID
    std::vector<uint64_t> GetInstancesInRecovery();
    
    // 中止实例的灵衢恢复流程
    void AbortInstanceNPURecovery(uint64_t instanceId);
    bool IsInstanceRecoveryInProgress();
    void AbortInstanceRecovery(uint64_t instanceId);
    bool HasCriticalFaultLevel(const fault::NodeFaultInfo& nodeInfo);
    // 读取故障
    nlohmann::json LoadProcessedSwitchFaults() const;
    std::vector<std::string> GetProcessedSwitchFaults() const;
    void SetProcessedSwitchFaults(const std::vector<std::string>& newFaults);

private:
    std::shared_ptr<NodeStatus> mNodeStatus = nullptr;
    std::shared_ptr<NodeManagerRequestSender> mNodeManagerSender = nullptr;
    std::shared_ptr<HttpClient> mNodeManagerClient = nullptr;
    std::shared_ptr<NodeScheduler> mNodeScheduler = nullptr;

    // 实例恢复信息结构体，将相关数据聚合在一起，减少查找次数并提高数据一致性
    struct InstanceRecoveryInfo {
        std::vector<FaultNodeInfo> faultNodes;
        std::unordered_set<std::string> podIPs;
        std::chrono::steady_clock::time_point startTime;
        
        InstanceRecoveryInfo() = default;
        InstanceRecoveryInfo(const std::vector<FaultNodeInfo>& nodes,
                           const std::unordered_set<std::string>& ips)
            : faultNodes(nodes), podIPs(ips), startTime(std::chrono::steady_clock::now()) {}
        
        // 更新时间戳
        void UpdateTimestamp()
        {
            startTime = std::chrono::steady_clock::now();
        }
    };
    
    // 实例级别的数据结构（使用并发 Map 替代 std::unordered_map + mutex）
    ConcurrentMap<uint64_t, InstanceRecoveryInfo> mInstanceRecoveryInfo;
    
    std::shared_ptr<InstanceRecoveryTimer> mGlobalNPUPollTimer = nullptr;
    
    // 故障码白名单相关
    std::set<std::string> mFaultCodeWhitelist = {
        "[0x08520003,na,L2,na]"
    };
    std::set<std::string> mFaultRecoveringCodeWhitelist = {
        "80CB8009", // 灵衢伴生故障码
        "80CB800A", // hbm伴生故障码
        "80E01801", // hbm伴生故障码
        "80E18404"  // hbm伴生故障码
    };

    // CQE(4C1F8608) 设备故障码白名单，用于 GetCQEInstanceIdsFromFaultMessage 识别 CQE 实例
    std::set<std::string> mDeviceFaultCQECodesWhitelist = {
        "4C1F8608"
    };

    using ErrCodeHandler = std::function<void(uint64_t)>;
    struct ErrCodeProcessor {
        std::string errCode;
        std::string recoveryFuncKey;       // 故障快恢配置项名
        LLMEngineFaultReason faultReason; // 告警填充故障原因
        ErrCodeHandler handler; // 故障快恢处理函数
    };

    // 故障码为全量上报, 按vector顺序决定优先级(靠前优先), 只执行第一个匹配的快恢流程函数
    std::vector<ErrCodeProcessor> mErrCodeProcessors = {
        {"MIE05E01000A", "oom", LLMEngineFaultReason::TEXT_GENERATOR_OUT_OF_MEMORY,
            std::bind(&NPURecoveryManager::OOMRecoveryHandler, this, std::placeholders::_1)},
        {"MIE05E01000B", "hbm", LLMEngineFaultReason::HBM_MULTI_BIT_ERROR,
            std::bind(&NPURecoveryManager::OOMRecoveryHandler, this, std::placeholders::_1)},
    };

    ConcurrentSet<uint64_t> mErrCodeAlarmExisted;
  
    // 故障处理相关
    ConcurrentSet<std::string> mProcessedSwitchFaults;
    
    // 实例恢复定时器相关
    ConcurrentMap<uint64_t, std::shared_ptr<InstanceRecoveryTimer>> mInstanceRecoveryTimers;

    // Coordinator相关
    std::atomic<bool> mCoordinatorReadyChecked{false};
    // Coordinator 未 ready 时的故障队列
    ConcurrentSet<std::string> mCoordinatorNotReadyFaults;

    // RoCE 异步恢复：ClusterClient callback同步阻塞，通过std::async+保存future使callback快速返回。
    std::deque<std::future<void>> mRoCERecoveryFutures;
    void PruneCompletedRoCEFutures();
    
    // NPU恢复相关方法
    void StartGlobalNPUPolling();
    void StopGlobalNPUPolling();
    void OnGlobalNPUPollTimerExpired();
    
    // OnGlobalNPUPollTimerExpired 的辅助函数
    struct InstanceRecoveryData {
        std::vector<uint64_t> instanceIds;
        std::unordered_map<uint64_t, std::unordered_set<std::string>> podIPsMap;
        std::unordered_map<uint64_t, std::chrono::steady_clock::time_point> startTimes;
    };
    bool CollectInstanceRecoveryData(InstanceRecoveryData& data);
    void CheckInstancesRecoveryStatus(const InstanceRecoveryData& data,
                                     std::vector<uint64_t>& recoveredInstances,
                                     std::vector<uint64_t>& timeoutInstances);
    void ProcessRecoveredAndTimeoutInstances(const std::vector<uint64_t>& recoveredInstances,
                                             const std::vector<uint64_t>& timeoutInstances,
                                             bool& shouldStopPolling);
    bool CheckInstanceRecoveryWithPodIPs(uint64_t instanceId, const std::unordered_set<std::string>& podIPs);
    void RecoverInstanceServiceWithPodIPs(uint64_t instanceId, const std::unordered_set<std::string>& podIPs);
    void RestoreInstanceNodeAvailability(uint64_t instanceId);
    void RestartInstance(const std::unordered_set<uint64_t>& faultyInstanceIds);
    
    // 故障码处理相关方法
    bool IsNonRecoverableErrCodeExists(const nlohmann::json& errorInfo);
    bool IsErrCodeExists(const nlohmann::json& errorInfo, const std::string& errCode);
    std::string GetErrorLocationStr(const nlohmann::json& errorInfo, const std::string& errCode);
    std::string GetErrorLocationStrFromFaultMsg(const fault::FaultMsgSignal& faultMsg,
        uint64_t instanceId, const std::string& targetErrCode);
    void ReportLLMEngineAlarm(const std::string& errorLocationStr, LLMEngineFaultReason faultReason);
    std::set<std::string> GetNonRecoverableErrCodes(const nlohmann::json& errorInfo);
    void OOMRecoveryHandler(uint64_t instanceId);

    // RoCE 故障恢复相关方法
    void ProcessCQEFault(const fault::FaultMsgSignal& faultMsg);
    std::unordered_set<uint64_t> GetCQEInstanceIdsFromFaultMessage(const fault::FaultMsgSignal& faultMsg);
    void ProcessRoCERecovery(uint64_t instanceId,
                             const std::vector<FaultNodeInfo>& faultNodes,
                             const std::vector<uint64_t>& instanceNodeIds,
                             const std::unordered_set<std::string>& instancePodIPs);

    // 灵衢故障处理相关方法
    std::unordered_map<uint64_t, std::vector<FaultNodeInfo>> FindFaultyInstances(const fault::FaultMsgSignal &faultMsg,
                                                                                 bool &isNeedToRestart);
    std::pair<bool, std::vector<ProcessedSwitchFaultInfo>> ExtractValidSwitchFaults(
        const fault::NodeFaultInfo& nodeInfo);
    void ProcessInstanceFaults(const std::unordered_map<uint64_t, std::vector<FaultNodeInfo>>& instanceFaultMap);

    // 故障恢复策略相关方法
    bool CheckIfPrefillInstance(const std::vector<FaultNodeInfo>& faultNodes);
    void ProcessPrefillInstanceFaults(uint64_t instanceId,
                                      const std::vector<FaultNodeInfo>& faultNodes,
                                      const std::vector<uint64_t>& instanceNodeIds);
    void ProcessFullNPURecovery(uint64_t instanceId,
                                const std::vector<FaultNodeInfo>& faultNodes,
                                const std::vector<uint64_t>& instanceNodeIds,
                                const std::unordered_set<std::string>& instancePodIPs);
    void OnPrefillIsolationTimerExpired(uint64_t instanceId, const std::vector<FaultNodeInfo>& faultNodes);
    
    // 辅助方法
    bool IsFaultCodeInWhitelist(const std::string& faultCode);
    uint64_t GetInstanceIdByPodIP(const std::string& podIP);
    uint64_t GetInstanceIdByNodeIP(const std::string& nodeIP);
    uint64_t GetInstanceIdByNodeId(uint64_t nodeId);
    FaultNodeInfo ConvertToFaultNodeInfo(const fault::NodeFaultInfo& nodeInfo);
    uint64_t FindFirstNodeIdByPodIP(const std::string& podIP);
    uint64_t FindNodeIdByIP(const std::string& nodeIP);
    bool SendNodeManagerCommandToInstancePods(uint64_t instanceId, NodeManagerCmd cmd);
    // 并发发送NodeManagerM命令到多个pod的公共函数
    bool SendNodeManagerCommandToPodsParallel(const std::unordered_set<std::string>& podIPs,
                                      uint64_t instanceId,
                                      NodeManagerCmd cmd);
    void UpdateProcessedSwitchFaults(const std::vector<ProcessedSwitchFaultInfo>& newFaults);
    bool IsFirstCoordinatorReady();
    void SetNodeUnavailable(uint64_t nodeId);
    void SetNodeAvailable(uint64_t nodeId);
    int32_t SaveProcessedSwitchFaults(const std::vector<std::string>& processedSwitchFaults) const;
    int32_t SaveFaultsWithLock(const std::string& path,
                               const std::vector<std::string>& newFaultsToAdd) const;
};

} // namespace MS
} // namespace MINDIE

#endif // MINDIE_MS_NPU_RECOVERY_MANAGER_H