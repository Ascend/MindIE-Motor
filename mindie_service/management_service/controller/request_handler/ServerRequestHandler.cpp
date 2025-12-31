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
#include "ServerRequestHandler.h"
#include <cstdint>
#include <set>
#include <sstream>
#include <thread>
#include "ControllerConfig.h"
#include "AlarmManager.h"
#include "AlarmRequestHandler.h"
#include "Util.h"

namespace MINDIE::MS {
constexpr int32_t CODE_OK = 200;
constexpr int64_t MAX_SERVER_NODES_PER_GROUP = 768; // 每个组内768个节点
constexpr int64_t MIN_INT_VALUE = 0;
constexpr int64_t MAX_INT_VALUE = 4294967295;
static bool IsValidStaticInfoResp(const std::string &response)
{
    try {
        if (!CheckJsonStringSize(response)) {
            LOG_E("[%s] [ServerRequestHandler] Invalid response: %s",
                  GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                  response.substr(0, JSON_STR_SIZE_HEAD).c_str());
            return false;
        }
        if (!nlohmann::json::accept(response)) {
            LOG_E("[%s] [ServerRequestHandler] Invalid response %s.",
                  GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                  response.c_str());
            return false;
        }
        auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        if (!IsJsonIntValid(bodyJson, "maxSeqLen", 0, MAX_INT_VALUE) ||
            !IsJsonIntValid(bodyJson, "maxOutputLen", 1, 4294967294) || // 取值范围1~4294967294
            !IsJsonIntValid(bodyJson, "cacheBlockSize", 1, 128) || // 取值范围1~128
            !IsJsonStringValid(bodyJson, "modelName")) {
            return false;
        }
        return true;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ServerRequestHandler] Failed to validate static information response, error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
              e.what());
        return false;
    }
}

static int32_t ParseInstanceStaticInfoResp(const std::string &response, NodeInfo &info)
{
    try {
        if (!CheckJsonStringSize(response)) {
            LOG_E("[%s] [ServerRequestHandler] Failed to parse instance static information response, invalid.",
                GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::SERVER_REQUEST_HANDLER).c_str());
            return -1;
        }
        LOG_I("[ServerRequestHandler] Parsing instance static information response: %s", response.c_str());
        auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        info.instanceInfo.staticInfo.maxSeqLen = bodyJson.at("maxSeqLen").get<size_t>();
        info.instanceInfo.staticInfo.maxOutputLen = bodyJson.at("maxOutputLen").get<size_t>();
        info.instanceInfo.staticInfo.blockSize = bodyJson.at("cacheBlockSize").get<size_t>();
        info.modelName = bodyJson.at("modelName").get<std::string>();
        return 0;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ServerRequestHandler] Failed to parse instance static information response, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            e.what());
        return -1;
    }
}

int32_t ServerRequestHandler::QueryInstanceInfo(HttpClient &client, NodeInfo &node) const
{
    auto port = node.mgmtPort;
    std::string response;
    int32_t code = 400;
    int32_t httpRet;
    std::string jsonString;
    std::map<boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    client.SetHostAndPort(node.ip, port);
    Request req = {ControllerConstant::GetInstance()->GetServerURI(ServerURI::GET_CONFIG),
                   boost::beast::http::verb::get, map, jsonString};
    httpRet = client.SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
        ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
    if (httpRet == 0 && code == CODE_OK && IsValidStaticInfoResp(response)) {
        if (ParseInstanceStaticInfoResp(response, node) != 0) {
            LOG_E("[%s] [ServerRequestHandler] Parse instance static information failed, node IP %s, port %s.",
                GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                node.ip.c_str(),
                port.c_str());
            return -1;
        }
        return 0;
    } else {
        LOG_W("[%s] [ServerRequestHandler] Request static info failed, node IP %s, port %s, "
              "ret code %d, request ret %d.",
              GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
              node.ip.c_str(), port.c_str(), code, httpRet);
        return -1;
    }
}

static void ParseNodeStatusPeers(std::vector<uint64_t> &peers, nlohmann::json &resource)
{
    if (resource.at("linkStatus").at("peers").empty()) {
        peers.clear();
        return;
    }
    auto peersList = resource.at("linkStatus").at("peers");
    uint64_t target {};
    for (auto &item : peersList) {
        target = item.at("target").get<uint64_t>();
        if (item.at("link").get<std::string>() != "ok") {
            LOG_W("[%s] [ServerRequestHandler] Link to node's peer %u failed, reason is %s.",
                GetWarnCode(ErrorType::WARNING, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                target, item.at("link").get<std::string>().c_str());
            continue;
        }

        uint64_t id = target;
        peers.push_back(id);
        LOG_D("[ServerRequestHandler] Parsing node status peers, add node's peer %lu.", id);
    }
}

bool ServerRequestHandler::IsUpdatePToDNeeded(const NodeInfo &node) const
{
    return node.currentRole == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE &&
        node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
}

bool ServerRequestHandler::IsUpdateDToPNeeded(const NodeInfo &node) const
{
    return node.currentRole == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE &&
        node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE;
}

bool ServerRequestHandler::IsUpdateRoleNeeded(const NodeInfo &node) const
{
    return IsUpdatePToDNeeded(node) || IsUpdateDToPNeeded(node);
}

static bool IsReadyToUpdateNodeStaticInfo(NodeStatus &nodeStatus, uint64_t id)
{
    auto node = nodeStatus.GetNode(id);
    if (node == nullptr) {
        return false;
    }
    if (node->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE
        && (node->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::READY))) {
        return true;
    }
    if (node->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE &&
        !node->activePeers.empty() && !node->peers.empty()) {
        return true;
    }
    return false;
}

