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
#include <cstdint>
#include <algorithm>
#include "Logger.h"
#include "Configure.h"
#include "ClusterNodes.h"

static constexpr size_t INS_NUM_MAX = 4096; // 集群实例上限4096个
namespace MINDIE::MS {

bool ClusterNodes::AddInstance(
    uint64_t id,
    const std::string &ip,
    const std::string &port,
    MINDIE::MS::DIGSInstanceRole role,
    const std::string &modelName)
{
    std::unique_lock<std::shared_mutex> lock(mtx);
    auto it = instanceInfos.find(id);
    if (it != instanceInfos.end()) {
        LOG_E("[%s] [ClusterNodes] Add instance failed. Duplicate instance id %lu.",
            GetErrorCode(ErrorType::OPERATION_REPEAT, CoordinatorFeature::CLUSTER_NODES).c_str(),
            id);
        return false;
    }
    try {
        instanceInfos.emplace(std::make_pair(id, std::make_unique<InstanceInfo>(ip, port, role, modelName)));
        ids.emplace_back(id);
        if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            LOG_M("[Add] Add instance D IP %s:%s", ip.c_str(), port.c_str());
        } else if (role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            LOG_M("[Add] Add instance P IP %s:%s", ip.c_str(), port.c_str());
        } else if (role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
            LOG_M("[Add] Add instance flex ip %s:%s", ip.c_str(), port.c_str());
        } else {
            LOG_M("[Add] Add instance IP %s:%s", ip.c_str(), port.c_str());
        }
        return true;
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterNodes] Add instance failed,  error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str(), e.what());
        return false;
    }
}

void ClusterNodes::RemoveInstance(uint64_t id)
{
    auto ip = GetIp(id);
    auto port = GetPort(id);
    auto role = GetRole(id);
    LOG_D("[ClusterNodes] RemoveInstance %lu info - IP: %s, Port: %s, Role: %d", id, ip.c_str(), port.c_str(),
        static_cast<int>(role));

    std::unique_lock<std::shared_mutex> lock(mtx);
    std::list<uint64_t>::const_iterator iter = std::find(ids.begin(), ids.end(), id);
    if (iter == ids.end()) {
        LOG_W("[ClusterNodes] Instance %lu not found in ids list, cannot remove", id);
        return;
    } else {
        ids.erase(iter);
    }
    instanceInfos.erase(id);
    if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        LOG_M("[Remove] Remove D instance IP: %s:%s", ip.c_str(), port.c_str());
    } else if (role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        LOG_M("[Remove] Remove P instance IP: %s:%s", ip.c_str(), port.c_str());
    } else if (role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
        LOG_M("[Remove] Remove flex instance ip: %s:%s", ip.c_str(), port.c_str());
    } else {
        LOG_M("[Remove] Remove instance IP: %s:%s.", ip.c_str(), port.c_str());
    }
}

int64_t ClusterNodes::GetTask(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return -1;
    } else {
        return static_cast<int64_t>(iter->second->tasks.size());
    }
}

bool ClusterNodes::IsFaultyNode(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    const auto& nodeInfo = instanceInfos.find(id);
    if (nodeInfo == instanceInfos.end()) {
        if (faultIds.find(id) == faultIds.end()) {
            LOG_D("[ClusterNodes] Instance %lu is not faulty (not in active list and not in fault list).", id);
            return false;
        } else {
            LOG_D("[ClusterNodes] Instance %lu is faulty (in fault list).", id);
            return true;
        }
    }

    // 检查节点的虚拟ID是否在故障列表中
    if (faultVirtualIds.find(nodeInfo->second->virtualId) != faultVirtualIds.end()) {
        LOG_D("[ClusterNodes] Instance %lu is faulty (virtualId %lu in fault list).", id, nodeInfo->second->virtualId);
        return true;
    }
    return false;
}

bool ClusterNodes::HasInstance(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    return instanceInfos.find(id) != instanceInfos.end();
}

void ClusterNodes::AddFaultNode(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    const auto& nodeInfo = instanceInfos.find(id);
    if (nodeInfo == instanceInfos.end()) {
        LOG_W("[%s] [ClusterNodes] Add fault node failed: Cannot find instance id %lu.",
              GetWarnCode(ErrorType::NOT_FOUND, CoordinatorFeature::CLUSTER_NODES).c_str(),
              id);
        return;
    }

    const auto virtualId = nodeInfo->second->virtualId;
    faultVirtualIds.insert(virtualId);
    faultIds.insert(id);
    virtualIdToDelTimeMap[virtualId] = system_clock::now();
    idToDelTimeMap[id] = system_clock::now();
    LOG_I("[ClusterNodes] Add fault node: instance id %lu, virtual id %lu.", id, virtualId);
}

