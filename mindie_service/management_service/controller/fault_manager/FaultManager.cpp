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
#include "FaultManager.h"
#include "ControllerConfig.h"
#include "FaultHandler.h"
#include "Logger.h"
#include "ServerRequestHandler.h"
#include "digs_instance.h"
#include "DpGroupingUtil.h"

namespace MINDIE::MS {

constexpr uint64_t DP_GROUP_NUM = 10000;
constexpr int64_t NOT_SCALE_OUT = 999999; // groupid for not scaling out
constexpr int TIMER_TIMEOUT = 30;         // seconds
constexpr int TIMER_MODE = 2; // 定时器首次超时时间为TIMER_TIMEOUT秒，之后每隔TIMER_TIMEOUT * 2秒才执行一次超时回调
// Maximum wait time in seconds for residual NPU processes to exit
constexpr uint32_t MAX_WAIT_TIME_FOR_NPU_PROCESS_EXIT{30};

static void RemoveElement(std::vector<uint64_t> &vec, uint64_t element)
{
    LOG_D("[FaultManager] RemoveElement: origin vector size %zu", vec.size());
    auto it = std::find(vec.begin(), vec.end(), element);
    if (it != vec.end()) {
        vec.erase(it);
        LOG_D("[FaultManager] RemoveElement: remove operation done, vector size %zu", vec.size());
    } else {
        LOG_E("[%s] [FaultManager] Failed to remove element, element %lu is not found.",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str(), element);
    }
}

static void AddElement(std::vector<uint64_t> &vec, uint64_t element)
{
    LOG_D("[FaultManager] AddElement: origin vector size %zu", vec.size());
    if (std::find(vec.begin(), vec.end(), element) == vec.end()) {
        vec.push_back(element);
    }
    LOG_D("[FaultManager] AddElement: add operation done, vector size %zu", vec.size());
}

int32_t FaultManager::Init(std::shared_ptr<NodeStatus> nodeStatus, DeployMode deployMode)
{
    mNodeStatus = nodeStatus;
    mDeployMode = deployMode;
    mStatusQueryClient = std::make_shared<HttpClient>();
    mReleaseInstanceClient = std::make_shared<HttpClient>();
    mAbortSetupClient = std::make_shared<HttpClient>();
    if (mStatusQueryClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0 ||
        mReleaseInstanceClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0 ||
        mAbortSetupClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0) {
        LOG_E("[%s] [FaultManager] Initialize failed because initialize server clients failed!",
              GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::FAULT_MANAGER).c_str());
        return -1;
    }
    int32_t ret = 0;
    ret = RegisterHardwareFaultHandler(HardwareFaultType::SUBHEALTHY,
        std::bind(SubHealthyHardwareFaultHandler, std::placeholders::_1, std::placeholders::_2));
    ret = RegisterHardwareFaultHandler(HardwareFaultType::UNHEALTHY,
        std::bind(UnhealthyHardwareFaultHandler, std::placeholders::_1, std::placeholders::_2));
    if (ret == 0) {
        LOG_I("[FaultManager] Fault manager initialize successfully.");
    }
    return ret;
}

int32_t FaultManager::RegisterSoftwareFaultHandler(SoftwareFaultType type, SoftwareFaultHandler handler)
{
    if (handler == nullptr) {
        LOG_E("[%s] [FaultManager] Register software fault handler failed because handler is nullptr.",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str());
        return -1;
    }
    if (mSoftwareFaultHandlers.find(type) != mSoftwareFaultHandlers.end()) {
        LOG_E("[%s] [FaultManager] Register software fault handler failed because "
              "handler type: %d is already Registered.",
              GetErrorCode(ErrorType::OPERATION_REPEAT, ControllerFeature::FAULT_MANAGER).c_str(),
              static_cast<int32_t>(type));
        return -1;
    }
    mSoftwareFaultHandlers[type] = handler;
    return 0;
}

int32_t FaultManager::RegisterHardwareFaultHandler(HardwareFaultType type, HardwareFaultHandler handler)
{
    if (handler == nullptr) {
        LOG_E("[%s] [FaultManager] Register hardware fault handler failed because handler is nullptr.",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str());
        return -1;
    }
    if (mHardwareFaultHandlers.find(type) != mHardwareFaultHandlers.end()) {
        LOG_E(
            "[%s] [FaultManager] Register hardware fault handler failed because handler type: "
            "%d is already Registered.",
            GetErrorCode(ErrorType::OPERATION_REPEAT, ControllerFeature::FAULT_MANAGER).c_str(),
            static_cast<int32_t>(type));
        return -1;
    }
    mHardwareFaultHandlers[type] = handler;
    return 0;
}

void FaultManager::HandleSoftwareFault(uint64_t id, SoftwareFaultType type)
{
    if (mSoftwareFaultHandlers.find(type) != mSoftwareFaultHandlers.end()) {
        LOG_I("FaultManager] Handle software fault for node %lu, type %d.", id, static_cast<int32_t>(type));
        int32_t ret = mSoftwareFaultHandlers[type](mNodeStatus, id);
        if (ret != 0) {
            LOG_E("[%s] [FaultManager] Handle software fault failed, node id: %lu, fault type: %d, error code: %d",
                  GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::FAULT_MANAGER).c_str(), id, type, ret);
        }
    } else {
        LOG_E("[%s] [FaultManager] Handle software fault failed because handler type: %d is not Registered.",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str(), type);
    }
}

