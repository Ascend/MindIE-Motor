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
#include "RoleSwitcher.h"
#include <thread>
#include <algorithm>
#include <cinttypes>
#include "ControllerConstant.h"
#include "ControllerConfig.h"
#include "ServerRequestHandler.h"
#include "CoordinatorRequestHandler.h"
#include "Logger.h"
#include "Util.h"
namespace MINDIE::MS {
constexpr int32_t CODE_OK = 200;
constexpr int64_t MIN_TASKS_NUMBER = -1;
constexpr int64_t MAX_TASKS_NUMBER = 2147483647;
RoleSwitcher::RoleSwitcher(std::shared_ptr<NodeStatus> nodeStatusInit,
    std::shared_ptr<CoordinatorStore> coordinatorStoreInit) : mNodeStatus(nodeStatusInit),
    mCoordinatorStore(coordinatorStoreInit)
{
    LOG_I("[RoleSwitcher] Create successfully.");
}

RoleSwitcher::~RoleSwitcher()
{
    LOG_I("[RoleSwitcher] Destroy successfully.");
}

int32_t RoleSwitcher::Init()
{
    try {
        mServerClient = std::make_shared<HttpClient>();
        mCoordinatorClient = std::make_shared<HttpClient>();
    } catch (const std::exception& e) {
        LOG_E("[%s] [RoleSwitcher] Failed to create HTTP client pointers.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::ROLE_SWITCHER).c_str());
        return -1;
    }
    if (mServerClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0 ||
        mCoordinatorClient->Init("", "", ControllerConfig::GetInstance()->GetRequestCoordinatorTlsItems()) != 0) {
        LOG_E("[%s] [RoleSwitcher] Initialize server client or coordinator client failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ROLE_SWITCHER).c_str());
        return -1;
    }
    return 0;
}

NodeInfo RoleSwitcher::BuildNodeInfo(uint64_t groupId, MINDIE::MS::DIGSInstanceRole role,
                                     const std::vector<uint64_t> &peers)
{
    NodeInfo nodeInfo;
    if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        nodeInfo.instanceInfo.staticInfo.role = MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE;
        nodeInfo.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::DECODE_STATIC;
    } else {
        nodeInfo.instanceInfo.staticInfo.role = MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
        nodeInfo.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC;
    }
    nodeInfo.roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::SWITCHING);
    nodeInfo.peers = peers;
    auto flexGroup = mNodeStatus->GetFlexGroup(groupId);
    if (!flexGroup.empty()) {
        nodeInfo.peers.insert(nodeInfo.peers.end(), flexGroup.begin(), flexGroup.end());
    }
    nodeInfo.isHealthy = false;
    nodeInfo.isInitialized = false;
    return nodeInfo;
}

bool IsPSwitchToD(const NodeInfo &node, const MINDIE::MS::DIGSRoleDecision &decision)
{
    return node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE &&
           decision.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE;
}

bool IsDSwitchToP(const NodeInfo &node, const MINDIE::MS::DIGSRoleDecision &decision)
{
    return node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE &&
           decision.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
}

void RoleSwitcher::ProcessSingleRoleSwitching(MINDIE::MS::DIGSRoleDecision &decision)
{
    auto node = mNodeStatus->GetNode(decision.id);
    if (node == nullptr) {
        LOG_E("[%s] [RoleSwitcher] Node %lu not found during role switching.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::ROLE_SWITCHER).c_str(),
            decision.id);
        return;
    }
    if (!node->isHealthy) {
        LOG_W("[%s] [RoleSwitcher] Ignoring unhealthy node ID %lu in group ID %lu.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::ROLE_SWITCHER).c_str(),
            decision.groupId, decision.id);
        return;
    };
    if (decision.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
        UpdateFlexNodeToServer(decision);
        return;
    }
    if (node->instanceInfo.staticInfo.role == decision.role) {
        LOG_I("[RoleSwitcher] Node %lu has already been set to role %c.",
              decision.id, decision.role);
        return;
    }
    auto group = mNodeStatus->GetGroup(decision.groupId);
    if (group.first.empty() && group.second.empty()) {
        LOG_E("[%s] [RoleSwitcher] Group ID %lu is not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::ROLE_SWITCHER).c_str(),
            decision.groupId);
        return;
    }
    RoleSwitchingInfo info;
    info.oldP = group.first;
    info.oldD = group.second;
    if (IsPSwitchToD(*node, decision)) {
        LOG_I("[RoleSwitcher] Starting P-to-D role switch for node %lu in group %lu.", decision.id, decision.groupId);
        ProcessPSwitchToD(decision.groupId, info, decision.id);
        LOG_I("[RoleSwitcher] Completed P-to-D role switch for node %lu in group %lu.", decision.id, decision.groupId);
        return;
    } else if (IsDSwitchToP(*node, decision)) {
        LOG_I("[RoleSwitcher] Starting D-to-P role switch for node %lu in group %lu.", decision.id, decision.groupId);
        ProcessDSwitchToP(decision.groupId, info, decision.id);
        LOG_I("[RoleSwitcher] Completed D-to-P role switch for node %lu in group %lu.", decision.id, decision.groupId);
        return;
    }
}

void RoleSwitcher::UpdateAbnormalRoleWhenRecoverCluster(NodeInfo &node,
    std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group,
    std::vector<std::unique_ptr<NodeInfo>> &nodesInGroup) const
{
    if (ServerRequestHandler::GetInstance()->IsUpdatePToDNeeded(node)) {
        LOG_I("[RoleSwitcher] Updating abnormal role: switching P-to-D for node %lu in group %lu.",
            node.instanceInfo.staticInfo.id, node.instanceInfo.staticInfo.groupId);
        UpdatePToDWhenRecoverCluster(group, node, nodesInGroup);
        LOG_I("[RoleSwitcher] Completed P-to-D update for node %lu in group %lu.",
            node.instanceInfo.staticInfo.id, node.instanceInfo.staticInfo.groupId);
    } else if (ServerRequestHandler::GetInstance()->IsUpdateDToPNeeded(node)) {
        LOG_I("[RoleSwitcher] Updating abnormal role: switching D-to-P for node %lu in group %lu.",
            node.instanceInfo.staticInfo.id, node.instanceInfo.staticInfo.groupId);
        UpdateDToPWhenRecoverCluster(group, node, nodesInGroup);
        LOG_I("[RoleSwitcher] Completed D-to-P update for node %lu in group %lu.",
            node.instanceInfo.staticInfo.id, node.instanceInfo.staticInfo.groupId);
    }
    return;
}

static void FillNodeInfo(NodeInfo &nodeInfo, MINDIE::MS::DIGSInstanceRole role, const std::vector<uint64_t> &peers)
{
    if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        nodeInfo.instanceInfo.staticInfo.role = MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE;
        nodeInfo.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::DECODE_STATIC;
    } else {
        nodeInfo.instanceInfo.staticInfo.role = MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
        nodeInfo.instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC;
    }
    nodeInfo.roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN);
    nodeInfo.peers = peers;
    nodeInfo.isHealthy = true;
    nodeInfo.isInitialized = true;
    return;
}

