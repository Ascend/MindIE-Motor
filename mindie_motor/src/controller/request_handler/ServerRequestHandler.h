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
#ifndef MINDIE_MS_CONTROLLER_SERVER_REQUEST_HANDLER_H
#define MINDIE_MS_CONTROLLER_SERVER_REQUEST_HANDLER_H

#include <cstdint>
#include <memory>
#include <set>
#include "NodeStatus.h"
#include "HttpClient.h"
#include "Logger.h"
namespace MINDIE::MS {
using RoleUnknownCallback = std::function<void(uint64_t, MINDIE::MS::DIGSInstanceRole)>;

/// The class that sends HTTP requests to the server.
///
/// This class sends HTTP requests to the server. It queries static configuration and dynamic status and
/// specifies the server's role.
class ServerRequestHandler {
public:

    /// Get the instance of the ServerRequestHandler class.
    ///
    /// \return The reference to the object.
    static ServerRequestHandler *GetInstance()
    {
        static ServerRequestHandler instance;
        return &instance;
    }

    /// Initialize the ServerRequestHandler object.
    ///
    /// \param roleUnknownCallback The callback function of the RoleUnknown server.
    /// \param roleUnknownDeleteCallback The callback function to delete the RoleUnknown server.
    /// \return The result of the initialization. 0 indicates success. -1 indicates failure.
    int32_t Init(RoleUnknownCallback roleUnknownCallback, RoleUnknownCallback roleUnknownDeleteCallback)
    {
        mRoleUnknownCallback = roleUnknownCallback;
        mRoleUnknownDeleteCallback = roleUnknownDeleteCallback;
        return 0;
    }

    /// Filter available nodes and faulty nodes when detect server node for the first time.
    ///
    /// \param client The HTTP client.
    /// \param allNodes All server nodes.
    /// \param availableNodes List that records successfully detected nodes.
    /// \param faultyNodes List that records unusable nodes.
    /// \param retryTimes The number of retry times.
    /// \return The result of detection. 0 indicates success. -1 indicates failure.
    int32_t GetAvailableNodes(HttpClient &client,
        std::vector<std::unique_ptr<NodeInfo>> &allNodes,
        std::vector<std::unique_ptr<NodeInfo>> &availableNodes,
        std::vector<std::unique_ptr<NodeInfo>> &faultyNodes,
        std::shared_ptr<NodeStatus> nodeStatus,
        uint32_t retryTimes = 0);

    /// Determine whether the role of the server needs to be specified.
    ///
    /// \param nodeInfo The information of the server.
    /// \return Boolean. False indicates that the role of the server doesn't need to be specified. True indicates need.
    bool IsPostRoleNeeded(const NodeInfo &nodeInfo) const;

    /// Query the static configuration of the server.
    ///
    /// \param client The HTTP client.
    /// \param nodeInfo The information of the server.
    /// \return The query result. 0 indicates success. -1 indicates failure.
    int32_t QueryInstanceInfo(HttpClient &client, NodeInfo &node) const;

    /// Query the dynamic status of the server.
    ///
    /// Update results to the nodeStatus.
    /// \param client The HTTP client.
    /// \param nodeStatus The shared pointer of nodeStatus which is globally managed in the controller.
    /// \param nodeInfo The information of the server.
    /// \param initStaticTotalInfo Whether to initialize the total resources.
    /// \param isReady Whether the server is RoleReady.
    /// \return The query result. 0 indicates success. -1 indicates failure.
    int32_t UpdateNodeStatus(HttpClient &client, NodeStatus &nodeStatus, NodeInfo &nodeInfo,
        bool initStaticTotalInfo, bool &isReady);

    void AddServerEventToAlarmMgr(int32_t sendRes, const NodeInfo &nodeInfo);

    /// Terminate this node's service.
    ///
    /// \param client The HTTP client.
    /// \param nodeInfo The information of the server.
    /// \return The request result. 0 indicates success. -1 indicates failure.
    int32_t TerminateService(HttpClient &client, NodeInfo &nodeInfo);

    /// Query the dynamic status of the server.
    ///
    /// Update results to the nodeInfo.
    /// \param client The HTTP client.
    /// \param nodeInfo The information of the server.
    /// \param initStaticTotalInfo Whether to initialize the total resources.
    /// \param isReady Whether the server is RoleReady.
    /// \return The query result. 0 indicates success. -1 indicates failure.
    int32_t UpdateNodeInfo(HttpClient &client, NodeInfo &nodeInfo, bool initStaticTotalInfo, bool &isReady);

    /// Specify the role of the server.
    ///
    /// Construct the request body based on the nodeStatus.
    /// \param client The HTTP client.
    /// \param nodeStatus The shared pointer of nodeStatus which is globally managed in the controller.
    /// \param id The ID of the server.
    /// \return The result of specifying the role. 0 indicates success. -1 indicates failure.
    int32_t PostSingleRoleById(HttpClient &client, NodeStatus &nodeStatus, uint64_t id) const;

    /// Specify the role of the server.
    ///
    /// Construct the request body based on the vector of servers.
    /// \param client The HTTP client.
    /// \param nodeVec The vector of servers.
    /// \param node The information of the server.
    /// \return The result of specifying the role. 0 indicates success. -1 indicates failure.
    int32_t PostSingleRoleByVec(HttpClient &client, std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
        NodeInfo &node) const;