void FaultManager::HandleHardwareFault(uint64_t id, HardwareFaultType type)
{
    if (mHardwareFaultHandlers.find(type) != mHardwareFaultHandlers.end()) {
        LOG_I("FaultManager] Handle hardware fault for node %lu, type %d.", id, static_cast<int32_t>(type));
        int32_t ret = mHardwareFaultHandlers[type](mNodeStatus, id);
        if (ret != 0) {
            LOG_E("[%s] [FaultManager] Handle hardware fault failed, node id: %lu, fault type: %d, error code: %d",
                  GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::FAULT_MANAGER).c_str(), id, type, ret);
        }
    } else {
        LOG_E("[%s] [FaultManager] Handle hardware fault failed because handler type: %d is not Registered.",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str(), type);
    }
}

void FaultManager::RecordSoftwareFaultyNode(uint64_t id, SoftwareFaultType type)
{
    LOG_I("[FaultManager] Record software fault for node %lu, type %d.", id, static_cast<int32_t>(type));
    mSoftwareFaultyNodes[id].insert(type);
    HandleSoftwareFault(id, type);
    return;
}

void FaultManager::RecordHardwareFaultyNode(uint64_t id, HardwareFaultType type)
{
    LOG_I("[FaultManager] Record hardware fault for node %lu, type %d.", id, static_cast<int32_t>(type));
    mHardwareFaultyNodes[id].insert(type);
    HandleHardwareFault(id, type);
    return;
}

void FaultManager::ScalingInstance(std::vector<std::unique_ptr<NodeInfo>> &serverNodes, const NodeChanges &nodeChanges)
{
    if (!nodeChanges.removedIDs.empty()) {
        ProcessScaleIn(nodeChanges);
    }
    if (!nodeChanges.newIDs.empty() || !nodeChanges.reappearIDs.empty()) {
        ProcessScaleOut(serverNodes, nodeChanges);
    }
}

SCALEIN_STRATEGY FaultManager::GenerateStrategy()
{
    // Currently, we only have one strategy for scale-in.
    // And we will have more recovery strategy in the future.
    return std::bind(&FaultManager::InstanceLevelNonRedundantScaleIn, this);
}

void FaultManager::StartTimerWithStrategy(SCALEIN_STRATEGY strategy)
{
    if (mInstanceGroupTimer == nullptr) {
        LOG_I("[FaultManager] FaultManager doesn't have timer, now create a scaling timer.");
        mInstanceGroupTimer = std::make_shared<ScaleInTimer>();
    }
    if (!mInstanceGroupTimer->IsActive()) {
        mInstanceGroupTimer->Start(TIMER_TIMEOUT, [strategy]() { return strategy(); });
    }
}

void FaultManager::InstanceLevelNonRedundantScaleIn()
{
    // 定时器触发次数
    mScaleInTimerMode++;
    if (mScaleInTimerMode % TIMER_MODE == 0) {
        LOG_I("[FaultManager] Instance level non-redundant scale in strategy is triggered, but time is not over "
            "%zu, so skip action this time.", TIMER_TIMEOUT * TIMER_MODE);
        return;
    }
    // 期望的PD实例个数，从静态扩缩容模板中读取
    STATIC_ELASTIC_PD_CNT expectedPDCnt = GetStaticElasticPdCnt();
    if (expectedPDCnt.first == std::numeric_limits<int32_t>::min() ||
        expectedPDCnt.second == std::numeric_limits<int32_t>::min()) {
        LOG_E("[%s] [FaultManager] Instance level non-redundant scale in strategy failed because get expected PD cnt "
              "failed.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::FAULT_MANAGER).c_str());
        return;
    }
    // 实际的PD实例个数，从global ranktable中获取
    GRT_PD_CNT actualPDCnt = GetGRTPdCnt();
    if (actualPDCnt.first == std::numeric_limits<int32_t>::min() ||
        actualPDCnt.second == std::numeric_limits<int32_t>::min()) {
        LOG_E("[%s] [FaultManager] Instance level non-redundant scale in strategy failed because get actual PD cnt "
              "failed.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::FAULT_MANAGER).c_str());
        return;
    }

    // 获取当前PD分组id，目前controller中只有一个组
    std::vector<uint64_t> allGroupIds = mNodeStatus->GetAllGroupIds();
    if (allGroupIds.size() == 0) {
        LOG_E("[%s] [FaultManager] Instance level non-redundant scale in strategy failed because get all group ids "
              "failed, no group found.",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str());
        return;
    }
    uint64_t groupId = allGroupIds.front();

    // 通过对比PD实例个数，可以知道当前是否有D实例处于故障状态，但无法知道具体故障了几个D实例节点
    if (expectedPDCnt.second - actualPDCnt.second > 0) {
        LOG_I("[FaultManager] Group %lu Current D instance number less %lu than expected, try to release P instances.",
              groupId, expectedPDCnt.second - actualPDCnt.second);
        if (SelectGroup2ReleaseInstance(groupId) != 0) {
            LOG_E("[%s] [FaultManager] All groups do not has enough prefill instance, "
                    "release prefill instance failed.",
                GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::FAULT_MANAGER).c_str(), groupId);
            return;
        }
    }
}

// #################################################
// ########## Public scaling functions #############
// #################################################
int32_t FaultManager::FilterAvailableServers(std::vector<std::unique_ptr<NodeInfo>> &serverNodes,
    const std::vector<uint64_t> &availableNodeIDs, std::vector<std::unique_ptr<NodeInfo>> &availableServers)
{
    std::vector<std::unique_ptr<NodeInfo>> availableNodes;
    for (auto &server : serverNodes) {
        if (std::find(availableNodeIDs.begin(), availableNodeIDs.end(), server->instanceInfo.staticInfo.id) ==
            availableNodeIDs.end()) {
            continue;
        }
        std::unique_ptr<NodeInfo> newNode;
        try {
            newNode = std::make_unique<NodeInfo>(*(server.get()));
        } catch (const std::exception &e) {
            LOG_E("[%s] [FaultManager] Create node information failed.",
                  GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::CONTROLLER).c_str());
            continue;
        }
        availableNodes.push_back(std::move(newNode));
    }
    std::vector<std::unique_ptr<NodeInfo>> faultyNodes;
    int32_t ret = ServerRequestHandler::GetInstance()->GetAvailableNodes(
        *mStatusQueryClient, availableNodes, availableServers, faultyNodes, mNodeStatus);
    LOG_I("[FaultManager] Scale out operation: available node size is %zu after collecting info.",
        availableServers.size());
    return ret;
}