static bool IsValidService(const nlohmann::json &service)
{
    if (!IsJsonStringValid(service, "roleStatus") ||
        !IsJsonStringValid(service, "currentRole")) {
        return false;
    }
    std::vector<std::string> roleStatus {"RoleUnknown", "RoleSwitching", "RoleReady"};
    if (std::find(roleStatus.begin(), roleStatus.end(), service["roleStatus"]) == roleStatus.end()) {
        LOG_E("[%s] [ServerRequestHandler] Service is invalid because role status is invalid.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::SERVER_REQUEST_HANDLER).c_str());
        return false;
    }

    std::vector<std::string> currentRole{"prefill", "decode", "flex", "none"};
    if (std::find(currentRole.begin(), currentRole.end(), service["currentRole"]) == currentRole.end()) {
        LOG_E("[%s] [ServerRequestHandler] Service is invalid because current rule is invalid.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::SERVER_REQUEST_HANDLER).c_str());
        return false;
    }
    return true;
}

static bool IsValidNodeStatusResp(const std::string &response)
{
    try {
        if (!CheckJsonStringSize(response)) {
            LOG_E("[%s] [ServerRequestHandler] IsValidNodeStatusResp: invalid resp %s",
                  GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                  response.substr(0, JSON_STR_SIZE_HEAD).c_str());
            return false;
        }
        if (!nlohmann::json::accept(response)) {
            LOG_E("[%s] [ServerRequestHandler] IsValidNodeStatusResp: invalid resp %s",
                  GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                  response.c_str());
            return false;
        }
        auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        if (!IsJsonObjValid(bodyJson, "service") || !IsValidService(bodyJson["service"]) ||
            !IsJsonObjValid(bodyJson, "resource")) {
            return false;
        }
        if (ControllerConfig::GetInstance()->IsMultiNodeMode()) {
            if (!IsJsonIntValid(bodyJson["resource"], "totalAvailNpuSlotsNum", MIN_INT_VALUE, 5000) || // 取值范围0~5000
                !IsJsonIntValid(bodyJson["resource"], "totalAvailNpuBlockNum", MIN_INT_VALUE, MAX_INT_VALUE) ||
                !IsJsonIntValid(bodyJson["resource"], "maxAvailNpuBlockNum", MIN_INT_VALUE, MAX_INT_VALUE)) {
                return false;
            }
        } else {
            if (!IsJsonIntValid(bodyJson["resource"], "availSlotsNum", MIN_INT_VALUE, 5000) || // 取值范围0~5000
                !IsJsonIntValid(bodyJson["resource"], "availBlockNum", MIN_INT_VALUE, MAX_INT_VALUE)) {
                return false;
            }
        }
        if (!bodyJson.contains("peers")) {
            return true;
        }
        if (!IsJsonArrayValid(bodyJson, "peers", 0, MAX_SERVER_NODES_PER_GROUP)) {
            return false;
        }
        for (auto &ip: bodyJson.at("peers")) {
            if (!ip.is_string() || !IsValidIp(ip)) {
                LOG_E("[%s] [ServerRequestHandler] Validating node status response, peers IP is invalid.",
                      GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::SERVER_REQUEST_HANDLER).c_str());
                return false;
            }
        }
        return true;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ServerRequestHandler] Failed to validate node status response, error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
              e.what());
        return false;
    }
}

int32_t ServerRequestHandler::ParseNodeStatusRespToNodeStatus(NodeStatus &nodeStatus, NodeInfo &nodeInfo,
    const std::string &response, bool initStaticTotalInfo, bool &isReady)
{
    auto mode = ControllerConfig::GetInstance()->GetDeployMode();
    try {
        if (!CheckJsonStringSize(response)) {
            LOG_E("[%s] [ServerRequestHandler] Failed to parse node status response to node status, invalid.",
                GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::SERVER_REQUEST_HANDLER).c_str());
            nodeStatus.UpdateRoleState(nodeInfo.instanceInfo.staticInfo.id,
                ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN), false, nodeInfo.isInitialized);
            return -1;
        }
        MINDIE::MS::DIGSInstanceDynamicInfo info;
        auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        auto roleStatus = bodyJson.at("service").at("roleStatus").get<std::string>();
        auto roleStr = bodyJson.at("service").at("currentRole").get<std::string>();
        auto role = ControllerConstant::GetInstance()->GetDIGSInstanceRoleByStr(roleStr);
        auto resource = bodyJson.at("resource");
        initStaticTotalInfo = (nodeInfo.roleState == "RoleSwitching") && (roleStatus == "RoleReady")
            && !nodeInfo.isStaticInfoinit;
        if (ControllerConfig::GetInstance()->IsMultiNodeMode()) {
            info.availSlotsNum = resource.at("totalAvailNpuSlotsNum").get<size_t>();
            info.availBlockNum = resource.at("totalAvailNpuBlockNum").get<size_t>();
            info.maxAvailBlockNum = resource.at("maxAvailNpuBlockNum").get<size_t>();
        } else {
            info.availSlotsNum = resource.at("availSlotsNum").get<size_t>();
            info.availBlockNum = resource.at("availBlockNum").get<size_t>();
            info.waitingRequestNum = resource.at("waitingRequestNum").get<size_t>();
            info.runningRequestNum = resource.at("runningRequestNum").get<size_t>();
            info.swappedRequestNum = resource.at("swappedRequestNum").get<size_t>();
            info.freeNpuBlockNums = resource.at("freeNpuBlockNums").get<size_t>();
            info.freeCpuBlockNums = resource.at("freeCpuBlockNums").get<size_t>();
            info.totalNpuBlockNums = resource.at("totalNpuBlockNums").get<size_t>();
            info.totalCpuBlockNums = resource.at("totalCpuBlockNums").get<size_t>();
        }
        std::vector<uint64_t> peers;
        ParseNodeStatusPeers(peers, bodyJson);
        isReady = (nodeInfo.roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::READY)) &&
            (role == nodeInfo.instanceInfo.staticInfo.role);
        nodeStatus.UpdateNodeDynamicStatus(nodeInfo.instanceInfo.staticInfo.id, role, roleStatus, info, peers);
        if (initStaticTotalInfo && IsReadyToUpdateNodeStaticInfo(nodeStatus, nodeInfo.instanceInfo.staticInfo.id)) {
            nodeStatus.UpdateNodeStaticInfo(nodeInfo.instanceInfo.staticInfo.id, info);
            nodeInfo.isStaticInfoinit = true;
        }
        if (mode != DeployMode::PD_SEPARATE) {
            return 0;
        }
        if (nodeStatus.IsPostRoleNeeded(nodeInfo.instanceInfo.staticInfo.id) && mRoleUnknownCallback != nullptr) {
            mRoleUnknownCallback(nodeInfo.instanceInfo.staticInfo.id, nodeInfo.instanceInfo.staticInfo.role);
        }
        return 0;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ServerRequestHandler] Failed to parse node status response to node status, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            e.what());
        nodeStatus.UpdateRoleState(nodeInfo.instanceInfo.staticInfo.id,
            ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN), false, nodeInfo.isInitialized);
        return -1;
    }
}

bool ServerRequestHandler::IsPostRoleNeeded(const NodeInfo &nodeInfo) const
{
    if (nodeInfo.roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN)) {
        LOG_W("[%s] [ServerRequestHandler] Need post role, node %lu, role %c, status %s.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            nodeInfo.instanceInfo.staticInfo.id, nodeInfo.instanceInfo.staticInfo.role, nodeInfo.roleState.c_str());
        return true;
    }

    if ((nodeInfo.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE ||
         nodeInfo.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) &&
        nodeInfo.activePeers.empty() && !nodeInfo.peers.empty()) {
        LOG_D("[%s] [ServerRequestHandler] Need post role, node %lu, role %c, status %s, active peers %zu, peers %zu.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            nodeInfo.instanceInfo.staticInfo.id, nodeInfo.instanceInfo.staticInfo.role, nodeInfo.roleState.c_str(),
            nodeInfo.activePeers.size(), nodeInfo.peers.size());
        return true;
    }
    return false;
}

