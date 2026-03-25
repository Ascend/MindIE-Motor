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
#include "NodeScheduler.h"
#include <chrono>
#include <string>
#include <algorithm>
#include <thread>
#include <set>
#include <csignal>
#include <numeric>
#include <vector>
#include <unordered_set>
#include <nlohmann/json.hpp>
#include "Logger.h"
#include "AlarmManager.h"
#include "GroupGeneratorFactory.h"
#include "GroupGenerator.h"
#include "ControllerConfig.h"
#include "ServerRequestHandler.h"
#include "ProcessManager.h"
#include "RoleManagerInitializer.h"
#include "CoordinatorRequestHandler.h"
#include "FaultManager.h"
#include "ResourceManager.h"
#include "DpGroupingUtil.h"
#include "NPURecoveryManager.h"
#include "Util.h"

namespace MINDIE::MS {
constexpr int32_t CODE_OK = 200;
constexpr size_t MAX_RATE = 15;
constexpr uint32_t MIN_SERVER_NODES = 2;
constexpr uint32_t MIN_SERVER_NODES_HAS_FLEX = 1;
constexpr int64_t MAX_SERVER_NUMBER = 6 * 16 * 3; // 集群信息表全部变更的场景下，可能出现全部替换
constexpr int64_t MIN_INT_VALUE = 0;
constexpr int64_t MAX_INT_VALUE = 4294967295;
constexpr uint64_t DP_GROUP_NUM = 10000;
constexpr size_t MAX_RELOAD_GRT_TIME = 30;
constexpr size_t MAX_LINK_SERVER_TIME = 2;
constexpr std::chrono::seconds NODE_SCHEDULER_ALARM_INTERVAL_SEC{5};
constexpr std::chrono::seconds RANK_TABLE_MONITOR_INTERVAL_SEC{5};
constexpr uint32_t ROLE_SEND_RETRY_INTERVAL_SEC{3};
using ExitCallback = std::function<void()>;
ExitCallback g_exitCallback = nullptr;
void SignalHandle(int signo)
{
    if (g_exitCallback != nullptr) {
        g_exitCallback();
    }
    LOG_I("[NodeScheduler] Receive signal %d, exit.", signo);
}
NodeScheduler::NodeScheduler(std::shared_ptr<NodeStatus> nodeStatusInit,
    std::shared_ptr<CoordinatorStore> coordinatorStoreInit) : mNodeStatus(nodeStatusInit),
    mCoordinatorStore(coordinatorStoreInit)
{
    LOG_I("[NodeScheduler] Create successfully.");
}

NodeScheduler::~NodeScheduler()
{
    Stop();
    LOG_I("[NodeScheduler] Destroy successfully.");
}

void NodeScheduler::Stop()
{
    mRun.store(false);
    monitorRankTableRunning = false;
    if (monitorRankTableThread.joinable()) {
        monitorRankTableThread.join();
    }
    nodeSchedulerAlarmThreadRunning = false;
    if (nodeSchedulerAlarmThread.joinable()) {
        nodeSchedulerAlarmThread.join();
    }
    LOG_I("[NodeScheduler] Stop successfully.");
}

static int32_t UpdateServerInfo(std::vector<std::unique_ptr<NodeInfo>> &serverNodes,
    std::vector<MINDIE::MS::DIGSRoleDecision> &tmpRoleDecisions,
    std::vector<std::pair<std::vector<uint64_t>, std::vector<uint64_t>>> &groups,
    std::vector<std::vector<uint64_t>> &flexGroups)
{
    std::map<uint64_t, uint32_t> instanceIdToIndex;
    for (uint32_t i = 0; i < tmpRoleDecisions.size(); i++) {
        instanceIdToIndex[tmpRoleDecisions[i].id] = i;
    }

    // 更新节点的身份信息
    for (uint32_t i = 0; i < serverNodes.size(); i++) {
        if (serverNodes[i] == nullptr) {
            continue;
        }
        auto iter = instanceIdToIndex.find(serverNodes[i]->instanceInfo.staticInfo.id);
        if (iter == instanceIdToIndex.end()) {
            LOG_E("[%s] [NodeScheduler] Updating server informaiton, instances ID %lu, IP %s is not found in "
                "role decisions", GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_SCHEDULER).c_str(),
                serverNodes[i]->instanceInfo.staticInfo.id,
                serverNodes[i]->ip.c_str());
            return -1;
        }
        if (iter->second >= tmpRoleDecisions.size()) {
            LOG_E("[%s] [NodeScheduler] index %lu Index out of bounds %lu ", iter->second, tmpRoleDecisions.size());
            return -1;
        }
        uint64_t groupId = tmpRoleDecisions[iter->second].groupId;
        if (groupId >= flexGroups.size()) {
            LOG_E("[%s] [NodeScheduler] groupId %lu Index out of bounds %lu ", groupId, flexGroups.size());
            return -1;
        }
        auto flexGroup = flexGroups[groupId];
        serverNodes[i]->instanceInfo.staticInfo.role = tmpRoleDecisions[iter->second].role;
        serverNodes[i]->instanceInfo.staticInfo.groupId = groupId;
        serverNodes[i]->instanceInfo.staticInfo.flexPRatio = tmpRoleDecisions[iter->second].flexPRatio;
        if (serverNodes[i]->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            serverNodes[i]->instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC;
            serverNodes[i]->peers = groups[groupId].second;
            serverNodes[i]->peers.insert(serverNodes[i]->peers.end(), flexGroup.begin(), flexGroup.end());
        } else if (serverNodes[i]->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            serverNodes[i]->instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::DECODE_STATIC;
            serverNodes[i]->peers = groups[groupId].first;
            serverNodes[i]->peers.insert(serverNodes[i]->peers.end(), flexGroup.begin(), flexGroup.end());
        } else if (serverNodes[i]->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
            serverNodes[i]->instanceInfo.staticInfo.label = MINDIE::MS::DIGSInstanceLabel::FLEX_STATIC;
            serverNodes[i]->peers = groups[groupId].first;
            std::vector<uint64_t> dNodes = groups[groupId].second;
            serverNodes[i]->peers.insert(serverNodes[i]->peers.end(), dNodes.begin(), dNodes.end());
        }
        LOG_I("[NodeScheduler] Updated server information: "
            "instance index %u, ID %lu, IP %s, role %c, label %d, peers %zu, "
            "group ID %lu.", i, serverNodes[i]->instanceInfo.staticInfo.id, serverNodes[i]->ip.c_str(),
            serverNodes[i]->instanceInfo.staticInfo.role, serverNodes[i]->instanceInfo.staticInfo.label,
            serverNodes[i]->peers.size(), groupId);
    }
    return 0;
}

int32_t NodeScheduler::AllocateDpGroup(std::vector<std::unique_ptr<NodeInfo>> &serverNodes) const
{
    for (size_t i = 0; i < serverNodes.size(); ++i) {
        if (serverNodes[i] == nullptr) {
            continue;
        }
        if (DpGroupingUtil::ProcessSingleNodeDpGrouping(serverNodes[i]) != 0) {
            return -1;
        }
    }
    return 0;
}

static size_t GetGreatestCommonDivisor(size_t a, size_t b)
{
    if (b == 0) {
        return a;
    }
    return GetGreatestCommonDivisor(b, a % b);
}

static int32_t ParsePDRateForHeterogeneous(size_t &pRate, size_t &dRate,
    const std::vector<MINDIE::MS::DIGSRoleDecision> &decisions)
{
    std::vector<uint64_t> pIndex;
    std::vector<uint64_t> dIndex;
    for (auto& decision : std::as_const(decisions)) {
        if (decision.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            pIndex.push_back(decision.id);
        } else {
            dIndex.push_back(decision.id);
        }
    }
    if (pIndex.empty() || dIndex.empty()) {
        LOG_E("[%s] [NodeScheduler] Prefill node size %zu or decode node size %zu should not be 0.",
              GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::NODE_SCHEDULER).c_str(),
              pIndex.size(), dIndex.size());
        return -1;
    }
    size_t gcdValue = GetGreatestCommonDivisor(pIndex.size(), dIndex.size());
    pRate = pIndex.size() / gcdValue;
    dRate = dIndex.size() / gcdValue;
    if (pRate <= MAX_RATE && dRate <= MAX_RATE) {
        LOG_I("[NodeScheduler] PD rate for heterogeneous: prefill rate %zu, decode rate %zu.", pRate, dRate);
        return 0;
    }
    if (pRate > MAX_RATE) {
        pRate = MAX_RATE;
    }
    if (dRate > MAX_RATE) {
        dRate = MAX_RATE;
    }
    gcdValue = GetGreatestCommonDivisor(pRate, dRate);
    pRate = pRate / gcdValue;
    dRate = dRate / gcdValue;
    LOG_I("[NodeScheduler] PD rate for heterogeneous: prefill rate %zu, decode rate %zu.", pRate, dRate);
    return 0;
}

static std::map<uint64_t, std::vector<std::unique_ptr<NodeInfo>>> MergeServersByGroupId(
    std::vector<std::unique_ptr<NodeInfo>> &nodeVec)
{
    std::map<uint64_t, std::vector<std::unique_ptr<NodeInfo>>> ret;
    for (auto &node : nodeVec) {
        ret[node->instanceInfo.staticInfo.groupId].push_back(std::move(node));
    }
    return ret;
}