void RoleSwitcher::DNodeUpdatePeers(uint64_t id, std::vector<std::unique_ptr<NodeInfo>> &nodesInGroup,
    const std::vector<uint64_t> &pNodes) const
{
    auto it = std::find_if(nodesInGroup.begin(), nodesInGroup.end(), [&id](const std::unique_ptr<NodeInfo> &obj) {
        return (obj != nullptr) && (obj->instanceInfo.staticInfo.id == id);
    });
    if (it == nodesInGroup.end()) {
        LOG_E("[%s] [RoleSwitcher] Node ID %lu is not found in group vector.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::ROLE_SWITCHER).c_str(),
            id);
        return;
    }
    uint32_t serverIndex = static_cast<uint32_t>(std::distance(nodesInGroup.begin(), it));
    nodesInGroup[serverIndex]->peers = pNodes;
    auto groupId = (*it)->instanceInfo.staticInfo.groupId;
    auto flexGroup = mNodeStatus->GetFlexGroup(groupId);
    if (!flexGroup.empty()) {
        nodesInGroup[serverIndex]->peers.insert(nodesInGroup[serverIndex]->peers.end(), flexGroup.begin(),
                                                flexGroup.end());
    }
}

void RoleSwitcher::PNodeUpdatePeers(uint64_t id, std::vector<std::unique_ptr<NodeInfo>> &nodesInGroup,
                                    const std::vector<uint64_t> &dNodes) const
{
    auto it = std::find_if(nodesInGroup.begin(), nodesInGroup.end(), [&id](const std::unique_ptr<NodeInfo> &obj) {
        return (obj != nullptr) && (obj->instanceInfo.staticInfo.id == id);
    });
    if (it == nodesInGroup.end()) {
        LOG_E("[%s] [RoleSwitcher] Node ID %lu is not found in group vector.",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::ROLE_SWITCHER).c_str(), id);
        return;
    }
    uint32_t serverIndex = static_cast<uint32_t>(std::distance(nodesInGroup.begin(), it));
    nodesInGroup[serverIndex]->peers = dNodes;
    auto groupId = (*it)->instanceInfo.staticInfo.groupId;
    auto flexGroup = mNodeStatus->GetFlexGroup(groupId);
    if (!flexGroup.empty()) {
        nodesInGroup[serverIndex]->peers.insert(nodesInGroup[serverIndex]->peers.end(), flexGroup.begin(),
                                                flexGroup.end());
    }
}

