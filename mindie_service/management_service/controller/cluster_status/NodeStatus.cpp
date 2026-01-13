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
#include "NodeStatus.h"
#include <string>
#include <algorithm>
#include <sstream>
#include <mutex>
#include <set>
#include "Logger.h"
#include "ControllerConstant.h"
#include "SecurityUtils.h"
namespace MINDIE::MS {

const size_t NODE_LINK_FAILED_LOG_FREQUENCY = 120;
const size_t NODE_CHANGE_PRINTED_LOG_NUM = 5;

void to_json(nlohmann::json& deviceJson, const DeviceInfo& deviceInfo)
{
    deviceJson = nlohmann::json::array();
    deviceJson = {
        { "device_id", ValidateAndSanitizeDeviceId(deviceInfo.id) },
        { "device_ip", ValidateAndSanitizeIP(deviceInfo.ip) },
        { "device_logical_id", deviceInfo.logicalId},
        { "rank_id", std::to_string(deviceInfo.rankId) }
    };
    if (deviceInfo.superDeviceId.has_value()) {
        deviceJson["super_device_id"] = ValidateAndSanitizeDeviceId(deviceInfo.superDeviceId.value());
    }
}

void to_json(nlohmann::json& serverInfoJson, const ServerInfo& serverInfo)
{
    serverInfoJson = nlohmann::json::array();
    serverInfoJson = {
        { "server_ip", ValidateAndSanitizeIP(serverInfo.ip) },
        { "host_ip", ValidateAndSanitizeIP(serverInfo.hostId)},
        { "sp_size", serverInfo.spSize },
        { "cp_size", serverInfo.cpSize }
    };
    if (serverInfo.superPodId.has_value()) {
        serverInfoJson["super_pod_id"] = ValidateAndSanitizeDeviceId(serverInfo.superPodId.value());
    }
    nlohmann::json dpGroupJson = nlohmann::json::array();
    for (const auto& dpPair : serverInfo.dpGroupInfos) {
        nlohmann::json dpJson;
        dpJson["dp_inst_id"] = dpPair.first;
        dpJson["device"] = nlohmann::json::array();
        for (auto &device : dpPair.second) {
            nlohmann::json deviceJson;
            deviceJson["device_id"] = ValidateAndSanitizeDeviceId(device.id);
            deviceJson["device_ip"] = ValidateAndSanitizeIP(device.ip);
            deviceJson["device_logical_id"] = device.logicalId;
            deviceJson["rank_id"] = std::to_string(device.rankId);
            if (device.superDeviceId.has_value()) {
                deviceJson["super_device_id"] = ValidateAndSanitizeDeviceId(device.superDeviceId.value());
            }
            dpJson["device"].emplace_back(deviceJson);
        }
        dpGroupJson.emplace_back(dpJson);
    }
    serverInfoJson["dp_inst_list"] = dpGroupJson;
}

void NodeStatus::AddNodes(std::vector<std::unique_ptr<NodeInfo>> &nodeVec)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    LOG_I("[NodeStatus] Adding nodes, input node size is %zu.", nodeVec.size());
    for (auto &node : nodeVec) {
        uint64_t id = node->instanceInfo.staticInfo.id;
        auto newNode = std::unique_ptr<NodeInfo>(node.release());
        if (newNode == nullptr) {
            LOG_E("[%s] [NodeStatus] Adding nodes, creat node failed, current node size is %zu.",
                GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::NODE_STATUS).c_str(),
                mNodes.size());
            continue;
        }

        // 检查节点ID是否已存在
        if (mNodes.find(id) != mNodes.end()) {
            LOG_W("[NodeStatus] Node ID %lu already exists, skip adding. IP: %s",
                id, node->ip.c_str());
            continue;  // 跳过重复节点，不覆盖已有信息
        }

        LOG_M("[Add] Add node ID %lu, IP %s",
            newNode->instanceInfo.staticInfo.id, newNode->ip.c_str());
        mNodes[id] = std::move(newNode);
    }
    LOG_I("[NodeStatus] Adding nodes, current node size is %zu.", mNodes.size());
}

void NodeStatus::AddNode(std::unique_ptr<NodeInfo> node)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    LOG_I("[NodeStatus] Adding a node.");
    uint64_t id = node->instanceInfo.staticInfo.id;
    LOG_M("[Add] Add node ID %lu, IP %s.", id, node->ip.c_str());
    mNodes[id] = std::move(node);
    LOG_I("[NodeStatus] Current node size is %zu.", mNodes.size());
    if (mFaultyNodes.find(id) == mFaultyNodes.end()) {
        return;
    }
    mFaultyNodes.erase(id);
    LOG_M("[Add] Adding a node, remove faulty node %lu.", id);
}