int32_t NodeScheduler::WaitForRoleDecision(std::vector<std::unique_ptr<NodeInfo>> &serverNodes,
    size_t pRate, size_t dRate, std::vector<std::pair<std::vector<uint64_t>, std::vector<uint64_t>>> &groups,
    std::vector<std::vector<uint64_t>> &flexGroups)
{
    LOG_I("[NodeScheduler] Starting role decision waiting process.");
    mWaitSeconds.store(10); // 身份决策最多等待10s
    while (mWaitSeconds.load() > 0 && (GetRoleDecisionsSize() != serverNodes.size())) {
        std::this_thread::sleep_for(std::chrono::seconds(1)); // 1s检查一次
        mWaitSeconds--;
    }
    LOG_I("[NodeScheduler] Completed role decision waiting process.");
    auto tmpRoleDecisions = GetRoleDecisions();
    if (tmpRoleDecisions.size() != serverNodes.size()) {
        LOG_E("[%s] [NodeScheduler] Mismatch in decisions count (%zu) vs servers count (%zu).",
              GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::NODE_SCHEDULER).c_str(),
              tmpRoleDecisions.size(), serverNodes.size());
        return -1;
    }
    std::string errorCode = GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER);
    if (ControllerConfig::GetInstance()->GetDIGSIsHeterogeneous() &&
        ParsePDRateForHeterogeneous(pRate, dRate, tmpRoleDecisions) != 0) {
        LOG_E("[%s] [NodeScheduler] Failed to parse prefill decode rate in heterogeneous mode.", errorCode.c_str());
        return -1;
    }
    auto type = ControllerConstant::GetInstance()->GetGroupGeneratorTypeDefault();
    auto obj = GroupGeneratorFactory::GetInstance().CreateGroupGenerator(type);
    if (obj == nullptr) {
        LOG_E("[%s] [NodeScheduler] Create group generator %s failed.", errorCode.c_str(), type.c_str());
        return -1;
    }

    if (obj->GenerateGroups(tmpRoleDecisions, groups, flexGroups) != 0) {
        LOG_E("[%s] [NodeScheduler] Generate groups failed.", errorCode.c_str());
        return -1;
    }
    if (UpdateServerInfo(serverNodes, tmpRoleDecisions, groups, flexGroups) != 0) {
        LOG_E("[%s] [NodeScheduler] Update server information failed.", errorCode.c_str());
        return -1;
    }
    return 0;
}