void RoleSwitcher::UpdatePToDWhenRecoverCluster(std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group,
    NodeInfo &newD, std::vector<std::unique_ptr<NodeInfo>> &nodesInGroup) const
{
    std::vector<uint64_t> pNodes = group.first;
    pNodes.erase(std::remove_if(pNodes.begin(), pNodes.end(),
        [&](const uint64_t &id) { return id == newD.instanceInfo.staticInfo.id; }), pNodes.end());
    std::vector<uint64_t> dNodes = group.second;
    if (std::find(dNodes.begin(), dNodes.end(), newD.instanceInfo.staticInfo.id) == dNodes.end()) {
        dNodes.push_back(newD.instanceInfo.staticInfo.id);
    }
    for (auto &id : group.second) { // 原始的D不需要修改状态，只需要删除旧的p，修改连接的peers
        DNodeUpdatePeers(id, nodesInGroup, pNodes);
    }
    FillNodeInfo(newD, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, pNodes);
    auto groupId = newD.instanceInfo.staticInfo.groupId;
    auto flexGroup = mNodeStatus->GetFlexGroup(groupId);
    newD.peers.insert(newD.peers.end(), flexGroup.begin(), flexGroup.end());
    for (auto &id: pNodes) { // 原始的P不需要修改状态，只需要修改连接的peers，增加新的d
        PNodeUpdatePeers(id, nodesInGroup, dNodes);
    }
    group.first = pNodes;
    group.second = dNodes; // 更新group信息
    LOG_I("[RoleSwitcher] P-to-D role switch completed for node %lu, with role %c.",
          newD.instanceInfo.staticInfo.id, newD.instanceInfo.staticInfo.role);
}

void RoleSwitcher::UpdateDToPWhenRecoverCluster(std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group,
    NodeInfo &newP, std::vector<std::unique_ptr<NodeInfo>> &nodesInGroup) const
{
    std::vector<uint64_t> dNodes = group.second;
    dNodes.erase(std::remove_if(dNodes.begin(), dNodes.end(),
        [&](const uint64_t &id) { return id == newP.instanceInfo.staticInfo.id; }), dNodes.end());
    std::vector<uint64_t> pNodes = group.first;
    if (std::find(pNodes.begin(), pNodes.end(), newP.instanceInfo.staticInfo.id) == pNodes.end()) {
        pNodes.push_back(newP.instanceInfo.staticInfo.id);
    }
    std::vector<uint64_t> success;
    FillNodeInfo(newP, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, dNodes);
    auto groupId = newP.instanceInfo.staticInfo.groupId;
    auto flexGroup = mNodeStatus->GetFlexGroup(groupId);
    newP.peers.insert(newP.peers.end(), flexGroup.begin(), flexGroup.end());
    for (auto &id: group.first) { // 旧的P清理peers
        PNodeUpdatePeers(id, nodesInGroup, dNodes);
    }
    for (auto &id: dNodes) { // 旧的D增加peers
        DNodeUpdatePeers(id, nodesInGroup, pNodes);
    }
    group.first = pNodes;
    group.second = dNodes; // 更新group信息
    LOG_I("[RoleSwitcher] D-to-P role switch completed for node %lu, with role %c.", newP.instanceInfo.staticInfo.id,
          newP.instanceInfo.staticInfo.role);
}

int32_t RoleSwitcher::OldInstanceOffline(uint64_t id)
{
    if (PostInstanceOffline(id) != 0) {
        LOG_E("[%s] [RoleSwitcher] Old instance offline failed, node ID is %lu.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ROLE_SWITCHER).c_str(),
            id);
        return -1;
    }
    auto tasksEndWaitSeconds = ControllerConfig::GetInstance()->GetTasksEndWaitSeconds();
    if (QueryInstanceTasks(id, tasksEndWaitSeconds) != 0) {
        LOG_E("[%s] [RoleSwitcher] Task unfinished, node id %lu",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ROLE_SWITCHER).c_str(),
            id);
        PostInstanceOnline(id);
        CoordinatorRequestHandler::GetInstance()->SendNodeStatus(mCoordinatorClient, mNodeStatus, mCoordinatorStore);
        LOG_I("[RoleSwitcher] Finish to recover node ID %lu", id);
        return -1;
    };
    return 0;
}

void RoleSwitcher::CheckStatusThenSynchronizeCluster(const std::vector<uint64_t> &success)
{
    ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, success);
    CoordinatorRequestHandler::GetInstance()->SendNodeStatus(mCoordinatorClient, mNodeStatus, mCoordinatorStore);
}

static void GetTargetPNodesAndDNodesWhenPSwitchToD(std::vector<uint64_t> &pNodes, std::vector<uint64_t> &dNodes,
    const RoleSwitchingInfo &roleSwitchingInfo, uint64_t newD)
{
    pNodes = roleSwitchingInfo.oldP;
    pNodes.erase(std::remove_if(pNodes.begin(), pNodes.end(),
        [&](const uint64_t &id) { return id == newD; }), pNodes.end());
    dNodes = roleSwitchingInfo.oldD;
    dNodes.push_back(newD);
}