void FaultManager::UpdateFaultyInstanceCnt(uint64_t groupId, MINDIE::MS::DIGSInstanceRole role, int cnt)
{
    std::lock_guard<std::mutex> lock(mFaultyGroupInstanceInfoMutex);
    // Only update faulty Decode instance count, cnt can be positive or negative number.
    if (mFaultyGroupInstanceInfo.find(groupId) == mFaultyGroupInstanceInfo.end()) {
        mFaultyGroupInstanceInfo[groupId] = 0;
    }
    LOG_D("[FaultManager] Current group %lu's faulty decode instance count: %ld, role %c, cnt %d.", groupId,
          mFaultyGroupInstanceInfo[groupId], role, cnt);
    if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE && mFaultyGroupInstanceInfo[groupId] + cnt >= 0) {
        mFaultyGroupInstanceInfo[groupId] += cnt;
        LOG_D("[FaultManager] Update group %lu's faulty decode instance count: %ld.", groupId,
              mFaultyGroupInstanceInfo[groupId]);
    }
}

void FaultManager::FilterUnscalableInstances(uint64_t groupId, const VECTOR_PAIR_ID_ROLE &changeInfos,
                                             GroupUpdateMsg &groupUpdateMsg)
{
    // filter out the unscalable instances when process scale out, if we don't do this, the unscalable
    // instances ocupies the NPU resources. we tend to terminate this instance to release the resources
    // for other instances.
    for (const auto &[nodeId, role] : changeInfos) {
        std::unique_ptr<NodeInfo> node = mNodeStatus->GetNode(nodeId);
        if (node == nullptr) {
            LOG_E("[%s] [FaultManager] Updating group info: node %lu not found.",
                  GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str(), nodeId);
            continue;
        }
        if (IsAllPeersUnAvailable(*node, groupUpdateMsg)) {
            LOG_W("[FaultManager] Updating group info: node %lu's peers are all unavailable, terminate this "
                  "instance.",
                  nodeId);
            ServerRequestHandler::GetInstance()->TerminateService(*mAbortSetupClient, *node);
            continue;
        }
        UpdateFaultyInstanceCnt(groupId, role, -1);
    }
}

void FaultManager::ScalingUpdateAllGroups(std::map<uint64_t, VECTOR_PAIR_ID_ROLE> changedGroups, ScalingMode mode)
{
    LOG_I("[FaultManager] Updating group info: all changed group number %zu.", changedGroups.size());
    for (const auto &[groupId, changeInfos] : changedGroups) {
        auto group = mNodeStatus->GetGroup(groupId);
        GroupUpdateMsg groupUpdateMsg = BuildGroupUpdateMsg(groupId, mode, changeInfos);
        if (mode == ScalingMode::SCALE_OUT) {
            FilterUnscalableInstances(groupId, changeInfos, groupUpdateMsg);
        }
        // update group info and instance peers info
        mNodeStatus->UpdateGroup(groupId, std::make_pair(groupUpdateMsg.pNodes, groupUpdateMsg.dNodes));
        UpdateGroupRoleStateAndPeers(groupUpdateMsg);

        // Send postRole message to new nodes
        if (!groupUpdateMsg.targetNewNodeIds.empty()) {
            LOG_I("[FaultManager] Now start to send new peers info to new instances.");
            ServerRequestHandler::GetInstance()->BatchPostRole(*mStatusQueryClient, *mNodeStatus,
                groupUpdateMsg.targetNewNodeIds, groupUpdateMsg.success);
            LOG_I("[FaultManager] Now finish to send new peers info to new instances.");
        }

        int64_t initRanktableChangeTime = mNodeStatus->GetRanktableChangeTime();
        if (isNeedWaitNpuProcessExit && mode == ScalingMode::SCALE_OUT) {
            for (uint32_t waitSeconds = 0; waitSeconds < MAX_WAIT_TIME_FOR_NPU_PROCESS_EXIT; ++waitSeconds) {
                sleep(1);
                if (initRanktableChangeTime != mNodeStatus->GetRanktableChangeTime()) {
                    LOG_I("[FaultManager] Ranktable changed, skip send postRole message to old nodes.");
                    return;
                }
            }
        }

        // Send postRole message to old nodes
        if (!groupUpdateMsg.targetOldNodeIds.empty()) {
            LOG_I("[FaultManager] Now start to send new peers info to old instances.");
            ServerRequestHandler::GetInstance()->BatchPostRole(*mStatusQueryClient, *mNodeStatus,
                groupUpdateMsg.targetOldNodeIds, groupUpdateMsg.success);
            LOG_I("[FaultManager] Now finish to send new peers info to old instances.");
        }

        // update inference type for prefill instances
        for (auto &id : groupUpdateMsg.pNodes) {
            mNodeStatus->UpdateInferenceType(id, InferenceType::AVAILABLE);
        }
        LOG_I("[FaultManager] Updating group info: instance group %lu has %zu prefill nodes and %zu decode nodes now.",
              groupId, groupUpdateMsg.pNodes.size(), groupUpdateMsg.dNodes.size());
    }
}