void NodeStatus::AddFaultyNodes(std::vector<std::unique_ptr<NodeInfo>> &nodeVec)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    LOG_I("[NodeStatus] Adding faulty nodes, input node size is %zu.", nodeVec.size());
    for (auto &node : nodeVec) {
        uint64_t id = node->instanceInfo.staticInfo.id;
        auto newNode = std::unique_ptr<NodeInfo>(node.release());
        if (newNode == nullptr) {
            LOG_E("[%s] [NodeStatus] Adding faulty nodes, creat node failed, current number of faulty node is %zu.",
                GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::NODE_STATUS).c_str(),
                mFaultyNodes.size());
            continue;
        }
        LOG_M("[Add] Adding faulty nodes, add node ID %lu, IP %s", newNode->instanceInfo.staticInfo.id,
              newNode->ip.c_str());
        mFaultyNodes[id] = std::move(newNode);
    }
    LOG_I("[NodeStatus]Adding faulty nodes, current number of faulty node is %zu.", mFaultyNodes.size());
}

void NodeStatus::AddFaultyNode(std::unique_ptr<NodeInfo> node)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    uint64_t id = node->instanceInfo.staticInfo.id;
    LOG_M("[Add] Adding a faulty node, node ID %lu, IP %s", id, node->ip.c_str());
    mFaultyNodes[id] = std::move(node);
    LOG_I("[NodeStatus] Adding a faulty node, current number of faulty nodes is %zu.", mFaultyNodes.size());
}

void NodeStatus::AddExpiredNode(uint64_t id)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    mExpiredNodeIds.insert(id);
    LOG_I("[NodeStatus] Add an expired node, node ID %lu", id);
}

void NodeStatus::UpdateNodeDynamicStatus(uint64_t id, MINDIE::MS::DIGSInstanceRole role, const std::string &roleState,
    const MINDIE::MS::DIGSInstanceDynamicInfo &info, const std::vector<uint64_t> &peers)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter != mNodes.end()) {
        iter->second->currentRole = role;
        iter->second->roleState = roleState;
        iter->second->instanceInfo.dynamicInfo.availSlotsNum = info.availSlotsNum;
        iter->second->instanceInfo.dynamicInfo.availBlockNum = info.availBlockNum;
        iter->second->instanceInfo.dynamicInfo.maxAvailBlockNum = info.maxAvailBlockNum;
        iter->second->activePeers = peers;
        // 如果是切换节点，当前roleState不为ready，则不更新activePeers
        if (iter->second->isRoleChangeNode) {
            if (iter->second->roleState != ControllerConstant::GetInstance()->GetRoleState(RoleState::READY)) {
                iter->second->activePeers.clear();
            } else {
                iter->second->isRoleChangeNode = false;
            }
        }
        iter->second->isHealthy = true;
        iter->second->instanceInfo.dynamicInfo.waitingRequestNum = info.waitingRequestNum;
        iter->second->instanceInfo.dynamicInfo.runningRequestNum = info.runningRequestNum;
        iter->second->instanceInfo.dynamicInfo.swappedRequestNum = info.swappedRequestNum;
        iter->second->instanceInfo.dynamicInfo.freeNpuBlockNums = info.freeNpuBlockNums;
        iter->second->instanceInfo.dynamicInfo.freeCpuBlockNums = info.freeCpuBlockNums;
        iter->second->instanceInfo.dynamicInfo.totalNpuBlockNums = info.totalNpuBlockNums;
        iter->second->instanceInfo.dynamicInfo.totalCpuBlockNums = info.totalCpuBlockNums;
    }
}

void NodeStatus::UpdateInferenceType(uint64_t id, InferenceType type)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Updating inference type, node ID %lu not found.",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    if (iter->second->inferenceType != type) {
        LOG_I("[NodeStatus] Updating inference type, node ID %lu, IP %s update inference type %d to %d.",
              id, iter->second->ip.c_str(), iter->second->inferenceType, type);
    }
    iter->second->inferenceType = type;
}

void NodeStatus::UpdateNodeStaticInfo(uint64_t id, const MINDIE::MS::DIGSInstanceDynamicInfo &info)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Updating node static information, node ID %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    iter->second->instanceInfo.staticInfo.totalSlotsNum = info.availSlotsNum;
    iter->second->instanceInfo.staticInfo.totalBlockNum = info.availBlockNum;
}

void NodeStatus::UpdateNodeScheduleInfo(uint64_t id, const MINDIE::MS::DIGSInstanceScheduleInfo &info)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Updating node schedule information, node ID %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    iter->second->instanceInfo.scheduleInfo.allocatedSlots = info.allocatedSlots;
    iter->second->instanceInfo.scheduleInfo.allocatedBlocks = info.allocatedBlocks;
}