void ServerRequestHandler::SetDynamicInfo(NodeInfo &nodeInfo, nlohmann::json &bodyJson)
{
    auto resource = bodyJson.at("resource");
    nodeInfo.instanceInfo.dynamicInfo.availSlotsNum = resource.at("availSlotsNum").get<size_t>();
    nodeInfo.instanceInfo.dynamicInfo.availBlockNum = resource.at("availBlockNum").get<size_t>();
    nodeInfo.instanceInfo.dynamicInfo.waitingRequestNum = resource.at("waitingRequestNum").get<size_t>();
    nodeInfo.instanceInfo.dynamicInfo.runningRequestNum = resource.at("runningRequestNum").get<size_t>();
    nodeInfo.instanceInfo.dynamicInfo.swappedRequestNum = resource.at("swappedRequestNum").get<size_t>();
    nodeInfo.instanceInfo.dynamicInfo.freeNpuBlockNums = resource.at("freeNpuBlockNums").get<size_t>();
    nodeInfo.instanceInfo.dynamicInfo.freeCpuBlockNums = resource.at("freeCpuBlockNums").get<size_t>();
    nodeInfo.instanceInfo.dynamicInfo.totalNpuBlockNums = resource.at("totalNpuBlockNums").get<size_t>();
    nodeInfo.instanceInfo.dynamicInfo.totalCpuBlockNums = resource.at("totalCpuBlockNums").get<size_t>();
}

int32_t ServerRequestHandler::ParseNodeStatusResp(NodeInfo &nodeInfo,
    const std::string &response, bool initStaticTotalInfo, bool &isReady)
{
    auto mode = ControllerConfig::GetInstance()->GetDeployMode();
    nodeInfo.isHealthy = false;
    try {
        if (!CheckJsonStringSize(response)) {
            LOG_E("[%s] [ServerRequestHandler] Faield to parse node status response, invalid.",
                GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::SERVER_REQUEST_HANDLER).c_str());
            nodeInfo.roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN);
            return -1;
        }
        MINDIE::MS::DIGSInstanceDynamicInfo info;
        auto bodyJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        nodeInfo.roleState = bodyJson.at("service").at("roleStatus").get<std::string>();
        auto roleStr = bodyJson.at("service").at("currentRole").get<std::string>();
        nodeInfo.currentRole = ControllerConstant::GetInstance()->GetDIGSInstanceRoleByStr(roleStr);
        auto resource = bodyJson.at("resource");
        if (ControllerConfig::GetInstance()->IsMultiNodeMode()) {
            nodeInfo.instanceInfo.dynamicInfo.availSlotsNum = resource.at("totalAvailNpuSlotsNum").get<size_t>();
            nodeInfo.instanceInfo.dynamicInfo.availBlockNum = resource.at("totalAvailNpuBlockNum").get<size_t>();
            nodeInfo.instanceInfo.dynamicInfo.maxAvailBlockNum = resource.at("maxAvailNpuBlockNum").get<size_t>();
        } else {
            SetDynamicInfo(nodeInfo, bodyJson);
        }
        ParseNodeStatusPeers(nodeInfo.activePeers, bodyJson);
        isReady = (nodeInfo.roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::READY)) &&
            (nodeInfo.currentRole == nodeInfo.instanceInfo.staticInfo.role);
        if (isReady && initStaticTotalInfo && !nodeInfo.isStaticInfoinit) {
            nodeInfo.instanceInfo.staticInfo.totalSlotsNum = nodeInfo.instanceInfo.dynamicInfo.availSlotsNum;
            nodeInfo.instanceInfo.staticInfo.totalBlockNum = nodeInfo.instanceInfo.dynamicInfo.availBlockNum;
            nodeInfo.isStaticInfoinit = true;
        }
        nodeInfo.isHealthy = true;
        nodeInfo.isInitialized = (nodeInfo.currentRole != MINDIE::MS::DIGSInstanceRole::UN_DEF_INSTANCE);
        if (mode != DeployMode::PD_SEPARATE) {
            return 0;
        }
        if (ServerRequestHandler::GetInstance()->IsUpdateRoleNeeded(nodeInfo)) {
            LOG_W("[%s] [ServerRequestHandler] Parsing node status response, node %lu needs to update role.",
                GetWarnCode(ErrorType::WARNING, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                nodeInfo.instanceInfo.staticInfo.id);
            return 0;
        }
        if (IsPostRoleNeeded(nodeInfo) && mRoleUnknownCallback != nullptr) {
            mRoleUnknownCallback(nodeInfo.instanceInfo.staticInfo.id, nodeInfo.instanceInfo.staticInfo.role);
        }
        return 0;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ServerRequestHandler] Faield to parse node status response, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(), e.what());
        nodeInfo.roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN);
        return -1;
    }
}

int32_t ServerRequestHandler::GetAvailableNodes(HttpClient &client, std::vector<std::unique_ptr<NodeInfo>> &allNodes,
    std::vector<std::unique_ptr<NodeInfo>> &availableNodes, std::vector<std::unique_ptr<NodeInfo>> &faultyNodes,
    std::shared_ptr<NodeStatus> nodeStatus, uint32_t retryTimes)
{
    auto limit = (retryTimes == 0 ? ControllerConfig::GetInstance()->GetServerOnlineAttemptTimes() : retryTimes);
    auto waitSeconds = ControllerConfig::GetInstance()->GetServerOnlineWaitSeconds();
    bool isReady = false; // Server is considered not ready when detecting them for the first time.
    std::set<uint64_t> finished;
    int64_t initRanktableChangeTime = nodeStatus->GetRanktableChangeTime();
    for (uint32_t attempt = 0; attempt < limit; ++attempt) {
        for (auto &node : allNodes) {
            if (node == nullptr || finished.find(node->instanceInfo.staticInfo.id) != finished.end()) {
                continue;
            }

            LOG_I("[ServerRequestHandler] Querying the static and dynamic info of node with ID %lu and IP %s.",
                node->instanceInfo.staticInfo.id, node->ip.c_str());
            if (QueryInstanceInfo(client, *node) != 0 || UpdateNodeInfo(client, *node, true, isReady) != 0) {
                continue;
            }

            finished.insert(node->instanceInfo.staticInfo.id);
            LOG_I("[ServerRequestHandler] Successfully query static and dynamic info of node with ID %lu and IP %s.",
                node->instanceInfo.staticInfo.id, node->ip.c_str());
        }

        if (finished.size() == allNodes.size()) {
            LOG_I("[ServerRequestHandler] All nodes are available after detected for %u times.", attempt);
            break;
        }

        // Exit early if the node ranktable has changed during iteration
        int64_t curRanktableChangeTime = nodeStatus->GetRanktableChangeTime();
        if (curRanktableChangeTime != initRanktableChangeTime && initRanktableChangeTime != -1) {
            LOG_I("[ServerRequestHandler] GetAvailableNodes exited early: global ranktable changed during iteration.");
            return -1;
        }

        LOG_I("[ServerRequestHandler] %zu nodes are available while %zu nodes remain unavailable "
            "after detected for %u times.", finished.size(), allNodes.size() - finished.size(), attempt);
        std::this_thread::sleep_for(std::chrono::seconds(waitSeconds)); // Sleep when there is unusable node.
    }

    CheckGroupNode(allNodes, finished, availableNodes, faultyNodes);
    
    return 0;
}