GroupUpdateMsg FaultManager::BuildGroupUpdateMsg(uint64_t groupId, ScalingMode mode,
                                                 const VECTOR_PAIR_ID_ROLE &changedInfo)
{
    GroupUpdateMsg groupUpdateMsg;
    groupUpdateMsg.groupId = groupId;
    auto targetGroup = mNodeStatus->GetGroup(groupId);
    groupUpdateMsg.pNodes = targetGroup.first;
    groupUpdateMsg.dNodes = targetGroup.second;
    auto instanceChangeFunc = [&groupUpdateMsg](ScalingMode mode, std::vector<uint64_t> &instances, uint64_t nodeId) {
        if (mode == ScalingMode::SCALE_IN) {
            RemoveElement(instances, nodeId);
        } else if (mode == ScalingMode::SCALE_OUT) {
            AddElement(instances, nodeId);
            // For scale out, we need to notify new instance it's role.
            groupUpdateMsg.targetNewNodeIds.push_back(nodeId);
        }
    };
    for (const auto &[nodeId, role] : changedInfo) {
        if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            instanceChangeFunc(mode, groupUpdateMsg.dNodes, nodeId);
            groupUpdateMsg.hasDecodeNode = true;
        } else if (role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            instanceChangeFunc(mode, groupUpdateMsg.pNodes, nodeId);
            groupUpdateMsg.hasPrefillNode = true;
        }
    }
    return groupUpdateMsg;
}

bool FaultManager::IsAllPeersUnAvailable(const NodeInfo &node, GroupUpdateMsg &groupUpdateMsg)
{
    auto group = mNodeStatus->GetGroup(node.instanceInfo.staticInfo.groupId);
    if (group.first.empty() && group.second.empty()) {
        // when this gorup's all instance is down, we should not remove this instance from scale out instance.
        return false;
    }
    bool peerAllUnavailable = true;
    auto role = node.instanceInfo.staticInfo.role;
    uint64_t id = node.instanceInfo.staticInfo.id;
    for (const uint64_t peerID : node.peers) {
        auto peer = mNodeStatus->GetNode(peerID);
        if (peer == nullptr) {
            continue;
        }
        if (peer->deleteTime == std::chrono::seconds(0)) {
            peerAllUnavailable = false;
            break;
        }
    }
    if (peerAllUnavailable && role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        if (groupUpdateMsg.hasPrefillNode) {
            peerAllUnavailable = false;
        } else {
            LOG_W("[FaultManager] decode instance %lu is unscalable, remove it from scale out instance!", id);
            RemoveElement(groupUpdateMsg.dNodes, id);
            RemoveElement(groupUpdateMsg.targetNewNodeIds, id);
            RemoveElement(groupUpdateMsg.targetOldNodeIds, id);
        }
    }
    if (peerAllUnavailable && role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        if (groupUpdateMsg.hasDecodeNode) {
            peerAllUnavailable = false;
        } else {
            LOG_W("[FaultManager] prefill instance %lu is unscalable, remove it from scale out instance!", id);
            RemoveElement(groupUpdateMsg.pNodes, id);
            RemoveElement(groupUpdateMsg.targetNewNodeIds, id);
            RemoveElement(groupUpdateMsg.targetOldNodeIds, id);
        }
    }
    return peerAllUnavailable;
}

void FaultManager::UpdateGroupRoleStateAndPeers(GroupUpdateMsg &groupUpdateMsg)
{
    if (groupUpdateMsg.hasDecodeNode) {
        for (auto &pNodeId : groupUpdateMsg.pNodes) { // 还在线的P节点
            LOG_D("[FaultManager] Update P node: %zu's role state and peers. peers size: %zu", pNodeId,
                  groupUpdateMsg.dNodes.size());
            mNodeStatus->UpdateInferenceType(pNodeId, InferenceType::PREFILL_UPDATING_PEERS);
            mNodeStatus->UpdateRoleStateAndPeers(groupUpdateMsg.groupId,
                pNodeId, ControllerConstant::GetInstance()->GetRoleState(RoleState::READY), groupUpdateMsg.dNodes);
            // Only send postRole message to healthy prefill instances.
            auto pNode = mNodeStatus->GetNode(pNodeId);
            if (pNode == nullptr) {
                LOG_W("[FaultManager] Updating P instance %zu's peers but this P instance was deleted, ignore it!",
                      pNodeId);
                continue;
            }
            if (pNode->deleteTime == std::chrono::seconds(0)) {
                groupUpdateMsg.targetOldNodeIds.push_back(pNodeId);
            }
        }
    }
    if (groupUpdateMsg.hasPrefillNode) {
        for (auto &dNodeId : groupUpdateMsg.dNodes) { // 还在线的D节点
            LOG_D("[FaultManager] Update D node: %zu's role state and peers. peers size: %zu", dNodeId,
                  groupUpdateMsg.pNodes.size());
            mNodeStatus->UpdateRoleStateAndPeers(groupUpdateMsg.groupId,
                dNodeId, ControllerConstant::GetInstance()->GetRoleState(RoleState::READY), groupUpdateMsg.pNodes);
            groupUpdateMsg.targetOldNodeIds.push_back(dNodeId);
        }
    }
    // Delete duplicate nodes in targetOld nodes from targetNew nodes
    std::set<uint64_t> targetNewNodeSet(groupUpdateMsg.targetNewNodeIds.begin(), groupUpdateMsg.targetNewNodeIds.end());
    for (auto it = groupUpdateMsg.targetOldNodeIds.begin(); it != groupUpdateMsg.targetOldNodeIds.end();) {
        if (targetNewNodeSet.find(*it) != targetNewNodeSet.end()) {
            it = groupUpdateMsg.targetOldNodeIds.erase(it);
        } else {
            ++it;
        }
    }
}

