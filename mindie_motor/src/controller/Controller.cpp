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
#include "Controller.h"
#include <pwd.h>
#include "JsonFileLoader.h"
#include "ControllerConfig.h"
#include "CoordinatorStore.h"
#include "FaultManager.h"
#include "ClusterClient.h"
#include "ControllerLeaderAgent.h"
#include "DistributedPolicy.h"
#include "NPURecoveryManager.h"
namespace MINDIE::MS {
static int32_t PrintUserInfo()
{
    uid_t uid = geteuid();
    struct passwd* pw = getpwuid(uid);
    if (pw == nullptr) {
        LOG_E("[%s] [Controller] Failed to get user information",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    LOG_M("[Start] Printing user information, initialize by user id %u, name %s.", pw->pw_uid, pw->pw_name);
    return 0;
}

bool IsUsingWithCCAE()
{
    bool result = !ControllerConfig::GetInstance()->GetCCAEIP().empty();
    return result;
}

int32_t Controller::CreatePointers()
{
    try {
        coordinatorStore = std::make_shared<CoordinatorStore>();
        coordinatorStoreWithMasterInfo = std::make_shared<CoordinatorStore>();
        nodeStatus = std::make_shared<NodeStatus>();
        nodeScheduler = std::make_shared<NodeScheduler>(nodeStatus, coordinatorStore);
        statusUpdater = std::make_unique<StatusUpdater>(nodeStatus, coordinatorStore);
        mCoordinatorBackupHandler = std::make_unique<CoordinatorBackupHandler>(
            coordinatorStore, coordinatorStoreWithMasterInfo);
        probeServer = std::make_unique<ProbeServer>();
    } catch (const std::exception& e) {
        LOG_E("[%s] [Controller] Initializing controller, create pointers failed.",
              GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    return 0;
}

int32_t Controller::InitParams()
{
    mDeployMode = ControllerConfig::GetInstance()->GetDeployMode();
    LOG_I("[Controller] Initializing controller, using deploy mode %d.", mDeployMode);
    if (CreatePointers() != 0) {
        return -1;
    }
    
    if (probeServer->Init() != 0) {
        LOG_E("[%s] [Controller] Initializing controller, initialize probe server failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    if (statusUpdater->Init(mDeployMode) != 0 ||
        ClusterStatusWriter::GetInstance()->Init(nodeStatus, coordinatorStore) != 0) {
        LOG_E("[%s] [Controller] Initializing controller, initialize status updater or cluster status writer failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    if (ControllerConfig::GetInstance()->IsMultiNodeMode() && AlarmManager::GetInstance()->Init(nodeStatus) != 0) {
        LOG_W("[%s] [Controller] Initializing controller, initialize alarm manager failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
    }
    if (nodeScheduler->Init(mDeployMode) != 0) {
        LOG_E("[%s] [Controller] Initializing controller, initialize node scheduler failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    
    if (FaultManager::GetInstance()->Init(nodeStatus, mDeployMode) != 0) {
        LOG_E("[%s] [Controller] Initializing controller, initialize fault manager failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    FaultManager::GetInstance()->SetRankTableLoader(nodeScheduler->GetRankTableLoader());
    
    if (NPURecoveryManager::GetInstance()->Init(nodeStatus, nodeScheduler) != 0) {
        LOG_E("[%s] [Controller] Initializing controller, initialize fault manager failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }

    // 设置ClusterClient和NodeScheduler共享的同步变量
    ClusterClient::GetInstance()->SetWaitClusterDGRTSave(&(this->mWaitClusterDGRTSave));
    nodeScheduler->SetWaitClusterDGRTSave(&(this->mWaitClusterDGRTSave));
    if (mCoordinatorBackupHandler->Init() != 0) {
        LOG_E("[%s] [Controller] Initializing controller, initialize coordinator backup failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    return 0;
}

void Controller::InitLeader()
{
    if (ControllerConfig::GetInstance()->GetCtrlBackUpConfig().funSw) {
        auto ctrlBackCfg = ControllerConfig::GetInstance()->GetCtrlBackUpConfig();
        const std::string partAddr = ctrlBackCfg.serverDns;
        const std::string etcdAddr = partAddr + ":" + std::to_string(ctrlBackCfg.serverPort);
        const std::string podIp = ControllerConfig::GetInstance()->GetPodIP();
        auto tlsConfig = ControllerConfig::GetInstance()->GetEtcdTlsItems();
        EtcdTimeInfo etcdTimeInfo;
        auto etcd_lock = std::make_unique<EtcdDistributedLock>(
            etcdAddr,
            "/cluster/leader-lock", // use for lock key
            podIp, // use for lock value
            tlsConfig, // use for etcd tls
            etcdTimeInfo // use for etcd time info
        );
        ControllerLeaderAgent::GetInstance()->RegisterStrategy(std::move(etcd_lock));
        ControllerLeaderAgent::GetInstance()->Start();
        LOG_I("[LeaderAgent] leader Campaign finish, serve ip is %s, is leader: %d.",
            etcdAddr.c_str(), ControllerConfig::GetInstance()->IsLeader());
    } else {
        ControllerConfig::GetInstance()->SetLeader(true);
    }
}

int32_t Controller::Init()
{
    if (ControllerConfig::GetInstance()->Init() != 0) {
        LOG_E("[%s] [Controller] Initialize controller config failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }

    InitLeader();
    
    if (PrintUserInfo() != 0) {
        LOG_E("[%s] [Controller] Initialize print user information failed.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    if (InitParams() == -1) {
        return -1;
    }
    if (IsUsingWithCCAE()) {
        mCCAEStatus = std::make_shared<CCAEStatus>();
        mCCAEReportClient = std::make_unique<CCAEReporter>(mCCAEStatus, coordinatorStore,
            coordinatorStoreWithMasterInfo, nodeStatus);
        CCAERequestHandler::GetInstance()->SetRankTableLoader(nodeScheduler->GetRankTableLoader());
        mCCAEReportClient->Init();
    }
    if (std::getenv("MINDX_TASK_ID") != nullptr &&
        std::getenv("MINDX_SERVER_IP") != nullptr) {
        if (ClusterClient::GetInstance()->Start(nodeStatus) != 0) {
            LOG_W("[%s] [Controller] Initializing controller, register cluster client failed.",
                GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        }
    }
    if (mCoordinatorBackupHandler->Start() != 0) {
        LOG_W("[%s] [Controller] Start coordinator backup handler thread failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
    }
    return 0;
}

int32_t Controller::Run()
{
    try {
        if (nodeScheduler->Run() != 0) {
            LOG_E("[%s] [Controller] Run node scheduler failed.",
                  GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
            return -1;
        }
        LOG_I("[Controller] Exit running.");
        return 0;
    } catch (const std::exception& e) {
        LOG_E("[%s] [Controller] Run failed, error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::CONTROLLER).c_str(), e.what());
        LOG_I("[Controller] Exit running.");
        return -1;
    }
}
}