void ServerRequestHandler::CheckGroupNode(std::vector<std::unique_ptr<NodeInfo>> &allNodes,
    std::set<uint64_t>& finished, std::vector<std::unique_ptr<NodeInfo>> &availableNodes,
    std::vector<std::unique_ptr<NodeInfo>> &faultyNodes) const
{
    std::ostringstream faultNodeSummaryOss;
    for (auto& node : allNodes) {
        if (node == nullptr) {
            continue;
        }
        uint64_t nodeId = node->instanceInfo.staticInfo.id;
        std::string nodeIp = node->ip;
        bool isAvailable = CheckGroupNodeAvailable(finished, *node);
        if (!isAvailable) {
            faultNodeSummaryOss << "(" << nodeId << "," << nodeIp << "),";
            faultyNodes.push_back(std::move(node));
        } else {
            availableNodes.push_back(std::move(node));
        }
    }
    std::string faultNodeSummaryStr = faultNodeSummaryOss.str();
    if (!faultNodeSummaryStr.empty()) {
        LOG_W("[%s] Following (Node ID,IP) are faulty: %s",
            GetErrorCode(ErrorType::UNAVAILABLE, ControllerFeature::NODE_SCHEDULER).c_str(),
            faultNodeSummaryStr.c_str());
    }
}

bool ServerRequestHandler::CheckGroupNodeAvailable(const std::set<uint64_t>& finished, const NodeInfo& node) const
{
    if (!node.isDistribute) {
        bool res = finished.find(node.instanceInfo.staticInfo.id) != finished.end();
        return res;
    }

    // 判断该节点是不是在finished里面, 如果不在, 直接为不可用
    if (finished.find(node.instanceInfo.staticInfo.id) == finished.end()) {
        return false;
    }

    // 分布式部署，所有peer都完成才算可用
    for (uint64_t peerId : node.dpGroupPeers) {
        if (finished.find(peerId) == finished.end()) {
            return false;
        }
    }
    return true;
}

int32_t ServerRequestHandler::UpdateNodeInfo(HttpClient &client, NodeInfo &nodeInfo,
    bool initStaticTotalInfo, bool &isReady)
{
    auto port = nodeInfo.mgmtPort;
    std::string response;
    int32_t code = 400;
    std::string jsonString;
    std::map <boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    client.SetHostAndPort(nodeInfo.ip, port);
    auto uriType = ControllerConfig::GetInstance()->IsMultiNodeMode() ?
        ServerURI::GET_STATUS_V2 : ServerURI::GET_STATUS_V1;
    auto uri = ControllerConstant::GetInstance()->GetServerURI(uriType);

    Request req = { uri, boost::beast::http::verb::get, map, jsonString };
    int32_t ret = client.SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
        ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
    LOG_D("[ServerRequestHandler] Updating node status: IP %s, ret code %d, request ret %d, response %s",
          nodeInfo.ip.c_str(), code, ret, response.c_str());
    if (ret == 0 && code == CODE_OK && IsValidNodeStatusResp(response)) {
        return ParseNodeStatusResp(nodeInfo, response, initStaticTotalInfo, isReady);
    } else {
        LOG_E("[%s] [ServerRequestHandler] Updating node status: send request failed, node ID %lu, IP %s, "
              "port %s, ret code %d, request ret %d",
              GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
              nodeInfo.instanceInfo.staticInfo.id, nodeInfo.ip.c_str(), port.c_str(), code, ret);
        nodeInfo.roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN);
        nodeInfo.isHealthy = false;
        return -1;
    }
    return 0;
}

void ServerRequestHandler::AddServerEventToAlarmMgr(int32_t sendRes, const NodeInfo &nodeInfo)
{
    std::string eventMsg = AlarmRequestHandler::GetInstance()->FillServerExceptionEventInfo(nodeInfo.ip,
        (sendRes == -1) ? ServerExceptionReason::SERVER_NO_REPLY : ServerExceptionReason::SERVER_RESPONSE_ERROR);
    AlarmManager::GetInstance()->AlarmAdded(eventMsg);
}

int32_t ServerRequestHandler::UpdateNodeStatus(HttpClient &client, NodeStatus &nodeStatus,
    NodeInfo &nodeInfo, bool initStaticTotalInfo, bool &isReady)
{
    auto port = nodeInfo.mgmtPort;
    std::string response;
    int32_t code = 400;
    std::string jsonString;
    std::map <boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    client.SetHostAndPort(nodeInfo.ip, port);
    auto uriType = ControllerConfig::GetInstance()->IsMultiNodeMode() ?
        ServerURI::GET_STATUS_V2 : ServerURI::GET_STATUS_V1;
    std::string uri = ControllerConstant::GetInstance()->GetServerURI(uriType);
    Request req = { uri, boost::beast::http::verb::get, map, jsonString };
    int32_t ret = client.SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
        ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
    LOG_D("[ServerRequestHandler] Getting node status, IP %s, ret code %d, request ret %d, response %s.",
          nodeInfo.ip.c_str(), code, ret, response.c_str());
    if (ret == 0 && code == CODE_OK && IsValidNodeStatusResp(response)) {
        return ParseNodeStatusRespToNodeStatus(nodeStatus, nodeInfo, response, initStaticTotalInfo, isReady);
    } else {
        LOG_E("[%s] [ServerRequestHandler] Getting node status, send request failed, "
              "node ID %lu, IP %s, port %s, ret code %d, request ret %d.",
              GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
              nodeInfo.instanceInfo.staticInfo.id, nodeInfo.ip.c_str(), port.c_str(), code, ret);
        AddServerEventToAlarmMgr(ret, nodeInfo);
        nodeStatus.UpdateRoleState(nodeInfo.instanceInfo.staticInfo.id,
                                   ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN), false,
                                   nodeInfo.isInitialized);
        return -1;
    }
    return 0;
}