int32_t NodeScheduler::InitRoleAndRoleManager(std::vector<std::unique_ptr<NodeInfo>> &serverNodes,
    size_t &pRate, size_t &dRate, bool isRecovering)
{
    ControllerConfig::GetInstance()->ParsePDRate(pRate, dRate);
    if (!ControllerConfig::GetInstance()->IsValidPRateAndDRate(pRate, dRate)) {
        LOG_E("[%s] [NodeScheduler] Init role and role manager failed, P rate %zu or D rate %zu is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::NODE_SCHEDULER).c_str(), pRate, dRate);
        return -1;
    }
    auto method = ControllerConfig::GetInstance()->GetRoleDecisionMethod();
    if (method != ControllerConstant::GetInstance()->GetDigsRoleDecisionMethod()) {
        LOG_E("[%s] [NodeScheduler] Init role and role manager failed, role decision method %s is not supported.",
            GetErrorCode(ErrorType::UNAVAILABLE, ControllerFeature::NODE_SCHEDULER).c_str(), method.c_str());
        return -1;
    }
    if (InitInstanceRoleManager(serverNodes, pRate, dRate, isRecovering) != 0) {
        LOG_E("[%s] [NodeScheduler] Init role and role manager failed, init digs role manager failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }
    return 0;
}

int32_t InitCrossNodeRolesForDifferencePDCrossNodeSize(std::vector<std::unique_ptr<NodeInfo>> &serverNodes,
    const uint32_t &pNodeSize, const uint32_t &dNodeSize)
{
    LOG_D("[NodeScheduler] InitCrossNodeRolesForDifferencePDCrossNodeSize.");
    for (auto &node : serverNodes) {
        if (node == nullptr) {
            continue;
        }
        if (node->serverInfoList.size() == pNodeSize) {
            node->instanceInfo.staticInfo.role = MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
        } else if (node->serverInfoList.size() == dNodeSize) {
            node->instanceInfo.staticInfo.role = MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE;
        } else {
            LOG_E("[%s] [NodeScheduler] InitCrossNodeRolesForDifferencePDCrossNodeSize: invalid cross node size %u.",
                GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str(),
                node->serverInfoList.size());
            return -1;
        }
    }
    return 0;
}

int32_t NodeScheduler::InitMultiPDRole(std::vector<std::unique_ptr<NodeInfo>> &serverNodes) const
{
    if (ControllerConfig::GetInstance()->GetPIsDistribute() || ControllerConfig::GetInstance()->GetDIsDistribute()) {
        return 0;
    }
    bool isMultiNodeMode = ControllerConfig::GetInstance()->IsMultiNodeMode();
    uint32_t pNodeSize = ControllerConfig::GetInstance()->GetPNodeNum();
    uint32_t dNodeSize = ControllerConfig::GetInstance()->GetDNodeNum();
    if (isMultiNodeMode && pNodeSize != dNodeSize) {
        if (InitCrossNodeRolesForDifferencePDCrossNodeSize(serverNodes, pNodeSize, dNodeSize) != 0) {
            LOG_E("[%s] [NodeScheduler] InitMultiPDRole: init roles for cross node p/d",
                GetErrorCode(ErrorType::RESOURCE_LIMIT, ControllerFeature::NODE_SCHEDULER).c_str());
            return -1;
        }
    }
    return 0;
}

int32_t NodeScheduler::PDModeInit(std::vector<std::unique_ptr<NodeInfo>> &serverNodes)
{
    LOG_I("[NodeScheduler] Available node count is %zu", serverNodes.size());
    if (serverNodes.size() < MIN_SERVER_NODES) {
        LOG_E("[%s] [NodeScheduler] Available nodes (%zu) are less than the required minimum (%u).",
            GetErrorCode(ErrorType::RESOURCE_LIMIT, ControllerFeature::NODE_SCHEDULER).c_str(),
            serverNodes.size(), MIN_SERVER_NODES);
        return -1;
    }
    InitMultiPDRole(serverNodes);
    size_t pRate;
    size_t dRate;
    if (InitRoleAndRoleManager(serverNodes, pRate, dRate) != 0) {
        return -1;
    }
    std::vector<std::pair<std::vector<uint64_t>, std::vector<uint64_t>>> groups;
    std::vector<std::vector<uint64_t>> flexGroups;
    WaitForRoleDecision(serverNodes, pRate, dRate, groups, flexGroups);
    
    // 根据身份和dp大小为每个Server的device进行dp分组
    if (ControllerConfig::GetInstance()->IsMultiNodeMode() && AllocateDpGroup(serverNodes) != 0) {
        LOG_E("[%s] [NodeScheduler] PDModeInit: Allocate dp group for cluster failed.",
              GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }

    if (ControllerConfig::GetInstance()->GetProcessManagerConfig().toFile) {
        ProcessManager::GetInstance()->SaveServerListToFile(serverNodes);
    }
    CoordinatorRequestHandler::GetInstance()->SetRunStatus(true);

    SendRole(serverNodes, groups, flexGroups);
    return 0;
}

static bool IsRecoverNodesChosen(const std::vector<std::unique_ptr<NodeInfo>> &nodes)
{
    bool isAnyNodeInitialized = false;
    for (auto& node : nodes) {
        if (node == nullptr || node->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN)) {
            continue;
        }
        isAnyNodeInitialized = true;
        LOG_W("[%s] [NodeScheduler] Node %lu has role %c, status %s.",
              GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_SCHEDULER).c_str(),
              node->instanceInfo.staticInfo.id, node->instanceInfo.staticInfo.role, node->roleState.c_str());
    }
    return isAnyNodeInitialized;
}

int32_t NodeScheduler::Init(DeployMode mode)
{
    g_exitCallback = std::bind(&NodeScheduler::Stop, this);
    std::signal(SIGINT, SignalHandle);
    std::signal(SIGTERM, SignalHandle);
    try {
        mServerClient = std::make_shared<HttpClient>();
        mCoordinatorClient = std::make_shared<HttpClient>();
        mRankTableLoader = RankTableLoader::GetInstance();
        mRoleSwitcher = std::make_unique<RoleSwitcher>(mNodeStatus, mCoordinatorStore);
    } catch (const std::exception& e) {
        LOG_E("[%s] [NodeScheduler] Initialize failed because create pointer failed.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }
    if (mServerClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0 ||
        mCoordinatorClient->Init("", "", ControllerConfig::GetInstance()->GetRequestCoordinatorTlsItems()) != 0 ||
        mRoleSwitcher->Init() != 0) {
        LOG_E("[%s] [NodeScheduler] Initialize failed because initialize server client or coordinator client or role "
            "switcher failed", GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }
    mDeployMode = mode;
    if (mode == DeployMode::SINGLE_NODE) {
        ServerRequestHandler::GetInstance()->Init(nullptr, nullptr);
        return 0;
    }
    ServerRequestHandler::GetInstance()->Init(
        std::bind(&NodeScheduler::RecordRoleUnknown, this, std::placeholders::_1, std::placeholders::_2),
        std::bind(&NodeScheduler::DeleteRoleUnknown, this, std::placeholders::_1, std::placeholders::_2));

    monitorRankTableThread = std::thread(&NodeScheduler::MonitorRankTable, this);
    nodeSchedulerAlarmThread = std::thread(&NodeScheduler::NodeSchedulerAlarm, this);
    return 0;
}

static void SetUpTotalAvailableResourceForNode(NodeInfo &nodeInfo)
{
    nodeInfo.instanceInfo.staticInfo.totalSlotsNum = nodeInfo.instanceInfo.dynamicInfo.availSlotsNum;
    nodeInfo.instanceInfo.staticInfo.totalBlockNum = nodeInfo.instanceInfo.dynamicInfo.availBlockNum;
}

static void SetUpTotalAvailableResource(std::vector<std::unique_ptr<NodeInfo>> &nodeVec)
{
    for (auto &node : nodeVec) {
        SetUpTotalAvailableResourceForNode(*node);
    }
}

int32_t NodeScheduler::SingleModeInit(std::vector<std::unique_ptr<NodeInfo>> &availableNodesAfterCollectDynamicInfo,
    std::vector<std::unique_ptr<NodeInfo>> &faultyNodesAfterCollectStaticInfo,
    std::vector<std::unique_ptr<NodeInfo>> &faultyNodesAfterCollectingDynamicInfo)
{
    auto processFile = ProcessManager::GetInstance()->LoadProcessFile();
    if (processFile.empty()) {
        SetUpTotalAvailableResource(availableNodesAfterCollectDynamicInfo);
        mNodeStatus->AddNodes(availableNodesAfterCollectDynamicInfo);
        mNodeStatus->AddFaultyNodes(faultyNodesAfterCollectStaticInfo);
        mNodeStatus->AddFaultyNodes(faultyNodesAfterCollectingDynamicInfo);
        CoordinatorRequestHandler::GetInstance()->SetRunStatus(true);
        return 0;
    } else {
        LOG_I("[NodeScheduler] Start to recover servers for single node mode.");
        std::vector<std::unique_ptr<NodeInfo>> availableNodes;
        auto ret = ProcessManager::GetInstance()->GetTotalAvailableResource(processFile,
            availableNodesAfterCollectDynamicInfo, availableNodes);
        if (ret != 0) {
            LOG_E("[%s] [NodeScheduler] Load status file failed. Please remove process file or "
                "restart servers.", GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
            return -1;
        }
        mNodeStatus->AddNodes(availableNodes);
        mNodeStatus->AddFaultyNodes(faultyNodesAfterCollectStaticInfo);
        mNodeStatus->AddFaultyNodes(faultyNodesAfterCollectingDynamicInfo);
        CoordinatorRequestHandler::GetInstance()->SetRunStatus(true);
        return 0;
    }
}

int32_t NodeScheduler::GetNodesFromRankTable(std::vector<std::unique_ptr<NodeInfo>> &availableNodes,
    std::vector<std::unique_ptr<NodeInfo>> &faultyNodes)
{
    std::vector<std::unique_ptr<NodeInfo>> serverNodes;
    for (size_t i = 0; i < MAX_RELOAD_GRT_TIME; i++) {
        serverNodes.clear();
        RestIptoIdMap();
        if (mRankTableLoader->LoadRankTable(serverNodes, mCoordinatorStore) != 0) {
            if (i == MAX_RELOAD_GRT_TIME - 1) {
                return -1;
            }
            LOG_E("[%s] [NodeScheduler] Initializing server cluster failed, load rank table failed, retry times %u.",
                GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str(), i);
            continue;
        }
        availableNodes.clear();
        faultyNodes.clear();

        ServerRequestHandler::GetInstance()->GetAvailableNodes(*mServerClient, serverNodes, availableNodes,
            faultyNodes, mNodeStatus, MAX_LINK_SERVER_TIME);

        if (faultyNodes.empty() && (serverNodes.size() == availableNodes.size())) {
            LOG_I("[NodeScheduler] Successfully loaded rank table and obtained %zu available nodes. "
                "No faulty nodes detected.", availableNodes.size());
            break;
        }
        LOG_W("[%s] [NodeScheduler] Some nodes are unavailable, try reload rank table for the %u times.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str(), i);
    }
    return 0;
}

int32_t NodeScheduler::InitServerCluster()
{
    std::vector<std::unique_ptr<NodeInfo>> availableNodes;
    std::vector<std::unique_ptr<NodeInfo>> faultyNodes;
    if (GetNodesFromRankTable(availableNodes, faultyNodes) != 0) {
        LOG_E("[%s] [NodeScheduler] Get available nodes from rank table failed!",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }
    std::vector<std::unique_ptr<InstanceInfo>> instanceInfoList =
        mRankTableLoader->GetInstanceInfoListByRankTable();
    ResourceManager::GetInstance()->Init(instanceInfoList);

    if (mDeployMode == DeployMode::SINGLE_NODE) {
        LOG_I("[NodeScheduler] Initializing server cluster, number of available nodes is %zu.",
            availableNodes.size());
        return SingleModeInit(availableNodes, faultyNodes,
            faultyNodes);
    }
    bool hasFlex = ControllerConfig::GetInstance()->GetHasFlex();
    if ((!hasFlex && availableNodes.size() < MIN_SERVER_NODES) ||
        (hasFlex && availableNodes.size() < MIN_SERVER_NODES_HAS_FLEX)) {
        LOG_E("[%s] [NodeScheduler] Initializing server cluster: number of available nodes %zu is less than %u",
              GetErrorCode(ErrorType::RESOURCE_LIMIT, ControllerFeature::NODE_SCHEDULER).c_str(), availableNodes.size(),
              (hasFlex) ? MIN_SERVER_NODES_HAS_FLEX : MIN_SERVER_NODES);
        return -1;
    }
    if (IsRecoverNodesChosen(availableNodes)) {
        LOG_I("[NodeScheduler] Initializing server cluster, start to recover servers.");
        auto processFile = ProcessManager::GetInstance()->LoadProcessFile();
        if (RecoverServerCluster(processFile, availableNodes) != 0) {
            return -1;
        }
        mNodeStatus->AddFaultyNodes(faultyNodes);
        CoordinatorRequestHandler::GetInstance()->SetRunStatus(true);
        // 灵衢故障读取入口
        if (ControllerConfig::GetInstance()->GetFaultRecoveryEnableByConfigKey("lingqu_link")) {
            RecoveryNPUFaultID();
        }
        return 0;
    }
    LOG_I("[NodeScheduler] Initializing server cluster, start to initialize servers.");
    if (PDModeInit(availableNodes) != 0) {
        return -1;
    }

    mNodeStatus->AddFaultyNodes(faultyNodes);
    return 0;
}

int32_t NodeScheduler::RecoveryNPUFaultID()
{
    try {
        nlohmann::json faultsJson = NPURecoveryManager::GetInstance()->LoadProcessedSwitchFaults();
        if (faultsJson.empty()) {
            LOG_W("[NodeScheduler] FaultsJson is empty, keeping existing set");
            return -1;
        }
        if (faultsJson.contains("processed_switch_faults") &&
            faultsJson["processed_switch_faults"].is_array()) {
            std::vector<std::string> newFaults;
            for (const auto& faultId : faultsJson["processed_switch_faults"]) {
                if (faultId.is_string()) {
                    newFaults.push_back(faultId.get<std::string>());
                }
            }
            // 替换
            NPURecoveryManager::GetInstance()->SetProcessedSwitchFaults(newFaults);
            LOG_I("[NodeScheduler] Recovery NPU fault IDs successfully, count: %zu",
                  NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults().size());
            return 0;
        } else {
            LOG_W("[NodeScheduler] Invalid faults data structure, keeping existing set");
            return -1;
        }
    } catch (const std::exception& e) {
        LOG_E("[NodeScheduler] Recovery NPU fault IDs failed: %s, keeping existing set", e.what());
        return -1;
    }
}

static bool GetAnyRoleAbnormal(std::vector<std::unique_ptr<NodeInfo>> &availableNodes)
{
    bool isAnyRoleAbnormal = false;
    for (auto &node : availableNodes) {
        if (node == nullptr) {
            continue;
        }
        if (ServerRequestHandler::GetInstance()->IsUpdateRoleNeeded(*node)) {
            isAnyRoleAbnormal = true;
        }
    }
    return isAnyRoleAbnormal;
}

int32_t NodeScheduler::RecoverServerCluster(const nlohmann::json& processFile,
    std::vector<std::unique_ptr<NodeInfo>> &nodes)
{
    std::vector<std::unique_ptr<NodeInfo>> availableNodes;
    GroupInfo groupInfo;
    std::map<uint64_t, std::pair<std::vector<uint64_t>, std::vector<uint64_t>>> &groups = groupInfo.groups;
    std::map<uint64_t, std::vector<uint64_t>> &flexGroup = groupInfo.flexGroups;
    if (processFile.empty() || ProcessManager::GetInstance()->GetNodeInfoFromPath(processFile, nodes, availableNodes,
                                                                                  groups, flexGroup) != 0) {
        LOG_E("[%s] [NodeScheduler] Failed to load process file during server cluster recovery. "
            "Please remove the process file or restart the servers.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }
    if (availableNodes.size() < MIN_SERVER_NODES) {
        LOG_W("[%s] [NodeScheduler] Available servers count (%zu) is less than the required minimum (%u).",
              GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_SCHEDULER).c_str(),
              availableNodes.size(), MIN_SERVER_NODES);
    }
    LOG_I("[NodeScheduler] Server cluster recovery successful, %zu available servers.", availableNodes.size());
    // 恢复全局变量ipToIdMap的信息，避免id重复分配
    mRankTableLoader->UpdateIpToIdMapByInstanceStartNumber(availableNodes, nodes);
    return RecoverServerClusterInfo(groups, flexGroup, availableNodes);
}

void NodeScheduler::StopUnavailableNodes(
    const std::vector<std::unique_ptr<NodeInfo>> &availableNodes) const
{
    if (!ControllerConfig::GetInstance()->IsAnyFaultRecoveryEnable()) {
        return;
    }
    // Step 1: Collect unique IPs of nodes marked as UNAVAILABLE with non-empty IP addresses.
    std::unordered_set<std::string> unavailableIps;
    for (const auto& node : availableNodes) {
        if (!node || node->inferenceType != InferenceType::UNAVAILABLE || node->ip.empty()) {
            continue;
        }
        unavailableIps.insert(node->ip);
    }

    if (unavailableIps.empty()) {
        LOG_I("[NodeScheduler] No UNAVAILABLE nodes found; nothing to stop.");
        return;
    }

    // Step 2: Initialize HTTP client (once for all requests).
    auto httpClient = std::make_shared<HttpClient>();
    if (httpClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0) {
        LOG_E("[NodeScheduler] Failed to initialize HttpClient for STOP_ENGINE requests");
        return;
    }

    // Step 3: Send STOP_ENGINE command to each unique unavailable node.
    for (const std::string& ip : unavailableIps) {
        int32_t ret = mNodeManagerSender->SendCommandToNodeManager(*httpClient, ip, NodeManagerCmd::STOP_ENGINE);
        if (ret != 0) {
            LOG_W("[NodeScheduler] Failed to send STOP_ENGINE to %s, maybe it is not running", ip.c_str());
        } else {
            LOG_I("[NodeScheduler] Successfully sent STOP_ENGINE to %s"
                "(node was marked UNAVAILABLE during controller failover)", ip.c_str());
        }
    }
}

int32_t NodeScheduler::RecoverServerClusterInfo(
    std::map<uint64_t, std::pair<std::vector<uint64_t>, std::vector<uint64_t>>> &groups,
    std::map<uint64_t, std::vector<uint64_t>> &flexGroup,
    std::vector<std::unique_ptr<NodeInfo>> &availableNodes
)
{
    size_t pRate;
    size_t dRate;
    if (InitRoleAndRoleManager(availableNodes, pRate, dRate, true) != 0) {
        return -1;
    }

    StopUnavailableNodes(availableNodes);

    bool isAnyRoleAbnormal = GetAnyRoleAbnormal(availableNodes);
    if (!isAnyRoleAbnormal) {
        mNodeStatus->AddNodes(availableNodes);
        for (auto &iter : groups) {
            mNodeStatus->AddGroup(iter.first, iter.second);
        }
        for (auto &iter : flexGroup) {
            mNodeStatus->AddFlexGroup(iter.first, iter.second);
        }
        return 0;
    }
    LOG_W("[%s] [NodeScheduler] Some node has abnormal role during cluster recovery",
          GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_SCHEDULER).c_str());
    auto serversByGroupId = MergeServersByGroupId(availableNodes);
    for (auto &iter : serversByGroupId) {
        if (flexGroup.find(iter.first) != flexGroup.end()) {
            mNodeStatus->AddFlexGroup(iter.first, flexGroup[iter.first]);
        }
        if (groups.find(iter.first) != groups.end()) {
            for (auto &node : iter.second) {
                if (ServerRequestHandler::GetInstance()->IsUpdateRoleNeeded(*node)) {
                    mRoleSwitcher->UpdateAbnormalRoleWhenRecoverCluster(*node,
                        groups[iter.first], iter.second);
                }
            }
            mNodeStatus->AddNodes(iter.second);
            mNodeStatus->AddGroup(iter.first, groups[iter.first]);
        }
    }
    return 0;
}
void NodeScheduler::DetectNodeChanges(std::vector<std::unique_ptr<NodeInfo>> &serverNodes)
{
    auto changes = mNodeStatus->DetectNodeChanges(serverNodes);
    if (changes.removedIDs.empty() && changes.newIDs.empty() && changes.reappearIDs.empty()) {
        return;
    }
    LOG_I("[NodeScheduler] Detected node changes: %zu new nodes, %zu removed nodes, %zu reappeared nodes.",
          changes.newIDs.size(), changes.removedIDs.size(), changes.reappearIDs.size());

    FaultManager::GetInstance()->ScalingInstance(serverNodes, changes);
};

int32_t NodeScheduler::GroupsInstanceInfo() const
{
    // 对instance分组
    std::string errorCode = GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER);
    auto type = ControllerConstant::GetInstance()->GetGroupGeneratorTypeDefault();
    auto obj = GroupGeneratorFactory::GetInstance().CreateGroupGenerator(type);
    if (obj == nullptr) {
        LOG_E("[%s] [NodeScheduler]InitDigsRoleManager: create group generator %s failed", errorCode.c_str(),
              type.c_str());
        return -1;
    }
    return 0;
}

int32_t GetSeverNodesTp(
    std::vector<std::unique_ptr<NodeInfo>> &serverNodes,
    bool isRecovering,
    std::vector<MINDIE::MS::DIGSInstanceInfo> &instanceInfos)
{
    uint32_t tpSum = 0;
    uint32_t tp = 1;
    for (auto &node : serverNodes) {
        if (node == nullptr || node->serverInfoList.empty()) {
            LOG_W("[%s] [NodeScheduler] InitDigsRoleManager: Instance has no server with devices.",
                GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::NODE_SCHEDULER).c_str());
            continue;
        }
        tpSum += node->serverInfoList[0].deviceInfos.size(); // Just keep compatible with non-cross node scene
        if (!isRecovering) {
            instanceInfos.push_back(node->instanceInfo);
            LOG_I("[NodeScheduler] Adding instance info for node ID %lu.", node->instanceInfo.staticInfo.id);
        }
    }
    if (serverNodes.empty()) {
        LOG_E("[%s] [NodeScheduler] No server nodes available, using default TP value %u.",
            GetErrorCode(ErrorType::RESOURCE_LIMIT, ControllerFeature::NODE_SCHEDULER).c_str(), tp);
    } else {
        tp = tpSum / serverNodes.size();
    }
    return tp;
}

int32_t NodeScheduler::InitInstanceRoleManager(std::vector<std::unique_ptr<NodeInfo>> &serverNodes, size_t &pRate,
    size_t &dRate, bool isRecovering)
{
    LOG_I("[NodeScheduler] Starting Instance role manager initialization. Is recovering: %d.", isRecovering);
    std::vector<MINDIE::MS::DIGSInstanceInfo> instanceInfos;
    auto tp = GetSeverNodesTp(serverNodes, isRecovering, instanceInfos);
    auto config = BuildInstanceRoleManagerConfig(tp);
    if (config.empty()) {
        LOG_E("[%s] [NodeScheduler] Failed to initialize DIGS role manager as config is empty.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }
    MINDIE::MS::roleManager::InstanceRoleManager::NotifyRoleDecision callout =
        std::bind(&NodeScheduler::RoleDecisionHandler, this, std::placeholders::_1);
    MINDIE::MS::roleManager::InstanceRoleManager::InstancesCollector collector =
    std::bind(&NodeScheduler::RoleDecisionInstancesCollector, this, std::placeholders::_1, std::placeholders::_2);
    if (MINDIE::MS::roleManager::InstanceRoleManager::Create(config, callout, collector, mScheduler) != 0) {
        LOG_E("[%s] [NodeScheduler] Failed to initialize DIGS role manager as create role manager failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }
    MINDIE::MS::DIGSRequestSummary summary {};
    summary.inputLength = ControllerConfig::GetInstance()->GetDIGSRequestInputLength();
    summary.outputLength = ControllerConfig::GetInstance()->GetDIGSRequestOutputLength();
    if (GroupsInstanceInfo() != 0) {
        return -1;
    }
    if (!mScheduler->Init(instanceInfos, summary, pRate, dRate)) {
        LOG_E("[%s] [NodeScheduler] Initialize role manager failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return 0;
    }
    LOG_I("[NodeScheduler] DIGS role manager initialized successfully: prefill rate %zu, decode rate %zu.",
        pRate, dRate);
    return 0;
}

bool NodeScheduler::ShouldSleep()
{
    std::unique_lock<std::mutex> lock(mMtx);
    return mRun.load() && mRoleUnknownPIds.empty() &&
           mRoleUnknownDIds.empty() && mRoleDecisions.empty();
}

void NodeScheduler::Wait()
{
    auto waitSeconds = ControllerConfig::GetInstance()->GetRankTableDetectingSeconds();
    mWaitSeconds.store(waitSeconds);
    while (mWaitSeconds.load() > 0 && ShouldSleep()) {
        std::this_thread::sleep_for(std::chrono::seconds(1)); // 1s检查一次
        mWaitSeconds--;
    }
    if (!ShouldSleep()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500)); // 至少等待500ms
    }
}

// 是否启动clusterD组件
static bool IsUseClusterD()
{
    return std::getenv("MINDX_TASK_ID") != nullptr && std::getenv("MINDX_SERVER_IP") != nullptr;
}

int32_t NodeScheduler::Run()
{
    // 主备差异化
    while (!ControllerConfig::GetInstance()->IsLeader()) {
        LOG_I("[NodeScheduler] is not leader or ready,just wait.");
        Wait();
    }
    // 等待首次保存clusterD传输过来的global ranktable完成
    while (IsUseClusterD() && !mWaitClusterDGRTSave->load() && mWaitClusterDGRTSaveTimeout > 0) {
        LOG_I("[NodeScheduler] Wait for the clusterD's GRT save to complete.");
        mWaitClusterDGRTSaveTimeout--;
        std::this_thread::sleep_for(std::chrono::seconds(1)); // 等待1s后重试
    }
    while (mRun.load() && InitServerCluster() != 0) {
        LOG_E("[%s] [NodeScheduler] Run failed becase failed to initialize server cluster.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        std::this_thread::sleep_for(std::chrono::seconds(5)); // 等待5后重试
    }
    if (!mRun.load()) {
        LOG_I("[NodeScheduler] Exit run after initializing server cluster.");
        return 0;
    }
    LOG_I("[NodeScheduler] Finished to initialize server cluster.");
    if (ProcessManager::GetInstance()->Init(mNodeStatus) != 0) {
        LOG_E("[%s] [NodeScheduler] Run failed because failed to initialize process manager.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }
    while (mRun.load()) {
        while (!ControllerConfig::GetInstance()->IsLeader()) {
            LOG_I("[NodeScheduler] Role is not leader, no need to detect change.");
            Wait();
        }
        // 判断是否有FullNPURecovery流程正在进行中，如果有则等待其执行完成
        if (ControllerConfig::GetInstance()->IsAnyFaultRecoveryEnable() &&
            NPURecoveryManager::GetInstance()->IsInstanceRecoveryInProgress()) {
            LOG_I("[NodeScheduler] Instance recovery is in progress, waiting for completion.");
            Wait();
            continue;
        }

        LOG_D("[NodeScheduler] Update node start.");

        if (mDeployMode == DeployMode::PD_SEPARATE) {
            RunForPDSeparate();
        } else {
            RunForSingleNode();
        }
        LOG_D("[NodeScheduler] Update node end.");
        Wait();
    }
    LOG_I("[NodeScheduler] Update exit.");
    return 0;
}

void NodeScheduler::RunForPDSeparate()
{
    if (!ControllerConfig::GetInstance()->GetPIsDistribute() && !ControllerConfig::GetInstance()->GetDIsDistribute()) {
        // Only use this function when P/D is centeralized and use same parallel config and npu number.
        ProcessRoleUnknown();
    }
    std::vector<std::unique_ptr<NodeInfo>> serverNodes;
    if (mRankTableLoader->LoadRankTable(serverNodes, mCoordinatorStore) != 0) {
        LOG_E("[%s] [NodeScheduler] Run for PD separate, load rank table failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
    } else {
        DetectNodeChanges(serverNodes);
    }
    if (!ControllerConfig::GetInstance()->GetPIsDistribute() && !ControllerConfig::GetInstance()->GetDIsDistribute()) {
        // Only use this function when P/D is centeralized and use same parallel config and npu number.
        ProcessRoleDecisionChanges();
    }
}

void NodeScheduler::RunForSingleNode()
{
    std::vector<std::unique_ptr<NodeInfo>> serverNodes;
    if (mRankTableLoader->LoadRankTable(serverNodes, mCoordinatorStore) != 0) {
        LOG_E("[%s] [NodeScheduler] Run for single node, load rank table failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
    } else {
        DetectNodeChanges(serverNodes);
    }
}

void NodeScheduler::ProcessRoleDecisionChanges()
{
    auto tmpRoleDecisions = GetRoleDecisions();
    if (tmpRoleDecisions.empty()) {
        return;
    }
    LOG_I("[NodeScheduler] Detect dynamic role decision, number of decisions is %zu.",
          tmpRoleDecisions.size());
    for (auto &decision : tmpRoleDecisions) {
        mRoleSwitcher->ProcessSingleRoleSwitching(decision);
    }
};

std::vector<MINDIE::MS::DIGSRoleDecision> NodeScheduler::GetRoleDecisions()
{
    std::lock_guard<std::mutex> lock(mMtx);
    LOG_I("[NodeScheduler] Role decisions start size %zu.", mRoleDecisions.size());
    auto tmp = mRoleDecisions;
    mRoleDecisions.clear();
    LOG_I("[NodeScheduler] Role decisions after size %zu.", mRoleDecisions.size());
    return tmp;
}

uint32_t NodeScheduler::GetRoleDecisionsSize()
{
    std::lock_guard<std::mutex> lock(mMtx);
    LOG_I("[NodeScheduler] GetRoleDecisionsSize: size %zu", mRoleDecisions.size());
    return mRoleDecisions.size();
}

int32_t NodeScheduler::RoleDecisionHandler(std::vector<MINDIE::MS::DIGSRoleDecision> decision)
{
    LOG_I("[NodeScheduler] Handling role decision, number of decision is %zu.", decision.size());
    std::unique_lock<std::mutex> lock(mMtx);
    mRoleDecisions = decision;
    for (auto instance : decision) {
        LOG_I("[NodeScheduler] Handling role decision, node ID is %lu, group ID is %lu, role is %c.",
              instance.id, instance.groupId, instance.role);
    }
    LOG_I("[NodeScheduler] Handling role decision, number of role decisions is %zu.", mRoleDecisions.size());
    return 0;
}

int32_t NodeScheduler::RoleDecisionInstancesCollector(std::vector<MINDIE::MS::DIGSInstanceInfo> &instances,
    MINDIE::MS::DIGSRequestSummary &summary)
{
    instances.clear();
    // 不考虑性能，在这个线程中做完整的处理流程
    if (GetCoordinatorInfo() != 0) {
        LOG_E("[%s] [NodeScheduler] Update coordinator info failed. Using request summary of last time period",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
    }
    try {
        {
            std::unique_lock<std::mutex> lock(mMtx);
            summary.inputLength = mRequestSummary.inputLength;
            summary.outputLength = mRequestSummary.outputLength;
        }
        auto nodes = mNodeStatus->GetAllNodes();
        for (auto &iter : std::as_const(nodes)) {
            if (iter.second->deleteTime > std::chrono::seconds(0)) {
                LOG_W("[%s] [NodeScheduler] Ignore deleted node %lu with IP %s.",
                    GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_SCHEDULER).c_str(),
                    iter.first, iter.second->ip.c_str());
                continue;
            }
            instances.push_back(iter.second->instanceInfo);
        }
        LOG_M("[Update] Collected %zu instances for role decision.", instances.size());
        return 0;
    } catch (const std::exception& e) {
        LOG_E("[%s] [NodeScheduler] Role decision instances collection failed with error %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::NODE_SCHEDULER).c_str(), e.what());
        return -1;
    }
}

static bool IsValidScheduleInstanceInfo(const nlohmann::json &instanceInfo)
{
    auto errCode = GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::NODE_SCHEDULER);
    if (!IsJsonIntValid(instanceInfo, "allocated_slots", 0, 5000) || // 取值范围0~5000
        !IsJsonIntValid(instanceInfo, "allocated_blocks", 0, MAX_INT_VALUE)) {
        return false;
    }
    if (!instanceInfo.contains("id") || !instanceInfo["id"].is_number_integer()) {
        LOG_E("[%s] [NodeScheduler] Key 'id' is missing or not an integer in instance information.", errCode.c_str());
        return false;
    }
    int64_t val = instanceInfo["id"].get<int64_t>();
    if (val < 0) {
        LOG_E("[%s] [NodeScheduler] Invalid value for key 'id' in instance information, "
            "the value must be non-negative.", errCode.c_str());
        return false;
    }
    return true;
}

static bool IsValidCoordinatorInfoResp(std::string &response)
{
    auto errCode = GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::NODE_SCHEDULER);
    if (!nlohmann::json::accept(response)) {
        LOG_E("[%s] [NodeScheduler] Coordinator information response is not valid JSON.", errCode.c_str());
        return false;
    }
    try {
        auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        if (!IsJsonArrayValid(bodyJson, "schedule_info", 0, MAX_SERVER_NUMBER) ||
            !IsJsonObjValid(bodyJson, "request_length_info") ||
            !IsJsonIntValid(bodyJson["request_length_info"], "input_len", MIN_INT_VALUE, MAX_INT_VALUE) ||
            !IsJsonIntValid(bodyJson["request_length_info"], "output_len", MIN_INT_VALUE, MAX_INT_VALUE)) {
            LOG_E("[%s] [NodeScheduler] Invalid coordinator info response.", errCode.c_str());
            return false;
        }
        auto scheduleInfo = bodyJson.at("schedule_info");
        for (const auto &infoIter : scheduleInfo) {
            if (!IsValidScheduleInstanceInfo(infoIter)) {
                LOG_E("[%s] [NodeScheduler] Invalid scheduler instance in coordinator response.", errCode.c_str());
                return false;
            }
        }
        return true;
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [NodeScheduler] Failed to read response, json parse error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::NODE_SCHEDULER).c_str(), e.what());
        return false;
    } catch (const std::exception &e) {
        LOG_E("[%s] [NodeScheduler] Exception while validating coordinator info response is %s.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::NODE_SCHEDULER).c_str(),
              e.what());
        return false;
    }
}

int32_t NodeScheduler::ParseCoordinatorInfoResp(std::string &response)
{
    if (!IsValidCoordinatorInfoResp(response)) {
        return -1;
    }
    try {
        auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        auto scheduleInfo = bodyJson.at("schedule_info");
        MINDIE::MS::DIGSInstanceScheduleInfo info;
        for (const auto &infoIt : scheduleInfo) {
            auto id = infoIt.at("id").get<uint64_t>();
            info.allocatedSlots = infoIt.at("allocated_slots").get<size_t>();
            info.allocatedBlocks = infoIt.at("allocated_blocks").get<size_t>();
            mNodeStatus->UpdateNodeScheduleInfo(id, info);
        }
        auto inputLen = bodyJson.at("request_length_info").at("input_len").get<size_t>();
        auto outputLen = bodyJson.at("request_length_info").at("output_len").get<size_t>();
        {
            std::unique_lock<std::mutex> lock(mMtx);
            mRequestSummary.inputLength = inputLen;
            mRequestSummary.outputLength = outputLen;
        }
        LOG_I("[NodeScheduler] Parsing coordinator info response, input length %zu, output length %zu.",
            inputLen, outputLen);
        return 0;
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [NodeScheduler] Failed to read response, json parse error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::NODE_SCHEDULER).c_str(), e.what());
        return -1;
    } catch (const std::exception &e) {
        LOG_E("[%s] [NodeScheduler] Error parsing coordinator info response: %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::NODE_SCHEDULER).c_str(), e.what());
        return -1;
    }
}

int32_t NodeScheduler::GetCoordinatorInfo()
{
    std::vector<std::unique_ptr<Coordinator>> coordinatorNodes;
    try {
        coordinatorNodes = mCoordinatorStore->GetCoordinators();
    } catch (const std::exception& e) {
        LOG_E("[%s] [NodeScheduler] Failed to get coordinators.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::NODE_SCHEDULER).c_str());
        return -1;
    }
    auto port = ControllerConfig::GetInstance()->GetCoordinatorPort();
    int32_t httpRet = -1;
    for (auto &node : std::as_const(coordinatorNodes)) {
        std::string response;
        int32_t code = 400;
        std::string jsonString;
        std::map<boost::beast::http::field, std::string> map;
        map[boost::beast::http::field::accept] = "*/*";
        map[boost::beast::http::field::content_type] = "application/json";
        mCoordinatorClient->SetHostAndPort(node->ip, port);
        Request req = {ControllerConstant::GetInstance()->GetCoordinatorURI(CoordinatorURI::GET_INFO),
                       boost::beast::http::verb::get, map, jsonString};
        httpRet = mCoordinatorClient->SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
            ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
        if (httpRet == 0 && code == CODE_OK) {
            mCoordinatorStore->UpdateCoordinatorStatus(node->ip, true);
            return ParseCoordinatorInfoResp(response);
        } else {
            LOG_E("[%s] [NodeScheduler] Request to coordinator at IP %s, port %s failed: HTTP code %d, request "
                "return %d.", GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::NODE_SCHEDULER).c_str(),
                node->ip.c_str(), port.c_str(), code, httpRet);
            mCoordinatorStore->UpdateCoordinatorStatus(node->ip, false);
            return -1;
        }
    }
    return -1;
}

void NodeScheduler::RecordRoleUnknown(uint64_t id, MINDIE::MS::DIGSInstanceRole role)
{
    std::unique_lock<std::mutex> lock(mMtx);
    if (role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE &&
        std::find(mRoleUnknownPIds.begin(), mRoleUnknownPIds.end(), id) == mRoleUnknownPIds.end()) {
        mRoleUnknownPIds.push_back(id);
        LOG_D("[NodeScheduler] Recording unknown role, prefill node with ID %lu, role %c.", id, role);
    }
    if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE &&
        std::find(mRoleUnknownDIds.begin(), mRoleUnknownDIds.end(), id) == mRoleUnknownDIds.end()) {
        mRoleUnknownDIds.push_back(id);
        LOG_D("[NodeScheduler] Recording unknown role, decode node with ID %lu, role %c", id, role);
    }
    if (role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE &&
        std::find(mRoleUnknownFlexIds.begin(), mRoleUnknownFlexIds.end(), id) == mRoleUnknownFlexIds.end()) {
        mRoleUnknownFlexIds.push_back(id);
        LOG_D("[NodeScheduler]RecordRoleUnknown: d node id %lu, role %c", id, role);
    }
    LOG_D("[NodeScheduler] Unknown role counts: %zu abnormal prefill nodes, %zu abnormal decode nodes,"
          "%zu abnormal flex nodes",
        mRoleUnknownPIds.size(), mRoleUnknownDIds.size(), mRoleUnknownFlexIds.size());
}

void NodeScheduler::UnlinkNodeFromPeers(uint64_t nodeId)
{
    LOG_I("[NodeScheduler]UnlinkNodeFromPeers: Start to unlink node %lu from its peers.", nodeId);
    std::vector<uint64_t> postId;
    std::vector<uint64_t> success;
    auto node = mNodeStatus->GetNode(nodeId);
    if (node == nullptr) {
        return;
    }
    // unlink request is constructed by removing node itself from its peer's peer
    for (auto &peer : std::as_const(node->peers)) {
        auto peerNode = mNodeStatus->GetNode(peer);
        if (peerNode == nullptr) {
            continue;
        }
        auto it = std::find(peerNode->peers.begin(), peerNode->peers.end(), nodeId);
        if (it != peerNode->peers.end()) {
            peerNode->peers.erase(it);
            postId.push_back(peer);
        }
    }
    // post unlink request and update peers info by checking server status
    ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, postId, success);
    ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, success);
    LOG_I("[NodeScheduler]UnlinkNodeFromPeers: Finish unlinking node %lu from its peers.", nodeId);
}

void NodeScheduler::LinkNodeToPeers(uint64_t nodeId)
{
    LOG_I("[NodeScheduler]LinkNodeToPeers: Start to link node %lu to its peers.", nodeId);
    std::vector<uint64_t> postId;
    std::vector<uint64_t> success;
    auto node = mNodeStatus->GetNode(nodeId);
    if (node == nullptr) {
        return;
    }
    // link request is constructed by adding node itself to its peer's peer
    for (auto &peer : std::as_const(node->peers)) {
        auto peerNode = mNodeStatus->GetNode(peer);
        if (peerNode == nullptr) {
            continue;
        }
        auto it = std::find(peerNode->peers.begin(), peerNode->peers.end(), nodeId);
        if (it == peerNode->peers.end()) {
            peerNode->peers.push_back(nodeId);
        }
        postId.push_back(peer);
    }
    // post link request and update peers info by checking server status
    ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, postId, success);
    ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, success);
    LOG_I("[NodeScheduler]LinkNodeToPeers: Finish linking node %lu to its peers.", nodeId);
}

void NodeScheduler::DeleteRoleUnknown(uint64_t id, MINDIE::MS::DIGSInstanceRole role)
{
    std::unique_lock<std::mutex> lock(mMtx);
    if (role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        auto it = std::find(mRoleUnknownPIds.begin(), mRoleUnknownPIds.end(), id);
        if (it != mRoleUnknownPIds.end()) {
            mRoleUnknownPIds.erase(it);
            LOG_I("[NodeScheduler] Deleting unknown role, prefill node with ID %lu, role %c.", id, role);
        }
    }
    if (role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        auto it = std::find(mRoleUnknownDIds.begin(), mRoleUnknownDIds.end(), id);
        if (it != mRoleUnknownDIds.end()) {
            mRoleUnknownDIds.erase(it);
            LOG_I("[NodeScheduler] Deleting unknown role, decode node with ID %lu, role %c.", id, role);
        }
    }
    LOG_I("[NodeScheduler] Unknown role counts: %zu abnormal prefill nodes, %zu abnormal decode nodes.",
        mRoleUnknownPIds.size(), mRoleUnknownDIds.size());
}

void NodeScheduler::ProcessRoleUnknownForP(std::vector<uint64_t> &pIds)
{
    LOG_I("[NodeScheduler] Processing nodes with unknown roles for prefill. Count: %zu.", pIds.size());
    std::vector<uint64_t> success;
    std::vector<uint64_t> postId;
    for (auto &id : pIds) {
        success.clear();
        if (!mNodeStatus->IsPostRoleNeeded(id)) {
            LOG_W("[%s] [NodeScheduler] Ignore node %lu.",
                  GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_SCHEDULER).c_str(), id);
            continue;
        }
        auto node = mNodeStatus->GetNode(id);
        if (node == nullptr) {
            continue;
        }
        for (auto &peer : std::as_const(node->peers)) {
            if (mNodeStatus->IsPostRoleNeeded(peer)) {
                LOG_W("[%s] [NodeScheduler] Ignore peer %lu when processing node %lu.",
                      GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_SCHEDULER).c_str(), peer, id);
                continue;
            }
            postId = { peer };
            ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, postId, success);
            ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, success);
        }
        postId = { id };
        mNodeStatus->AddInitRetryTimes(id);
        ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, postId, success);
        success = ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, success, true);
        if (success.empty()) {
            LOG_E("[%s] [NodeScheduler] Node %lu failed to recover role %c.",
                GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str(),
                id, node->instanceInfo.staticInfo.role);
            continue;
        }
        ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, node->peers, success);
        ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, success);
    }
    LOG_I("[NodeScheduler] End processing nodes with unknown roles for prefill.");
}