// #################################################
// ############  scaling in functions ##############
// #################################################
void FaultManager::ProcessScaleIn(const NodeChanges &nodeChanges)
{
    LOG_I("[FaultManager] Scale in operation: detected %zu deleted nodes.", nodeChanges.removedIDs.size());
    std::map<uint64_t, VECTOR_PAIR_ID_ROLE> changedGroups;
    for (const uint64_t removedID : nodeChanges.removedIDs) {
        mNodeStatus->UpdateNodeDeleteTime(removedID);
        auto node = mNodeStatus->GetNode(removedID);
        if (node == nullptr) {
            LOG_E("[%s] [FaultManager] Get node failed.",
                  GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str());
            continue;
        }
        uint64_t groupId = node->instanceInfo.staticInfo.groupId;
        UpdateFaultyInstanceCnt(groupId, node->instanceInfo.staticInfo.role, 1);
        // Record the deleted instance info for this group
        if (changedGroups.find(groupId) == changedGroups.end()) {
            changedGroups[groupId] = VECTOR_PAIR_ID_ROLE();
        }
        changedGroups[groupId].push_back(std::make_pair(removedID, node->instanceInfo.staticInfo.role));
        mNodeStatus->RemoveExpiredNode(removedID);
        mNodeStatus->RemoveNode(removedID);
    }
    ScalingUpdateAllGroups(changedGroups, ScalingMode::SCALE_IN);
}