int32_t ServerRequestHandler::TerminateService(HttpClient &client, NodeInfo &nodeInfo)
{
    auto port = nodeInfo.mgmtPort;
    std::string response;
    int32_t code = 400;
    std::string jsonBody;
    std::map <boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    client.SetHostAndPort(nodeInfo.ip, port);
    // Due to the request is GET, so we should not set the body instead of using the http params.
    // the uri's params is "mode=Force" mean that we force to stop the service.
    // is param is empty, it means that we stop the service gracefully.
    LOG_I("[ServerRequestHandler] Terminating instance, role %c, IP %s, port %s.",
          nodeInfo.instanceInfo.staticInfo.role, nodeInfo.ip.c_str(), port.c_str());
    std::string uri = ControllerConstant::GetInstance()->GetServerURI(ServerURI::STOP_SERVICE);
    uri += "?mode=Force";
    Request req = {uri, boost::beast::http::verb::get, map, jsonBody};
    int32_t ret = client.SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
        ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
    LOG_I("[ServerRequestHandler] Get terminate request result, IP %s, port %s, ret code %d, request ret %d.",
          nodeInfo.ip.c_str(), port.c_str(), code, ret);
    if (ret != 0 || code != CODE_OK) {
        LOG_E("[%s] [ServerRequestHandler] Terminate node, send request failed, "
              "node ID %lu, IP %s, port %s, ret code %d, request ret %d.",
              GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
              nodeInfo.instanceInfo.staticInfo.id, nodeInfo.ip.c_str(), port.c_str(), code, ret);
        return -1;
    }
    LOG_I("[ServerRequestHandler] Terminate service succeed!");
    return 0;
}

std::string ServerRequestHandler::BuildPostRoleV2ReqBody(NodeStatus &nodeStatus, const NodeInfo &node) const
{
    nlohmann::json body;
    nlohmann::json local;
    body["local"] = nlohmann::json::array();
    for (auto &serverInfo : node.serverInfoList) {
        nlohmann::json localJson = serverInfo;
        body["local"].emplace_back(localJson);
    }

    body["peers"] = nlohmann::json::array();
    for (auto &id : node.peers) {
        auto nodePtr = nodeStatus.GetNode(id);
        if (nodePtr == nullptr || nodePtr->deleteTime > std::chrono::seconds(0)) {
            LOG_W("[%s] [ServerRequestHandler] BuildPostRoleV2ReqBody: ignore node %lu",
                GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                id);
            continue;
        }
        nlohmann::json serverJson = nlohmann::json::array();
        for (auto &serverInfo : nodePtr->serverInfoList) {
            nlohmann::json peerJson = serverInfo;
            serverJson.emplace_back(peerJson);
        }
        body["peers"].emplace_back(serverJson);
    }
    return body.dump();
}


std::string ServerRequestHandler::BuildPostRoleV2ReqBodyByVec(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
    const NodeInfo &node) const
{
    nlohmann::json body;
    nlohmann::json local;
    body["local"] = nlohmann::json::array();
    for (auto &serverInfo : node.serverInfoList) {
        nlohmann::json localJson = serverInfo;
        body["local"].emplace_back(localJson);
    }

    body["peers"] = nlohmann::json::array();
    for (auto &id : node.peers) {
        auto it = std::find_if(nodeVec.begin(), nodeVec.end(), [&id](const std::unique_ptr<NodeInfo> &obj) {
            return (obj != nullptr) && (obj->instanceInfo.staticInfo.id == id);
        });
        if (it == nodeVec.end()) {
            LOG_W("[%s] [ServerRequestHandler] BuildPostRoleV2ReqBodyByVec: node id: %lu is not find in server list",
                  GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                  id);
            continue;
        }
        uint32_t severIndex = static_cast<uint32_t>(std::distance(nodeVec.begin(), it));
        auto &nodePtr = nodeVec[severIndex];
        nlohmann::json serverJson = nlohmann::json::array();
        for (auto &serverInfo : nodePtr->serverInfoList) {
            nlohmann::json peerJson = serverInfo;
            serverJson.emplace_back(peerJson);
        }
        body["peers"].emplace_back(serverJson);
    }
    return body.dump();
}

std::string BuildPostRoleReqBodyJson(NodeStatus &nodeStatus, const NodeInfo &node,
    nlohmann::json &body)
{
    for (auto &id : node.peers) {
        auto nodePtr = nodeStatus.GetNode(id);
        if (nodePtr == nullptr || nodePtr->deleteTime > std::chrono::seconds(0)) {
            LOG_W("[%s] [ServerRequestHandler] Building role request body, ignore node %lu.",
                GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                id);
            continue;
        }
        nlohmann::json pInfo;
        pInfo["device"] = nlohmann::json::array();
        pInfo["server_ip"] = nodePtr->ip;
        pInfo["id"] = id;
        pInfo["host_ip"] = nodePtr->hostId;
        if (nodePtr->serverInfoList.empty()) {
            LOG_W("[%s] [ServerRequestHandler] BuildPostRoleReqBody: Peer %lu has no server with devices.",
                GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(), id);
            continue;
        }
        if (nodePtr->serverInfoList[0].superPodId.has_value()) {
            pInfo["super_pod_id"] = node.serverInfoList[0].superPodId.value();
        }
        for (auto &device : nodePtr->serverInfoList[0].deviceInfos) {
            nlohmann::json deviceJson = device;
            pInfo["device"].emplace_back(deviceJson);
        }
        body["peers"].emplace_back(pInfo);
    }
    return body.dump();
}

std::string ServerRequestHandler::BuildPostRoleReqBody(NodeStatus &nodeStatus, const NodeInfo &node) const
{
    nlohmann::json body;
    nlohmann::json local;
    local["device"] = nlohmann::json::array();
    local["server_ip"] = node.ip;
    local["id"] = node.instanceInfo.staticInfo.id;
    if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
        local["p_percentage"] = node.instanceInfo.staticInfo.flexPRatio;
    }
    local["instance_idx_in_pod"] = node.instanceIdxInPod;
    local["num_instances_per_pod"] = node.numInstancesPerPod;
    local["is_single_container"] = ControllerConfig::GetInstance()->GetDIGSIsSingleContainer();
    local["host_ip"] = node.hostId;
    if (node.serverInfoList.empty()) {
        LOG_W("[%s] [ServerRequestHandler] BuildPostRoleReqBody: node %lu has no server with devices.",
              GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
              node.instanceInfo.staticInfo.id);
        return "";
    }
    if (node.serverInfoList[0].superPodId.has_value()) {
        local["super_pod_id"] = node.serverInfoList[0].superPodId.value();
    }
    for (auto &device : node.serverInfoList[0].deviceInfos) {
        nlohmann::json deviceJson = device;
        local["device"].emplace_back(deviceJson);
    }
    body["local"] = local;
    body["peers"] = nlohmann::json::array();

    return BuildPostRoleReqBodyJson(nodeStatus, node, body);
}