void NodeScheduler::ProcessRoleUnknownForD(std::vector<uint64_t> &dIds)
{
    LOG_I("[NodeScheduler] Processing nodes with unknown roles for decode. Count: %zu.", dIds.size());
    std::vector<uint64_t> success;
    std::vector<uint64_t> postId;
    for (auto &id : dIds) {
        success.clear();
        postId.clear();
        if (!mNodeStatus->IsPostRoleNeeded(id)) {
            LOG_W("[%s] [NodeScheduler] Ignore node %lu.",
                  GetWarnCode(ErrorType::WARNING, ControllerFeature::NODE_SCHEDULER).c_str(), id);
            continue;
        }
        auto node = mNodeStatus->GetNode(id);
        if (node == nullptr) {
            continue;
        }
        postId.push_back(id);
        mNodeStatus->AddInitRetryTimes(id);
        ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, postId, success);
        success = ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, success, true);
        if (success.empty()) {
            LOG_E("[%s] [NodeScheduler] Node %lu failed to recover role %c.",
                GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str(),
                id, node->instanceInfo.staticInfo.role);
            continue;
        }
    }
    LOG_I("[NodeScheduler]End processing nodes with unknown roles for decode.");
}

void NodeScheduler::ProcessRoleUnknownForPD(std::vector<uint64_t> &nodeIds)
{
    LOG_I("[NodeScheduler]ProcessRoleUnknownForPD: abnormal nodes %zu", nodeIds.size());
    // vector used for posting role
    std::vector<uint64_t> postId;
    // vector used for capturing role posting result
    std::vector<uint64_t> success;
    for (auto &id : nodeIds) {
        postId.clear();
        success.clear();
        if (!mNodeStatus->IsPostRoleNeeded(id)) {
            LOG_W("[NodeScheduler]ProcessRoleUnknownForPD: ignore node %lu", id);
            continue;
        }
        auto node = mNodeStatus->GetNode(id);
        if (node == nullptr) {
            continue;
        }
        // unlink node from its' peers
        UnlinkNodeFromPeers(id);
        // re-post node's role with original attempt
        mNodeStatus->AddInitRetryTimes(id);
        postId.push_back(id);
        ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, postId, success);
        // post link request to node's peers at the same time
        LinkNodeToPeers(id);
        // update node status after building two-way connection
        auto repostRet = ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, success);
        if (repostRet.empty()) {
            LOG_E("[NodeScheduler]ProcessRoleUnknownForPD: Re-post role for node %lu failed, "
                  "current role is %c.",
                  id, node->instanceInfo.staticInfo.role);
        }
    }
    LOG_I("[NodeScheduler]ProcessRoleUnknownForPD: end");
}