void ClusterNodes::RemoveFaultNode(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    const auto& nodeInfo = instanceInfos.find(id);
    if (nodeInfo == instanceInfos.end()) {
        LOG_W("[%s] [ClusterNodes] Remove fault node failed: Cannot find instance id %lu.",
              GetWarnCode(ErrorType::NOT_FOUND, CoordinatorFeature::CLUSTER_NODES).c_str(),
              id);
        return;
    }

    const auto virtualId = nodeInfo->second->virtualId;
    virtualIdToDelTimeMap.erase(virtualId);
    faultVirtualIds.erase(virtualId);
    auto allIds = virtualToIdsMap[virtualId];
    for (uint64_t iterId : allIds) {
        LOG_D("[ClusterNodes] Instance %lu restored and removed from failure tracking.", iterId);
        idToDelTimeMap.erase(iterId);
        faultIds.erase(iterId);
    }
    virtualToIdsMap.erase(virtualId);
    LOG_I("[ClusterNodes] Remove fault node: instance id %lu, virtual id %lu.", id, virtualId);
}

std::unordered_set<uint64_t> ClusterNodes::GetVirtualIdToIds(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    const auto& nodeInfo = instanceInfos.find(id);
    if (nodeInfo == instanceInfos.end()) {
        LOG_W("[%s] [ClusterNodes] Get virtual id to ids failed: Cannot find instance id %lu.",
              GetWarnCode(ErrorType::NOT_FOUND, CoordinatorFeature::CLUSTER_NODES).c_str(),
              id);
        return {};
    }

    const auto& virtualId = nodeInfo->second->virtualId;
    const auto& it = virtualToIdsMap.find(virtualId);
    return it != virtualToIdsMap.end() ? it->second : std::unordered_set<uint64_t>{};
}

system_clock::time_point ClusterNodes::GetDeleteTime(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    const auto& nodeInfo = instanceInfos.find(id);
    if (nodeInfo == instanceInfos.end()) {
        if (idToDelTimeMap.find(id) == idToDelTimeMap.end()) {
            LOG_W("[%s] [ClusterNodes] Get delete time failed: Cannot find instance id %lu.",
                  GetWarnCode(ErrorType::NOT_FOUND, CoordinatorFeature::CLUSTER_NODES).c_str(),
                  id);
            return system_clock::now();
        } else {
            return idToDelTimeMap[id];
        }
    }

    const auto& virtualId = nodeInfo->second->virtualId;
    const auto& it = virtualIdToDelTimeMap.find(virtualId);
    return it != virtualIdToDelTimeMap.end() ? it->second : system_clock::now();
}

void ClusterNodes::UpdateExtraInfo(uint64_t id, std::pair<const std::string&, const std::string &> httpParam,
    size_t totalBlockNum, size_t totalSlotsNum, uint64_t virtualId)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        LOG_E("[%s] [ClusterNodes] Update extra information for instance failed. Cannot find instance id %lu.",
              GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::CLUSTER_NODES).c_str(),
              id);
        return;
    }
    iter->second->metricPort = httpParam.first;
    iter->second->interCommPort = httpParam.second;
    iter->second->totalBlockNum = totalBlockNum;
    iter->second->totalSlotsNum = totalSlotsNum;
    iter->second->virtualId = virtualId;
    if (virtualToIdsMap.find(virtualId) == virtualToIdsMap.end()) {
        std::unordered_set<uint64_t> initSet { id };
        virtualToIdsMap[virtualId] = initSet;
    } else {
        virtualToIdsMap[virtualId].insert(id);
    }
}

std::string ClusterNodes::GetIp(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return "";
    } else {
        return iter->second->ip;
    }
}

std::string ClusterNodes::GetPort(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return "";
    } else {
        return iter->second->port;
    }
}

std::string ClusterNodes::GetInterCommPort(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return "";
    } else {
        return iter->second->interCommPort;
    }
}

