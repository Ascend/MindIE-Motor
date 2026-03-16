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
#ifndef MINDIE_MS_NODE_SCHEDULER_H
#define MINDIE_MS_NODE_SCHEDULER_H
#include <atomic>
#include <mutex>
#include <memory>
#include <condition_variable>
#include <thread>
#include "NodeStatus.h"
#include "CoordinatorStore.h"
#include "ControllerConstant.h"
#include "HttpClient.h"
#include "RankTableLoader.h"
#include "RoleSwitcher.h"
#include "role_manager/InstanceRoleManager.h"
#include "SharedMemoryUtils.h"
#include "AlarmRequestHandler.h"
#include "node_manager_sender/NodeManagerRequestSender.h"

namespace MINDIE::MS {
class NodeScheduler {
public:
    NodeScheduler(const NodeScheduler &obj) = delete;
    NodeScheduler &operator=(const NodeScheduler &obj) = delete;
    NodeScheduler(NodeScheduler &&obj) = delete;
    NodeScheduler &operator=(NodeScheduler &&obj) = delete;
    NodeScheduler(std::shared_ptr<NodeStatus> nodeStatusInit, std::shared_ptr<CoordinatorStore> coordinatorStoreInit);
    ~NodeScheduler();
    int32_t Init(DeployMode mode);
    int32_t Run();
    void Stop();
    void RecordRoleUnknown(uint64_t id, MINDIE::MS::DIGSInstanceRole role);
    void DeleteRoleUnknown(uint64_t id, MINDIE::MS::DIGSInstanceRole role);
    std::shared_ptr<RankTableLoader> GetRankTableLoader() const;
    bool monitorRankTableRunning = true;
    bool nodeSchedulerAlarmThreadRunning = true;

    void SetWaitClusterDGRTSave(std::atomic<bool>* waitClusterDGRTSave)
    {
        mWaitClusterDGRTSave = waitClusterDGRTSave;
    }

    bool ProcessBatchUnlinkAndLink(const std::vector<uint64_t> &nodeIds);

private:
    std::vector<MINDIE::MS::DIGSRoleDecision> mRoleDecisions {};
    std::shared_ptr<HttpClient> mServerClient = nullptr;
    std::shared_ptr<HttpClient> mCoordinatorClient = nullptr;
    std::shared_ptr<NodeStatus> mNodeStatus = nullptr;
    std::shared_ptr<CoordinatorStore> mCoordinatorStore = nullptr;
    std::shared_ptr<RankTableLoader> mRankTableLoader = nullptr;
    std::unique_ptr<RoleSwitcher> mRoleSwitcher = nullptr;
    std::unique_ptr<MINDIE::MS::roleManager::InstanceRoleManager> mScheduler = nullptr;
    DeployMode mDeployMode = DeployMode::SINGLE_NODE;
    std::shared_ptr<NodeManagerRequestSender> mNodeManagerSender = nullptr;
    std::atomic<bool> mRun = { true };
    std::atomic<uint32_t> mWaitSeconds = { 0 };
    std::vector<uint64_t> mRoleUnknownPIds {};
    std::vector<uint64_t> mRoleUnknownDIds {};
    std::vector<uint64_t> mRoleUnknownFlexIds {};
    std::vector<std::unique_ptr<NodeInfo>> mServerNodes;
    std::mutex mMtx {};
    MINDIE::MS::DIGSRequestSummary mRequestSummary {};
    std::thread monitorRankTableThread;
    std::thread nodeSchedulerAlarmThread;