void NodeStatus::AddInitRetryTimes(uint64_t id)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Adding initialize retry times, node ID %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    iter->second->initRetryTimes++;
}

void NodeStatus::UpdateRoleState(uint64_t id, const std::string &roleState, bool isHealthy, bool isInitialized)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Updating role state, node ID %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    if (iter->second->roleState != roleState) {
        LOG_I("[NodeStatus] Updating role state, node ID %lu, IP %s update role state %s to %s.",
              id, iter->second->ip.c_str(), iter->second->roleState.c_str(), roleState.c_str());
    }
    iter->second->roleState = roleState;
    iter->second->isHealthy = isHealthy;
    iter->second->isInitialized = isInitialized;
}

void NodeStatus::UpdateNode(uint64_t id, const NodeInfo &nodeInfo)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Updating node, node ID %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    if (iter->second->roleState != nodeInfo.roleState) {
        LOG_I("[NodeStatus] Updating node, node ID %lu, IP %s update role state %s to %s.",
              id, iter->second->ip.c_str(), iter->second->roleState.c_str(), nodeInfo.roleState.c_str());
    }
    if (iter->second->instanceInfo.staticInfo.role != nodeInfo.instanceInfo.staticInfo.role) {
        LOG_I("[NodeStatus] Updating node, node ID %lu, IP %s update role %c to %c.",
              id, iter->second->ip.c_str(), iter->second->instanceInfo.staticInfo.role,
              nodeInfo.instanceInfo.staticInfo.role);
    }
    iter->second->instanceInfo.staticInfo.role = nodeInfo.instanceInfo.staticInfo.role;
    iter->second->instanceInfo.staticInfo.label = nodeInfo.instanceInfo.staticInfo.label;
    iter->second->instanceInfo.staticInfo.flexPRatio = nodeInfo.instanceInfo.staticInfo.flexPRatio;
    iter->second->currentRole = nodeInfo.instanceInfo.staticInfo.role;
    iter->second->virtualId = nodeInfo.instanceInfo.staticInfo.virtualId;
    iter->second->roleState = nodeInfo.roleState;
    iter->second->peers = nodeInfo.peers;
    iter->second->activePeers.clear();
    iter->second->isHealthy = nodeInfo.isHealthy;
    iter->second->isInitialized = nodeInfo.isInitialized;
    iter->second->initRetryTimes = 0;
    iter->second->isRoleChangeNode = nodeInfo.isRoleChangeNode;
    LOG_I("[NodeStatus] Updating node, node ID %lu, IP %s, is healthy %d, is initialized %d.",
          id, iter->second->ip.c_str(), iter->second->isHealthy, iter->second->isInitialized);
}

void NodeStatus::UpdateRoleStateAndPeers(uint64_t groupId, uint64_t id, const std::string &roleState,
                                         const std::vector<uint64_t> &peers)
{
    auto flexGroup = GetFlexGroup(groupId);
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Updateing role state and peers, node ID is %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    if (iter->second->roleState != roleState) {
        LOG_I("[NodeStatus] Updateing role state and peers, node ID %lu, IP %s update role %s to role %s.",
              id, iter->second->ip.c_str(), iter->second->roleState.c_str(), roleState.c_str());
    }
    iter->second->roleState = roleState;
    iter->second->peers = peers;
    if (!flexGroup.empty()) {
        iter->second->peers.insert(iter->second->peers.end(), flexGroup.begin(), flexGroup.end());
    }
    std::vector<uint64_t> result;
    std::set<uint64_t> set1(iter->second->peers.begin(), iter->second->peers.end());
    std::set<uint64_t> set2(peers.begin(), peers.end());
    std::set_intersection(set1.begin(), set1.end(), set2.begin(), set2.end(), std::back_inserter(result));
    iter->second->activePeers = result;
    iter->second->initRetryTimes = 0;
}

void NodeStatus::UpdateInheritInfo(uint64_t id, uint64_t inheritId)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Updating inherit information, node ID %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    if (iter->second->isInherited) {
        LOG_E("[%s] [NodeStatus] Updating inherit information, node ID %lu has been inherited by node id %lu.",
            GetErrorCode(ErrorType::OPERATION_REPEAT, ControllerFeature::NODE_STATUS).c_str(), id,
            iter->second->inheritedId);
        return;
    }
    iter->second->inheritedId = inheritId;
    iter->second->isInherited = true;
    iter->second->activePeers.clear();
    iter->second->isHealthy = false;
    LOG_M("[Update] Updating inherit, node ID %lu is inherited by node id %lu.", id, inheritId);
}

std::string NodeStatus::GetRoleState(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Getting role state, node %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return "";
    } else {
        return iter->second->roleState;
    }
}