MINDIE::MS::DIGSInstanceRole ClusterNodes::GetRole(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return MINDIE::MS::DIGSInstanceRole::UN_DEF_INSTANCE;
    } else {
        return iter->second->role;
    }
}

ClusterNodes::RollType ClusterNodes::Roll(const std::vector<uint64_t> &newIds)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    std::vector<uint64_t> addVec;
    std::vector<uint64_t> updateVec;
    std::vector<uint64_t> removeVec;
    for (auto &id : newIds) {
        std::list<uint64_t>::const_iterator iter = std::find(ids.begin(), ids.end(), id);
        if (iter == ids.end()) { // 新增实例列表
            addVec.emplace_back(id);
        } else { // 更新实例列表
            updateVec.emplace_back(id);
        }
    }
    for (auto &id : std::as_const(ids)) {
        auto iter = std::find(newIds.begin(), newIds.end(), id);
        if (iter == newIds.end()) { // 删除实例列表
            removeVec.emplace_back(id);
        }
    }
    return std::make_tuple(addVec, updateVec, removeVec);
}

const std::map<uint64_t, std::unique_ptr<InstanceInfo>> &ClusterNodes::GetInstanceInfos()
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    return instanceInfos;
}

bool ClusterNodes::IsAvailable()
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    std::string deployMode = Configure::Singleton()->schedulerConfig["deploy_mode"]; // 部署模式
    if (deployMode == "pd_separate" || deployMode == "pd_disaggregation" ||
        deployMode == "pd_disaggregation_single_container") {
        bool hasP = false;
        bool hasD = false;
        for (auto it = instanceInfos.begin(); it != instanceInfos.end(); ++it) {
            if (it->second->role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
                hasP = true;
                break;
            }
        }
        for (auto it = instanceInfos.begin(); it != instanceInfos.end(); ++it) {
            if (it->second->role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
                hasD = true;
                break;
            }
        }
        bool isAvailable = hasP && hasD;
        LOG_D("[ClusterNodes] PD mode - Has PREFILL_INSTANCE: %s, Has DECODE_INSTANCE: %s, Available: %s",
              hasP ? "yes" : "no", hasD ? "yes" : "no", isAvailable ? "yes" : "no");
        return isAvailable;
    } else {
        bool isAvailable = !instanceInfos.empty();
        LOG_D("[ClusterNodes] Non-PD mode - Instance count: %lu, Available: %s",
              instanceInfos.size(), isAvailable ? "yes" : "no");
        return isAvailable;
    }
}

uint64_t ClusterNodes::GetId(const std::string &ip, const std::string &port)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    for (auto& it : std::as_const(instanceInfos)) {
        if (it.second->ip == ip && it.second->port == port) {
            return it.first;
        }
    }
    return UINT64_MAX;
}

void ClusterNodes::AddTask(uint64_t id, const std::string &reqId)
{
    std::unique_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return;
    } else {
        iter->second->tasks.emplace(reqId);
    }
}

void ClusterNodes::DecreaseTask(uint64_t id, const std::string &reqId)
{
    std::unique_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return;
    } else {
        auto &instanceInfoTasks = iter->second->tasks;
        auto iter1 = instanceInfoTasks.find(reqId);
        if (iter1 != instanceInfoTasks.end()) {
            instanceInfoTasks.erase(iter1);
        }
    }
}

void ClusterNodes::AddRetry(uint64_t id)
{
    std::unique_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return;
    } else {
        iter->second->retry++;
    }
}

size_t ClusterNodes::GetRetry(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return 0;
    } else {
        return iter->second->retry;
    }
}

std::string ClusterNodes::GetModelName(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return "";
    } else {
        return iter->second->modelName;
    }
}

uint64_t ClusterNodes::GetTokenizerIns()
{
    uint64_t taskMin = UINT64_MAX;
    std::shared_lock<std::shared_mutex> lock(mtx);
    for (auto &it : std::as_const(instanceInfos)) {
        auto &insInfo = it.second;
        uint64_t tasksSize = insInfo->tasks.size();
        if (tasksSize < taskMin) {
            taskMin = tasksSize;
        }
    }
    for (auto &it : std::as_const(instanceInfos)) {
        auto &insInfo = it.second;
        if (insInfo->tasks.size() == taskMin) {
            return it.first;
        }
    }
    return UINT64_MAX;
}

bool ClusterNodes::HasModelName(const std::string &modelName)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    for (auto &it : std::as_const(instanceInfos)) {
        if (GetModelName(it.first) == modelName) {
            return true;
        } else {
            return false;
        }
    }
    return false;
}