    void Wait();
    bool ShouldSleep();
    int32_t InitServerCluster();
    // 灵衢故障读取
    int32_t RecoveryNPUFaultID();
    int32_t SingleModeInit(std::vector<std::unique_ptr<NodeInfo>> &availableNodesAfterCollectDynamicInfo,
        std::vector<std::unique_ptr<NodeInfo>> &faultyNodesAfterCollectStaticInfo,
        std::vector<std::unique_ptr<NodeInfo>> &faultyNodesAfterCollectingDynamicInfo);
    int32_t RecoverServerCluster(const nlohmann::json& processFile, std::vector<std::unique_ptr<NodeInfo>> &nodes);
    int32_t RecoverServerClusterInfo(
        std::map<uint64_t, std::pair<std::vector<uint64_t>, std::vector<uint64_t>>> &groups,
        std::map<uint64_t, std::vector<uint64_t>> &flexGroup,
        std::vector<std::unique_ptr<NodeInfo>> &availableNodes);
    int32_t PDModeInit(std::vector<std::unique_ptr<NodeInfo>> &serverNodes);
    int32_t InitRoleAndRoleManager(std::vector<std::unique_ptr<NodeInfo>> &serverNodes, size_t &pRate, size_t &dRate,
        bool isRecovering = false);
    void StopUnavailableNodes(const std::vector<std::unique_ptr<NodeInfo>> &availableNodes) const;
    int32_t WaitForRoleDecision(std::vector<std::unique_ptr<NodeInfo>> &serverNodes, size_t pRate, size_t dRate,
        std::vector<std::pair<std::vector<uint64_t>, std::vector<uint64_t>>> &groups,
        std::vector<std::vector<uint64_t>> &flexGroups);
    void SendPDRoleWithinAttempt(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
        std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group,
        std::vector<uint64_t> &flexGroup, std::vector<uint64_t> &pSuccess,
        std::vector<uint64_t> &dSuccess, std::vector<uint64_t> &flexSuccess);
    int32_t InitMultiPDRole(std::vector<std::unique_ptr<NodeInfo>> &serverNodes) const;
    void SendRole(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
        std::vector<std::pair<std::vector<uint64_t>, std::vector<uint64_t>>> &groupsVec,
        std::vector<std::vector<uint64_t>> &flexGroupVec);
    int32_t GetCoordinatorInfo();
    int32_t ParseCoordinatorInfoResp(std::string &response);
    int32_t InitInstanceRoleManager(std::vector<std::unique_ptr<NodeInfo>> &serverNodes, size_t &pRate,
        size_t &dRate, bool isRecovering);
    int32_t RoleDecisionHandler(std::vector<MINDIE::MS::DIGSRoleDecision> decision);
    int32_t RoleDecisionInstancesCollector(std::vector<MINDIE::MS::DIGSInstanceInfo> &instances,
        MINDIE::MS::DIGSRequestSummary &summary);
    std::vector<MINDIE::MS::DIGSRoleDecision> GetRoleDecisions();
    uint32_t GetRoleDecisionsSize();
    void RunForPDSeparate();
    void RunForSingleNode();
    void ProcessRoleUnknown();
    void ProcessRoleUnknownForP(std::vector<uint64_t> &pIds);
    void ProcessRoleUnknownForD(std::vector<uint64_t> &dIds);
    void ProcessRoleUnknownForPD(std::vector<uint64_t> &nodeIds);
    void UnlinkNodeFromPeers(uint64_t nodeId);
    void LinkNodeToPeers(uint64_t nodeId);
    bool BatchUnlinkNodes(const std::vector<uint64_t> &nodeIds);
    bool BatchLinkNodes(const std::vector<uint64_t> &nodeIds);
    void UpdateNodeStatusAfterSendRole(std::vector<std::unique_ptr<NodeInfo>> &servers,
                                       std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group,
                                       std::vector<uint64_t> &flexGroup, uint64_t groupId,
                                       std::vector<uint64_t> &pSuccess, std::vector<uint64_t> &dSuccess,
                                       std::vector<uint64_t> &flexSuccess);
    int32_t GroupsInstanceInfo() const;
    void DetectNodeChanges(std::vector<std::unique_ptr<NodeInfo>> &serverNodes);
    void DetectNodeChangesForAlarm(std::vector<std::unique_ptr<NodeInfo>> &serverNodes);
    void NodeSchedulerAlarm();
    void AddServersToNodeStatus(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
        const std::vector<uint64_t> &pSuccess, const std::vector<uint64_t> &dSuccess,
        const std::vector<uint64_t> &flexSuccess);
    void ProcessRoleDecisionChanges();
    int32_t AllocateDpGroup(std::vector<std::unique_ptr<NodeInfo>> &serverNodes) const;
    int32_t GetNodesFromRankTable(std::vector<std::unique_ptr<NodeInfo>> &availableNodes,
    std::vector<std::unique_ptr<NodeInfo>> &faultyNodes);
    void MonitorRankTable();
    bool HasRankTableChanged();
    // 被ClusterClient类和NodeScheduler类共享，用于同步初始化时序，优先使用ClusterD传输过来的global ranktable，
    // 再使用deploy_acjob.py生成的global ranktable，在第一次保存ClusterD传输过来的global ranktable后，置为true
    std::atomic<bool>* mWaitClusterDGRTSave;
    // 等待clusterD传输过来的global ranktable保存的最大时间，单位秒
    size_t mWaitClusterDGRTSaveTimeout = 6;
};
}
#endif // MINDIE_MS_NODE_SCHEDULER_H