std::string NodeStatus::GetIpForAllTypeNodes(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);
    auto iter = mFaultyNodes.find(id);
    if (iter != mFaultyNodes.end()) {
        return iter->second->ip;
    }
    iter = mNodes.find(id);
    if (iter != mNodes.end()) {
        return iter->second->ip;
    }
    LOG_E("[%s] [NodeStatus] Get IP for all type nodes. node %lu not found.",
        GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
    return "";
}

std::unique_ptr<NodeInfo> NodeStatus::GetNode(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Node %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return nullptr;
    }
    std::unique_ptr<NodeInfo> node;
    try {
        // 使用深拷贝来创建NodeInfo对象
        node = std::make_unique<NodeInfo>(*iter->second);
    } catch (const std::exception& e) {
        LOG_E("[%s] [NodeStatus] Getting node, create node %lu failed.",
              GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::NODE_STATUS).c_str(), id);
        return nullptr;
    }
    return node;
}

std::unique_ptr<NodeInfo> NodeStatus::GetNode(std::string ip, std::string port)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);

    for (auto it = mNodes.begin(); it != mNodes.end(); ++it) {
        const std::unique_ptr<NodeInfo>& nodePtr = it->second;
        if (nodePtr && nodePtr->ip == ip && nodePtr->port == port) {
            return std::make_unique<NodeInfo>(*nodePtr);
        }
    }
    LOG_E("[%s] [NodeStatus] Node %s:%s not found.",
        GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), ip.c_str(), port.c_str());
    return nullptr;
}

void NodeStatus::AddGroup(uint64_t groupId, std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    LOG_M("[Add] Add roup ID %lu, %zu P nodes, %zu D nodes.", groupId, group.first.size(),
          group.second.size());
    mGroups[groupId] = std::move(group);
    LOG_I("[NodeStatus] Adding group, current number of groups is %zu.", mGroups.size());
}

void NodeStatus::AddFlexGroup(uint64_t groupId, std::vector<uint64_t> &flexGroup)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    LOG_M("[Add] NodeStatus AddFlexGroup: add group id %lu, %zu flex nodes", groupId, flexGroup.size());
    mFlexGroups[groupId] = std::move(flexGroup);
    LOG_I("[NodeStatus]AddFlexGroup: current number of groups %zu", mFlexGroups.size());
}

void NodeStatus::AddFaultyGroup(uint64_t groupId, std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    LOG_M("[Add] Add faulty group ID %lu, %zu P nodes, %zu D nodes.", groupId, group.first.size(),
          group.second.size());
    mFaultyGroups[groupId] = std::move(group);
    LOG_I("[NodeStatus] Adding faulty group, current number of faulty groups is %zu.", mFaultyGroups.size());
}

void NodeStatus::AddFaultyFlexGroup(uint64_t groupId, std::vector<uint64_t> &flexGroup)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    LOG_M("[Add] NodeStatus AddFaultyFlexGroup: add group id %lu, %zu flex nodes", groupId, flexGroup.size());
    mFaultyFlexGroups[groupId] = std::move(flexGroup);
    LOG_I("[NodeStatus]AddFaultyFlexGroup: current number of faulty groups %zu", mFaultyFlexGroups.size());
}

void NodeStatus::UpdateGroup(uint64_t groupId, std::pair<std::vector<uint64_t>, std::vector<uint64_t>> group)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mGroups.find(groupId);
    if (iter == mGroups.end()) {
        LOG_E("[%s] [NodeStatus] Updating group, group ID %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), groupId);
        return;
    }
    mGroups[groupId] = std::move(group);
    LOG_M("[Update] Updating group, group ID %lu is updated to %zu P nodes, %zu D nodes.", groupId,
          mGroups[groupId].first.size(), mGroups[groupId].second.size());
}

void NodeStatus::UpdateFlexGroup(uint64_t groupId, std::vector<uint64_t> flexGroup)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mFlexGroups.find(groupId);
    if (iter == mFlexGroups.end()) {
        LOG_E("[%s] [NodeStatus]UpdateFlexGroup: group id %lu not found",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), groupId);
        return;
    }
    mFlexGroups[groupId] = std::move(flexGroup);
    LOG_M("[Update] NodeStatus UpdateFlexGroup: group id %lu is updated to %zu flex nodes",
        groupId, mFlexGroups.size());
}