void RoleSwitcher::UpdateFlexNodeToServer(MINDIE::MS::DIGSRoleDecision &decision)
{
    std::string logCode = GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ROLE_SWITCHER);
    auto nodeInfo = mNodeStatus->GetNode(decision.id);
    if (nodeInfo == nullptr) {
        return;
    }
    uint64_t id = decision.id;
    if ((nodeInfo->instanceInfo.staticInfo.flexPRatio != 0 && decision.flexPRatio == 0) ||
        (nodeInfo->instanceInfo.staticInfo.flexPRatio != MINDIE::MS::FLEX_INSTANCE_P_PERCENTAGE_MAX &&
         decision.flexPRatio == MINDIE::MS::FLEX_INSTANCE_P_PERCENTAGE_MAX)) {
        // controller先通知server切换flex比例，再通知coordinator切换flex比例。
        // 这个时间差coordinator可能向FLEX发请求。因此需要先停业务。
        if (OldInstanceOffline(id) != 0) {
            LOG_E("[%s] [UpdateFlexNodeToServer]: instance offline failed, group id %lu, node id %lu", logCode.c_str(),
                  decision.groupId, id);
            return;
        }
        // 清理相关的Peers节点的任务
        std::vector<uint64_t> success;
        auto peers = nodeInfo->peers;
        QueryInstancePeersTasksFlex(id, peers, success);
        if (success.size() != peers.size()) {
            ProcessFlexAbnormal(id);
            return;
        }
        nodeInfo->isRoleChangeNode = true;
    }

    std::vector<uint64_t> ids{id};
    std::vector<uint64_t> success;
    nodeInfo->instanceInfo.staticInfo.role = decision.role;
    nodeInfo->instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::FLEX_STATIC;
    nodeInfo->instanceInfo.staticInfo.flexPRatio = decision.flexPRatio;
    nodeInfo->isHealthy = false;
    nodeInfo->isInitialized = false;
    mNodeStatus->UpdateNode(decision.id, *nodeInfo);
    ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, ids, success);
    if (ids.size() != success.size()) {
        LOG_E("[%s]UpdateFlexNodeToServer: Failed to update flex node, nodeId=%u, groupId=%lu", logCode.c_str(), id,
              decision.groupId);
    }
    success = {id};
    CheckStatusThenSynchronizeCluster(success);
    mNodeStatus->UpdateFlexGroup(decision.groupId, ids);
}

void RoleSwitcher::SendSwitchedPDRoles(const std::vector<uint64_t> &ids) const
{
    auto limits = ControllerConfig::GetInstance()->GetInitRoleAttemptTimes();
    uint32_t attempt = 0;
    std::vector<uint64_t> success;
    // 限制下发重试次数
    while (success.size() != ids.size() && attempt <= limits) {
        for (auto &id: ids) {
            // 跳过已经下发成功的节点
            if (std::find(success.begin(), success.end(), id) != success.end()) {
                continue;
            }
            // 下发节点
            if (ServerRequestHandler::GetInstance()->PostSingleRoleById(*mServerClient, *mNodeStatus, id) == 0) {
                success.push_back(id);
            }
        }

        if (success.size() != ids.size()) {
            sleep(5); // 休眠5s之后重试下发
        }
        attempt++;
    }
    if (success.size() != ids.size()) {
        LOG_E("[%s] [RoleSwitcher] Failed to dispatch some nodes after retried for %u times",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ROLE_SWITCHER), attempt);
    } else {
        LOG_I("[RoleSwitcher] Successfully sent role to all %lu nodes.", success.size());
    }

    // 检查&更新状态
    success = ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, success);
}

void RoleSwitcher::ProcessPSwitchToD(uint64_t groupId, const RoleSwitchingInfo &roleSwitchingInfo, uint64_t newD)
{
    std::vector<uint64_t> pNodes {};
    std::vector<uint64_t> dNodes {};
    GetTargetPNodesAndDNodesWhenPSwitchToD(pNodes, dNodes, roleSwitchingInfo, newD);
    std::string logCode = GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ROLE_SWITCHER);
    auto stateReady = ControllerConstant::GetInstance()->GetRoleState(RoleState::READY);

    // 将目标节点下线，不再接受请求并等待任务清零
    if (OldInstanceOffline(newD) != 0) {
        LOG_E("[%s] [ProcessPSwitchToD] Instance offline failed, group id %lu, node id %lu.",
              logCode.c_str(), groupId, newD);
        return;
    }

    // 等待旧的D节点与目标节点相关的任务清零
    for (auto &id : roleSwitchingInfo.oldD) {
        if (QueryInstancePeerTasks(newD, id, MINDIE::MS::DIGSRoleChangeType::DIGS_ROLE_CHANGE_P2D) != 0) {
            LOG_E("[%s] [ProcessPSwitchToD] Decode node ID %lu in group ID %lu failed to clear tasks.", logCode.c_str(),
                  id, groupId);
            // 若仍有与目标节点相关的任务未清零则重新上线目标节点
            ProcessPSwitchToDAbnormal(groupId, newD, roleSwitchingInfo);
            return;
        }
    }

    std::vector<uint64_t> ids;
    // 更新目标节点身份信息为D,并更新peers
    NodeInfo nodeInfo = BuildNodeInfo(groupId, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, pNodes);
    mNodeStatus->UpdateNode(newD, nodeInfo);
    ids.push_back(newD);

    // 更新旧的D节点peers
    for (auto &id: roleSwitchingInfo.oldD) {
        mNodeStatus->UpdateRoleStateAndPeers(groupId, id, stateReady, pNodes);
        ids.push_back(id);
    }

    // 更新旧的P节点peers
    for (auto &id: pNodes) {
        mNodeStatus->UpdateInferenceType(id, InferenceType::PREFILL_UPDATING_PEERS);
        mNodeStatus->UpdateRoleStateAndPeers(groupId, id, stateReady, dNodes);
        ids.push_back(id);
    }

    SendSwitchedPDRoles(ids);
    // 更新信息
    mNodeStatus->UpdateGroup(groupId, std::make_pair(pNodes, dNodes));
    CoordinatorRequestHandler::GetInstance()->SendNodeStatus(mCoordinatorClient, mNodeStatus, mCoordinatorStore);
}