int32_t FaultManager::SelectGroup2ReleaseInstance(uint64_t groupId)
{
    if (groupId >= NOT_SCALE_OUT) {
        LOG_E("[%s] [FaultManager] SelectGroup2ReleaseInstance: groupId %lu is invalid.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::FAULT_MANAGER).c_str(), groupId);
        return -1;
    }
    // Try to release prefill instances in itself group first.
    // If failed to release prefill instances in itself group,
    // try to release prefill instances in other groups.
    uint32_t ret = ReleasePrefillInstances(groupId);
    if (ret != 0) {
        LOG_W("[FaultManager] Group %lu does not has enough prefill instance, try to release prefill instance "
              "in other groups.",
              groupId);
        std::vector<uint64_t> allGroupIds = mNodeStatus->GetAllGroupIds();
        for (auto it = allGroupIds.begin(); it != allGroupIds.end(); ++it) {
            if (*it == groupId) {
                continue;
            }
            ret = ReleasePrefillInstances(*it);
            if (ret == 0) {
                LOG_I("[FaultManager] Release prefill instance in group %lu successfully.", *it);
                break;
            } else {
                LOG_W("[FaultManager] Release prefill instance in group %lu failed.", *it);
            }
        }
    }
    return ret;
}

// 适配P实例分布式，杀掉一个P实例中的所有NodeInfo
bool FaultManager::ReleaseDpGroupPeersForNode(uint64_t groupId, std::unique_ptr<NodeInfo> node,
    std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group)
{
    if (node == nullptr) {
        LOG_E("[%s] [FaultManager] Get node failed.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str());
        return false;
    }
    uint64_t ExpiredPrefillNodeId = std::numeric_limits<uint64_t>::max();
    // 获取同一个P实例的所有NodeInfo
    std::vector<uint64_t> dpGroupPeers = node->dpGroupPeers;
    for (const auto &peerId : dpGroupPeers) {
        auto peerNode = mNodeStatus->GetNode(peerId);
        if (peerNode == nullptr) {
            LOG_E("[%s] [FaultManager] Get node failed.",
                GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str());
            continue;
        }
        auto ret = ServerRequestHandler::GetInstance()->TerminateService(*mReleaseInstanceClient, *peerNode);
        if (ret == 0) {
            LOG_I("[FaultManager] Release group %zu's prefill instance node %lu successfully.", groupId, peerId);
            ExpiredPrefillNodeId = node->instanceInfo.staticInfo.id;
            break;
        } else {
            LOG_W("[FaultManager] Release group %zu's prefill instance node %lu failed.", groupId, peerId);
        }
    }
    if (ExpiredPrefillNodeId != std::numeric_limits<uint64_t>::max()) {
        // 释放掉P实例后，更新controller中的实例信息
        for (const auto &peerId : dpGroupPeers) {
            mNodeStatus->AddExpiredNode(peerId);
            mNodeStatus->UpdateNodeDeleteTime(peerId);
            group.first.erase(std::remove(group.first.begin(), group.first.end(), peerId), group.first.end());
            mNodeStatus->UpdateGroup(groupId, group);
        }
        return true;
    }
    return false;
}

// 杀死一个P实例，给D实例腾出资源
int32_t FaultManager::ReleasePrefillInstances(uint64_t groupId)
{
    auto group = mNodeStatus->GetGroup(groupId);
    // Only release prefill instance when the group has more than one prefill instance
    if (GetGroupActivePrefillInstanceCnt(groupId) <= 1) {
        LOG_W("[FaultManager] Group %lu doesn't have more than 1 prefill instance, should not release prefill instance "
              "from this group.",
              groupId);
        return -1;
    }
    LOG_I("[FaultManager] Releasing prefill instances for group %zu...", groupId);

    bool isReleasePInstanceSuccess = false;
    for (auto &pNodeId : group.first) {
        auto node = mNodeStatus->GetNode(pNodeId);
        isReleasePInstanceSuccess = ReleaseDpGroupPeersForNode(groupId, std::move(node), group);
        if (isReleasePInstanceSuccess) {
            break;
        }
    }
    
    if (!isReleasePInstanceSuccess) {
        LOG_E("[%s] [FaultManager] Release prefill instance failed because all prefill instances node release failed.",
              GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::FAULT_MANAGER).c_str());
        return -1;
    }
    return 0;
}

int32_t FaultManager::GetGroupActivePrefillInstanceCnt(uint64_t groupId)
{
    int32_t activePrefillCnt = 0;
    auto group = mNodeStatus->GetGroup(groupId);
    if (group.first.empty()) {
        LOG_W("[FaultManager] Group %lu has no prefill instance.", groupId);
        return activePrefillCnt;
    }
    // 每个P实例的NodeInfo数目，适配P实例为分布式
    uint32_t nodeInfoNumPerInstance = 1;
    for (const auto &pNodeId : group.first) {
        auto node = mNodeStatus->GetNode(pNodeId);
        if (node == nullptr) {
            LOG_E("[%s] [FaultManager] Get node failed.",
                  GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::FAULT_MANAGER).c_str());
            continue;
        }
        nodeInfoNumPerInstance = node->dpGroupPeers.size();
        // When Decode instance is greater than 1, we use activePeers to determine whether this prefill instance is
        // active. We use peers number to confirm if there is only 1 Decode instance, if only 1 Decode instance in
        // cluster, we must release prefill, even if it has no active.
        if (!node->activePeers.empty() || node->peers.empty()) {
            activePrefillCnt++;
        }
    }
    
    // 计算出当前有多少个P实例是活跃的
    if (nodeInfoNumPerInstance == 0) {
        LOG_E("[%s] [FaultManager] Get group %lu's active prefill instance count failed because nodeInfoNumPerInstance "
              "is 0.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::FAULT_MANAGER).c_str(), groupId);
        return 0;
    }
    activePrefillCnt /= nodeInfoNumPerInstance;
    LOG_I("[FaultManager] Group %lu has %d active prefill instances.", groupId, activePrefillCnt);
    return activePrefillCnt;
}

// #################################################
// ############ scaling out functions ##############
// #################################################
void FaultManager::ProcessScaleOutInSingleMode(std::vector<std::unique_ptr<NodeInfo>> &serverNodes,
                                               const NodeChanges &nodeChanges)
{
    if (nodeChanges.newIDs.empty() && nodeChanges.reappearIDs.empty()) {
        return;
    }
    std::vector<uint64_t> availableNodeIDs = nodeChanges.reappearIDs;
    std::copy(nodeChanges.newIDs.begin(), nodeChanges.newIDs.end(), std::back_inserter(availableNodeIDs));
    for (auto &node : serverNodes) {
        if (std::find(availableNodeIDs.begin(), availableNodeIDs.end(), node->instanceInfo.staticInfo.id) ==
            availableNodeIDs.end()) {
            continue;
        }
        bool isReady = false;
        if (ServerRequestHandler::GetInstance()->QueryInstanceInfo(*mStatusQueryClient, *node) != 0 ||
            ServerRequestHandler::GetInstance()->UpdateNodeInfo(*mStatusQueryClient, *node, false, isReady) != 0) {
            continue;
        }
        node->instanceInfo.staticInfo.totalSlotsNum = node->instanceInfo.dynamicInfo.availSlotsNum;
        node->instanceInfo.staticInfo.totalBlockNum = node->instanceInfo.dynamicInfo.availBlockNum;
        mNodeStatus->AddNode(std::move(node));
    }
}

void FaultManager::ProcessScaleOut(std::vector<std::unique_ptr<NodeInfo>> &serverNodes, const NodeChanges &nodeChanges)
{
    LOG_I("[FaultManager] Scale out operation: detected %zu new nodes.", nodeChanges.newIDs.size());
    if (mDeployMode != DeployMode::PD_SEPARATE) {
        ProcessScaleOutInSingleMode(serverNodes, nodeChanges);
        return;
    }
    TryStopTimer(nodeChanges.newIDs);
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    if (FilterAvailableServers(serverNodes, nodeChanges.newIDs, availableServers) == -1) {
        LOG_I("[FaultManager] Scale out operation interrupted: global ranktable changed.");
        return;
    }
    std::vector<uint64_t> groupIds = mNodeStatus->GetAllGroupIds();
    std::map<uint64_t, VECTOR_PAIR_ID_ROLE> changedGroups;
    for (auto &server : availableServers) {
        uint64_t selectGroupId = 0;
        if (server->serverInfoList[0].superPodId.has_value()) { // A3
            selectGroupId = AddInstance2GroupA3(*server, groupIds);
        } else { // A2
            selectGroupId = AddInstance2GroupA2(*server, groupIds);
        }
        if (selectGroupId == NOT_SCALE_OUT) {
            LOG_W("[%s] [FaultManager] Add instance to group failed, instance %zu will not be scaled out.",
                  GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::FAULT_MANAGER).c_str(),
                  server->instanceInfo.staticInfo.id);
            ServerRequestHandler::GetInstance()->TerminateService(*mAbortSetupClient, *server);
            mNodeStatus->AddExpiredNode(server->instanceInfo.staticInfo.id);
            continue;
        }
        if (ControllerConfig::GetInstance()->IsMultiNodeMode() &&
            DpGroupingUtil::ProcessSingleNodeDpGrouping(server) != 0) {
            LOG_E("[%s] [FaultManager] Allocate dp group for instance failed using DpGroupingUtil.",
                  GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::FAULT_MANAGER).c_str());
            continue;
        }
        if (changedGroups.find(selectGroupId) == changedGroups.end()) {
            changedGroups[selectGroupId] = VECTOR_PAIR_ID_ROLE();
        }
        changedGroups[selectGroupId].push_back(
            std::make_pair(server->instanceInfo.staticInfo.id, server->instanceInfo.staticInfo.role));
        mNodeStatus->AddNode(std::move(server));
    }
    ScalingUpdateAllGroups(changedGroups, ScalingMode::SCALE_OUT);
}