std::map<uint64_t, std::unique_ptr<NodeInfo>> NodeStatus::GetAllFaultyNodes()
{
    std::map<uint64_t, std::unique_ptr<NodeInfo>> ret;
    std::shared_lock<std::shared_mutex> lock(mMtx);
    LOG_D("[NodeStatus] Geting all faulty nodes, total number of faulty nodes is %zu.", mFaultyNodes.size());
    for (auto &iter : std::as_const(mFaultyNodes)) {
        auto node = std::make_unique<NodeInfo>();
        if (node == nullptr) {
            LOG_E("[%s] [NodeStatus] Create node failed.",
                GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::NODE_STATUS).c_str());
            return {};
        }
        node->hostId = iter.second->hostId;
        node->ip = iter.second->ip;
        node->port = iter.second->port;
        node->mgmtPort = iter.second->mgmtPort;
        node->metricPort = iter.second->metricPort;
        node->interCommPort = iter.second->interCommPort;
        node->isHealthy = iter.second->isHealthy;
        node->isInitialized = iter.second->isInitialized;
        node->inferenceType = iter.second->inferenceType;
        node->currentRole = iter.second->currentRole;
        node->roleState = iter.second->roleState;
        node->modelName = iter.second->modelName;
        node->serverInfoList = iter.second->serverInfoList;
        node->instanceInfo = iter.second->instanceInfo;
        node->peers = iter.second->peers;
        node->activePeers = iter.second->activePeers;
        node->isInherited = iter.second->isInherited;
        node->inheritedId = iter.second->inheritedId;
        node->initRetryTimes = iter.second->initRetryTimes;
        node->deleteTime = iter.second->deleteTime;
        node->isDistribute = iter.second->isDistribute;
        node->isRoleChangeNode = iter.second->isRoleChangeNode;
        ret.emplace(iter.first, std::move(node));
    }
    LOG_D("[NodeStatus] Geting all faulty node, result node count is %zu.", ret.size());
    return ret;
}

std::map<uint64_t, std::unique_ptr<NodeInfo>> NodeStatus::GetAllNodes()
{
    std::map<uint64_t, std::unique_ptr<NodeInfo>> ret;
    std::shared_lock<std::shared_mutex> lock(mMtx);
    LOG_D("[NodeStatus] Total node size is %zu.", mNodes.size());
    for (auto &iter : std::as_const(mNodes)) {
        auto node = std::make_unique<NodeInfo>();
        if (node == nullptr) {
            LOG_E("[%s] [NodeStatus] Getting all nodes, create node failed.",
                GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::NODE_STATUS).c_str());
            return {};
        }
        node->hostId = iter.second->hostId;
        node->ip = iter.second->ip;
        node->port = iter.second->port;
        node->mgmtPort = iter.second->mgmtPort;
        node->metricPort = iter.second->metricPort;
        node->interCommPort = iter.second->interCommPort;
        node->isHealthy = iter.second->isHealthy;
        node->isInitialized = iter.second->isInitialized;
        node->inferenceType = iter.second->inferenceType;
        node->currentRole = iter.second->currentRole;
        node->roleState = iter.second->roleState;
        node->modelName = iter.second->modelName;
        node->serverInfoList = iter.second->serverInfoList;
        node->instanceInfo = iter.second->instanceInfo;
        node->peers = iter.second->peers;
        node->activePeers = iter.second->activePeers;
        node->isInherited = iter.second->isInherited;
        node->inheritedId = iter.second->inheritedId;
        node->initRetryTimes = iter.second->initRetryTimes;
        node->deleteTime = iter.second->deleteTime;
        node->isDistribute = iter.second->isDistribute;
        node->dpGroupPeers = iter.second->dpGroupPeers;
        node->isRoleChangeNode = iter.second->isRoleChangeNode;
        ret.emplace(iter.first, std::move(node));
    }
    LOG_D("[NodeStatus] Getting all nodes, result node count is %zu.", ret.size());
    return ret;
}

std::vector<uint64_t> NodeStatus::GetAllGroupIds()
{
    std::vector<uint64_t> ret;
    std::shared_lock<std::shared_mutex> lock(mMtx);
    LOG_I("[NodeStatus] Getting all groups' ID, total group size is %zu.", mGroups.size());
    for (auto &iter : std::as_const(mGroups)) {
        ret.push_back(iter.first);
    }
    LOG_I("[NodeStatus] Getting all groups' ID, result group count is %zu", ret.size());
    return ret;
}

std::vector<uint64_t> NodeStatus::GetAllNodeIds()
{
    std::vector<uint64_t> ret;
    std::shared_lock<std::shared_mutex> lock(mMtx);
    LOG_D("[NodeStatus] Getting all node IDs, total node size is %zu.", mNodes.size());
    for (auto &iter : std::as_const(mNodes)) {
        ret.push_back(iter.first);
    }
    LOG_D("[NodeStatus] Getting all node IDs, result node count is %zu", ret.size());
    return ret;
}