    /// Specify the role of the server in batches.
    ///
    /// Construct the request body based on the nodeStatus.
    /// \param client The HTTP client.
    /// \param nodeStatus The shared pointer of nodeStatus which is globally managed in the controller.
    /// \param nodes The vector of servers' ID.
    /// \param success The vector of the servers' ID that successfully specifies the role.
    void BatchPostRole(HttpClient &client, NodeStatus &nodeStatus, const std::vector<uint64_t> &nodes,
        std::vector<uint64_t> &success) const;

    /// Query the dynamic status of the server in batches.
    ///
    /// Check the dynamic status of the servers and update the result to the nodeStatus.
    /// \param client The HTTP client.
    /// \param nodeStatus The shared pointer of nodeStatus which is globally managed in the controller.
    /// \param nodeIds The vector of servers' ID.
    /// \param initStaticTotalInfo Whether to initialize the total resources.
    /// \return The vector of the ID of the RoleReady servers.
    std::vector<uint64_t> CheckStatus(HttpClient &client, NodeStatus &nodeStatus,
        const std::vector<uint64_t> &nodeIds, bool initStaticTotalInfo = false, uint32_t maxAttemptsOverride = 0);

    /// Query the dynamic status of the server in batches.
    ///
    /// Check the dynamic status of the servers and update the result to the vector.
    /// \param client The HTTP client.
    /// \param nodeVec The vector of servers.
    /// \param nodeIds The vector of servers' ID.
    /// \param initStaticTotalInfo Whether to initialize the total resources.
    /// \return The vector of the ID of the RoleReady servers.
    std::vector<uint64_t> CheckStatusByVec(HttpClient &client, std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
        const std::vector<uint64_t> &nodeIds, bool initStaticTotalInfo = false);

    /// Whether the server needs to be updated from P role to D role.
    ///
    /// \param nodeInfo The information of the server.
    /// \return Boolean. False means no update is required. True means an update is required.
    bool IsUpdatePToDNeeded(const NodeInfo &node) const;

    /// Whether the server needs to be updated from D role to P role.
    ///
    /// \param nodeInfo The information of the server.
    /// \return Boolean. False means no update is required. True means an update is required.
    bool IsUpdateDToPNeeded(const NodeInfo &node) const;

    /// Whether the server needs to be updated.
    ///
    /// \param nodeInfo The information of the server.
    /// \return Boolean. False means no update is required. True means an update is required.
    bool IsUpdateRoleNeeded(const NodeInfo &node) const;

    ServerRequestHandler(const ServerRequestHandler &obj) = delete;
    ServerRequestHandler &operator=(const ServerRequestHandler &obj) = delete;
    ServerRequestHandler(ServerRequestHandler &&obj) = delete;
    ServerRequestHandler &operator=(ServerRequestHandler &&obj) = delete;
private:
    ServerRequestHandler() = default;
    ~ServerRequestHandler() = default;
    int32_t PostSingleRole(HttpClient &client, NodeStatus &nodeStatus, NodeInfo &node) const;
    int32_t LoopPostPDRole(HttpClient &client, NodeStatus &nodeStatus, NodeInfo &node, std::string& url,
                           std::string& body) const;
    int32_t ParseNodeStatusRespToNodeStatus(NodeStatus &nodeStatus, NodeInfo &nodeInfo,
        const std::string &response, bool initStaticTotalInfo, bool &isReady);
    int32_t ParseNodeStatusV2RespToNodeStatus(NodeStatus &nodeStatus, const NodeInfo &nodeInfo,
        const std::string &response, bool initStaticTotalInfo, bool &isReady);

    int32_t ParseNodeStatusResp(NodeInfo &nodeInfo, const std::string &response, bool initStaticTotalInfo,
        bool &isReady);
    std::string BuildPostRoleV2ReqBody(NodeStatus &nodeStatus, const NodeInfo &node) const;
    nlohmann::json BuildLocalNodeInfo(const NodeInfo &node) const;
    nlohmann::json BuildPeerNodeInfos(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
        const NodeInfo &node) const;
    std::string BuildPostRoleV2ReqBodyByVec(std::vector<std::unique_ptr<NodeInfo>> &nodeVec,
        const NodeInfo &node) const;
    bool GetUrlAndBody(std::vector<std::unique_ptr<NodeInfo>> &nodeVec, NodeInfo &node,
        std::string &body, std::string &url) const;
    std::string BuildPostRoleReqBody(NodeStatus &nodeStatus, const NodeInfo &node) const;
    std::string BuildPostRoleReqBodyByVec(std::vector<std::unique_ptr<NodeInfo>> &nodeVec, const NodeInfo &node) const;
    void CheckGroupNode(std::vector<std::unique_ptr<NodeInfo>> &allNodes,
        std::set<uint64_t>& finished, std::vector<std::unique_ptr<NodeInfo>> &availableNodes,
        std::vector<std::unique_ptr<NodeInfo>> &faultyNodes) const;
    bool CheckGroupNodeAvailable(const std::set<uint64_t>& finished, const NodeInfo& node) const;
    void SetDynamicInfo(NodeInfo &nodeInfo, nlohmann::json &bodyJson);

    RoleUnknownCallback mRoleUnknownCallback = nullptr;  /// The callback function of the RoleUnknown server.
    RoleUnknownCallback mRoleUnknownDeleteCallback = nullptr;
};
}

#endif // MINDIE_MS_CONTROLLER_SERVER_REQUEST_HANDLER_H