void FaultManager::TryStopTimer(const std::vector<uint64_t> &newNodeIds)
{
    if (mHardwareFaultyNodes.empty()) {
        LOG_I("[FaultManager] Scale-out operation: no faulty nodes detected, no need to stop timer.");
        return;
    }
    uint64_t totalFaultyDecodeNum = 0;
    for (const auto &info : mFaultyGroupInstanceInfo) {
        totalFaultyDecodeNum += info.second;
    }
    if (newNodeIds.size() >= totalFaultyDecodeNum) {
        LOG_I("[FaultManager] Scale-out: has enough new nodes to recovery all decode instances, stop timer.");
        {
            std::lock_guard<std::mutex> lock(mFaultyGroupInstanceInfoMutex);
            mFaultyGroupInstanceInfo.clear();
            mScaleInTimerMode = 0; // Reset the scale-in timer mode
        }
        StopTimer();
    } else {
        LOG_I("[FaultManager] Scale-out: does not have enough new nodes to recovery all decode instances, "
              "timer is still running.");
    }
}

void FaultManager::StopTimer()
{
    if (mInstanceGroupTimer != nullptr && mInstanceGroupTimer->IsActive()) {
        mInstanceGroupTimer->Stop();
        LOG_I("[FaultManager] Timer stopped successfully.");
    } else {
        LOG_D("[FaultManager] Timer is not started.");
    }
}

int64_t FaultManager::SelectBestGroup(MINDIE::MS::DIGSInstanceRole role, const std::vector<uint64_t> &allGroupIds)
{
    // Select the group with the least number of prefill or decode instances
    uint64_t leastPrefillNum = UINT64_MAX;
    uint64_t leastDecodeNum = UINT64_MAX;
    uint64_t selectGroupId = 0;
    for (uint64_t groupId : allGroupIds) {
        auto group = mNodeStatus->GetGroup(groupId);
        if (role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            if (group.first.size() < leastPrefillNum) {
                leastPrefillNum = group.second.size();
                selectGroupId = groupId;
            }
        } else if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            if (group.second.size() < leastDecodeNum) {
                leastDecodeNum = group.first.size();
                selectGroupId = groupId;
            }
        }
    }
    return selectGroupId;
}

uint64_t FaultManager::AddInstance2GroupA2(NodeInfo &instance, std::vector<uint64_t> &allGroupIds)
{
    // Only support PD separate mode
    uint64_t instanceId = instance.instanceInfo.staticInfo.id;
    MINDIE::MS::DIGSInstanceRole role = instance.instanceInfo.staticInfo.role;
    instance.instanceInfo.staticInfo.groupId = SelectBestGroup(role, allGroupIds);
    uint64_t groupId = instance.instanceInfo.staticInfo.groupId;
    auto group = mNodeStatus->GetGroup(groupId);
    LOG_I("[FaultManager] A2: Add %c instance %zu to group %zu.", role, instanceId, groupId);
    if (role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        instance.peers = group.second;
        instance.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC;
    } else if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        instance.peers = group.first;
        instance.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::DECODE_STATIC;
    } else { // UNDEF_ROLE, this is for undistributed mode. for now we should not reach here.
        instance.instanceInfo.staticInfo.role = MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
        instance.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC;
        instance.peers = group.second;
    }
    LOG_I("[FaultManager] A2: assign %c role to instance. peers number: %zu", instance.instanceInfo.staticInfo.role,
          instance.peers.size());
    if (group.first.empty() && group.second.empty()) {
        LOG_W("[FaultManager] A2: group %zu has no peers, instance %zu should be scaled out.", groupId, instanceId);
        return groupId;
    } else {
        return instance.peers.empty() ? NOT_SCALE_OUT : groupId;
    }
}

uint64_t FaultManager::AddInstance2GroupA3(NodeInfo &instance, std::vector<uint64_t> &allGroupIds)
{
    // Only support PD separate mode, Use super pod id as group id
    uint64_t instanceId = instance.instanceInfo.staticInfo.id;
    MINDIE::MS::DIGSInstanceRole role = instance.instanceInfo.staticInfo.role;
    instance.instanceInfo.staticInfo.groupId = SelectBestGroup(role, allGroupIds);
    uint64_t groupId = instance.instanceInfo.staticInfo.groupId;
    LOG_I("[FaultManager] A3: Add %c instance %zu to group %zu.", role, instanceId, groupId);
    auto group = mNodeStatus->GetGroup(groupId);
    std::string roleName;
    if (role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        instance.peers = group.second;
        instance.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC;
    } else if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        instance.peers = group.first;
        instance.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::DECODE_STATIC;
    } else { // UNDEF_ROLE
        instance.instanceInfo.staticInfo.role = MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
        instance.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC;
        instance.peers = group.second;
    }
    LOG_I("[FaultManager] A3: assign %c role to instance. peers number: %zu", instance.instanceInfo.staticInfo.role,
          instance.peers.size());
    if (group.first.empty() && group.second.empty()) {
        LOG_W("[FaultManager] A3: group %zu has no peers, instance %zu should be scaled out.", groupId, instanceId);
        return groupId;
    } else {
        return instance.peers.empty() ? NOT_SCALE_OUT : groupId;
    }
}