void RoleSwitcher::ProcessPSwitchToDAbnormal(uint64_t groupId, uint64_t id, const RoleSwitchingInfo &roleSwitchingInfo)
{
    PostInstanceOnline(id);
    for (auto &dId: roleSwitchingInfo.oldD) { // 原始的D恢复原有连接
        mNodeStatus->UpdateRoleStateAndPeers(groupId, dId,
            ControllerConstant::GetInstance()->GetRoleState(RoleState::READY), roleSwitchingInfo.oldP);
    }
    CoordinatorRequestHandler::GetInstance()->SendNodeStatus(mCoordinatorClient, mNodeStatus, mCoordinatorStore);
}

void RoleSwitcher::ProcessFlexAbnormal(uint64_t id)
{
    PostInstanceOnline(id);
    CoordinatorRequestHandler::GetInstance()->SendNodeStatus(mCoordinatorClient, mNodeStatus, mCoordinatorStore);
}

void RoleSwitcher::UpdatePToDWhenAbnormal(uint64_t groupId, uint64_t id, const RoleSwitchingInfo &roleSwitchingInfo)
{
    NodeInfo nodeInfo = BuildNodeInfo(groupId, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, roleSwitchingInfo.oldP);
    nodeInfo.isInitialized = true;
    mNodeStatus->UpdateNode(id, nodeInfo);
}

void RoleSwitcher::UpdateDToPWhenAbnormal(uint64_t groupId, uint64_t id, const RoleSwitchingInfo &roleSwitchingInfo)
{
    NodeInfo nodeInfo = BuildNodeInfo(groupId, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, roleSwitchingInfo.oldD);
    nodeInfo.isInitialized = true;
    mNodeStatus->UpdateNode(id, nodeInfo);
}

void RoleSwitcher::QueryInstancePeersTasksFlex(uint64_t flexId, std::vector<uint64_t> peers,
                                               std::vector<uint64_t> &success)
{
    std::string code = GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ROLE_SWITCHER);
    for (uint64_t id : peers) {
        auto node = mNodeStatus->GetNode(id);
        if (node == nullptr) {
            continue;
        }
        if (node->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            if (QueryInstancePeerTasks(id, flexId, MINDIE::MS::DIGSRoleChangeType::DIGS_ROLE_CHANGE_D2P) != 0) {
                LOG_W("[%s]ProcessPSwitchToD: p node %lu, d node %lu still have tasks", code.c_str(), id, flexId);
                continue;
            }
        } else if (node->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            if (QueryInstancePeerTasks(flexId, id, MINDIE::MS::DIGSRoleChangeType::DIGS_ROLE_CHANGE_P2D) != 0) {
                LOG_W("[%s]ProcessPSwitchToD: p node %lu, d node %lu still have tasks", code.c_str(), flexId, id);
                continue;
            }
        }
        success.push_back(id);
    }
}

void RoleSwitcher::ProcessDSwitchToP(uint64_t groupId, const RoleSwitchingInfo &roleSwitchingInfo, uint64_t newP)
{
    std::vector<uint64_t> dNodes = roleSwitchingInfo.oldD;
    dNodes.erase(std::remove_if(dNodes.begin(), dNodes.end(),
        [&](const uint64_t &id) { return id == newP; }), dNodes.end());
    std::vector<uint64_t> pNodes = roleSwitchingInfo.oldP;
    pNodes.push_back(newP);
    std::string code = GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::ROLE_SWITCHER);
    if (OldInstanceOffline(newP) != 0) {
        LOG_E("[%s] [ProcessDSwitchToP] Instance offline failed, group ID %lu, node ID %lu.",
              code.c_str(), groupId, newP);
        return;
    }

    std::vector<uint64_t> ids;
    // 更新目标节点身份为P，且更新peers
    NodeInfo nodeInfo = BuildNodeInfo(groupId, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, dNodes);
    mNodeStatus->UpdateNode(newP, nodeInfo);
    ids.push_back(newP);

    // 更新P节点peers
    for (auto &id : roleSwitchingInfo.oldP) {
        mNodeStatus->UpdateInferenceType(id, InferenceType::PREFILL_UPDATING_PEERS);
        mNodeStatus->UpdateRoleStateAndPeers(groupId, id,
            ControllerConstant::GetInstance()->GetRoleState(RoleState::READY), dNodes);
        ids.push_back(id);
    }

    // 更新D节点peers
    for (auto &id : dNodes) { // 更新D节点的peers信息
        mNodeStatus->UpdateRoleStateAndPeers(groupId, id,
            ControllerConstant::GetInstance()->GetRoleState(RoleState::READY), pNodes);
        ids.push_back(id);
    }

    SendSwitchedPDRoles(ids);
    // 更新信息
    mNodeStatus->UpdateGroup(groupId, std::make_pair(pNodes, dNodes));
    CoordinatorRequestHandler::GetInstance()->SendNodeStatus(mCoordinatorClient, mNodeStatus, mCoordinatorStore);
}