bool NodeScheduler::BatchUnlinkNodes(const std::vector<uint64_t> &nodeIds)
{
    std::vector<uint64_t> postId;
    std::vector<uint64_t> success;
    const std::string stateReady = ControllerConstant::GetInstance()->GetRoleState(RoleState::READY);
    const std::vector<uint64_t> emptyPeers;

    for (uint64_t nodeId : nodeIds) {
        auto node = mNodeStatus->GetNode(nodeId);
        if (node == nullptr) {
            LOG_W("[NodeScheduler]BatchUnlinkNodes: node %lu not found", nodeId);
            return false;
        }
        for (auto &peer : std::as_const(node->peers)) {
            auto peerNode = mNodeStatus->GetNode(peer);
            if (peerNode == nullptr) {
                LOG_W("[NodeScheduler]BatchUnlinkNodes: peer %lu not found", peer);
                return false;
            }
            auto it = std::find(peerNode->peers.begin(), peerNode->peers.end(), nodeId);
            if (it != peerNode->peers.end()) {
                peerNode->peers.erase(it);
                mNodeStatus->UpdateRoleStateAndPeers(peerNode->instanceInfo.staticInfo.groupId, peer, stateReady,
                    peerNode->peers);
                postId.push_back(peer);
            }
        }
    }
    std::sort(postId.begin(), postId.end());
    postId.erase(std::unique(postId.begin(), postId.end()), postId.end());
    LOG_I("[NodeScheduler]BatchUnlinkNodes: postId size %zu (unique peers to receive new role).", postId.size());

    for (uint64_t nodeId : nodeIds) {
        auto node = mNodeStatus->GetNode(nodeId);
        if (node == nullptr) {
            continue;
        }
        mNodeStatus->UpdateRoleStateAndPeers(node->instanceInfo.staticInfo.groupId, nodeId, stateReady, emptyPeers);
    }
    for (uint64_t nodeId : nodeIds) {
        postId.push_back(nodeId);
    }

    std::sort(postId.begin(), postId.end());
    postId.erase(std::unique(postId.begin(), postId.end()), postId.end());
    ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, postId, success);
    if (success.size() != postId.size()) {
        LOG_E("[NodeScheduler]BatchUnlinkNodes: Not all nodes unlinked, success %zu / postId %zu.",
            success.size(), postId.size());
        return false;
    }
    LOG_I("[NodeScheduler]BatchUnlinkNodes: BatchPostRole successfully unlinked %zu nodes", success.size());
    return true;
}

