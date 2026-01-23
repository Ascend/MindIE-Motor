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
#include "CCAERequestHandler.h"
#include <thread>
#include <unordered_map>
#include "ControllerConfig.h"
#include "Util.h"
#include "NodeStatus.h"
#include "RankTableLoader.h"
#include "ResourceManager.h"
#include "SecurityUtils.h"
namespace MINDIE::MS {
constexpr int32_t CODE_OK = 200;
constexpr int32_t CODE_CREATED = 201;
constexpr int32_t MODEL_ID_LEN = 36;
constexpr int32_t MIN_MTRX_PERIOD = 0;
constexpr int32_t MAX_MTRX_PERIOD = 5;
constexpr int32_t COORDINATOR_GROUP = 0;
constexpr int32_t CONTROLLER_GROUP = 1;
constexpr int32_t SERVER_GROUP = 2;

constexpr const char* PREFILL_INSTANCE = "p";
constexpr const char* DECODE_INSTANCE = "d";
constexpr const char* UN_DEF_INSTANCE = "unknown";

std::string Base64Encode(const std::string &input)
{
    const std::string base64Chars =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789+/";
    
    const int bitShiftStep = 6; // Process 6 bits each time
    const int inputBitSize = 8; // size of input bit
    const unsigned int bitMask = 0x3F;  // mask bit
    const int groupSize = 4;    // 4 bits per group
    const char paddingChar = '='; // padding char
    
    std::string output;
    unsigned int val = 0;
    int valBits = -bitShiftStep;
    for (unsigned char c : input) {
        val = (val << inputBitSize) + c;
        valBits += inputBitSize;
        while (valBits >= 0) {
            output.push_back(base64Chars[(val >> valBits) & bitMask]);
            valBits -= bitShiftStep;
        }
    }

    if (valBits > -bitShiftStep) {
        output.push_back(base64Chars[((val << inputBitSize) >> (valBits + inputBitSize)) & bitMask]);
    }

    while (output.size() % groupSize != 0) {
        output.push_back(paddingChar);
    }

    return output;
}

nlohmann::json GetMetricsJson(int64_t metricPeriod, std::string response)
{
    nlohmann::json metricsInfo;

    metricsInfo["metricPeriod"] = metricPeriod;
    metricsInfo["metric"] = Base64Encode(response);

    return metricsInfo;
}

std::vector<std::shared_ptr<PodInfo>> GetPodInfoByGroupId(
    std::vector<std::unique_ptr<PodInfo>>& podInfoList,
    int targetGroupId)
{
    std::vector<std::shared_ptr<PodInfo>> result;
    auto it = podInfoList.begin();
    while (it != podInfoList.end()) {
        bool match = false;
        
        if (targetGroupId == SERVER_GROUP) {
            try {
                int currentId = std::stoi((*it)->groupId);
                match = (currentId >= SERVER_GROUP);
            } catch (...) {
                match = false;
            }
        } else {
            try {
                match = (std::stoi((*it)->groupId) == targetGroupId);
            } catch (...) {
                match = false;
            }
        }

        if (match) {
            result.push_back(std::move(*it));
            it = podInfoList.erase(it);
        } else {
            ++it;
        }
    }
    
    return result;
}

nlohmann::json CreateNewInstanceInfo(const std::string pdFlag, const std::string serverId)
{
    nlohmann::json instanceInfo;
    instanceInfo["ID"] = pdFlag + "_" + serverId;
    instanceInfo["Name"] = instanceInfo["ID"];
    instanceInfo["serverList"] = nlohmann::json::array();
    instanceInfo["serverIPList"] = nlohmann::json::array();
    instanceInfo["podInfoList"] = nlohmann::json::array();
    return instanceInfo;
}

nlohmann::json CCAERequestHandler::GetInstanceInfo(const std::string pdFlag, const std::string serverId)
{
    auto itPD = mInstanceMap.find(pdFlag);
    if (itPD != mInstanceMap.end()) {
        auto itServers = itPD->second;
        auto itServer = itServers.find(serverId);
        if (itServer != itServers.end()) {
            return itServer->second;
        }
    }
   
    return CreateNewInstanceInfo(pdFlag, serverId);
}

std::vector<nlohmann::json> CCAERequestHandler::GetInstances(std::string pdFlag)
{
    std::vector<nlohmann::json> instances;
    auto itPDs = mInstanceMap.find(pdFlag);
    if (itPDs != mInstanceMap.end()) {
        auto itPD = itPDs->second;
        for (auto it = itPD.begin(); it != itPD.end(); ++it) {
            instances.push_back(it->second);
        }
    }
    return instances;
}

PodInfo ServerToPodInfo(const ServerInfo& info)
{
    PodInfo podInfo;
    podInfo.podIP = info.ip;
    podInfo.podName = "";
    
    for (const auto& device : info.deviceInfos) {
        NPUInfo npuInfo;
        npuInfo.npuID = device.id;
        npuInfo.npuIP = device.ip;
        podInfo.podAssociatedInfoList.push_back(npuInfo);
    }
    return podInfo;
}

void AppendDeviceInfosToPodInfo(PodInfo& podInfo, const std::vector<DeviceInfo>& deviceInfos)
{
    for (const auto& device : deviceInfos) {
        NPUInfo npuInfo;
        npuInfo.npuID = device.id;
        npuInfo.npuIP = device.ip;
        podInfo.podAssociatedInfoList.push_back(npuInfo);
    }
}

void to_json(nlohmann::json& podInfo, const PodInfo& info)
{
    podInfo["podID"] = info.podIP;
    podInfo["podName"] = "";
    podInfo["podAssociatedInfoList"] = nlohmann::json::array();
    for (const auto& device : info.podAssociatedInfoList) {
        nlohmann::json associatedJson;
        associatedJson["NPUID"] = device.npuID;
        associatedJson["NPUIP"] = device.npuIP;
        podInfo["podAssociatedInfoList"].emplace_back(associatedJson);
    }
}

void to_json(nlohmann::json& npuInfoList, const std::vector<NPUInfo>& npuList)
{
    npuInfoList = nlohmann::json::array();
    for (const auto &npu : npuList) {
        nlohmann::json npuInfo = nlohmann::json::object();
        npuInfo["NPUID"] = npu.npuID;
        npuInfo["NPUIP"] = npu.npuIP;
        npuInfoList.emplace_back(npuInfo);
    }
}

void UpdateArray(nlohmann::json& array, const nlohmann::json& newObj, const std::string& key)
{
    if (!newObj.contains(key)) {
        return;
    }
    auto targetId = newObj.at(key).get<std::string>();

    for (auto it = array.begin(); it != array.end();) {
        if (it->contains(key) && it->at(key).get<std::string>() == targetId) {
            it = array.erase(it);
        } else {
            ++it;
        }
    }

    array.push_back(newObj);
}

bool IsExist(const nlohmann::json& array, const std::string& target)
{
    if (!array.is_array()) {
        return false;
    }
    return std::any_of(array.begin(), array.end(), [&target](const nlohmann::json& item) {
            return item.is_string() && item.get<std::string>() == target;
    });
}

void CCAERequestHandler::BuildCentralizedInstances(nlohmann::json instanceInfo, std::string pdFlag, std::string serId,
    const NodeInfo &nodeInfo)
{
    LOG_D("[CCAEReporter] nodeInfo.serverInfoList size : %zu", nodeInfo.serverInfoList.size());
    ServerInfo server;
    std::unordered_map<std::string, nlohmann::json> subMap;
    for (auto &it : nodeInfo.serverInfoList) {
        server = it;
        instanceInfo["serverIPList"].push_back(server.hostId);
        nlohmann::json podInfoJson = ServerToPodInfo(server);
        instanceInfo["podInfoList"].emplace_back(podInfoJson);
        if (mInstanceMap.find(pdFlag) != mInstanceMap.end()) {
            subMap = mInstanceMap[pdFlag];
            subMap.emplace(serId, instanceInfo);
        } else {
            subMap[serId]= instanceInfo;
        }
        mPodToInstanceMap[server.ip] = instanceInfo["ID"];
    }
    mInstanceMap[pdFlag] = subMap;
}

void CCAERequestHandler::BuildDistributeInstance(nlohmann::json instanceInfo, std::string pdFlag,
    std::string serverId, const NodeInfo &nodeInfo)
{
    try {
        std::string serverIdKey = mServerIPToInstLogicIDMap[serverId];

        ServerInfo server;
        std::unordered_map<std::string, nlohmann::json> subMap;
        for (auto &it : nodeInfo.serverInfoList) {
            server = it;

            if (!IsExist(instanceInfo["serverIPList"], server.hostId)) {
                instanceInfo["serverIPList"].push_back(server.hostId);
            }

            PodInfo podInfo;
            std::string podKey = serverId;
            if (mPodMap.find(podKey) != mPodMap.end()) {
                podInfo = mPodMap[podKey];
                AppendDeviceInfosToPodInfo(podInfo, server.deviceInfos);
            } else {
                mPodToInstanceMap[server.ip] = instanceInfo["ID"];
                podInfo = ServerToPodInfo(server);
            }

            mPodMap[podKey] = podInfo;
            nlohmann::json podInfoJson = podInfo;

            UpdateArray(instanceInfo["podInfoList"], podInfoJson, "podID");
        }

        if (mInstanceMap.find(pdFlag) != mInstanceMap.end()) {
            subMap = mInstanceMap[pdFlag];
            subMap[serverIdKey] = instanceInfo;
        } else {
            subMap[serverIdKey] = instanceInfo;
        }
        mInstanceMap[pdFlag] = subMap;
    } catch (const std::exception &e) {
        LOG_E("[CCAERequestHandler] BuildDistributeInstance has error, error : %s.", e.what());
    }
}

bool CCAERequestHandler::ParsePDInstance(std::shared_ptr<NodeStatus> nodeStatus)
{
    auto allNodes = nodeStatus->GetAllNodes();
    LOG_I("[CCAEReporter] serverNodes size : %zu", allNodes.size());

    if (allNodes.size() == 0) {
        return false;
    }

    for (auto &it : std::as_const(allNodes)) {
        if (mServerIPToInstLogicIDMap.find(it.second->hostId) == mServerIPToInstLogicIDMap.end()) {
            continue;
        }
        
        if (it.second->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::UN_DEF_INSTANCE) {
            auto deployMode = ControllerConfig::GetInstance()->GetDeployMode();
            if (deployMode == DeployMode::SINGLE_NODE) {
                LOG_D("[CCAEReporter] instance is single node!");
                BuildCentralizedInstances(this->GetInstanceInfo("pd", it.second->hostId), "pd",
                    it.second->hostId, *(it.second));
            }
        }
        if (it.second->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            if (it.second->isDistribute) {
                LOG_D("[CCAEReporter] D instance is distribute!");
                BuildDistributeInstance(this->GetInstanceInfo(DECODE_INSTANCE,
                    mServerIPToInstLogicIDMap[it.second->hostId]), DECODE_INSTANCE, it.second->hostId, *(it.second));
            } else {
                LOG_D("[CCAEReporter] D instance is centralized!");
                BuildCentralizedInstances(this->GetInstanceInfo(DECODE_INSTANCE,
                    mServerIPToInstLogicIDMap[it.second->hostId]), DECODE_INSTANCE, it.second->hostId, *(it.second));
            }
        }
        if (it.second->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
            if (it.second->isDistribute) {
                LOG_D("[CCAEReporter] P instance is distribute!");
                BuildDistributeInstance(this->GetInstanceInfo(PREFILL_INSTANCE,
                    mServerIPToInstLogicIDMap[it.second->hostId]), PREFILL_INSTANCE, it.second->hostId, *(it.second));
            } else {
                LOG_D("[CCAEReporter] P instance is centralized!");
                BuildCentralizedInstances(this->GetInstanceInfo(PREFILL_INSTANCE,
                    mServerIPToInstLogicIDMap[it.second->hostId]), PREFILL_INSTANCE, it.second->hostId, *(it.second));
            }
        }
    }
    return true;
}

std::string GetDPRole(NodeInfo &nodeInfo, const ServerInfo &serverInfo)
{
    if (nodeInfo.isDistribute) {
        return "Central";
    }
    return serverInfo.isMaster ? "Central" : "Worker";
}

std::string CCAERequestHandler::GetPDInstIDForDPGroup(NodeInfo &nodeInfo, const ServerInfo &serverInfo)
{
    std::string serverIp = serverInfo.ip;
    std::string curRole;
    if (nodeInfo.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        curRole = PREFILL_INSTANCE;
    } else if (nodeInfo.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        curRole = DECODE_INSTANCE;
    } else {
        curRole = UN_DEF_INSTANCE;
        return UN_DEF_INSTANCE;
    }

    for (const auto &it : std::as_const(mPodToInstanceMap)) {
        if (it.first == serverIp) {
            return it.second;
        }
    }
    return UN_DEF_INSTANCE;
}

nlohmann::json GetNPUInfoList(const std::vector<DeviceInfo> &deviceInfos)
{
    nlohmann::json npuInfoList = nlohmann::json::array();
    for (const auto &device : deviceInfos) {
        nlohmann::json npuInfo = nlohmann::json::object();
        npuInfo["NPUID"] = device.id;
        npuInfo["NPUIP"] = device.ip;
        npuInfoList.emplace_back(npuInfo);
    }
    return npuInfoList;
}

nlohmann::json GetPodInfoList(const ServerInfo &serverInfo, const std::vector<std::shared_ptr<PodInfo>> serverPodList)
{
    nlohmann::json podInfoList = nlohmann::json::array();
    nlohmann::json podInfo = nlohmann::json::object();
    podInfo["podName"] = "";
    podInfo["podID"] = serverInfo.ip;
    if (serverPodList.empty()) {
        LOG_W("[CCAERequestHandler] GetPDPodInfoList failed, pdPodInfo is empty!");
        podInfo["podAssociatedInfoList"] = nlohmann::json::array();
        podInfoList.emplace_back(podInfo);
        return podInfoList;
    }
    for (auto &it : std::as_const(serverPodList)) {
        if (it->podIP == serverInfo.ip) {
            podInfo["podAssociatedInfoList"] = nlohmann::json::array();
            for (auto &itDevice : it->podAssociatedInfoList) {
                nlohmann::json podAssociatedInfo = nlohmann::json::object();
                podAssociatedInfo["NPUID"] = itDevice.npuID;
                podAssociatedInfo["NPUIP"] = itDevice.npuIP;
                podInfo["podAssociatedInfoList"].emplace_back(podAssociatedInfo);
            }
            podInfoList.emplace_back(podInfo);
            return podInfoList;
        }
    }
    podInfo["podAssociatedInfoList"] = nlohmann::json::array();
    podInfoList.emplace_back(podInfo);
    return podInfoList;
}

void CCAERequestHandler::FillDPGroupInfo(nlohmann::json &dpGroupJson, NodeInfo &nodeInfo, const ServerInfo &serverInfo,
    const size_t dpGroupID, const std::vector<std::shared_ptr<PodInfo>> serverPodList)
{
    dpGroupJson["DPGroupID"] = std::to_string(dpGroupID);
    dpGroupJson["DPGroupName"] = std::to_string(dpGroupID);
    dpGroupJson["DPList"] = nlohmann::json::array();
    
    auto dp = nlohmann::json::object();
    dp["DPID"] = "0";
    dp["DPName"] = "";
    dp["DPRole"] = GetDPRole(nodeInfo, serverInfo);
    dp["PDInstID"] = this->GetPDInstIDForDPGroup(nodeInfo, serverInfo);
    dp["serverList"] = nlohmann::json::array();

    auto server = nlohmann::json::object();
    server["serverID"] = serverInfo.hostId;
    server["serverIP"] = serverInfo.ip;
    server["serverName"] = "";

    server["NPUInfoList"] = GetNPUInfoList(serverInfo.deviceInfos);

    dp["serverList"].emplace_back(server);
    dp["podInfoList"] = GetPodInfoList(serverInfo, serverPodList);
    dpGroupJson["DPList"].emplace_back(dp);
}

nlohmann::json CCAERequestHandler::GetDPGroupList(std::shared_ptr<NodeStatus> nodeStatus,
    const std::vector<std::shared_ptr<PodInfo>> serverPodList)
{
    nlohmann::json dpGroupList = nlohmann::json::array();
    if (!ControllerConfig::GetInstance()->IsMultiNodeMode()) {
        return dpGroupList;
    }
    auto allNodes = nodeStatus->GetAllNodes();
    for (auto &it : std::as_const(allNodes)) {
        auto nodeInfo = *(it.second);
        for (auto &itServer : nodeInfo.serverInfoList) {
            auto serverInfo = itServer;

            for (auto &itDPGroup : serverInfo.dpGroupInfos) {
                auto dpGroup = itDPGroup.second;
                auto dpGroupID = itDPGroup.first;
                nlohmann::json dpGroupJson = nlohmann::json::object();

                FillDPGroupInfo(dpGroupJson, nodeInfo, serverInfo, dpGroupID, serverPodList);
                dpGroupList.emplace_back(dpGroupJson);
            }
        }
    }
    return dpGroupList;
}


nlohmann::json GetControllerInfo(const std::vector<std::shared_ptr<PodInfo>> controllerPodList, bool isMaster)
{
    std::string currentPodIp = ControllerConfig::GetInstance()->GetPodIP();
    nlohmann::json controllerNodesList = nlohmann::json::array();
    for (auto &node : std::as_const(controllerPodList)) {
        nlohmann::json controllerNode = nlohmann::json::object();
        if (isMaster) {
            if (currentPodIp == node->podIP) {
                controllerNode["serverSN"] = "";
                controllerNode["serverIP"] = node->hostIP;
                controllerNodesList.emplace_back(controllerNode);
            }
        } else {
            if (currentPodIp != node->podIP) {
                controllerNode["serverSN"] = "";
                controllerNode["serverIP"] = node->hostIP;
                controllerNodesList.emplace_back(controllerNode);
            }
        }
    }
    return controllerNodesList;
}

nlohmann::json GetCoordinatorInfo(const std::vector<std::shared_ptr<PodInfo>> coordinatorPodList)
{
    nlohmann::json coordinatorNodesList = nlohmann::json::array();
    
    for (auto &node : std::as_const(coordinatorPodList)) {
        nlohmann::json coordinatorNode = nlohmann::json::object();
        coordinatorNode["serverSN"] = "";
        coordinatorNode["serverIP"] = node->hostIP;
        coordinatorNodesList.emplace_back(coordinatorNode);
    }
    return coordinatorNodesList;
}

nlohmann::json GetBackupServerInfo()
{
    nlohmann::json backupServer;

    backupServer["backupInfoList"] = nlohmann::json::array();
    nlohmann::json backupInfo = nlohmann::json::object();
    backupInfo["serverIP"] = "";
    backupInfo["backupRole"] = "";
    backupServer["backupInfoList"].emplace_back(backupInfo);

    return backupServer;
}

nlohmann::json GetExpertInfo()
{
    nlohmann::json expertInfo;

    expertInfo["ID"] = "";
    expertInfo["Name"] = "";
    expertInfo["DPID"] = "";
    expertInfo["serverIP"] = "";
    expertInfo["podInfoList"] = nlohmann::json::array();

    nlohmann::json podInfo = nlohmann::json::object();
    podInfo["podName"] = "";
    podInfo["podAssociatedInfoList"] = nlohmann::json::array();
    nlohmann::json podAssociatedInfo = nlohmann::json::object();
    podAssociatedInfo["NPUID"] = "";
    podAssociatedInfo["NPUIP"] = "";
    podInfo["podAssociatedInfoList"].emplace_back(podAssociatedInfo);
    podInfo["podID"] = "";
    expertInfo["podInfoList"].emplace_back(podInfo);

    return expertInfo;
}

void CCAERequestHandler::GenerateServerIPToInstLogicIDMap(MINDIE::MS::DIGSInstanceRole role)
{
    std::vector<uint64_t> instancesLogicIds =
        ResourceManager::GetInstance()->GetInstancesLogicIds(role);
    for (const auto& instancesLogicId : instancesLogicIds) {
        InstanceStatus instanceStatus =
            ResourceManager::GetInstance()->GetInstanceStatus(role, instancesLogicId);
        if (instanceStatus != InstanceStatus::ACTIVE) {
            continue;
        }
        std::vector<std::string> instanceAllServerIP =
            ResourceManager::GetInstance()->GetInstanceAllServerIP(role, instancesLogicId);
        for (const auto& instanceServerIP : instanceAllServerIP) {
            LOG_D("[CCAERequestHandler] instanceServerIP : %s is active, match instancesLogicId : %s.",
                instanceServerIP.c_str(), std::to_string(instancesLogicId).c_str());
            mServerIPToInstLogicIDMap[instanceServerIP] = std::to_string(instancesLogicId);
        }
    }
}

nlohmann::json CCAERequestHandler::GetInventoriesJson(bool isForcedUpdate, std::shared_ptr<NodeStatus> mNodeStatus)
{
    this->mInstanceMap.clear();
    this->mPodMap.clear();
    this->mPodToInstanceMap.clear();
    this->mServerIPToInstLogicIDMap.clear();

    nlohmann::json inventoriesInfo;

    inventoriesInfo["forceUpdate"] = isForcedUpdate;
    inventoriesInfo["serverIPList"] = nlohmann::json::array();

    std::vector<std::unique_ptr<PodInfo>> pdPodInfoAll =
                           CCAERequestHandler::GetInstance()->GetRankTableLoader()->GetPDPodInfoList();

    std::vector<std::shared_ptr<PodInfo>> CoordinatorPodList = GetPodInfoByGroupId(pdPodInfoAll, COORDINATOR_GROUP);
    std::vector<std::shared_ptr<PodInfo>> ControllerPodList = GetPodInfoByGroupId(pdPodInfoAll, CONTROLLER_GROUP);
    std::vector<std::shared_ptr<PodInfo>> ServerPodList = GetPodInfoByGroupId(pdPodInfoAll, SERVER_GROUP);

    if (ServerPodList.empty()) {
        LOG_D("[CCAEReporter] nodeInfo is empty!");
        return inventoriesInfo;
    }
    
    GenerateServerIPToInstLogicIDMap(MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE);
    GenerateServerIPToInstLogicIDMap(MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    for (auto &it : std::as_const(ServerPodList)) {
        if (!IsExist(inventoriesInfo["serverIPList"], it->hostIP)) {
            inventoriesInfo["serverIPList"].push_back(it->hostIP);
        }
    }

    if (!ParsePDInstance(mNodeStatus)) {
        return inventoriesInfo;
    }

    auto deployMode = ControllerConfig::GetInstance()->GetDeployMode();
    if (deployMode == DeployMode::SINGLE_NODE) {
        inventoriesInfo["PInstanceList"] = nlohmann::json::array();
        inventoriesInfo["DInstanceList"] = nlohmann::json::array();
        inventoriesInfo["PDHybridList"] = this->GetInstances("pd");
    } else {
        inventoriesInfo["PInstanceList"] = this->GetInstances(PREFILL_INSTANCE);
        inventoriesInfo["DInstanceList"] = this->GetInstances(DECODE_INSTANCE);
        inventoriesInfo["PDHybridList"] = nlohmann::json::array();
    }

    inventoriesInfo["backupServerList"] = nlohmann::json::array();
    inventoriesInfo["backupServerList"].emplace_back(GetBackupServerInfo());
    inventoriesInfo["serverOfManagerMaster"] = GetControllerInfo(ControllerPodList, true);
    inventoriesInfo["serverOfManagerSlave"] = GetControllerInfo(ControllerPodList, false);
    inventoriesInfo["serverOfCoordinator"] = GetCoordinatorInfo(CoordinatorPodList);
    inventoriesInfo["DPGroupList"] = GetDPGroupList(mNodeStatus, ServerPodList);
    inventoriesInfo["expertList"] = nlohmann::json::array();
    inventoriesInfo["expertList"].emplace_back(GetExpertInfo());

    return inventoriesInfo;
}

std::string CCAERequestHandler::FillInventoryRequest(std::vector<std::string> modelIDs,
    std::shared_ptr<CCAEStatus> mCCAEStatus,
    std::shared_ptr<NodeStatus> mNodeStatus,
    std::string metricsInfo)
{
    // 验证指针参数
    if (mCCAEStatus == nullptr || mNodeStatus == nullptr) {
        LOG_E("[CCAERequestHandler] Null pointer parameters detected");
        return "{}";
    }
    
    // 验证modelIDs不为空
    if (modelIDs.empty()) {
        LOG_E("[CCAERequestHandler] ModelIDs vector is empty");
        return "{}";
    }
    
    // 验证metricsInfo
    if (!IsValidMetricsInfo(metricsInfo)) {
        LOG_E("[CCAERequestHandler] Invalid metricsInfo detected");
        return "{}";
    }

    nlohmann::json inventoryJ;
    inventoryJ["componentType"] = ControllerConstant::GetInstance()->GetComponentType();
    inventoryJ["modelServiceInfo"] = nlohmann::json::array();

    for (const auto& modelID : modelIDs) {
        // 验证MODEL_ID格式
        if (!IsValidModelID(modelID)) {
            LOG_E("[CCAERequestHandler] Invalid MODEL_ID detected: %s", modelID.c_str());
            continue; // 跳过无效的modelID，继续处理其他的
        }
        
        nlohmann::json model = nlohmann::json::object();
        model["modelName"] = ControllerConfig::GetInstance()->GetModelType();
        model["modelType"] = ControllerConfig::GetInstance()->GetModelType();
        model["modelID"] = modelID;
        model["timeStamp"] = GetTimeStampNowInMillisec();
        model["modelState"] = static_cast<int>(this->GetModelState());
        model["metrics"] = GetMetricsJson(mCCAEStatus->GetMetricPeriod(modelID), metricsInfo);
        model["inventories"] = this->GetInventoriesJson(mCCAEStatus->ISForcedUpdate(modelID), mNodeStatus);
        inventoryJ["modelServiceInfo"].emplace_back(model);
    }
    std::string jsonString = inventoryJ.dump();
    return jsonString;
}

static std::string FillRegisterRequest()
{
    nlohmann::json configRequest;
    auto ctrlConfigInst = ControllerConfig::GetInstance();
    int64_t timeStamp = GetTimeStampNowInMillisec();
    configRequest["timeStamp"] = timeStamp;
    configRequest["version"]= ControllerConstant::GetInstance()->GetMindIEVersion();
    configRequest["componentType"] = ControllerConstant::GetInstance()->GetComponentType();
    for (size_t i = 0; i < ctrlConfigInst->GetModelNum(); i++) {
        configRequest["modelServiceInfo"][i]["modelID"] = ctrlConfigInst->GetModelID(i);
        configRequest["modelServiceInfo"][i]["modelName"] = ctrlConfigInst->GetModelType();
    }
    return configRequest.dump();
}

static int32_t ParseResp(CCAEStatus &ccaeStatus, const std::string &response)
{
    try {
        nlohmann::json responseJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        if (!responseJson.contains("reqList") || !responseJson["reqList"].is_array()) {
            LOG_E("[CCAERequestHandler] Missing or invalid reqList field");
            return -1;
        }
        auto& reqList = responseJson["reqList"];
        for (size_t i = 0; i < reqList.size(); i++) {
            auto modelID = reqList.at(i).at("modelID").get<std::string>();
            ccaeStatus.SetMetricPeriod(modelID, reqList.at(i).at("metrics").at("metricPeriod").get<int64_t>());
            ccaeStatus.SetForcedUpdate(modelID, reqList.at(i).at("inventories").at("forceUpdate").get<bool>());
        }
        return 0;
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[CCAERequestHandler] JSON processing error: %s", e.what());
        return -1;
    } catch (const std::exception& e) {
        LOG_E("[CCAERequestHandler] Error: %s", e.what());
        return -1;
    }
}

static bool IsValidRegisterResp(const std::string &response)
{
    if (!nlohmann::json::accept(response)) {
        LOG_E("[CCAERequestHandler] Invalid CCAE register response %s", response.c_str());
        return false;
    }
    // check if return code and message are valid
    auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
    if (!IsJsonIntValid(bodyJson, "retCode", 0, 1) ||
        !IsJsonStringValid(bodyJson, "retMsg", 0, 128)) { // return message should be between 0 and 128 charactors
        LOG_E("[CCAERequestHandler] Invalid CCAE register response return code or message data type");
        return false;
    }

    // check if returned correctly by checking the return code, if not drop error log
    try {
        int64_t retCode = bodyJson.at("retCode").get<int64_t>();
        std::string retMsg = bodyJson.at("retMsg").get<std::string>();
        
        if (retCode == 1) {
            LOG_E("[CCAERequestHandler] CCAE register returns error code %ld and error message: %s",
                retCode, retMsg.c_str());
            return false;
        }
    } catch (const std::exception& e) {
        LOG_E("[CCAERequestHandler] Failed to process CCAE register response: %s", e.what());
        return false;
    }
    if (!IsJsonArrayValid(bodyJson, "reqList", 1, 2048)) { // one Controller can maintain at most 2048 NPUs
        LOG_E("[CCAERequestHandler] CCAE register dose not return with valid request list.");
        return false;
    }

    for (const auto &reqJson : bodyJson.at("reqList")) {
        if (!IsJsonStringValid(reqJson, "modelID", MODEL_ID_LEN, MODEL_ID_LEN)) {
            LOG_E("[CCAERequestHandler] CCAE register dose not return with valid modelID.");
            return false;
        }

        if (!IsJsonObjValid(reqJson, "metrics") ||
            !IsJsonIntValid(reqJson.at("metrics"), "metricPeriod", MIN_MTRX_PERIOD, MAX_MTRX_PERIOD)) {
            LOG_E("[CCAERequestHandler] CCAE register dose not return with valid metrics.");
            return false;
        }

        if (!IsJsonObjValid(reqJson, "inventories") || !IsJsonBoolValid(reqJson.at("inventories"), "forceUpdate")) {
            LOG_E("[CCAERequestHandler] CCAE register dose not return with valid inventories.");
            return false;
        }
    }

    LOG_D("[CCAERequestHandler] CCAE register returns success.");
    return true;
}

static bool IsSuccessInventoriesResp(const std::string &response)
{
    if (!nlohmann::json::accept(response)) {
        LOG_E("[CCAERequestHandler] Invalid CCAE inventories response");
        return false;
    }

    // check if return code and message are valid
    auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
    if (!IsJsonIntValid(bodyJson, "retCode", 0, 1) || !IsValidString(bodyJson["retMsg"])) {
        LOG_E("[CCAERequestHandler] Invalid CCAE inventories response return code or message data type");
        return false;
    }

    // check if returned correctly by checking the return code, if not drop error log
    try {
        int64_t retCode = bodyJson["retCode"].get<int64_t>();
        std::string retMsg = bodyJson["retMsg"].get<std::string>();
        if (retCode == 1) {
            LOG_E("[CCAERequestHandler] CCAE inventories return error code %ld and error message: %s",
                retCode, retMsg.c_str());
            return false;
        }
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[CCAERequestHandler] Invalid CCAE inventories response: value extraction failed: %s",
            e.what());
        return false;
    }
    LOG_D("[CCADRDquestHandler] CCAE inventories return success.");
    return true;
}

int32_t CCAERequestHandler::SendRegister2UpdateStatus(HttpClient &client, CCAEStatus &ccaeStatus) const
{
    auto ctrlConfigInst = ControllerConfig::GetInstance();
    auto port = ctrlConfigInst->GetCCAEPort();
    auto ip = ctrlConfigInst->GetCCAEIP();
    std::string response;
    int32_t code = 400;
    std::string jsonString = FillRegisterRequest();
    std::map <boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    Request req = {
        ControllerConstant::GetInstance()->GetCCAEURI(CCAEURI::REGISTER),
        boost::beast::http::verb::post,
        map,
        jsonString
    };

    client.SetHostAndPort(ip, std::to_string(port));
    int32_t ret = client.SendRequest(req, 1, 0, response, code);
    LOG_D("[CCAERequestHandler] Getting register response, IP %s, ret code %d, request ret %d.",
        ip.c_str(), code, ret);

    if (ret == 0 && code == CODE_OK && IsValidRegisterResp(response)) {
        return ParseResp(ccaeStatus, response);
    } else {
        LOG_E("[CCAERequestHandler] Getting register response, send request failed, "
              "IP %s, port %ld, ret code %d, request ret %d.", ip.c_str(), port, code, ret);
        return -1;
    }
}

int32_t CCAERequestHandler::SendInventories(HttpClient &client, const std::string &jsonString) const
{
    auto ctrlConfigInst = ControllerConfig::GetInstance();
    auto port = ctrlConfigInst->GetCCAEPort();
    auto ip = ctrlConfigInst->GetCCAEIP();
    std::string response;
    int32_t code = 400;
    std::map <boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    Request req = {
        ControllerConstant::GetInstance()->GetCCAEURI(CCAEURI::INVENTORY),
        boost::beast::http::verb::post,
        map,
        jsonString
    };

    client.SetHostAndPort(ip, std::to_string(port));
    int32_t ret = client.SendRequest(req, 1, 0, response, code);
    LOG_D("[CCAERequestHandler] Getting inverntory response, IP %s, ret code %d, request ret %d.",
        ip.c_str(), code, ret);

    if (ret == 0 && code == CODE_CREATED && IsSuccessInventoriesResp(response)) {
        return 0;
    } else {
        LOG_E("[CCAERequestHandler] Getting inverntory response, send request failed, "
              "IP %s, port %ld, ret code %d, request ret %d.", ip.c_str(), port, code, ret);
        return -1;
    }
}

void CCAERequestHandler::SetRankTableLoader(std::shared_ptr<RankTableLoader> loader)
{
    if (loader != nullptr) {
        mRankTableLoader = loader;
    } else {
        LOG_E("[CCAERequestHandler] SetRankTableLoader failed, ranktable loader is nullptr");
    }
}

std::shared_ptr<RankTableLoader> CCAERequestHandler::GetRankTableLoader() const
{
    if (mRankTableLoader != nullptr) {
        return mRankTableLoader;
    } else {
        LOG_E("[CCAERequestHandler] GetRankTableLoader failed, ranktable loader is nullptr");
        return nullptr;
    }
}

ModelState CCAERequestHandler::GetModelState() const
{
    // 检查coordinator是否能ping通,ping不通就返回Unhealthy
    if (!coordinatorHealthy.load()) {
        LOG_D("[CCAERequestHandler] Model state is Unhealthy due to coordinator ping failure");
        return ModelState::UNHEALTHY;
    }

    // 检查coordinator是否能正常提供推理服务
    if (!coordinatorServiceReady.load()) {
        LOG_D("[CCAERequestHandler] Model state is Unhealthy due to coordinator service not ready");
        return ModelState::UNHEALTHY;
    }

    // 检查所有实例是否ok
    bool pHealthy = AreAllInstancesHealthy(MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE);
    bool dHealthy = AreAllInstancesHealthy(MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    if (pHealthy && dHealthy) {
        return ModelState::HEALTHY;
    } else {
        return ModelState::SUBHEALTHY;
    }
}

bool CCAERequestHandler::AreAllInstancesHealthy(MINDIE::MS::DIGSInstanceRole role) const
{
    std::vector<uint64_t> instancesLogicIds =
        ResourceManager::GetInstance()->GetInstancesLogicIds(role);

    for (uint64_t logicId : instancesLogicIds) {
        auto status = ResourceManager::GetInstance()->GetInstanceStatus(role, logicId);
        if (status != InstanceStatus::ACTIVE) {
            return false;
        }
    }
    return true;
}

void CCAERequestHandler::SetCoordinatorServiceReady(bool isReady)
{
    coordinatorServiceReady.store(isReady);
}

void CCAERequestHandler::SetcoordinatorHealthy(bool isHealthy)
{
    coordinatorHealthy.store(isHealthy);
}
}