std::unordered_set<std::string> ClusterNodes::GetTasksById(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return {};
    } else {
        return iter->second->tasks;
    }
}

size_t ClusterNodes::GetTotalBlockNum(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return 0;
    } else {
        return iter->second->totalBlockNum;
    }
}

size_t ClusterNodes::GetTotalSlotsNum(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    auto iter = instanceInfos.find(id);
    if (iter == instanceInfos.end()) {
        return 0;
    } else {
        return iter->second->totalSlotsNum;
    }
}

int32_t ClusterNodes::RemoveRedundantInsInFlexPeers(nlohmann::json& flexInstance, const std::vector<uint64_t> dumpInfo)
{
    try {
        auto& flexPeerVec = flexInstance.at("dynamic_info").at("peers");
        for (const uint64_t& iter : dumpInfo) {
            auto find = std::find(flexPeerVec.begin(), flexPeerVec.end(), iter);
            if (find != flexPeerVec.end()) {
                flexPeerVec.erase(find);
            }
        }
        return 0;
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterNodes] RemoveRedundantInsInFlexPeers error: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str(), e.what());
        return -1;
    }
}

int32_t ClusterNodes::InstancePreProcInConvertMToD(nlohmann::json::iterator &iter, uint64_t flexId,
                                                   uint64_t flexGroupId, std::vector<uint64_t> &dInsIdVec)
{
    try {
        if (iter->at("static_info").at("group_id").template get<uint64_t>() != flexGroupId) {
            return 0;
        }
        if (iter->at("static_info").at("role").template get<MINDIE::MS::DIGSInstanceRole>() ==
            MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            auto& peerVec = iter->at("dynamic_info").at("peers");
            auto find = std::find(peerVec.begin(), peerVec.end(), flexId);
            if (find != peerVec.end()) {
                *find = DECODE_INS_ID_TRANSFER_BY_FLEX;
            }
        }
        if (iter->at("static_info").at("role").template get<MINDIE::MS::DIGSInstanceRole>() ==
            MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            dInsIdVec.emplace_back(iter->at("id").template get<uint64_t>());
            auto& peerVec = iter->at("dynamic_info").at("peers");
            auto find = std::find(peerVec.begin(), peerVec.end(), flexId);
            if (find != peerVec.end()) {
                peerVec.erase(find);
            }
        }
        return 0;
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterNodes] InstancePreProcInConvertMToD error: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str(), e.what());
        return -1;
    }
}
int32_t ClusterNodes::ConvertMInstanceToD(std::vector<uint64_t>& mNodeIds, nlohmann::json& instance,
                                          nlohmann::json& instances)
{
    try {
        uint64_t flexId = instance.at("id").template get<uint64_t>();
        uint64_t flexGroupId = instance.at("static_info").at("group_id").template get<uint64_t>();
        std::vector<uint64_t> dInsIdVec;
        for (size_t i = 0; i < instances.size(); ++i) {
            auto iter = instances.begin() + i;
            // 收集和Flex同Group的D实例列表、将与Flex同Group的P实例的Peers字段中M的ID改为分裂的D的ID
            if (InstancePreProcInConvertMToD(iter, flexId, flexGroupId, dInsIdVec) != 0) {
                LOG_E("[ClusterNodes] ConvertMInstanceToD InstancePreProcInConvertMToD error!");
                return -1;
            }
        }
        instance.at("static_info").at("role") = MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE;
        instance.at("static_info").at("label") = MINDIE::MS::DIGSInstanceLabel::DECODE_STATIC;
        instance.at("id") = DECODE_INS_ID_TRANSFER_BY_FLEX;
        auto iter = std::find(mNodeIds.begin(), mNodeIds.end(), flexId);
        if (iter != mNodeIds.end()) {
            *iter = DECODE_INS_ID_TRANSFER_BY_FLEX;
        } else {
            LOG_E("[%s] [ClusterNodes] ConvertMInstanceToD: Can't find flex instance in json body.\n",
                  GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str());
            return -1;
        }
        // 当Flex完全转化为D实例时，移除Flex的Peers内的其他同组的D实例ID
        int32_t ret = RemoveRedundantInsInFlexPeers(instance, dInsIdVec);
        if (ret != 0) {
            LOG_E("[ClusterNodes] ConvertMInstanceToD RemoveRedundantInsInFlexPeers error: %d", ret);
            return ret;
        } else {
            LOG_D("[ClusterNodes]ConvertMInstanceToD: Transfer a flex-instance to d-instance done.\n");
            return 0;
        }
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterNodes] ConvertMInstanceToD error: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str(), e.what());
        return -1;
    }
}