std::vector<uint64_t> NodeStatus::GetDeletedNodeIds()
{
    std::vector<uint64_t> ret;
    std::shared_lock<std::shared_mutex> lock(mMtx);
    LOG_I("[NodeStatus] Getting deleted node IDs, total node size is %zu.", mNodes.size());
    for (auto &iter : std::as_const(mNodes)) {
        if (iter.second->deleteTime == std::chrono::seconds(0) || iter.second->isInherited) {
            continue;
        }
        ret.push_back(iter.first);
    }
    LOG_I("[NodeStatus] Getting deleted node IDs, result node count is %zu", ret.size());
    return ret;
}

std::set<uint64_t> NodeStatus::GetExpiredNodeIds() { return mExpiredNodeIds; }

static bool IsDeviceInfoChanged(const std::vector<ServerInfo> &oldRankTableInfo, const std::vector<ServerInfo> &newInfo)
{
    if (oldRankTableInfo.size() != newInfo.size()) {
        return true;
    }
    for (size_t i = 0; i < oldRankTableInfo.size(); ++i) {
        auto oldDevices = oldRankTableInfo[i].deviceInfos;
        auto newDevices = newInfo[i].deviceInfos;
        for (const auto &device : std::as_const(newDevices)) {
            auto it = std::find_if(oldDevices.begin(), oldDevices.end(),
                [&device](const DeviceInfo &obj) {
                    return obj.id == device.id && obj.ip == device.ip &&
                        obj.logicalId == device.logicalId && obj.rankId == device.rankId;
                });
            if (it == oldDevices.end()) {
                return true;
            }
        }
    }
    return false;
}

std::string NodeStatus::ConvertNodeIdVector2Str(const std::vector<uint64_t>& nodeIdVec)
{
    std::ostringstream oss;

    for (size_t i = 0; i < nodeIdVec.size(); ++i) {
        if (i > 0) {
            oss << ",";
        }
        oss << nodeIdVec[i];
    }

    return oss.str();
}

void PrintNodeChangeLogs(const std::vector<std::vector<uint64_t>>& nodeIdVecs, size_t nodeChangePrintedLogNum)
{
    if (nodeIdVecs.size() != nodeChangePrintedLogNum) {
        return;
    }

    // assist with the order of the enum NodeChangePrintLogType
    static std::vector<std::string> nodeChangeTypes = {
        "removed", "ignore expired", "new", "reappeared", "device changed"
    };

    for (size_t i = 0; i < nodeIdVecs.size(); i++) {
        if (nodeIdVecs[i].size() != 0) {
            LOG_I("[NodeStatus] Detecting node changes, %s nodes %s.", nodeChangeTypes[i].c_str(),
                NodeStatus::ConvertNodeIdVector2Str(nodeIdVecs[i]).c_str());
        }
    }
}

NodeChanges NodeStatus::DetectNodeChanges(const std::vector<std::unique_ptr<NodeInfo>> &serverNodes)
{
    NodeChanges nodeChanges;
    std::vector<std::vector<uint64_t>> nodeIdVecs(NODE_CHANGE_PRINTED_LOG_NUM);
    {
        std::shared_lock<std::shared_mutex> lock(mMtx);
        LOG_D("[NodeStatus] Detecting node changes, input node size is %zu.", serverNodes.size());
        // 检查减少的 IP
        for (const auto& pair : std::as_const(mNodes)) {
            auto it = std::find_if(serverNodes.begin(), serverNodes.end(),
                [&pair](const std::unique_ptr<NodeInfo>& obj) {
                    return obj->instanceInfo.staticInfo.id == pair.first;
                });
            if (it == serverNodes.end()) {
                nodeIdVecs[static_cast<size_t>(NodeChangePrintLogType::REMOVED)].push_back(pair.first);
                nodeChanges.removedIDs.push_back(pair.first);
            }
        }
        for (const auto &node : std::as_const(serverNodes)) {
            auto iter = mNodes.find(node->instanceInfo.staticInfo.id);
            if (mExpiredNodeIds.find(node->instanceInfo.staticInfo.id) != mExpiredNodeIds.end()) {
                nodeIdVecs[static_cast<size_t>(NodeChangePrintLogType::EXPIRED)].push_back(
                    node->instanceInfo.staticInfo.id);
                continue;
            }
            if (iter == mNodes.end()) {
                // 检查新增的 IP
                nodeIdVecs[static_cast<size_t>(NodeChangePrintLogType::NEW)].push_back(
                    node->instanceInfo.staticInfo.id);
                nodeChanges.newIDs.push_back(node->instanceInfo.staticInfo.id);
                continue;
            }
            if (iter->second->deleteTime > std::chrono::seconds(0)) {
                // 检查重新出现的且不是被释放而导致放逐的实例IP
                nodeIdVecs[static_cast<size_t>(NodeChangePrintLogType::REAPPEARED)].push_back(
                    node->instanceInfo.staticInfo.id);
                nodeChanges.reappearIDs.push_back(node->instanceInfo.staticInfo.id);
                continue;
            }
            if (IsDeviceInfoChanged(iter->second->serverInfoList, node->serverInfoList)) {
                nodeIdVecs[static_cast<size_t>(NodeChangePrintLogType::DEVICE_CHANGED)].push_back(
                    node->instanceInfo.staticInfo.id);
                nodeChanges.removedIDs.push_back(node->instanceInfo.staticInfo.id);
                nodeChanges.newIDs.push_back(node->instanceInfo.staticInfo.id);
            }
        }
        PrintNodeChangeLogs(nodeIdVecs, NODE_CHANGE_PRINTED_LOG_NUM);
    }
    return nodeChanges;
}