std::string ServerRequestHandler::BuildPostRoleReqBodyByVec(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
    const NodeInfo &node) const
{
    nlohmann::json body;
    nlohmann::json local;
    local["device"] = nlohmann::json::array();
    local["server_ip"] = node.ip;
    local["id"] = node.instanceInfo.staticInfo.id;
    if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
        local["p_percentage"] = node.instanceInfo.staticInfo.flexPRatio;
    }
    local["host_ip"] = node.hostId;
    local["instance_idx_in_pod"] = node.instanceIdxInPod;
    local["num_instances_per_pod"] = node.numInstancesPerPod;
    local["is_single_container"] = ControllerConfig::GetInstance()->GetDIGSIsSingleContainer();
    if (node.serverInfoList.empty()) {
        LOG_W("[%s] [ServerRequestHandler] BuildPostRoleReqBodyByVec: node %lu has no server with devices.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            node.instanceInfo.staticInfo.id);
        return "";
    }
    if (node.serverInfoList[0].superPodId.has_value()) {
        local["super_pod_id"] = node.serverInfoList[0].superPodId.value();
    }
    for (auto &device : node.serverInfoList[0].deviceInfos) {
        nlohmann::json deviceJson = device;
        local["device"].emplace_back(deviceJson);
    }
    body["local"] = local;
    body["peers"] = nlohmann::json::array();
    for (auto &id : node.peers) {
        auto it = std::find_if(nodeVec.begin(), nodeVec.end(), [&id](const std::unique_ptr<NodeInfo> &obj) {
            return (obj != nullptr) && (obj->instanceInfo.staticInfo.id == id);
        });
        if (it == nodeVec.end()) {
            LOG_W("[%s] [ServerRequestHandler] Building role request body by vector, "
                "node ID %lu is not find in server list.",
                GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                id);
            continue;
        }
        uint32_t serverIndex = static_cast<uint32_t>(std::distance(nodeVec.begin(), it));
        auto &nodePtr = nodeVec[serverIndex];
        nlohmann::json pInfo;
        pInfo["device"] = nlohmann::json::array();
        pInfo["server_ip"] = nodePtr->ip;
        pInfo["id"] = id;
        pInfo["host_ip"] = nodePtr->hostId;
        if (nodePtr->serverInfoList.empty()) {
            LOG_W("[%s] [ServerRequestHandler] BuildPostRoleReqBodyByVec: Peer %lu has no server with devices.",
                GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(), id);
            continue;
        }
        if (nodePtr->serverInfoList[0].superPodId.has_value()) {
            pInfo["super_pod_id"] = node.serverInfoList[0].superPodId.value();
        }
        for (auto &device : nodePtr->serverInfoList[0].deviceInfos) {
            nlohmann::json deviceJson = device;
            pInfo["device"].emplace_back(deviceJson);
        }
        body["peers"].emplace_back(pInfo);
    }
    return body.dump();
}

void SetSpSizeForNodes(NodeInfo &node)
{
    if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        for (auto &serverInfo : node.serverInfoList) {
            serverInfo.spSize = ControllerConfig::GetInstance()->GetPSpSize();
        }
        LOG_D("[NodeScheduler] Set prefill node %s to %u and its server's info gets %u.",
            node.hostId.c_str(), ControllerConfig::GetInstance()->GetPSpSize(), node.serverInfoList[0].spSize);
    } else if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        for (auto &serverInfo : node.serverInfoList) {
            serverInfo.spSize = ControllerConfig::GetInstance()->GetDSpSize();
        }
        LOG_D("[NodeScheduler] Set decode node %s to %u and its server's info gets %u.",
            node.hostId.c_str(), ControllerConfig::GetInstance()->GetDSpSize(), node.serverInfoList[0].spSize);
    }
}

void SetCpSizeForNodes(NodeInfo &node)
{
    if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        for (auto &serverInfo : node.serverInfoList) {
            serverInfo.cpSize = ControllerConfig::GetInstance()->GetPCpSize();
        }
        LOG_D("[NodeScheduler] Set prefill node %s cp_size to %u and its server's info gets %u.",
            node.hostId.c_str(), ControllerConfig::GetInstance()->GetPCpSize(), node.serverInfoList[0].cpSize);
    } else if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        for (auto &serverInfo : node.serverInfoList) {
            serverInfo.cpSize = ControllerConfig::GetInstance()->GetDCpSize();
        }
        LOG_D("[NodeScheduler] Set decode node %s cp_size to %u and its server's info gets %u.",
            node.hostId.c_str(), ControllerConfig::GetInstance()->GetDCpSize(), node.serverInfoList[0].cpSize);
    }
}

bool ServerRequestHandler::GetUrlAndBody(std::vector<std::unique_ptr<NodeInfo>> &nodeVec, NodeInfo &node,
    std::string &body, std::string &url) const
{
    if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        if (ControllerConfig::GetInstance()->IsMultiNodeMode()) {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_PREFILL_ROLE_V2);
            body = BuildPostRoleV2ReqBodyByVec(nodeVec, node);
        } else {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_PREFILL_ROLE_V1);
            body = BuildPostRoleReqBodyByVec(nodeVec, node);
        }
    } else if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        if (ControllerConfig::GetInstance()->IsMultiNodeMode()) {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_DECODE_ROLE_V2);
            body = BuildPostRoleV2ReqBodyByVec(nodeVec, node);
        } else {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_DECODE_ROLE_V1);
            body = BuildPostRoleReqBodyByVec(nodeVec, node);
        }
    } else if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
        if (ControllerConfig::GetInstance()->IsMultiNodeMode()) {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_FLEX_ROLE_V2);
            body = BuildPostRoleV2ReqBodyByVec(nodeVec, node);
        } else {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_FLEX_ROLE_V1);
            body = BuildPostRoleReqBodyByVec(nodeVec, node);
        }
    } else {
        LOG_E("[%s] [ServerRequestHandler] Node ID %lu, role %c is invalid.",
            GetErrorCode(ErrorType::UNAVAILABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            node.instanceInfo.staticInfo.id, node.instanceInfo.staticInfo.role);
        return false;
    }
    LOG_D("[ms] [ServerRequestHandler] GlobalInfo, body as: %s", body.c_str());
    return true;
}