int32_t ClusterNodes::ConvertMInstanceToP(nlohmann::json& instance,
                                          nlohmann::json& instances)
{
    try {
        uint64_t flexId = instance.at("id").template get<uint64_t>();
        uint64_t flexGroupId = instance.at("static_info").at("group_id").template get<uint64_t>();
        std::vector<uint64_t> pInsIdVec;
        for (auto iter = instances.begin(); iter != instances.end(); iter++) {
            if (iter->at("static_info").at("group_id").template get<uint64_t>() != flexGroupId ||
                iter->at("static_info").at("role").template get<MINDIE::MS::DIGSInstanceRole>() !=
                MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
                continue;
            }
            // 当Flex完全转化为P实例时，与Flex同组的P实例的Peers字段内需要移除原来存在的Flex的ID
            pInsIdVec.emplace_back(iter->at("id").template get<uint64_t>());
            auto& peerVec = iter->at("dynamic_info").at("peers");
            auto find = std::find_if(peerVec.begin(), peerVec.end(), [flexId](const uint64_t& peer) {
                return peer == flexId;
            });
            if (find != peerVec.end()) {
                peerVec.erase(find);
            }
        }
        instance.at("static_info").at("role") = MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
        instance.at("static_info").at("label") = MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC;
        // 当Flex完全转化为P实例时，移除Flex的Peers内的其他同组的P实例ID
        int32_t ret = RemoveRedundantInsInFlexPeers(instance, pInsIdVec);
        if (ret != 0) {
            LOG_E("[ClusterNodes] ConvertMInstanceToP RemoveRedundantInsInFlexPeers error: %d", ret);
            return ret;
        } else {
            LOG_D("[ClusterNodes]ConvertMInstanceToP: Transfer a flex-instance to p-instance done.\n");
            return 0;
        }
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterNodes] ConvertMInstanceToP error: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str(), e.what());
        return -1;
    }
}

int32_t ClusterNodes::FillInstancesInfoSplitedByFlex(nlohmann::json &instance, nlohmann::json &splitDIns,
                                                     uint64_t flexId, std::vector<uint64_t> pInsIdVec,
                                                     std::vector<uint64_t> dInsIdVec)
{
    try {
        auto pPercentage = static_cast<double>(instance.at("static_info").at("p_percentage").template get<uint64_t>()) /
                                  MINDIE::MS::FLEX_INSTANCE_P_PERCENTAGE_MAX;
        auto flexTotalSlotsNum = instance.at("static_info").at("total_slots_num").template get<uint64_t>();
        auto flexTotalBlockNum = instance.at("static_info").at("total_block_num").template get<uint64_t>();
        auto flexAvaiSlotsNum = instance.at("dynamic_info").at("avail_slots_num").template get<uint64_t>();
        auto flexAvaiBlockNum = instance.at("dynamic_info").at("avail_block_num").template get<uint64_t>();
        instance.at("static_info").at("total_slots_num") = static_cast<uint64_t>(flexTotalSlotsNum * pPercentage);
        instance.at("static_info").at("total_block_num") = static_cast<uint64_t>(flexTotalBlockNum * pPercentage);
        instance.at("dynamic_info").at("avail_slots_num") = static_cast<uint64_t>(flexAvaiSlotsNum * pPercentage);
        instance.at("dynamic_info").at("avail_block_num") = static_cast<uint64_t>(flexAvaiBlockNum * pPercentage);
        instance.at("static_info").at("role") = MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
        instance.at("static_info").at("label") = MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC;
        // 更新Flex及其衍生的D实例中Peers信息，及如果是P-Flex，移除Peers内同组的其他P实例，如果是D-Flex，移除Peers内同组的其他D实例
        RemoveRedundantInsInFlexPeers(instance, pInsIdVec);
        instance.at("dynamic_info").at("peers").emplace_back(DECODE_INS_ID_TRANSFER_BY_FLEX);
        splitDIns.at("static_info").at("total_slots_num") =
            static_cast<uint64_t>(flexTotalSlotsNum * (1 - pPercentage));
        splitDIns.at("static_info").at("total_block_num") =
            static_cast<uint64_t>(flexTotalBlockNum * (1 - pPercentage));
        splitDIns.at("dynamic_info").at("avail_slots_num") =
            static_cast<uint64_t>(flexAvaiSlotsNum * (1 - pPercentage));
        splitDIns.at("dynamic_info").at("avail_block_num") =
            static_cast<uint64_t>(flexAvaiBlockNum * (1 - pPercentage));
        splitDIns.at("static_info").at("role") = MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE;
        splitDIns.at("static_info").at("label") = MINDIE::MS::DIGSInstanceLabel::DECODE_STATIC;
        splitDIns.at("id") = DECODE_INS_ID_TRANSFER_BY_FLEX;
        // 更新Flex及其衍生的D实例中Peers信息，及如果是P-Flex，移除Peers内同组的其他P实例，如果是D-Flex，移除Peers内同组的其他D实例
        RemoveRedundantInsInFlexPeers(splitDIns, dInsIdVec);
        splitDIns.at("dynamic_info").at("peers").emplace_back(flexId);
        return 0;
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterNodes] FillInstancesInfoSplitedByFlex error: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str(), e.what());
        return -1;
    }
}