bool CheckStaticElasticTemplate(nlohmann::json &staticElasticTemplate)
{
    if (!staticElasticTemplate.contains("elastic_scaling_list")) {
        return false;
    }
    if (staticElasticTemplate["elastic_scaling_list"].size() == 0) {
        return false;
    }
    if (!staticElasticTemplate["elastic_scaling_list"][0].contains("group_list")) {
        return false;
    }
    if (staticElasticTemplate["elastic_scaling_list"][0]["group_list"].size() <= 1) {
        return false;
    }
    if (!staticElasticTemplate["elastic_scaling_list"][0]["group_list"][0].contains("group_num") ||
        !staticElasticTemplate["elastic_scaling_list"][0]["group_list"][1].contains("group_num")) {
        return false;
    }
    return true;
}

STATIC_ELASTIC_PD_CNT FaultManager::GetStaticElasticPdCnt()
{
    int32_t prefillCnt = std::numeric_limits<int32_t>::min();
    int32_t decodeCnt = std::numeric_limits<int32_t>::min();

    std::string staticElasticTemplatePath = ControllerConfig::GetInstance()->GetStaticElasticTemplatePath();
    if (staticElasticTemplatePath.empty()) {
        LOG_E("[FaultManager] Static elastic template path is empty.");
        return std::pair<int32_t, int32_t>(prefillCnt, decodeCnt);
    }

    uint32_t mode = (ControllerConfig::GetInstance()->GetCheckMountedFiles()) ? 0640 : 0777; // 权限要求是0640, 不校验是0777
    auto staticElasticTemplate = FileToJsonObj(staticElasticTemplatePath, mode,
        (ControllerConfig::GetInstance()->GetCheckMountedFiles()));
    if (staticElasticTemplate.empty()) {
        LOG_E("[FaultManager] Get static elastic template failed from %s.", staticElasticTemplatePath);
        return std::pair<int32_t, int32_t>(prefillCnt, decodeCnt);
    }

    if (!CheckStaticElasticTemplate(staticElasticTemplate)) {
        LOG_E("[FaultManager] Static elastic template is invalid, missing required fields.");
        return std::pair<int32_t, int32_t>(prefillCnt, decodeCnt);
    }

    try {
        prefillCnt = std::stoi(
            staticElasticTemplate["elastic_scaling_list"][0]["group_list"][0]["group_num"].get<std::string>());
        decodeCnt = std::stoi(
            staticElasticTemplate["elastic_scaling_list"][0]["group_list"][1]["group_num"].get<std::string>());
    } catch (const std::out_of_range& e) {
        LOG_E("Value out of range: %s", e.what());
        return std::pair<int32_t, int32_t>(prefillCnt, decodeCnt);
    }
    LOG_I("[FaultManager] Get static elastic pd cnt from template, prefill cnt: %d, decode cnt: %d.",
        prefillCnt, decodeCnt);

    return std::pair<int32_t, int32_t>(prefillCnt, decodeCnt);
}

GRT_PD_CNT FaultManager::GetGRTPdCnt()
{
    int32_t prefillCnt = std::numeric_limits<int32_t>::min();
    int32_t decodeCnt = std::numeric_limits<int32_t>::min();

    if (mRankTableLoader == nullptr) {
        LOG_E("[FaultManager] Get GRT PD cnt failed because ranktable loader is nullptr.");
        return std::pair<int32_t, int32_t>(prefillCnt, decodeCnt);
    }

    std::vector<std::unique_ptr<InstanceInfo>> instanceInfos = mRankTableLoader->GetInstanceInfoListByRankTable();
    if (instanceInfos.empty()) {
        LOG_E("[FaultManager] Get GRT PD cnt failed because instance info list is empty.");
        return std::pair<int32_t, int32_t>(prefillCnt, decodeCnt);
    }

    prefillCnt = decodeCnt = 0;
    for (const auto &instanceInfo : instanceInfos) {
        if (instanceInfo->role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            prefillCnt++;
        } else if (instanceInfo->role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            decodeCnt++;
        }
    }

    LOG_I("[FaultManager] Get GRT PD cnt from ranktable, prefill cnt: %d, decode cnt: %d.", prefillCnt, decodeCnt);

    return std::pair<int32_t, int32_t>(prefillCnt, decodeCnt);
}

void FaultManager::SetRankTableLoader(std::shared_ptr<RankTableLoader> loader)
{
    if (loader != nullptr) {
        mRankTableLoader = loader;
    } else {
        LOG_E("[FaultManager] SetRankTableLoader failed, ranktable loader is nullptr");
    }
}

std::shared_ptr<RankTableLoader> FaultManager::GetRankTableLoader() const
{
    if (mRankTableLoader != nullptr) {
        return mRankTableLoader;
    } else {
        LOG_E("[FaultManager] GetRankTableLoader failed, ranktable loader is nullptr");
        return nullptr;
    }
}

} // namespace MINDIE::MS