static std::string BuildPostInstanceOfflineBody(const std::vector<uint64_t> &ids)
{
    nlohmann::json body;
    body["ids"] = nlohmann::json::array();
    for (auto &id: std::as_const(ids)) {
        body["ids"].emplace_back(id);
    }
    return body.dump();
}

int32_t RoleSwitcher::PostInstanceOffline(uint64_t id)
{
    int32_t httpRet = -1;
    std::vector<uint64_t> ids {id};
    std::vector<std::unique_ptr<Coordinator>> coordinatorNodes;
    try {
        coordinatorNodes = mCoordinatorStore->GetCoordinators();
    } catch (const std::exception& e) {
        LOG_E("[%s] [RoleSwitcher] Failed to get coordinators for taking instance %" PRIu64 " offline.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ROLE_SWITCHER).c_str(), id);
        return -1;
    }
    auto port = ControllerConfig::GetInstance()->GetCoordinatorPort();
    for (auto &node: std::as_const(coordinatorNodes)) {
        std::string response;
        int32_t code = 400;
        std::string jsonString = BuildPostInstanceOfflineBody(ids);
        std::map <boost::beast::http::field, std::string> map;
        map[boost::beast::http::field::accept] = "*/*";
        map[boost::beast::http::field::content_type] = "application/json";
        LOG_M("[Update] Posting instance offline for node IP %s with request: %s.", node->ip.c_str(),
            jsonString.c_str());
        mCoordinatorClient->SetHostAndPort(node->ip, port);
        Request req = {ControllerConstant::GetInstance()->GetCoordinatorURI(CoordinatorURI::POST_OFFLINE),
            boost::beast::http::verb::post, map, jsonString};
        httpRet = mCoordinatorClient->SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
            ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
        if (httpRet != 0 || code != CODE_OK) {
            LOG_E("[%s] [RoleSwitcher] Post instance offline failed, node IP %s, "
                  "port %s, ret code %d, send request ret %d.",
                  GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::ROLE_SWITCHER).c_str(),
                  node->ip.c_str(), port.c_str(), code, httpRet);
            mCoordinatorStore->UpdateCoordinatorStatus(node->ip, false);
            return -1;
        }
        mCoordinatorStore->UpdateCoordinatorStatus(node->ip, true);
        return 0;
    }
    return -1;
}

static std::string BuildPostInstanceOnlineBody(const NodeInfo &nodeInfo)
{
    nlohmann::json nodes;
    nodes["ids"] = nlohmann::json::array();
    nodes["ids"].emplace_back(nodeInfo.instanceInfo.staticInfo.id);
    LOG_I("[RoleSwitcher] Building online body for node ID %lu with role %c.", nodeInfo.instanceInfo.staticInfo.id,
          nodeInfo.instanceInfo.staticInfo.role);
    return nodes.dump();
}

void RoleSwitcher::PostInstanceOnline(uint64_t id)
{
    int32_t httpRet = -1;
    std::vector<std::unique_ptr<Coordinator>> coordinatorNodes;
    try {
        coordinatorNodes = mCoordinatorStore->GetCoordinators();
    } catch (const std::exception& e) {
        LOG_E("[%s] [RoleSwitcher] Failed to get coordinators for posting instance online.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ROLE_SWITCHER).c_str());
        return;
    }
    auto port = ControllerConfig::GetInstance()->GetCoordinatorPort();
    for (auto &node: std::as_const(coordinatorNodes)) {
        std::string response;
        int32_t code = 400;
        auto nodeInfoPtr = mNodeStatus->GetNode(id);
        if (nodeInfoPtr == nullptr) {
            LOG_E("[%s] [RoleSwitcher] Node %lu not found for posting online.",
                GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::ROLE_SWITCHER).c_str(),
                id);
            return;
        }
        std::string jsonString = BuildPostInstanceOnlineBody(*nodeInfoPtr);
        std::map <boost::beast::http::field, std::string> map;
        map[boost::beast::http::field::accept] = "*/*";
        map[boost::beast::http::field::content_type] = "application/json";
        mCoordinatorClient->SetHostAndPort(node->ip, port);
        Request req = {ControllerConstant::GetInstance()->GetCoordinatorURI(CoordinatorURI::POST_ONLINE),
                       boost::beast::http::verb::post, map, jsonString};
        httpRet = mCoordinatorClient->SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
                                                  ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
        if (httpRet != 0 || code != CODE_OK) {
            LOG_E("[%s] [RoleSwitcher] Post instance online failed, node IP %s, "
                  "port %s, ret code %d, send request ret %d.",
                  GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::ROLE_SWITCHER).c_str(),
                  node->ip.c_str(), port.c_str(), code, httpRet);
            mCoordinatorStore->UpdateCoordinatorStatus(node->ip, false);
            continue;
        }
        LOG_M("[Update] Posted instance online for node IP %s, node ID %lu.", nodeInfoPtr->ip.c_str(),
              nodeInfoPtr->instanceInfo.staticInfo.id);
        mCoordinatorStore->UpdateCoordinatorStatus(node->ip, true);
    }
}