void ClusterNodes::HandlePInsPeersInSameGroupWithFlex(nlohmann::json::iterator &iter, uint64_t flexId,
                                                      std::vector<uint64_t> &pInsIdVec)
{
    try {
        pInsIdVec.emplace_back(iter->at("id").template get<uint64_t>());
        auto& peerVec = iter->at("dynamic_info").at("peers");
        auto find = std::find(peerVec.begin(), peerVec.end(), flexId);
        if (find != peerVec.end()) {
            *find = DECODE_INS_ID_TRANSFER_BY_FLEX;
        }
        return;
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterNodes] HandlePInsPeersInSameGroupWithFlex error: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str(), e.what());
        return;
    }
}

int32_t ClusterNodes::SplitMInstanceToPAndD(std::vector<uint64_t>& mNodeIds, nlohmann::json& instance,
                                            nlohmann::json& instances)
{
    try {
        uint64_t flexId = instance.at("id").template get<uint64_t>();
        uint64_t flexGroupId = instance.at("static_info").at("group_id").template get<uint64_t>();
        std::vector<uint64_t> pInsIdVec;
        std::vector<uint64_t> dInsIdVec;
        for (size_t i = 0; i < instances.size(); ++i) {
            auto iter = instances.begin() + i;
            if (iter->at("static_info").at("group_id").template get<uint64_t>() != flexGroupId) {
                continue;
            }
            // 收集和Flex同Group的P和D实例列表、将与Flex同Group的P实例的Peers字段中M的ID改为分裂的D的ID
            if (iter->at("static_info").at("role").template get<MINDIE::MS::DIGSInstanceRole>() ==
                MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
                HandlePInsPeersInSameGroupWithFlex(iter, flexId, pInsIdVec);
            }
            if (iter->at("static_info").at("role").template get<MINDIE::MS::DIGSInstanceRole>() ==
                MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
                dInsIdVec.emplace_back(iter->at("id").template get<uint64_t>());
            }
        }
        nlohmann::json splitDIns(instance);
        // 填充由Flex分裂来的P和D的两个实例的信息
        int32_t ret = FillInstancesInfoSplitedByFlex(instance, splitDIns, flexId, pInsIdVec, dInsIdVec);
        if (ret != 0) {
            LOG_E("[ClusterNodes] SplitMInstanceToPAndD FillInstancesInfoSplitedByFlex error: %d.\n", ret);
            return ret;
        }
        mNodeIds.emplace_back(DECODE_INS_ID_TRANSFER_BY_FLEX);
        instances.emplace_back(splitDIns);
        LOG_D("[ClusterNodes]SplitMInstanceToPAndD: Transfer a flex-instance to p&d-instance done.\n");
        return 0;
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterNodes] SplitMInstanceToPAndD error: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str(), e.what());
        return -1;
    }
}