bool NodeScheduler::BatchLinkNodes(const std::vector<uint64_t> &nodeIds, uint32_t maxCheckAttempts)
{
    std::set<uint64_t> groupIds;
    for (uint64_t nodeId : nodeIds) {
        auto node = mNodeStatus->GetNode(nodeId);
        if (node == nullptr) {
            LOG_W("[NodeScheduler]BatchLinkNodes: node %lu not found", nodeId);
            return false;
        }
        groupIds.insert(node->instanceInfo.staticInfo.groupId);
    }

    const std::string stateReady = ControllerConstant::GetInstance()->GetRoleState(RoleState::READY);
    std::vector<uint64_t> postId;
    std::vector<uint64_t> success;
    for (uint64_t groupId : groupIds) {
        auto group = mNodeStatus->GetGroup(groupId);
        const std::vector<uint64_t> &pNodes = group.first;
        const std::vector<uint64_t> &dNodes = group.second;
        LOG_I("[NodeScheduler]BatchLinkNodes: group %lu pNodes %zu, dNodes %zu", groupId, pNodes.size(), dNodes.size());
        if (pNodes.empty() && dNodes.empty()) {
            LOG_I("[NodeScheduler]BatchLinkNodes: group %lu empty, skip", groupId);
            continue;
        }
        for (uint64_t pNodeId : pNodes) {
            mNodeStatus->UpdateRoleStateAndPeers(groupId, pNodeId, stateReady, dNodes);
            postId.push_back(pNodeId);
        }
        for (uint64_t dNodeId : dNodes) {
            mNodeStatus->UpdateRoleStateAndPeers(groupId, dNodeId, stateReady, pNodes);
            postId.push_back(dNodeId);
        }
    }
    std::sort(postId.begin(), postId.end());
    postId.erase(std::unique(postId.begin(), postId.end()), postId.end());

    ServerRequestHandler::GetInstance()->BatchPostRole(*mServerClient, *mNodeStatus, postId, success);
    if (success.size() != postId.size()) {
        LOG_E("[NodeScheduler]BatchLinkNodes: Not all nodes linked, success %zu / postId %zu.",
            success.size(), postId.size());
        return false;
    }
    
    auto readyIds = ServerRequestHandler::GetInstance()->CheckStatus(*mServerClient, *mNodeStatus, postId, false,
        maxCheckAttempts);
    if (readyIds.size() < postId.size()) {
        LOG_E("[NodeScheduler]BatchLinkNodes: CheckStatus not ready, count %zu", postId.size() - readyIds.size());
        return false;
    }
    LOG_I("[NodeScheduler]BatchLinkNodes: CheckStatus done, ready %zu / postId %zu.", readyIds.size(), postId.size());
    return true;
}