int32_t ServerRequestHandler::PostSingleRoleByVec(HttpClient &client, std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
    NodeInfo &node) const
{
    auto port = node.mgmtPort;
    int32_t code = 400;
    std::map <boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    std::string body;
    std::string url;
    std::string response;

    if (!GetUrlAndBody(nodeVec, node, body, url)) {
        return -1;
    }

    LOG_M("[Update] Posting single role, IP %s, URL %s, body %s.", node.ip.c_str(), url.c_str(), body.c_str());
    client.SetHostAndPort(node.ip, node.mgmtPort);
    Request req = {url, boost::beast::http::verb::post, map, body};
    int32_t httpRet = client.SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
        ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
    if (httpRet != 0 || code != CODE_OK) {
        LOG_E("[%s] [ServerRequestHandler] Send request failed during posting single role, "
              "node ID %lu, IP %s, port %s, ret code %d request ret %d",
              GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
              node.instanceInfo.staticInfo.id, node.ip.c_str(), node.mgmtPort.c_str(), code, httpRet);
        node.roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN);
        node.isHealthy = false;
        node.isInitialized = false;
        return -1;
    } else {
        node.roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::SWITCHING);
        node.isHealthy = true;
        node.isInitialized = true;
    }
    return 0;
}

int32_t ServerRequestHandler::PostSingleRoleById(HttpClient &client, NodeStatus &nodeStatus, uint64_t id) const
{
    auto node = nodeStatus.GetNode(id);
    if (node == nullptr) {
        LOG_E("[%s] [ServerRequestHandler] Posting single role by ID, get node %lu failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            id);
        return -1;
    }
    if (node->deleteTime > std::chrono::seconds(0)) {
        LOG_E("[%s] [ServerRequestHandler] Posting single role by ID, ignore deleted node %lu.",
            GetErrorCode(ErrorType::UNAVAILABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            id);
        return 0;
    }
    return PostSingleRole(client, nodeStatus, *node);
}

int32_t ServerRequestHandler::LoopPostPDRole(HttpClient &client, NodeStatus &nodeStatus, NodeInfo &node,
                                             std::string& url, std::string& body) const
{
    std::map <boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    std::string response;
    int32_t code = 400;
    Request req = {url, boost::beast::http::verb::post, map, body};
    // when instance role is switching, need to send assign request repeatedly until it is ready
    int64_t initRanktableChangeTime = nodeStatus.GetRanktableChangeTime();
    size_t retryTimes = 1440;
    while (retryTimes-- > 0) {
        int32_t httpRet = client.SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
                                             ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
        if (httpRet != 0 || code != CODE_OK || !CheckJsonStringSize(response)) {
            LOG_E("[%s] [ServerRequestHandler] Send request failed, node ID %lu, IP %s, port %s, ret code %d, "
                  "request ret %d, response %s",
                  GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                  node.instanceInfo.staticInfo.id, node.ip.c_str(), node.mgmtPort.c_str(), code, httpRet,
                  response.substr(0, JSON_STR_SIZE_HEAD).c_str());
            nodeStatus.UpdateRoleState(node.instanceInfo.staticInfo.id,
                ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN), false, false);
            return -1;
        }
        if (!nlohmann::json::accept(response)) {
            LOG_E("[%s] [ServerRequestHandler] Send request failed, node ID %lu, IP %s, port %s, ret code %d, "
                  "request ret %d, response %s",
                  GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                  node.instanceInfo.staticInfo.id, node.ip.c_str(), node.mgmtPort.c_str(), code, httpRet,
                  response.c_str());
            nodeStatus.UpdateRoleState(node.instanceInfo.staticInfo.id,
                ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN), false, false);
            return -1;
        }
        LOG_D("[ServerRequestHandler] Send request successfully, node ID %lu, IP %s, port %s, response %s.",
              node.instanceInfo.staticInfo.id, node.ip.c_str(), node.mgmtPort.c_str(), response.c_str());
        auto responseJson = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        // when return message is ok, break the loop
        try {
            if (responseJson.at("result").get<std::string>() == "ok") {
                nodeStatus.UpdateRoleState(node.instanceInfo.staticInfo.id,
                    ControllerConstant::GetInstance()->GetRoleState(RoleState::SWITCHING), true, true);
                return 0;
            }
        } catch (const nlohmann::json::exception& e) {
            LOG_E("JSON parsing error: %s", e.what());
            return -1;
        } catch (const std::exception& e) {
            LOG_E("Exception: %s", e.what());
            return -1;
        }
        int sendWaitTime = 5;
        std::this_thread::sleep_for(std::chrono::seconds(sendWaitTime));
        
        // Exit early if the node ranktable has changed during iteration
        int64_t curRanktableChangeTime = nodeStatus.GetRanktableChangeTime();
        if (curRanktableChangeTime != initRanktableChangeTime && initRanktableChangeTime != -1) {
            LOG_I("[ServerRequestHandler] LoopPostPDRole exited early: global ranktable changed during iteration.");
            return -1;
        }
    }
    LOG_E("[%s] [ServerRequestHandler] Maximum retry times reached while posting PD role, fail to post PD role",
        GetErrorCode(ErrorType::UNAVAILABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str());
    return -1;
}

int32_t ServerRequestHandler::PostSingleRole(HttpClient &client, NodeStatus &nodeStatus,
    NodeInfo &node) const
{
    std::string body;
    std::string url;
    SetSpSizeForNodes(node);
    SetCpSizeForNodes(node);
    if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
        if (ControllerConfig::GetInstance()->IsMultiNodeMode()) {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_PREFILL_ROLE_V2);
            body = BuildPostRoleV2ReqBody(nodeStatus, node);
        } else {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_PREFILL_ROLE_V1);
            body = BuildPostRoleReqBody(nodeStatus, node);
        }
    } else if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        if (ControllerConfig::GetInstance()->IsMultiNodeMode()) {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_DECODE_ROLE_V2);
            body = BuildPostRoleV2ReqBody(nodeStatus, node);
        } else {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_DECODE_ROLE_V1);
            body = BuildPostRoleReqBody(nodeStatus, node);
        }
    } else if (node.instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
        if (ControllerConfig::GetInstance()->IsMultiNodeMode()) {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_FLEX_ROLE_V2);
            body = BuildPostRoleV2ReqBody(nodeStatus, node);
        } else {
            url = ControllerConstant::GetInstance()->GetServerURI(ServerURI::POST_FLEX_ROLE_V1);
            body = BuildPostRoleReqBody(nodeStatus, node);
        }
    } else {
        LOG_E("[%s] [ServerRequestHandler] Posting single role with node ID %lu, role %c is invalid.",
            GetErrorCode(ErrorType::UNAVAILABLE, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            node.instanceInfo.staticInfo.id, node.instanceInfo.staticInfo.role);
        return -1;
    }
    LOG_M("[Update] Posting single role: IP %s, URL %s, body %s.", node.ip.c_str(), url.c_str(), body.c_str());
    client.SetHostAndPort(node.ip, node.mgmtPort);
    return LoopPostPDRole(client, nodeStatus, node, url, body);
}

