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
#ifndef MINDIE_MS_STATUS_UPDATOR_H
#define MINDIE_MS_STATUS_UPDATOR_H

#include <atomic>
#include <mutex>
#include <memory>
#include <condition_variable>
#include <thread>
#include "NodeStatus.h"
#include "CoordinatorStore.h"
#include "ControllerConfig.h"
#include "HttpClient.h"
#include "digs_instance.h"
#include "HeartbeatProducer.h"
#include "node_manager_sender/NodeManagerRequestSender.h"

namespace MINDIE::MS {
class StatusUpdater {
public:
    StatusUpdater(const StatusUpdater &obj) = delete;
    StatusUpdater &operator=(const StatusUpdater &obj) = delete;
    StatusUpdater(StatusUpdater &&obj) = delete;
    StatusUpdater &operator=(StatusUpdater &&obj) = delete;
    StatusUpdater(std::shared_ptr<NodeStatus> nodeStatusInit,
                  std::shared_ptr<CoordinatorStore> coordinatorStoreInit);
    ~StatusUpdater();
    int32_t Init(DeployMode deployMode);
    void Stop();
private:
    std::shared_ptr<HttpClient> mServerClient = nullptr;
    std::shared_ptr<HttpClient> mCoordinatorClient = nullptr;
    std::shared_ptr<HttpClient> mNodeManagerClient = nullptr;
    std::shared_ptr<NodeManagerRequestSender> mNodeManagerSender = nullptr;
    std::shared_ptr<NodeStatus> mNodeStatus = nullptr;
    std::shared_ptr<CoordinatorStore> mCoordinatorStore = nullptr;
    std::vector<uint64_t> mNodeIds {};
    DeployMode mDeployMode = DeployMode::SINGLE_NODE;
    std::atomic<bool> mRun = { true };
    std::unique_ptr<std::thread> mMainThread = nullptr;
    std::unique_ptr<std::thread> mQueryNodeManagerThread = nullptr;
    std::unique_ptr<std::thread> mSendThread = nullptr;
    std::shared_ptr<HeartbeatProducer> mOmControllerHBProducer;
    void UpdateAllNodeStatus();
    void QueryNodeManagers();
    void SetPeers(nlohmann::json &dynamicInfo, const NodeInfo &nodeInfo);
    void SetStaticInfo(nlohmann::json &staticInfo, const NodeInfo &nodeInfo);
    void ParseNodeStatusResp(uint32_t id, std::string &response, bool initStaticTotalInfo);
    void SendNodeStatus() const;
    void Wait();
    std::string GenerateNodeStatusStr();
};
}
#endif // MINDIE_MS_STATUS_UPDATOR_H