bool NodeScheduler::ProcessBatchUnlinkAndLink(const std::vector<uint64_t> &nodeIds)
{
    if (nodeIds.empty()) {
        return true;
    }
    if (!BatchUnlinkNodes(nodeIds)) {
        LOG_E("[NodeScheduler]ProcessBatchUnlinkAndLink: BatchUnlinkNodes failed");
        return false;
    }
    // RoCE 恢复使用较短超时(10 次 * 5s ≈ 50s)，避免长时间阻塞导致无法下发 STOP_ENGINE
    constexpr uint32_t roceCheckStatusAttempts = 10;
    if (!BatchLinkNodes(nodeIds, roceCheckStatusAttempts)) {
        LOG_E("[NodeScheduler]ProcessBatchUnlinkAndLink: BatchLinkNodes failed (CheckStatus not ready)");
        return false;
    }
    LOG_I("[NodeScheduler]BatchUnlinkAndLink done");
    return true;
}

void NodeScheduler::ProcessRoleUnknown()
{
    std::vector<uint64_t> pIds;
    std::vector<uint64_t> dIds;
    std::vector<uint64_t> flexIds;
    {
        std::unique_lock<std::mutex> conditionLock(mMtx);
        if (mRoleUnknownPIds.empty() && mRoleUnknownDIds.empty() && mRoleUnknownFlexIds.empty()) {
            return;
        }
        pIds = mRoleUnknownPIds;
        dIds = mRoleUnknownDIds;
        flexIds = mRoleUnknownFlexIds;
        mRoleUnknownPIds.clear();
        mRoleUnknownDIds.clear();
        mRoleUnknownFlexIds.clear();
    }
    LOG_I("[NodeScheduler] Start to process role unknown.");
    ProcessRoleUnknownForPD(pIds);
    ProcessRoleUnknownForPD(dIds);
    ProcessRoleUnknownForPD(flexIds);
    LOG_I("[NodeScheduler] End to process role unknown.");
}

static uint32_t FindNodeInfoInNodeVec(std::vector<std::unique_ptr<NodeInfo>> &nodeVec, uint64_t id)
{
    auto it = std::find_if(nodeVec.begin(), nodeVec.end(),
        [&id](const std::unique_ptr<NodeInfo> &obj) {
            return (obj != nullptr) && (obj->instanceInfo.staticInfo.id == id);
        });
    return (it == nodeVec.end()) ? static_cast<uint32_t>(nodeVec.size()) :
        static_cast<uint32_t>(std::distance(nodeVec.begin(), it));
}

static std::vector<uint64_t> CheckRoleStatus(std::vector<uint64_t> &success,
    std::vector<std::unique_ptr<NodeInfo>> &nodeVec)
{
    std::vector<uint64_t> realSuccess;
    for (size_t i = 0; i < success.size(); ++i) {
        uint32_t index = FindNodeInfoInNodeVec(nodeVec, success[i]);
        if (index >= nodeVec.size()) {
            continue; // 节点未找到，跳过
        }

        auto& checkNode = nodeVec[index];
        bool isSuccess = true;
        for (const auto& peerId : checkNode->dpGroupPeers) {
            if (std::find(success.begin(), success.end(), peerId) == success.end()) {
                isSuccess = false;
                break;
            }
        }
        if (isSuccess) {
            realSuccess.push_back(success[i]);
        }
    }
    return realSuccess;
}

static void UpdatePDNodeLinkStatus(std::vector<std::unique_ptr<NodeInfo>> &nodeVec, std::vector<uint64_t> &pSuccess,
    std::vector<uint64_t> &dSuccess, std::vector<uint64_t> &flexSuccess)
{
    for (auto &node : std::as_const(nodeVec)) {
        // Check if there is at least one link between p and d
        std::vector<uint64_t> trueActivePeers {};
        std::vector<uint64_t> nodeActivePeers = node->activePeers;
        if (node->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::READY)) {
            nodeActivePeers = node->peers;
        }
        for (auto peer : nodeActivePeers) {
            auto it = std::find_if(nodeVec.begin(), nodeVec.end(), [&peer](const std::unique_ptr<NodeInfo> &obj) {
                return (obj != nullptr) && (obj->instanceInfo.staticInfo.id == peer);
            });
            if (it == nodeVec.end()) {
                LOG_I("[NodeScheduler] Node %lu has an connected peer %lu that is not in"
                    " current group, will ignore it when checking PD link.", node->instanceInfo.staticInfo.id, peer);
                continue;
            }
            uint32_t peerIndex = static_cast<uint32_t>(std::distance(nodeVec.begin(), it));
            // When a node is ready, it means that the node has connected with all of its peers successfully
            if (nodeVec[peerIndex]->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::READY)) {
                node->roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::READY);
                trueActivePeers.push_back(peer);
                continue;
            }
            // Active peer is a peer which the node has successfully connected with in single direction
            for (auto acitvePeer : nodeVec[peerIndex]->activePeers) {
                if (acitvePeer != node->instanceInfo.staticInfo.id) {
                    continue;
                }
                node->roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::READY);
                trueActivePeers.push_back(peer);
            }
        }
        node->activePeers = trueActivePeers;
        if (!trueActivePeers.empty() &&
            node->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            pSuccess.push_back(node->instanceInfo.staticInfo.id);
        } else if (!trueActivePeers.empty() &&
            node->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            dSuccess.push_back(node->instanceInfo.staticInfo.id);
        } else if (!trueActivePeers.empty() &&
            node->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
            flexSuccess.push_back(node->instanceInfo.staticInfo.id);
        }
        pSuccess = CheckRoleStatus(pSuccess, nodeVec);
        dSuccess = CheckRoleStatus(dSuccess, nodeVec);
        flexSuccess = CheckRoleStatus(flexSuccess, nodeVec);
    }
}