int32_t ClusterNodes::ProcessFlexInstance(std::vector<uint64_t>& nodeIds, nlohmann::json& instances)
{
    try {
        auto it = std::find_if(instances.begin(), instances.end(), [](const nlohmann::json& ins) {
            return ins.at("static_info").at("role").template get<MINDIE::MS::DIGSInstanceRole>() ==
                MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE;
        });
        if (it == instances.end()) {
            return 0;
        }
        auto id = it->at("id").template get<uint64_t>();
        auto pPercentage = it->at("static_info").at("p_percentage").template get<uint64_t>();
        // ClusterNode里记录Flex实例相关信息(如果本轮是删除FLEX实例，则在具体实例删除时清除record内的记录)
        UpdateClusterFlexInstanceInfo(id, pPercentage);
        LOG_D("[ClusterNodes] Found a flex-instance, id is %lu, p_percentage is %lu.\n", id, pPercentage);

        if (pPercentage > MINDIE::MS::FLEX_INSTANCE_P_PERCENTAGE_MAX) {
            LOG_E("[ClusterNodes] ProcessFlexInstance: p_percentage %lu out of range[0, 100].\n", pPercentage);
            return -1;
        } else if (pPercentage == MINDIE::MS::FLEX_INSTANCE_P_PERCENTAGE_MAX) {
            LOG_D("[ClusterNodes] Transfer a flex-instance to p-instance, id is %lu.\n", id);
            return ConvertMInstanceToP(*it, instances);
        } else if (pPercentage == 0) {
            LOG_D("[ClusterNodes] Transfer a flex-instance to d-instance, id is %lu.\n", id);
            return ConvertMInstanceToD(nodeIds, *it, instances);
        } else {
            LOG_D("[ClusterNodes] Transfer a flex-instance to p-instance and d-instance, id is %lu.\n", id);
            return SplitMInstanceToPAndD(nodeIds, *it, instances);
        }
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterNodes] AddInstance error: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CLUSTER_NODES).c_str(), e.what());
        return -1;
    }
}

void ClusterNodes::UpdateClusterFlexInstanceInfo(uint64_t oriFlexId, uint64_t pPercentage)
{
    std::unique_lock<std::shared_mutex> lock(mtx);
    clusterFlexInsInfo.clusterHasFlex = true;
    clusterFlexInsInfo.pPercentage = pPercentage;
    clusterFlexInsInfo.originFlexInsId = oriFlexId;
    clusterFlexInsInfo.splitDInsId = DECODE_INS_ID_TRANSFER_BY_FLEX;
}

void ClusterNodes::ClearClusterFlexInstanceInfo()
{
    std::unique_lock<std::shared_mutex> lock(mtx);
    clusterFlexInsInfo.clusterHasFlex = false;
    clusterFlexInsInfo.pPercentage = 0;
    clusterFlexInsInfo.originFlexInsId = 0;
}

bool ClusterNodes::IsClusterHasFlex()
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    return clusterFlexInsInfo.clusterHasFlex;
}

uint64_t ClusterNodes::GetOriFlexInsId()
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    return clusterFlexInsInfo.originFlexInsId;
}

uint64_t ClusterNodes::GetSplitedDInsId()
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    return clusterFlexInsInfo.splitDInsId;
}

bool ClusterNodes::IsFlexSplitedIntoTwoInstance()
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    return clusterFlexInsInfo.clusterHasFlex && clusterFlexInsInfo.pPercentage != 0 &&
           clusterFlexInsInfo.pPercentage != MINDIE::MS::FLEX_INSTANCE_P_PERCENTAGE_MAX;
}

bool ClusterNodes::IsVecContainsFlex(const std::vector<uint64_t>& vec)
{
    bool isPFound = false;
    bool isDFound = false;
    std::shared_lock<std::shared_mutex> lock(mtx);
    if (clusterFlexInsInfo.pPercentage == 0) {
        isPFound = true;
    }
    if (clusterFlexInsInfo.pPercentage == MINDIE::MS::FLEX_INSTANCE_P_PERCENTAGE_MAX) {
        isDFound = true;
    }
    for (auto iter:vec) {
        if (iter == clusterFlexInsInfo.originFlexInsId) {
            isPFound = true;
        }
        if (iter == clusterFlexInsInfo.splitDInsId) {
            isDFound = true;
        }
    }
    return isPFound && isDFound;
}

bool ClusterNodes::IsBothPAndDFromFlex(uint64_t pId, uint64_t dId)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    return pId == clusterFlexInsInfo.originFlexInsId && dId == clusterFlexInsInfo.splitDInsId;
}