static std::string BuildTasksRequestUrl(const std::vector<uint64_t> &ids)
{
    auto url = ControllerConstant::GetInstance()->GetCoordinatorURI(CoordinatorURI::GET_TASK);
    std::string str1 = "?";
    std::string str2 = "id=";
    std::string str3 = "&";
    url += str1;

    for (const auto& id : ids) {
        url += str2 + std::to_string(id) + str3;
    }

    url.pop_back(); // 删除最后一个多余的 '&'
    return url;
}

static bool IsValidTasksResponse(const nlohmann::json bodyJson)
{
    if (!IsJsonArrayValid(bodyJson, "tasks", 1, 1)) { // 只会查询1个节点上的任务
        return false;
    }
    auto tasks = bodyJson.at("tasks");
    for (const auto &task : tasks) {
        if (!task.is_number_integer()) {
            return false;
        }
        int64_t val = task;
        if (val < MIN_TASKS_NUMBER || val > MAX_TASKS_NUMBER) {
            LOG_E("[%s] [RoleSwitcher] Task value %ld is out of range [%ld, %ld]",
                GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ROLE_SWITCHER).c_str(), val,
                MIN_TASKS_NUMBER, MAX_TASKS_NUMBER);
            return false;
        }
    }
    return true;
}

static bool AssertInstanceTasksEnd(std::string &response)
{
    if (!nlohmann::json::accept(response)) {
        LOG_E("[%s] [RoleSwitcher] Invalid instance task response %s.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ROLE_SWITCHER).c_str(),
            response.c_str());
        return false;
    }
    try {
        LOG_D("Instance tasks response: %s", response.c_str());
        auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        if (!IsValidTasksResponse(bodyJson)) {
            LOG_E("[%s] [RoleSwitcher] Instance tasks response is invalid.",
                  GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ROLE_SWITCHER).c_str());
            return false;
        }
        auto tasks = bodyJson.at("tasks");
        for (const auto &task : tasks) {
            auto num = task.get<int64_t>();
            if (num > 0) {
                return false;
            }
        }
        return true;
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [RoleSwitcher] Assert instance read response failed, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ROLE_SWITCHER).c_str(),
            e.what());
        return false;
    } catch (const std::exception &e) {
        LOG_E("[%s] [RoleSwitcher] Assert instance tasks end failed, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ROLE_SWITCHER).c_str(),
            e.what());
        return false;
    }
}