void NodeScheduler::UpdateNodeStatusAfterSendRole(std::vector<std::unique_ptr<NodeInfo>> &servers,
                                                  std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group,
                                                  std::vector<uint64_t> &flexGroup, uint64_t groupId,
                                                  std::vector<uint64_t> &pSuccess, std::vector<uint64_t> &dSuccess,
                                                  std::vector<uint64_t> &flexSuccess)
{
    // 若未全部检查ready，则确认节点是否有互相连通的link
    std::vector<uint64_t> pIds = pSuccess;
    std::vector<uint64_t> dIds = dSuccess;
    if (flexGroup.size() != 0) {
        for (auto &flexId : flexSuccess) {
            auto iter = std::find_if(servers.begin(), servers.end(), [flexId](const std::unique_ptr<NodeInfo> &server) {
                return (server != nullptr) & (server->instanceInfo.staticInfo.id == flexId);
            });
            if (iter == servers.end()) {
                continue;
            }
            uint64_t flexPRatio = (*iter)->instanceInfo.staticInfo.flexPRatio;
            if (flexPRatio == 0) {
                dIds.push_back(flexId);
            } else if (flexPRatio == MINDIE::MS::FLEX_INSTANCE_P_PERCENTAGE_MAX) {
                pIds.push_back(flexId);
            } else {
                dIds.push_back(flexId);
                pIds.push_back(flexId);
            }
        }
    }
    if (pIds.empty() || dIds.empty()) {
        LOG_E("[NodeScheduler]SendRole: all p or d or flex node failed in group %lu", groupId);
        mNodeStatus->AddFaultyNodes(servers);
        mNodeStatus->AddFaultyGroup(groupId, group);
        mNodeStatus->AddFaultyFlexGroup(groupId, flexGroup);
    } else {
        AddServersToNodeStatus(servers, pSuccess, dSuccess, flexSuccess);
        group.first = pSuccess;
        group.second = dSuccess;
        mNodeStatus->AddGroup(groupId, group);
        mNodeStatus->AddFlexGroup(groupId, flexSuccess);
    }
}

void NodeScheduler::SendPDRoleWithinAttempt(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
    std::pair<std::vector<uint64_t>, std::vector<uint64_t>> &group,
    std::vector<uint64_t> &flexGroup,
    std::vector<uint64_t> &pSuccess,
    std::vector<uint64_t> &dSuccess,
    std::vector<uint64_t> &flexSuccess)
{
    auto limit = ControllerConfig::GetInstance()->GetInitRoleAttemptTimes();
    std::set<uint64_t> success;
    std::vector<uint64_t> nodeSuccess;
    uint32_t attempt = 0;
    while (success.size() != nodeVec.size() && attempt <= limit) {
        for (auto &node : std::as_const(nodeVec)) {
            if (success.find(node->instanceInfo.staticInfo.id) != success.end()) {
                continue;
            }
            if (ServerRequestHandler::GetInstance()->PostSingleRoleByVec(*mServerClient, nodeVec, *node) == 0) {
                success.insert(node->instanceInfo.staticInfo.id);
                nodeSuccess.push_back(node->instanceInfo.staticInfo.id);
            }
        }
        sleep(ROLE_SEND_RETRY_INTERVAL_SEC); // 休眠3s后再重试下发身份或查询身份状态
        attempt++;
    }

    if (success.size() != nodeVec.size()) {
        LOG_E("[%s] [NodeScheduler] Failed to send some node's role after %u attempts.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str(), limit);
        if (ControllerConfig::GetInstance()->GetDIGSIsSingleContainer()) {
            LOG_E("[%s] [NodeScheduler] Failed to initialize cluster when using single "
                "container.", GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
            return;
        }
    } else {
        LOG_I("[NodeScheduler] Send role for all prefill and decode nodes success.");
    }
    auto statusRes = ServerRequestHandler::GetInstance()->CheckStatusByVec(*mServerClient, nodeVec, nodeSuccess, true);
    if (statusRes.size() == nodeVec.size()) {
        pSuccess = group.first;
        dSuccess = group.second;
        if (!flexGroup.empty()) {
            flexSuccess = flexGroup;
        }
        LOG_I("[NodeScheduler] Initialize all prefill and decode nodes in current group success.");
        return;
    }

    LOG_E("[%s] [NodeScheduler] Some nodes' role are not ready after checking for %u times.",
        GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str(),
        ControllerConfig::GetInstance()->GetCheckRoleAttemptTimes());
    if (ControllerConfig::GetInstance()->GetDIGSIsSingleContainer()) {
        LOG_E("[%s] [NodeScheduler] Failed to initialize cluster when using single container.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return;
    }
    UpdatePDNodeLinkStatus(nodeVec, pSuccess, dSuccess, flexSuccess);
}

void NodeScheduler::AddServersToNodeStatus(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
    const std::vector<uint64_t> &pSuccess, const std::vector<uint64_t> &dSuccess,
    const std::vector<uint64_t> &flexSuccess)
{
    for (auto &node : nodeVec) {
        if (std::find(dSuccess.begin(), dSuccess.end(), node->instanceInfo.staticInfo.id) == dSuccess.end() &&
            std::find(pSuccess.begin(), pSuccess.end(), node->instanceInfo.staticInfo.id) == pSuccess.end() &&
            std::find(flexSuccess.begin(), flexSuccess.end(), node->instanceInfo.staticInfo.id) == flexSuccess.end()) {
            mNodeStatus->AddFaultyNode(std::move(node));
            continue;
        }
        SetUpTotalAvailableResourceForNode(*node);
        mNodeStatus->AddNode(std::move(node));
        LOG_I("[NodeScheduler] Add servers to node status.");
    }
}

void NodeScheduler::SendRole(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
    std::vector<std::pair<std::vector<uint64_t>, std::vector<uint64_t>>> &groupsVec,
    std::vector<std::vector<uint64_t>> &flexGroupVec)
{
    auto serversByGroupId = MergeServersByGroupId(nodeVec);
    std::vector<uint64_t> pSuccess;
    std::vector<uint64_t> dSuccess;
    std::vector<uint64_t> flexSuccess;
    uint64_t maxGpId = std::min(groupsVec.size(), flexGroupVec.size());
    maxGpId = std::min(maxGpId, serversByGroupId.size());
    LOG_I("[NodeScheduler] Start sending role. group size %lu", maxGpId);
    for (uint64_t i = 0; i < maxGpId; i++) {
        auto &group = groupsVec[i];
        auto &flexGroup = flexGroupVec[i];
        auto &servers = serversByGroupId[i];
        pSuccess.clear();
        dSuccess.clear();
        flexSuccess.clear();
        SendPDRoleWithinAttempt(servers, group, flexGroup, pSuccess, dSuccess, flexSuccess);
        UpdateNodeStatusAfterSendRole(servers, group, flexGroup, i, pSuccess, dSuccess, flexSuccess);
        LOG_I("[NodeScheduler]SendRole: add group %lu, p nodes %zu, d nodes %zu, flex nodes %zu", i, group.first.size(),
              group.second.size(), flexGroup.size());
        CoordinatorRequestHandler::GetInstance()->SendNodeStatus(mCoordinatorClient, mNodeStatus, mCoordinatorStore);
    }
    LOG_I("[NodeScheduler] End to send role.");
}

std::shared_ptr<RankTableLoader> NodeScheduler::GetRankTableLoader() const
{
    return mRankTableLoader;
}

void NodeScheduler::DetectNodeChangesForAlarm(std::vector<std::unique_ptr<NodeInfo>> &serverNodes)
{
    auto changes = mNodeStatus->DetectNodeChanges(serverNodes);
    if (changes.removedIDs.empty() && changes.newIDs.empty() && changes.reappearIDs.empty()) {
        return;
    }
    LOG_I("[NodeScheduler] NodeScheduler alarm detected node changes:"
        " %zu new nodes, %zu removed nodes, %zu reappeared nodes.",
        changes.newIDs.size(), changes.removedIDs.size(), changes.reappearIDs.size());
};

void NodeScheduler::NodeSchedulerAlarm()
{
    if (nodeSchedulerAlarmThreadRunning) {
        LOG_I("[NodeScheduler] NodeScheduler alarm thread started.");
    }
    
    while (nodeSchedulerAlarmThreadRunning) {
        std::vector<std::unique_ptr<NodeInfo>> serverNodes;
        if (mRankTableLoader->LoadRankTable(serverNodes, mCoordinatorStore) != 0) {
            LOG_E("[%s] [NodeScheduler] Run for NodeScheduler alarm, load rank table failed.",
                GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        } else {
            std::vector<std::unique_ptr<InstanceInfo>> instanceInfoList =
                mRankTableLoader->GetInstanceInfoListByRankTable();
            ResourceManager::GetInstance()->UpdateInstanceTable(instanceInfoList);
            DetectNodeChangesForAlarm(serverNodes);
        }

        // Sleep for configured interval
        std::this_thread::sleep_for(std::chrono::seconds(NODE_SCHEDULER_ALARM_INTERVAL_SEC));
    }
    return;
}

bool NodeScheduler::HasRankTableChanged()
{
    // Load new server nodes from rank table
    std::vector<std::unique_ptr<NodeInfo>> newServerNodes;
    if (mRankTableLoader->LoadRankTable(newServerNodes, mCoordinatorStore) != 0) {
        LOG_E("[%s] [NodeScheduler] Load rank table failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::NODE_SCHEDULER).c_str());
        return false;
    }

    // If the number of nodes is different, the rank table has changed
    if (newServerNodes.size() != mServerNodes.size()) {
        mServerNodes = std::move(newServerNodes);
        return true;
    }

    // Build a set of old node IDs for fast lookup
    std::unordered_set<uint64_t> oldNodeIds;
    for (const auto& node : mServerNodes) {
        if (node) {
            oldNodeIds.insert(node->instanceInfo.staticInfo.id);
        }
    }

    // Check each new node to see if it matches an old one
    for (const auto& newNode : newServerNodes) {
        if (!newNode) {
            continue;
        }

        uint64_t id = newNode->instanceInfo.staticInfo.id;
        if (oldNodeIds.find(id) == oldNodeIds.end()) {
            LOG_D("[NodeScheduler] Rank table monitor thread: new node ID detected: %d", id);
            mServerNodes = std::move(newServerNodes);
            return true;
        }
    }
    return false;
}

void NodeScheduler::MonitorRankTable()
{
    if (monitorRankTableRunning) {
        LOG_I("[NodeScheduler] Rank table monitor thread started.");
    }
    
    while (monitorRankTableRunning) {
        if (HasRankTableChanged()) {
            mNodeStatus->UpdateRanktableChangeTime();
            LOG_I("[NodeScheduler] Rank table has changed, timestamp Updated.");
        }

        // Sleep for configured interval
        std::this_thread::sleep_for(std::chrono::seconds(RANK_TABLE_MONITOR_INTERVAL_SEC));
    }
    return;
}

}
