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
#include "StatusUpdater.h"
#include <string>
#include <algorithm>
#include <iterator>
#include <unordered_set>
#include "nlohmann/json.hpp"
#include "Logger.h"
#include "ServerRequestHandler.h"
#include "CoordinatorRequestHandler.h"
#include "IPCConfig.h"
#include "HeartbeatProducer.h"

namespace MINDIE::MS {
constexpr int32_t CODE_OK = 200;
StatusUpdater::StatusUpdater(std::shared_ptr<NodeStatus> nodeStatusInit,
    std::shared_ptr<CoordinatorStore> coordinatorStoreInit) : mNodeStatus(nodeStatusInit),
    mCoordinatorStore(coordinatorStoreInit)
{
    LOG_I("[StatusUpdater] Create successfully.");
}

StatusUpdater::~StatusUpdater()
{
    Stop();
    LOG_I("[StatusUpdater] Destroy successfully.");
}

void StatusUpdater::Stop()
{
    mRun.store(false);
    if (mOmControllerHBProducer) {
        mOmControllerHBProducer->Stop();
        LOG_I("[StatusUpdater] MS Controller Heartbeat stopped successfully.");
    }
    if (mSendThread != nullptr && mSendThread->joinable()) {
        mSendThread->join();
        LOG_I("[StatusUpdater] Send thread stopped successfully.");
    }
    if (mMainThread != nullptr && mMainThread->joinable()) {
        mMainThread->join();
        LOG_I("[StatusUpdater] Stop successfully.");
    }
}

void StatusUpdater::Wait()
{
    auto waitSeconds = ControllerConfig::GetInstance()->GetClusterSynchronizationSeconds();
    while (waitSeconds > 0 && mRun.load()) {
        std::this_thread::sleep_for(std::chrono::seconds(1)); // 1s检查一次
        waitSeconds--;
    }
}

int32_t StatusUpdater::Init(DeployMode deployMode)
{
    try {
        mServerClient = std::make_shared<HttpClient>();
        mCoordinatorClient = std::make_shared<HttpClient>();
        if (mServerClient == nullptr || mCoordinatorClient == nullptr ||
            mServerClient->Init("", "", ControllerConfig::GetInstance()->GetRequestServerTlsItems()) != 0 ||
            mCoordinatorClient->Init("", "", ControllerConfig::GetInstance()->GetRequestCoordinatorTlsItems()) != 0) {
            LOG_E("[%s] [StatusUpdater] Initialize status updater failed because create server client or coordinator "
                "client failed.", GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::STATUS_UPDATER).c_str());
            return -1;
        }
        mMainThread = std::make_unique<std::thread>([this]() {
            while (mRun.load()) {
                LOG_D("[StatusUpdater] Start main thread.");
                if (ControllerConfig::GetInstance()->IsLeader()) {
                    UpdateAllNodeStatus();
                }
                LOG_D("[StatusUpdater] End main thread.");
                Wait();
            }
        });

        mSendThread = std::make_unique<std::thread>([this]() {
            while (mRun.load()) {
                LOG_D("[StatusUpdater] Start send thread.");
                if (ControllerConfig::GetInstance()->IsLeader()) {
                    SendNodeStatus();
                }
                LOG_D("[StatusUpdater] End send thread.");
                Wait();
            }
        });
    } catch (const std::exception& e) {
        LOG_E("[%s] [StatusUpdater] Failed to create main thread for status updater. Error: %s.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::STATUS_UPDATER).c_str(), e.what());
        return -1;
    }
    // Controller Heartbeat Initialize
    try {
        auto producerInterval = std::chrono::milliseconds(HEARTBEAT_PRODUCER_INTERVAL_MS);

        mOmControllerHBProducer = std::make_shared<HeartbeatProducer>(
            producerInterval,
            HB_CTRL_SHM_NAME,
            HB_CTRL_SEM_NAME,
            DEFAULT_HB_BUFFER_SIZE
        );

        mOmControllerHBProducer->Start();
    } catch (const std::exception& e) {
        LOG_E("[StatusUpdater] MS Controller Heartbeat Initialize failed: %s", e.what());
        return -1;
    }
    mDeployMode = deployMode;
    return 0;
}

void StatusUpdater::UpdateAllNodeStatus()
{
    int32_t ret = 0;
    mNodeIds = mNodeStatus->GetAllNodeIds();
    if (mNodeIds.empty()) {
        LOG_D("[StatusUpdater] No node IDs to update.");
        return;
    }
    std::vector<uint64_t> ignoredNodes;
    std::vector<uint64_t> deletedNodes;
    std::vector<uint64_t> updateFailedNodes;
    for (auto id : std::as_const(mNodeIds)) {
        auto node = mNodeStatus->GetNode(id);
        if (node == nullptr) {
            ignoredNodes.push_back(id);
            continue;
        }
        bool isReady = false;
        if (node->deleteTime > std::chrono::seconds(0)) {
            deletedNodes.push_back(node->instanceInfo.staticInfo.id);
            continue;
        }
        ret = ServerRequestHandler::GetInstance()->UpdateNodeStatus(
            *mServerClient, *mNodeStatus, *node, false, isReady);
        if (ret != 0) {
            updateFailedNodes.push_back(node->instanceInfo.staticInfo.id);
            continue;
        }
    }
    if (!ignoredNodes.empty()) {
        LOG_W("[StatusUpdater] Ignore node id: %s.",
            NodeStatus::ConvertNodeIdVector2Str(ignoredNodes).c_str());
    }
    if (!deletedNodes.empty()) {
        LOG_W("[%s] [StatusUpdater] Updating all node status, ignore deleted nodes: %s",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::STATUS_UPDATER).c_str(),
            NodeStatus::ConvertNodeIdVector2Str(deletedNodes).c_str());
    }
    if (!updateFailedNodes.empty()) {
        LOG_E("[%s] [StatusUpdater] Updating all node status, failed to update status for nodes: %s.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::STATUS_UPDATER).c_str(),
            NodeStatus::ConvertNodeIdVector2Str(updateFailedNodes).c_str());
    }
}

void StatusUpdater::SendNodeStatus() const
{
    CoordinatorRequestHandler::GetInstance()->SendNodeStatus(mCoordinatorClient, mNodeStatus, mCoordinatorStore);
}
}