bool ClusterNodes::IsInstanceFromFlex(uint64_t id)
{
    std::shared_lock<std::shared_mutex> lock(mtx);
    return clusterFlexInsInfo.clusterHasFlex &&
           (id == clusterFlexInsInfo.splitDInsId || id == clusterFlexInsInfo.originFlexInsId);
}

size_t ClusterNodes::GetInsNumMax(void)
{
    if (IsFlexSplitedIntoTwoInstance()) {
        return INS_NUM_MAX + 1;
    }
    return INS_NUM_MAX;
}

void ClusterNodes::ProcSchedulerInfoUnderFlexSituation(std::vector<MINDIE::MS::DIGSInstanceScheduleInfo>& schedulerInfo)
{
    if (!IsClusterHasFlex()) {
        return;
    }
    auto oriFlexId = GetOriFlexInsId();
    auto splitedDInsId = GetSplitedDInsId();
    if (IsFlexSplitedIntoTwoInstance()) {
        auto flexIter = std::find_if(schedulerInfo.begin(), schedulerInfo.end(),
            [oriFlexId](const MINDIE::MS::DIGSInstanceScheduleInfo& info) {
            return info.id == oriFlexId;
        });
        auto splitDIter = std::find_if(schedulerInfo.begin(), schedulerInfo.end(),
            [splitedDInsId](const MINDIE::MS::DIGSInstanceScheduleInfo& info) {
            return info.id == splitedDInsId;
        });
        if (flexIter == schedulerInfo.end()) {
            LOG_E("[ClusterNodes] ProcSchedulerInfoUnderFlexSituation: cannot find ins with id %lu.\n", oriFlexId);
            return;
        }
        if (splitDIter == schedulerInfo.end()) {
            LOG_E("[ClusterNodes] ProcSchedulerInfoUnderFlexSituation: cannot find ins with id %lu.\n", splitedDInsId);
            return;
        }
        flexIter->allocatedSlots += splitDIter->allocatedSlots;
        flexIter->allocatedBlocks += splitDIter->allocatedBlocks;
        schedulerInfo.erase(splitDIter);
    } else {
        // 若Flex不是拆成P+D的情况，那么这里只需要处理化作D的情况，P不用额外处理
        auto splitDIter = std::find_if(schedulerInfo.begin(), schedulerInfo.end(),
            [splitedDInsId](const MINDIE::MS::DIGSInstanceScheduleInfo& info) {
            return info.id == splitedDInsId;
        });
        if (splitDIter != schedulerInfo.end()) {
            splitDIter->id = oriFlexId;
        }
    }
    return;
}

void ClusterNodes::ProcInstanceIdsUnderFlexSituation(std::vector<uint64_t>& nodeIds)
{
    if (!IsClusterHasFlex()) {
        return;
    }
    uint64_t flexId = GetOriFlexInsId();
    auto find = std::find(nodeIds.begin(), nodeIds.end(), flexId);
    // 当Flex被拆分成P+D的形式时，直接在数组中添加D实例的ID
    if (IsFlexSplitedIntoTwoInstance() && find != nodeIds.end()) {
        nodeIds.emplace_back(GetSplitedDInsId());
        return;
    }
    // 当Flex为单独的P或D的形式时，只需要处理D的情况，这时将Flex的实例ID替换为D的；P的情况保持原状
    if (clusterFlexInsInfo.pPercentage == 0 && find != nodeIds.end()) {
        *find = GetSplitedDInsId();
    }
}

int64_t ClusterNodes::GetInstanceTaskNumUnderFlexSituation(uint64_t id)
{
    if (!IsClusterHasFlex() || id != GetOriFlexInsId()) {
        return GetTask(id);
    }
    uint64_t oriFlexId = GetOriFlexInsId();
    uint64_t splitedDId = GetSplitedDInsId();
    if (IsFlexSplitedIntoTwoInstance()) {
        return GetTask(oriFlexId) + GetTask(splitedDId);
    }
    if (clusterFlexInsInfo.pPercentage == 0) {
        return GetTask(splitedDId);
    } else {
        return GetTask(oriFlexId);
    }
}

void ClusterNodes::ProcTaskQuaryDInstanceIdUnderFlexSituation(uint64_t& dId)
{
    if (IsClusterHasFlex() && dId == clusterFlexInsInfo.originFlexInsId) {
        dId = DECODE_INS_ID_TRANSFER_BY_FLEX;
    }
}
}