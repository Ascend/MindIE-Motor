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
#ifndef MINDIE_MS_FAULT_MANAGER_H
#define MINDIE_MS_FAULT_MANAGER_H
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <set>
#include <utility>
#include <mutex>

#include "ControllerConstant.h"
#include "HttpClient.h"
#include "NodeStatus.h"
#include "ScaleInTimer.h"
#include "RankTableLoader.h"

namespace MINDIE::MS {

enum class SoftwareFaultType {
    HTTP_COMM_FAILED = 1,
};

enum class HardwareFaultType {
    SUBHEALTHY = 0,
    UNHEALTHY = 1,
};

// for normal scale in and out to update instance groups
enum class ScalingMode {
    SCALE_IN = 0,
    SCALE_OUT = 1,
};

struct GroupUpdateMsg {
    uint64_t groupId = 0;
    std::vector<uint64_t> pNodes = {};
    std::vector<uint64_t> dNodes = {};
    std::vector<uint64_t> targetNewNodeIds = {};
    std::vector<uint64_t> targetOldNodeIds = {};
    std::vector<uint64_t> success = {};
    bool hasPrefillNode = false;
    bool hasDecodeNode = false;
};

using SoftwareFaultHandler = std::function<int32_t(std::shared_ptr<NodeStatus>, uint64_t)>;
using HardwareFaultHandler = std::function<int32_t(std::shared_ptr<NodeStatus>, uint64_t)>;

// Pair of vector of p nodes and d nodes ids.
using PAIR_VECTOR_ID_ID = std::pair<std::vector<uint64_t>, std::vector<uint64_t>>;
// Pair of vector of deleted node ids and new node ids that inhrent the deleted node.
using VECTOR_PAIR_ID_ID = std::vector<std::pair<uint64_t, uint64_t>>;
// Pair of vector of deleted node ids and their roles.
using VECTOR_PAIR_ID_ROLE = std::vector<std::pair<uint64_t, MINDIE::MS::DIGSInstanceRole>>;
// ScaleIn strategies for Non-Redundant systems
using SCALEIN_STRATEGY = std::function<void()>;
// 当前静态扩缩容模板中的PD实例个数，frist为P实例数，second为D实例数
using STATIC_ELASTIC_PD_CNT = std::pair<int32_t, int32_t>;
// 当前global ranktable中实际的PD实例个数，frist为P实例数，second为D实例数
using GRT_PD_CNT = std::pair<int32_t, int32_t>;

class FaultManager {
public:
    /// Get the singleton instance of FaultManager.
    /// \return The singleton instance of FaultManager.
    static FaultManager *GetInstance()
    {
        static FaultManager instance;
        return &instance;
    }

    FaultManager() = default;
    FaultManager(const FaultManager &obj) = delete;
    FaultManager &operator=(const FaultManager &obj) = delete;
    FaultManager(FaultManager &&obj) = delete;
    FaultManager &operator=(FaultManager &&obj) = delete;

    int32_t Init(std::shared_ptr<NodeStatus> nodeStatus, DeployMode deployMode);
    void RecordSoftwareFaultyNode(uint64_t id, SoftwareFaultType type);
    void RecordHardwareFaultyNode(uint64_t id, HardwareFaultType type);
    void ScalingInstance(std::vector<std::unique_ptr<NodeInfo>> &serverNodes, const NodeChanges &nodeChanges);

    // Generate a scale-in strategy for non-redundant systems.
    SCALEIN_STRATEGY GenerateStrategy();
    void StartTimerWithStrategy(SCALEIN_STRATEGY strategy);
    void InstanceLevelNonRedundantScaleIn();
    void SetRankTableLoader(std::shared_ptr<RankTableLoader> loader);
    std::shared_ptr<RankTableLoader> GetRankTableLoader() const;
    bool isNeedWaitNpuProcessExit = true;

    // 读取当前静态扩缩容模板中的PD实例个数
    STATIC_ELASTIC_PD_CNT GetStaticElasticPdCnt();
    // 读取当前global ranktable中实际的PD实例个数
    GRT_PD_CNT GetGRTPdCnt();
    
private:
    int32_t RegisterSoftwareFaultHandler(SoftwareFaultType type, SoftwareFaultHandler handler);
    int32_t RegisterHardwareFaultHandler(HardwareFaultType type, HardwareFaultHandler handler);

