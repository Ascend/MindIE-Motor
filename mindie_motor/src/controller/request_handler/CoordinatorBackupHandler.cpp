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
#include "CoordinatorBackupHandler.h"

#include <chrono>
#include <string>
#include <algorithm>
#include <thread>
#include <set>
#include <numeric>

#include "NodeScheduler.h"
#include "Logger.h"
#include "GroupGeneratorFactory.h"
#include "GroupGenerator.h"
#include "ControllerConfig.h"
#include "ServerRequestHandler.h"
#include "ProcessManager.h"
#include "RoleManagerInitializer.h"
#include "FaultManager.h"
#include "Util.h"

namespace MINDIE::MS {
constexpr uint64_t CHECK_COORDINATOR_STATUS = 5;
constexpr uint64_t COORDINATOR_MASTER_SIZE = 2;
constexpr int32_t CODE_OK = 200;

CoordinatorBackupHandler::CoordinatorBackupHandler(
    std::shared_ptr<CoordinatorStore> coordinatorStoreInit,
    std::shared_ptr<CoordinatorStore> coordinatorStoreWithMasterInfoInit
    ) : mCoordinatorStore(coordinatorStoreInit), mCoordinatorStoreWithMasterInfo(coordinatorStoreWithMasterInfoInit)
{
    LOG_I("[CoordinatorBackupHandler] Create successfully.");
}

CoordinatorBackupHandler::~CoordinatorBackupHandler()
{
    Stop();
    LOG_I("[CoordinatorBackupHandler] Delete successfully.");
}

int32_t CoordinatorBackupHandler::Init()
{
    try {
        mCoordinatorClient = std::make_shared<HttpClient>();
    } catch (const std::exception& e) {
        LOG_E("[CoordinatorBackupHandler] Initialize failed because create pointer failed.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str());
        return -1;
    }
    if (mCoordinatorClient->Init("", "", ControllerConfig::GetInstance()->GetRequestCoordinatorTlsItems()) != 0) {
        LOG_E("[CoordinatorBackupHandler] Initialize failed because create pointer failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str());
        return -1;
        }
    return 0;
}

static std::string BuildCoordinatorBackupRequestBody(bool isMaster, bool isAbnormal, bool isRandomPick)
{
    nlohmann::json body;
    body["is_master"] = isMaster;
    body["is_abnormal"] = isAbnormal;
    body["is_random_pick"] = isRandomPick;
    return body.dump();
}

int32_t CoordinatorBackupHandler::SendCoordinatorBackupRequest(const std::unique_ptr<Coordinator> &node,
    boost::beast::http::verb verb, CoordinatorURI type, std::string& body, std::string &response)
{
    auto ret = -1;
    auto port = ControllerConfig::GetInstance()->GetCoordinatorPort();
    int32_t code = 400;
    std::map <boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";

    mCoordinatorClient->SetHostAndPort(node->ip, port);
    Request req = {ControllerConstant::GetInstance()->GetCoordinatorURI(type),
                   verb, map, body};
    ret = mCoordinatorClient->SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
        ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
    if (ret != 0 || code != CODE_OK) {
        LOG_W("[%s] [CoordinatorBackupHandler] %s coordinator BackupStatus information failed, IP %s, port %s, "
                "ret code %d, request ret %d",
              GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str(),
              boost::beast::http::to_string(verb).data(),
              node->ip.c_str(), port.c_str(), code, ret);
        mCoordinatorStore->UpdateCoordinatorStatus(node->ip, false);
        mCoordinatorStoreWithMasterInfo->UpdateCoordinatorStatus(node->ip, false);
        return -1;
    }
    if (response.empty()) {
        LOG_E("[CoordinatorBackupHandler] Response is empty, response failed.");
        return -1;
    }
    LOG_I("[CoordinatorBackupHandler] %s coordinator BackupStatus information success, IP %s.",
          boost::beast::http::to_string(verb).data(), node->ip.c_str());
    return 0;
}

void CoordinatorBackupHandler::SetCoordinatorIsMasterBackup(std::vector<std::unique_ptr<Coordinator>> &coordinatorNodes)
{
    bool backupStatus = true;
    for (auto it = mCoordinatorBackupStatus.begin(); it != mCoordinatorBackupStatus.end(); ++it) {
        if (it->second) {
            backupStatus = false;
            break;
        }
    }
    if (mCoordinatorBackupStatus.empty() || backupStatus) {
        coordinatorNodes[0]->isMaster = true;
        coordinatorNodes[1]->isMaster = false;
        isRandomPick = true;
        LOG_I("[CoordinatorBackupHandler] Controller choose a coordinator to be master, update successfully.");
        return;
    }
    for (auto &coordinator : coordinatorNodes) {
        if (!mCoordinatorBackupStatus[coordinator->ip]) {
            coordinator->isMaster = true;
        } else {
            coordinator->isMaster = false;
        }
    }
    LOG_D("[CoordinatorBackupHandler] Coordinator backupstatus already exists.");
    return;
}

static bool GetCoordinatorAbnormalStatus(int32_t masterRecvFlow, int32_t backRecvFlow)
{
    if (masterRecvFlow > 0 && backRecvFlow > 0) {
        LOG_I("[CoordinatorBackupHandler] Coordinator master and back info, masterRecvFlow = %d, backRecvFlow = %d.",
            masterRecvFlow, backRecvFlow);
        return true;
    }
    return false;
}

void CoordinatorBackupHandler::UpdateCoordinatorMasterInfo(std::vector<std::unique_ptr<Coordinator>> &coordinatorNodes)
{
    if (coordinatorNodes[0]->recvFlow == 0 && coordinatorNodes[1]->recvFlow == 0) {
        if (coordinatorNodes[0]->isMaster && coordinatorNodes[1]->isMaster) {
            SetCoordinatorIsMasterBackup(coordinatorNodes);
        }
    } else if (coordinatorNodes[0]->recvFlow > 0 && coordinatorNodes[1]->recvFlow > 0) {
        if (coordinatorNodes[0]->isMaster && coordinatorNodes[1]->isMaster) {
            SetCoordinatorIsMasterBackup(coordinatorNodes);
        }
    } else {
        coordinatorNodes[0]->isMaster = coordinatorNodes[0]->recvFlow > 0 ? true : false;
        coordinatorNodes[1]->isMaster = coordinatorNodes[1]->recvFlow > 0 ? true : false;
    }
    LOG_I("[CoordinatorBackupHandler] Set coordinator master and backup status successfully.");
    for (auto &node : std::as_const(coordinatorNodes)) {
        auto it = mCoordinatorBackupStatus.find(node->ip);
        if (it != mCoordinatorBackupStatus.end()) {
            it->second = node->isMaster;
        } else {
            mCoordinatorBackupStatus.insert({node->ip, node->isMaster});
        }
    }
    LOG_I("[CoordinatorBackupHandler] Update coordinator master and backup status successfully.");
    return;
}

void CoordinatorBackupHandler::GetCoordinatorBackupInfo(std::vector<std::unique_ptr<Coordinator>> &coordinatorNodes)
{
    for (auto &node : std::as_const(coordinatorNodes)) {
        std::string backupInfo;
        std::string body;
        if (SendCoordinatorBackupRequest(node, boost::beast::http::verb::get, CoordinatorURI::GET_RECVS_INFO,
            body, backupInfo) != 0) {
            LOG_W("[CoordinatorBackupHandler] get backup info, response is failed, IP %s", node->ip.c_str());
            continue;
        }
        try {
            nlohmann::json statusInfo = nlohmann::json::parse(backupInfo, CheckJsonDepthCallBack);
            node->isMaster = statusInfo.value("is_master", false);
            node->recvFlow = statusInfo.value("recv_flow", 0);
        } catch (const nlohmann::json::exception& e) {
            LOG_E("[CoordinatorBackupHandler] JSON parse error: %s, IP %s, response: %s",
                  e.what(), node->ip.c_str(), backupInfo.c_str());
            continue;
        } catch (const std::exception& e) {
            LOG_E("[CoordinatorBackupHandler] Standard exception: %s, IP %s",
                  e.what(), node->ip.c_str());
            continue;
        }
    }
}

void CoordinatorBackupHandler::DealWithCoordinatorBackup()
{
    if (!ControllerConfig::GetInstance()->IsLeader()) {
        return;
    }
    bool isAbnormal = false;
    isRandomPick = false;
    std::vector<std::unique_ptr<Coordinator>> coordinatorNodes;
    try {
        coordinatorNodes = mCoordinatorStore->GetCoordinators();
    } catch (const std::exception& e) {
        LOG_E("[%s] [CoordinatorBackupHandler] Failed to get coordinator node, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str(), e.what());
        return;
    }
    if (coordinatorNodes.size() != COORDINATOR_MASTER_SIZE) {
        LOG_W("[CoordinatorBackupHandler] Coordinator master and backup node not ready.");
        mCoordinatorStoreWithMasterInfo->UpdateCoordinators(coordinatorNodes);
        return;
    }
    GetCoordinatorBackupInfo(coordinatorNodes);
    isAbnormal = GetCoordinatorAbnormalStatus(coordinatorNodes[0]->recvFlow, coordinatorNodes[1]->recvFlow);
    UpdateCoordinatorMasterInfo(coordinatorNodes);
    for (auto &node : std::as_const(coordinatorNodes)) {
        std::string response;
        auto jsonString = BuildCoordinatorBackupRequestBody(node->isMaster, isAbnormal, isRandomPick);
        if (SendCoordinatorBackupRequest(node, boost::beast::http::verb::post, CoordinatorURI::POST_BACKUP_INFO,
            jsonString, response) != 0) {
            LOG_W("[CoordinatorBackupHandler] Post backup info, response is failed, IP %s.", node->ip.c_str());
            continue;
        } else {
            LOG_D("[CoordinatorBackupHandler] Post backup info successfully, IP %s.", node->ip.c_str());
        }
    }
    mCoordinatorStoreWithMasterInfo->UpdateCoordinators(coordinatorNodes);
}

int32_t CoordinatorBackupHandler::StartCoordinatorStatusThread()
{
    try {
        mCoordinatorStatusThread = std::make_unique<std::thread>([this]() {
            LOG_I("[CoordinatorBackupHandler] Start Query status thread.");
            while (mRun.load()) {
                DealWithCoordinatorBackup();
                std::this_thread::sleep_for(std::chrono::seconds(CHECK_COORDINATOR_STATUS));
            }
            LOG_I("[CoordinatorBackupHandler] End Query status thread.");
            return 0;
        });
        return 0;
    } catch (const std::exception& e) {
        LOG_E("[CoordinatorBackupHandler] Failed to create Query status thread. Error: %s.", e.what());
        return -1;
    }
}

int32_t CoordinatorBackupHandler::Start()
{
    if (mRun.load()) {
        LOG_I("[CoordinatorBackupHandler] Thread is already exist.");
        return 0;
    }
    LOG_I("[CoordinatorBackupHandler] Start to create coordinator status thread.");
    mRun.store(true);
    if (StartCoordinatorStatusThread() != 0) {
        LOG_E("[%s] [CoordinatorBackupHandler] Run failed because failed to create coordinator status thread.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str());
        mRun.store(false);
        return -1;
    }
    LOG_I("[CoordinatorBackupHandler] Create coordinator status thread success.");
    return 0;
}

void CoordinatorBackupHandler::Stop()
{
    mRun.store(false);
    LOG_I("[CoordinatorBackupHandler] Stop successfully");
}

}