std::pair<std::vector<uint64_t>, std::vector<uint64_t>> NodeStatus::GetGroup(uint64_t groupId)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);
    auto iter = mGroups.find(groupId);
    if (iter == mGroups.end()) {
        LOG_E("[%s] [NodeStatus] Getting group, group ID %lu is not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), groupId);
        return {};
    }
    return mGroups[groupId];
}

std::vector<uint64_t> NodeStatus::GetFlexGroup(uint64_t groupId)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);
    auto iter = mFlexGroups.find(groupId);
    if (iter == mFlexGroups.end()) {
        LOG_E("[%s] [NodeStatus]GetGroup: group id %lu is not found",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), groupId);
        return {};
    }
    return mFlexGroups[groupId];
}

uint64_t NodeStatus::GetNodesInGroup(uint64_t groupId)
{
    auto group = GetGroup(groupId);
    auto flexGroup = GetFlexGroup(groupId);
    return group.first.size() + group.second.size() + flexGroup.size();
}

void NodeStatus::UpdateNodeDeleteTime(uint64_t id)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Updating time of delete node, node ID %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    if (iter->second->deleteTime > std::chrono::seconds(0)) {
        return;
    }
    iter->second->isHealthy = false;
    iter->second->isInitialized = false;
    iter->second->deleteTime = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().
            time_since_epoch());
    LOG_M("[Update] Updating time of delete node, node ID %lu is deleted.", id);
}

void NodeStatus::RemoveNode(uint64_t id)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Removing node, node %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return;
    }
    mNodes.erase(id);
    LOG_M("[Remove] Remove node %lu.", id);
}

void NodeStatus::RemoveExpiredNode(uint64_t id)
{
    std::unique_lock<std::shared_mutex> lock(mMtx);
    if (mExpiredNodeIds.find(id) == mExpiredNodeIds.end()) {
        // no need to log error.
        return;
    }
    mExpiredNodeIds.erase(id);
    LOG_I("[NodeStatus] Remove expired node %lu.", id);
}

bool NodeStatus::IsPostRoleNeeded(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Node %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return false;
    }
    if (iter->second->deleteTime > std::chrono::seconds(0)) {
        LOG_D("[%s] [NodeStatus] Checking if post role is needed, node %lu is deleted.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_STATUS).c_str(), id);
        return false;
    }
    if (!iter->second->isInitialized && iter->second->isHealthy) {
        LOG_D("[%s] [NodeStatus] Checking if post role is needed, node %lu is not initialized, is healthy %d.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_STATUS).c_str(),
            id, iter->second->isHealthy);
        return true;
    }
    if ((iter->second->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN)) &&
        (iter->second->currentRole == MINDIE::MS::DIGSInstanceRole::UN_DEF_INSTANCE) &&
        iter->second->isHealthy) {
        LOG_D("[%s] [NodeStatus] Checking if post role is needed, node %lu, role %c, current role %c, status %s, "
            "is healthy %d.", GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_STATUS).c_str(),
            id, iter->second->instanceInfo.staticInfo.role, iter->second->currentRole,
            iter->second->roleState.c_str(), iter->second->isHealthy);
        return true;
    }
    // 对于P/D/FLEX实例，如果peers不为空但是activePeers为空，则需要向server发送请求
    if (!iter->second->isRoleChangeNode) {
        if (iter->second->activePeers.empty() && !iter->second->peers.empty() && iter->second->isHealthy) {
            LOG_D("[NodeStatus]IsPostRoleNeeded: d node %lu, role %c, status %s, active peers %zu, peers %zu, "
                  "is healthy %d",
                  id, iter->second->instanceInfo.staticInfo.role, iter->second->roleState.c_str(),
                  iter->second->activePeers.size(), iter->second->peers.size(), iter->second->isHealthy);
            return true;
        }
    }
    return false;
}