bool RoleSwitcher::GetCoordinatorNodes(std::vector<std::unique_ptr<Coordinator>> &coordinatorNodes)
{
    try {
        coordinatorNodes = mCoordinatorStore->GetCoordinators();
    } catch (const std::exception& e) {
        LOG_E("[%s] [RoleSwitcher] Failed to get coordinators for querying tasks on instance.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ROLE_SWITCHER).c_str());
        return false;
    }
    return true;
}

int32_t RoleSwitcher::QueryInstanceTasks(uint64_t id, uint64_t waitSeconds)
{
    std::vector<uint64_t> ids {id};
    std::vector<std::unique_ptr<Coordinator>> coordinatorNodes;
    if (!GetCoordinatorNodes(coordinatorNodes)) {
        return -1;
    }
    auto port = ControllerConfig::GetInstance()->GetCoordinatorPort();
    for (uint32_t attempt = 0; attempt < waitSeconds; ++attempt) {
        bool isEnd = false;
        for (auto &node: std::as_const(coordinatorNodes)) {
            std::string response;
            int32_t code = 400;
            std::string jsonString;
            std::map <boost::beast::http::field, std::string> map;
            map[boost::beast::http::field::accept] = "*/*";
            map[boost::beast::http::field::content_type] = "application/json";
            auto requestUrl = BuildTasksRequestUrl(ids);
            mCoordinatorClient->SetHostAndPort(node->ip, port);
            Request req = {requestUrl, boost::beast::http::verb::get, map, jsonString};
            LOG_D("[RoleSwitcher] Querying tasks for instance ip %s, request URL %s, request %s.", node->ip.c_str(),
                requestUrl.c_str(), jsonString.c_str());
            int32_t httpRet = mCoordinatorClient->SendRequest(req,
                ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
                ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
            if (httpRet != 0 || code != CODE_OK) {
                LOG_E("[%s] [RoleSwitcher] Request instance tasks failed, node IP %s, "
                    "port %s, ret code %d, request ret %d",
                    GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::ROLE_SWITCHER).c_str(),
                    node->ip.c_str(), port.c_str(), code, httpRet);
                isEnd = false;
                mCoordinatorStore->UpdateCoordinatorStatus(node->ip, false);
            } else {
                mCoordinatorStore->UpdateCoordinatorStatus(node->ip, true);
                isEnd = AssertInstanceTasksEnd(response);
            }
        }
        if (isEnd) {
            LOG_I("[RoleSwitcher] Query instance task finished in waiting epoch %u.", attempt);
            return 0;
        } else {
            LOG_W("[%s] [RoleSwitcher] Query instance task unfinished in waiting epoch %u.",
                GetWarnCode(ErrorType::WARNING, ControllerFeature::ROLE_SWITCHER).c_str(), attempt);
            std::this_thread::sleep_for(std::chrono::seconds(1)); // 有任务则休眠1s
        }
    }
    return -1;
}


static bool AssertInstancePeerTasksEnd(std::string &response)
{
    if (!nlohmann::json::accept(response)) {
        LOG_E("[%s] [RoleSwitcher] Invalid instance peer tasks response: %s.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ROLE_SWITCHER).c_str(),
            response.c_str());
        return false;
    }
    try {
        LOG_D("Instance peer tasks response: %s", response.c_str());
        auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        if (!IsJsonBoolValid(bodyJson, "is_end")) {
            LOG_E("[%s] [RoleSwitcher] Instance task response is invalid.",
                  GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::ROLE_SWITCHER).c_str());
            return false;
        }
        return bodyJson.at("is_end").get<bool>();
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [RoleSwitcher] Assert instance read response failed, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ROLE_SWITCHER).c_str(),
            e.what());
        return false;
    } catch (const std::exception &e) {
        LOG_E("[%s] [RoleSwitcher] Assert instance tasks end failed, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::ROLE_SWITCHER).c_str(),
            e.what());
        return false;
    }
}

static std::string BuildPeerTasksRequestBody(uint64_t pId, uint64_t dId, MINDIE::MS::DIGSRoleChangeType type)
{
    nlohmann::json body;
    body["p_id"] = pId;
    body["d_id"] = dId;
    body["role_change_type"] = type;
    return body.dump();
}

int32_t RoleSwitcher::QueryInstancePeerTasks(uint64_t pId, uint64_t dId, MINDIE::MS::DIGSRoleChangeType type)
{
    auto waitSeconds = ControllerConfig::GetInstance()->GetTasksEndWaitSeconds();
    auto coordinatorNodes = mCoordinatorStore->GetCoordinators();
    auto port = ControllerConfig::GetInstance()->GetCoordinatorPort();
    int32_t httpRet = -1;
    for (uint32_t attempt = 0; attempt < waitSeconds; ++attempt) {
        bool isEnd = false;
        for (auto &node: std::as_const(coordinatorNodes)) {
            std::string response;
            int32_t code = 400;
            std::map <boost::beast::http::field, std::string> map;
            map[boost::beast::http::field::accept] = "*/*";
            map[boost::beast::http::field::content_type] = "application/json";
            auto jsonString = BuildPeerTasksRequestBody(pId, dId, type);
            mCoordinatorClient->SetHostAndPort(node->ip, port);
            Request req = {ControllerConstant::GetInstance()->GetCoordinatorURI(CoordinatorURI::POST_QUERY_TASK),
                           boost::beast::http::verb::post, map, jsonString};
            LOG_D("[RoleSwitcher] Querying peer tasks, IP %s, request URL %s, request %s.", node->ip.c_str(),
                  ControllerConstant::GetInstance()->GetCoordinatorURI(CoordinatorURI::POST_QUERY_TASK).c_str(),
                  jsonString.c_str());
            httpRet = mCoordinatorClient->SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
                ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
            if (httpRet != 0 || code != CODE_OK) {
                LOG_E("[%s] [RoleSwitcher] Request instance tasks failed, node IP %s, "
                      "port %s, ret code %d, request ret %d.",
                      GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::ROLE_SWITCHER).c_str(),
                      node->ip.c_str(), port.c_str(), code, httpRet);
                isEnd = false;
                mCoordinatorStore->UpdateCoordinatorStatus(node->ip, false);
            } else {
                mCoordinatorStore->UpdateCoordinatorStatus(node->ip, true);
                isEnd = AssertInstancePeerTasksEnd(response);
            }
        }
        if (isEnd) {
            LOG_I("[RoleSwitcher] Task finished in waiting epoch %u.", attempt);
            return 0;
        } else {
            LOG_W("[%s] [RoleSwitcher] Task unfinished in waiting epoch %u",
                GetWarnCode(ErrorType::WARNING, ControllerFeature::ROLE_SWITCHER).c_str(), attempt);
            std::this_thread::sleep_for(std::chrono::seconds(1)); // 有任务则休眠1s
        }
    }
    return -1;
}
}
