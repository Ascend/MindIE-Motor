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
#ifndef MINDIE_MS_CONTROLLER_H
#define MINDIE_MS_CONTROLLER_H

#include <memory>
#include <cstdint>
#include "CoordinatorBackupHandler.h"
#include "NodeScheduler.h"
#include "AlarmManager.h"
#include "CoordinatorStore.h"
#include "StatusUpdater.h"
#include "ProbeServer.h"
#include "ProcessManager.h"
#include "ClusterStatusWriter.h"
#include "CCAEStatus.h"
#include "CCAEReporter.h"
#include "CCAERequestHandler.h"
#include "Logger.h"
namespace MINDIE::MS {
class Controller {
public:
    Controller(const Controller &obj) = delete;
    Controller &operator=(const Controller &obj) = delete;
    Controller(Controller &&obj) = delete;
    Controller &operator=(Controller &&obj) = delete;
    Controller() = default;
    ~Controller()
    {
        ProcessManager::GetInstance()->Stop();
        AlarmManager::GetInstance()->Stop();
        ClusterStatusWriter::GetInstance()->Stop();
        if (probeServer != nullptr) {
            probeServer->Stop();
            probeServer.reset();
        }
        if (statusUpdater != nullptr) {
            statusUpdater->Stop();
            statusUpdater.reset();
        }
        nodeScheduler.reset();
        nodeStatus.reset();
        coordinatorStore.reset();
        LOG_I("[Controller]destroy successfully");
    };
    int32_t Init();
    int32_t Run();
private:
    DeployMode mDeployMode = DeployMode::SINGLE_NODE;
    std::shared_ptr<NodeStatus> nodeStatus = nullptr;
    std::shared_ptr<CoordinatorStore> coordinatorStore = nullptr;
    std::shared_ptr<CoordinatorStore> coordinatorStoreWithMasterInfo = nullptr;
    std::shared_ptr<CCAEStatus> mCCAEStatus = nullptr;
    std::shared_ptr<NodeScheduler> nodeScheduler = nullptr;
    std::shared_ptr<CoordinatorBackupHandler> mCoordinatorBackupHandler = nullptr;
    std::unique_ptr<StatusUpdater> statusUpdater = nullptr;
    std::unique_ptr<ProbeServer> probeServer = nullptr;
    std::unique_ptr<CCAEReporter> mCCAEReportClient = nullptr;
    // 被ClusterClient类和NodeScheduler类共享，用于同步初始化时序，优先使用ClusterD传输过来的global ranktable，
    // 再使用deploy_acjob.py生成的global ranktable，在第一次保存ClusterD传输过来的global ranktable后，置为true
    std::atomic<bool> mWaitClusterDGRTSave{false};

    int32_t InitParams();
    void InitLeader();
    int32_t CreatePointers();
};
}
#endif // MINDIE_MS_CONTROLLER_H