bool NodeStatus::IsIgnoredInPDSeparate(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Check if ignore for PD separate, node %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return true;
    }
    std::string warnCode = GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_STATUS);
    if (iter->second->deleteTime > std::chrono::seconds(0)) {
        LOG_D("[%s] [NodeStatus] Check if ignore for PD separate, node %lu is deleted.", warnCode.c_str(), id);
        return true;
    }
    if (!iter->second->isInitialized) {
        LOG_D("[%s] [NodeStatus] Check if ignore for PD separate, node %lu is not initialized.", warnCode.c_str(), id);
        return true;
    }
    if (!iter->second->isHealthy) {
        LOG_D("[%s] [NodeStatus] Check if ignore for PD separate, node %lu is not healthy.", warnCode.c_str(), id);
        return true;
    }
    if (iter->second->inferenceType == InferenceType::INITIALIZING_STATIC_TOTAL_INFO) {
        LOG_D("[%s] [NodeStatus] Check if ignore for PD separate, node %lu is initializing static total information.",
            warnCode.c_str(), id);
        return true;
    }
    if (iter->second->currentRole != iter->second->instanceInfo.staticInfo.role) {
        LOG_D("[%s] [NodeStatus] Check if ignore for PD separate, node %lu, current role %c, role %c.",
            warnCode.c_str(), id, iter->second->currentRole, iter->second->instanceInfo.staticInfo.role);
        return true;
    }
    if (iter->second->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN)) {
        LOG_D("[%s] [NodeStatus] Check if ignore for PD separate, node %lu, role %c, status %s.", warnCode.c_str(),
              id, iter->second->instanceInfo.staticInfo.role, iter->second->roleState.c_str());
        return true;
    }
    if (iter->second->isRoleChangeNode) {
        LOG_D("[NodeStatus] IsIgnoredInPDSeparate: node %lu, role %c, isRoleChangeNode %d", id,
              iter->second->instanceInfo.staticInfo.role, iter->second->isRoleChangeNode);
        return true;
    }
    // 对于P/D实例，如果peers或者activePeers为空，则忽略该实例。
    // 如果实例处于RoleSwithching状态，只要activePeers不为空，就表示该实例已经有link链路，可以将该实例发给coordinator
    if (iter->second->instanceInfo.staticInfo.role != MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE &&
        (iter->second->activePeers.empty() || iter->second->peers.empty())) {
        LOG_D("[%s] [NodeStatus] Check if ignore for PD separate, node %lu, role %c, status %s, active peers %zu, "
              "peers %zu.",
              warnCode.c_str(), id, iter->second->instanceInfo.staticInfo.role, iter->second->roleState.c_str(),
              iter->second->activePeers.size(), iter->second->peers.size());
        return true;
    }
    return false;
}

bool NodeStatus::IsIgnoredInSingleNode(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);
    auto iter = mNodes.find(id);
    if (iter == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Check if ignore in single mode, node %lu not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id);
        return true;
    }
    if (!iter->second->isHealthy) {
        LOG_D("[%s] [NodeStatus] Check if ignore in single mode, node %lu, role %c, status %s",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_STATUS).c_str(),
            id, iter->second->instanceInfo.staticInfo.role, iter->second->roleState.c_str());
        return true;
    }
    if (iter->second->deleteTime > std::chrono::seconds(0)) {
        LOG_D("[%s] [NodeStatus] Check if ignore in single mode, node %lu is deleted.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_STATUS).c_str(), id);
        return true;
    }
    return false;
}

bool NodeStatus::IsNodeLinkedByPeer(uint64_t peer, uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mMtx);
    uint64_t checkNodeLinkRounds = mCheckNodeLinkRoundCounter.fetch_add(1, std::memory_order_relaxed);
    auto peerNode = mNodes.find(peer);
    if (peerNode == mNodes.end()) {
        LOG_E("[%s] [NodeStatus] Peer %lu is not found in cluster.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), peer);
        return false;
    }
    auto it = std::find_if(peerNode->second->activePeers.begin(), peerNode->second->activePeers.end(),
        [&id](const uint64_t &activePeer) { return activePeer == id; });
    if (it == peerNode->second->activePeers.end()) {
        if (checkNodeLinkRounds % NODE_LINK_FAILED_LOG_FREQUENCY == 0) {
            LOG_E("[%s] [NodeStatus] Node %lu is not linked to its peer %lu.",
                GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_STATUS).c_str(), id, peer);
        }
        return false;
    }
    return true;
}

void NodeStatus::UpdateRanktableChangeTime()
{
    int64_t currentTimestamp = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
    ranktableChangeTime.store(currentTimestamp, std::memory_order_relaxed);
}

int64_t NodeStatus::GetRanktableChangeTime() const
{
    return ranktableChangeTime.load(std::memory_order_relaxed);
}

}