    void HandleSoftwareFault(uint64_t id, SoftwareFaultType type);
    void HandleHardwareFault(uint64_t id, HardwareFaultType type);

    // Public functions for faulty and normal scaling.
    int32_t FilterAvailableServers(std::vector<std::unique_ptr<NodeInfo>> &serverNodes,
        const std::vector<uint64_t> &availableNodeIDs, std::vector<std::unique_ptr<NodeInfo>> &availableServers);
    void UpdateFaultyInstanceCnt(uint64_t groupId, MINDIE::MS::DIGSInstanceRole role, int cnt);
    void FilterUnscalableInstances(uint64_t groupId, const VECTOR_PAIR_ID_ROLE &changeInfos,
                                   GroupUpdateMsg &groupUpdateMsg);
    void ScalingUpdateAllGroups(std::map<uint64_t, VECTOR_PAIR_ID_ROLE> changedGroups, ScalingMode mode);
    GroupUpdateMsg BuildGroupUpdateMsg(uint64_t groupId, ScalingMode mode, const VECTOR_PAIR_ID_ROLE &changedInfo);
    bool IsAllPeersUnAvailable(const NodeInfo &node, GroupUpdateMsg &groupUpdateMsg);
    void UpdateGroupRoleStateAndPeers(GroupUpdateMsg &groupUpdateMsg);

    // Scale in functions
    void ProcessScaleIn(const NodeChanges &nodeChanges);
    int32_t SelectGroup2ReleaseInstance(uint64_t groupId);
    int32_t ReleasePrefillInstances(uint64_t groupId);
    bool ReleaseDpGroupPeersForNode(uint64_t groupId, std::unique_ptr<NodeInfo> node,
        std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group);
    int32_t GetGroupActivePrefillInstanceCnt(uint64_t groupId);

    // Scale out functions used by fault recovery scale out.
    void ProcessScaleOutInSingleMode(std::vector<std::unique_ptr<NodeInfo>> &serverNodes,
                                        const NodeChanges &nodeChanges);
    void ProcessScaleOut(std::vector<std::unique_ptr<NodeInfo>> &serverNodes, const NodeChanges &nodeChanges);
    void TryStopTimer(const std::vector<uint64_t> &newNodeIds);
    void StopTimer();
    int64_t SelectBestGroup(MINDIE::MS::DIGSInstanceRole role, const std::vector<uint64_t> &allGroupIds);
    uint64_t AddInstance2GroupA2(NodeInfo &instance, std::vector<uint64_t> &allGroupIds);
    uint64_t AddInstance2GroupA3(NodeInfo &instance, std::vector<uint64_t> &allGroupIds);
    void AssignInstanceRole(NodeInfo &instance);

    DeployMode mDeployMode = DeployMode::SINGLE_NODE;
    std::shared_ptr<NodeStatus> mNodeStatus = nullptr;
    // this client is used to query new instance's status when we need to scale out.
    std::shared_ptr<HttpClient> mStatusQueryClient = nullptr;
    // this client is used to release instance when we don't have enough node resources to recover faulty instance.
    std::shared_ptr<HttpClient> mReleaseInstanceClient = nullptr;
    // this client is used to stop instance setup when incorrect instance schedule is happened.
    std::shared_ptr<HttpClient> mAbortSetupClient = nullptr;
    // 用于读取global ranktable中的PD实例信息
    std::shared_ptr<RankTableLoader> mRankTableLoader = nullptr;
    std::map<SoftwareFaultType, SoftwareFaultHandler> mSoftwareFaultHandlers;
    std::map<HardwareFaultType, HardwareFaultHandler> mHardwareFaultHandlers;
    std::map<uint64_t, std::set<SoftwareFaultType>> mSoftwareFaultyNodes;
    std::map<uint64_t, std::set<HardwareFaultType>> mHardwareFaultyNodes;
    std::shared_ptr<ScaleInTimer> mInstanceGroupTimer = nullptr;
    std::map<uint64_t, int32_t> mFaultyGroupInstanceInfo; // this is used to stop nonredundant scale in timer
    std::mutex mFaultyGroupInstanceInfoMutex;             // mutex for mFaultyGroupInstanceInfo
    std::uint32_t mScaleInTimerMode = 0;
};
} // namespace MINDIE::MS
#endif // MINDIE_MS_FAULT_MANAGER_H