void ServerRequestHandler::BatchPostRole(HttpClient &client, NodeStatus &nodeStatus,
    const std::vector<uint64_t> &nodes, std::vector<uint64_t> &success) const
{
    success.clear();
    for (auto &id : nodes) {
        if (PostSingleRoleById(client, nodeStatus, id) == 0) {
            success.push_back(id);
        }
    }
}

static void AddInstanceIfNotExist(std::vector<uint64_t> &ret, uint64_t id)
{
    if (std::find(ret.begin(), ret.end(), id) == ret.end()) {
        ret.push_back(id);
    }
}

std::vector<uint64_t> ServerRequestHandler::CheckStatusByVec(HttpClient &client,
    std::vector<std::unique_ptr<NodeInfo>> &nodeVec, const std::vector<uint64_t> &nodeIds,
    bool initStaticTotalInfo)
{
    if (nodeIds.empty()) {
        return {};
    }
    auto waitSeconds = ControllerConfig::GetInstance()->GetCheckRoleWaitSeconds();
    auto limit = ControllerConfig::GetInstance()->GetCheckRoleAttemptTimes();
    std::vector<uint64_t> ret;
    uint32_t unknown;
    bool isReady = false;
    for (uint32_t attempt = 0; attempt < limit; ++attempt) {
        unknown = 0;
        for (auto &id : nodeIds) {
            auto it = std::find_if(nodeVec.begin(), nodeVec.end(), [&id](const std::unique_ptr<NodeInfo> &obj) {
                return (obj != nullptr) && (obj->instanceInfo.staticInfo.id == id);
            });
            if (it == nodeVec.end()) {
                continue;
            }
            uint32_t serverIndex = static_cast<uint32_t>(std::distance(nodeVec.begin(), it));
            auto &node = nodeVec[serverIndex];
            if (node->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::READY)) {
                AddInstanceIfNotExist(ret, id);
                continue;
            }
            if (UpdateNodeInfo(client, *node, initStaticTotalInfo, isReady) != 0) {
                LOG_E("[%s] [ServerRequestHandler] Checking status by vector, send request to node ID %lu,"
                      "IP %s failed in time %u.",
                      GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                      node->instanceInfo.staticInfo.id, node->ip.c_str(), attempt);
                continue;
            }
            if (isReady) {
                AddInstanceIfNotExist(ret, id);
                // Node is ready, delete it from role unknown list.
                mRoleUnknownDeleteCallback(id, node->instanceInfo.staticInfo.role);
                continue;
            }
            if (node->roleState == ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN)) {
                unknown++;
            }
        }
        if (unknown + ret.size() == nodeIds.size()) {
            LOG_I("[ServerRequestHandler] Checking status by vector: %u unknown nodes, %zu ready nodes,"
                "exit in epoch %u", unknown, ret.size(), attempt);
            break;
        }
        LOG_W("[%s] [ServerRequestHandler] Checking status by vector: total number of unready node is %zu in epoch %u.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            nodeIds.size() - ret.size(), attempt);
        std::this_thread::sleep_for(std::chrono::seconds(waitSeconds)); // 有节点失败则休眠
    }
    return ret;
}

static void UpdateIsInitializingStaticTotalInfo(NodeStatus &nodeStatus,
    const std::vector<uint64_t> &nodeIds, bool initStaticTotalInfo, InferenceType type)
{
    if (!initStaticTotalInfo) {
        return;
    }
    for (auto &id : std::as_const(nodeIds)) {
        nodeStatus.UpdateInferenceType(id, type);
    }
}

std::vector<uint64_t> ServerRequestHandler::CheckStatus(HttpClient &client, NodeStatus &nodeStatus,
    const std::vector<uint64_t> &nodeIds, bool initStaticTotalInfo)
{
    if (nodeIds.empty()) {
        return {};
    }
    auto waitSeconds = ControllerConfig::GetInstance()->GetCheckRoleWaitSeconds();
    auto limit = ControllerConfig::GetInstance()->GetCheckRoleAttemptTimes();
    std::vector<uint64_t> ret;
    uint32_t unknown;
    bool isReady = false;
    UpdateIsInitializingStaticTotalInfo(nodeStatus, nodeIds, initStaticTotalInfo,
        InferenceType::INITIALIZING_STATIC_TOTAL_INFO);
    for (uint32_t attempt = 0; attempt < limit; ++attempt) {
        unknown = 0;
        for (auto &id : std::as_const(nodeIds)) {
            auto node = nodeStatus.GetNode(id);
            if (node == nullptr) {
                continue;
            }
            if (UpdateNodeStatus(client, nodeStatus, *node, initStaticTotalInfo, isReady) != 0) {
                LOG_E("[%s] [ServerRequestHandler] CheckStatus: send request to node id %lu, ip %s failed in time %u",
                      GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
                      node->instanceInfo.staticInfo.id, node->ip.c_str(), attempt);
                continue;
            }
            if (isReady) {
                AddInstanceIfNotExist(ret, id);
                continue;
            }
            if (nodeStatus.GetRoleState(id) == ControllerConstant::GetInstance()->GetRoleState(RoleState::UNKNOWN)) {
                unknown++;
            }
        }
        if (unknown + ret.size() == nodeIds.size()) {
            LOG_I("[ServerRequestHandler] Checking status, %u unknown nodes, %zu ready nodes, exit in epoch %u",
                  unknown, ret.size(), attempt);
            break;
        }
        LOG_W("[%s] [ServerRequestHandler] Checking status, total number of unready node is %zu in epoch %u.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::SERVER_REQUEST_HANDLER).c_str(),
            nodeIds.size() - ret.size(), attempt);
        std::this_thread::sleep_for(std::chrono::seconds(waitSeconds)); // 有节点失败则休眠
    }
    UpdateIsInitializingStaticTotalInfo(nodeStatus, nodeIds, initStaticTotalInfo, InferenceType::AVAILABLE);
    return ret;
}
}