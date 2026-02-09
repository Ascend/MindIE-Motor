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
#ifndef MINDIE_MS_CCAE_REQUEST_HANDLER_H
#define MINDIE_MS_CCAE_REQUEST_HANDLER_H

#include <atomic>
#include <cstdint>
#include <memory>
#include "HttpClient.h"
#include "Logger.h"
#include "CCAEStatus.h"
#include "CoordinatorStore.h"
#include "NodeStatus.h"
#include "RankTableLoader.h"

namespace MINDIE::MS {

void to_json(nlohmann::json& podInfo, const PodInfo& info);
void to_json(nlohmann::json& npuInfoList, const NPUInfo& npuList);

enum class ModelState {
    HEALTHY = 1,
    SUBHEALTHY,
    UNHEALTHY
};

/// The class that sends HTTP requests to the server.
///
/// This class sends HTTP requests to the server. It queries static configuration and dynamic status and
/// specifies the server's role.
class CCAERequestHandler {
public:
    /// Get the instance of the CCAERequestHandler class.
    /// \return The reference to the object.
    static CCAERequestHandler *GetInstance()
    {
        static CCAERequestHandler instance;
        return &instance;
    }

    int32_t SendRegister2UpdateStatus(HttpClient &client, CCAEStatus &ccaeStatus) const;
    std::string FillInventoryRequest(std::vector<std::string> modelIDs, std::shared_ptr<CCAEStatus> mCCAEStatus,
        std::shared_ptr<NodeStatus> mNodeStatus,
        std::string metricsInfo);
    int32_t SendInventories(HttpClient &client, const std::string &jsonString) const;
        nlohmann::json GetInstanceInfo(const std::string pdFlag, const std::string serverId);
    std::vector<nlohmann::json> GetInstances(std::string pdFlag);
    void BuildCentralizedInstances(nlohmann::json instanceInfo, std::string pdFlag, std::string serId,
        const NodeInfo &nodeInfo);
    void BuildDistributeInstance(nlohmann::json instanceInfo, std::string pdFlag, std::string serverId,
        const NodeInfo &nodeInfo);
    bool ParaPDInstance(std::shared_ptr<NodeStatus> nodeStatus);
    nlohmann::json GetInventoriesJson(bool isForcedUpdate, std::shared_ptr<NodeStatus> mNodeStatus);
    void GenerateServerIPToInstLogicIDMap(MINDIE::MS::DIGSInstanceRole role);
    bool ParsePDInstance(std::shared_ptr<NodeStatus> nodeStatus);
    void SetRankTableLoader(std::shared_ptr<RankTableLoader> loader);
    std::string GetLogicIDByServerIP(const std::string serverIP);
    std::shared_ptr<RankTableLoader> GetRankTableLoader() const;
    std::string GetPDInstIDForDPGroup(NodeInfo &nodeInfo, const ServerInfo &serverInfo);
    nlohmann::json GetDPGroupList(std::shared_ptr<NodeStatus> nodeStatus,
        const std::vector<std::shared_ptr<PodInfo>> serverPodList);
    void FillDPGroupInfo(nlohmann::json &dpGroupJson, NodeInfo &nodeInfo, const ServerInfo &serverInfo,
        const size_t dpGroupID, const std::vector<std::shared_ptr<PodInfo>> serverPodList);
    ModelState GetModelState() const;
    bool AreAllInstancesHealthy(MINDIE::MS::DIGSInstanceRole role) const;
    static void SetCoordinatorServiceReady(bool isReady);
    static void SetcoordinatorHealthy(bool isHealthy);

    CCAERequestHandler(const CCAERequestHandler &obj) = delete;
    CCAERequestHandler &operator=(const CCAERequestHandler &obj) = delete;
    CCAERequestHandler(CCAERequestHandler &&obj) = delete;
    CCAERequestHandler &operator=(CCAERequestHandler &&obj) = delete;
private:
    std::unordered_map<std::string, std::unordered_map<std::string, nlohmann::json>> mInstanceMap {};
    std::unordered_map<std::string, PodInfo> mPodMap {};
    std::unordered_map<std::string, std::string> mPodToInstanceMap {};
    std::unordered_map<std::string, std::string> mServerIPToInstLogicIDMap {};
    std::shared_ptr<RankTableLoader> mRankTableLoader = nullptr;
    inline static std::atomic<bool> coordinatorServiceReady{false};
    inline static std::atomic<bool> coordinatorHealthy{false};
    CCAERequestHandler() = default;
    ~CCAERequestHandler() = default;
};
}

#endif // MINDIE_MS_CCAE_REQUEST_HANDLER_H