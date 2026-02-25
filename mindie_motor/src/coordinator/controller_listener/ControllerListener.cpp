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
#include "ControllerListener.h"
#include <string>
#include "boost/uuid/uuid_io.hpp"
#include "AlarmRequestHandler.h"
#include "Logger.h"
#include "nlohmann/json.hpp"
#include "Configure.h"
#include "Communication.h"
#include "Util.h"

namespace MINDIE::MS {

static constexpr size_t SET_MASTER = 5;
static constexpr uint32_t MAX_CHECK_TIMES_OF_COOR_STATUS = 10;
static constexpr uint32_t MAX_ALLOWED_PARAM_NUM_IN_SINGLE_URL = 100;

ControllerListener::ControllerListener(std::unique_ptr<MINDIE::MS::DIGSScheduler> &schedulerInit,
    std::unique_ptr<ClusterNodes>& instancesRec, std::unique_ptr<RequestRepeater>& requestRepeater1,
    std::unique_ptr<ReqManage>& reqManageInit, std::unique_ptr<ExceptionMonitor>& exceptionMonitor)
    : inputLen(0), outputLen(0), scheduler(schedulerInit), instancesRecord(instancesRec),
      requestRepeater(requestRepeater1), reqManage(reqManageInit), exceptionMonitor(exceptionMonitor)
{
    dataReady = std::make_shared<std::atomic<bool>>();
    dataReady->store(false);
}

void ControllerListener::StopMasterCheck()
{
    masterCheck.store(false);
    LOG_I("[ControllerListener] Stop master checking.");
}

void ControllerListener::Master2Worker()
{
    auto &instanceInfos = instancesRecord->GetInstanceInfos();
    for (auto it = instanceInfos.begin(); it != instanceInfos.end(); ++it) {
        if (it->second->role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
            std::string ip = it->second->ip;
            std::string port = it->second->port;
            CloseConnection(ip, port);
        }
    }
}

void ControllerListener::CloseConnection(std::string ip, std::string port)
{
    if (requestRepeater->CheckLinkWithDNode(ip, port)) {
        auto &httpClient = requestRepeater->GetHttpClientAsync();
        auto connIds = httpClient.FindId(std::string(ip), std::string(port));
        LOG_D("[ControllerListener] Close connection link with decode node at %s:%s.", ip.c_str(), port.c_str());
        for (auto &connId : connIds) {
            std::shared_ptr<ClientConnection> conn = httpClient.GetConnection(connId);
            if (conn != nullptr) {
                conn->DoClose();
            } else {
                LOG_W("[ControllerListener] failed to get connection for ID: %u", connId);
            }
        }
    }
}

void SchedulerSetBlockSize(uint32_t blockSize, MINDIE::MS::DIGSScheduler& scheduler)
{
    const size_t maxBlockSize = 128;
    if (blockSize < 1 || blockSize > maxBlockSize) { // 128: Max valid block size.
        LOG_E("[%s] [ControllerListener] Invalid block size %u, should be in range [1, %zu].",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            blockSize, maxBlockSize);
        return;
    }
    scheduler.SetBlockSize(blockSize);
}

void ControllerListener::GetControllerIP(std::shared_ptr<ServerConnection> connection)
{
    try {
        auto& socket = connection->GetSocket().socket();
        boost::asio::ip::tcp::endpoint remoteEp = socket.remote_endpoint();
        std::string clientIP = remoteEp.address().to_string();
        if (Configure::Singleton()->SetControllerIP(clientIP) != 0) {
            LOG_E("[%s] [ControllerListener] Failed to set controller IP.",
                GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        } else {
            LOG_D("[ControllerListener] Set controller IP successfully.");
        }
    } catch (const std::exception& e) {
        LOG_E("[%s] [ControllerListener] Failed to get client IP: %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), e.what());
    }
}

void ControllerListener::InstancesRefreshHandler(std::shared_ptr<ServerConnection> connection)
{
    std::unique_lock<std::shared_mutex> lock(refreshMtx);
    GetControllerIP(connection);
    auto &req = connection->GetReq();
    auto body = boost::beast::buffers_to_string(req.body().data());
    ServerRes res;
    try {
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        auto instances = bodyJson.at("instances");
        if (!init) {
            if (instances.size() > 0) {
                auto blockSize = instances.at(0).at("static_info").at("block_size").template get<uint32_t>();
                SchedulerSetBlockSize(blockSize, *scheduler);
                init = true;
            }
        }
        auto idsVec = bodyJson.at("ids").template get<std::vector<uint64_t>>();
        auto flexRet = instancesRecord->ProcessFlexInstance(idsVec, instances);
        if (flexRet != 0) {
            res.state = boost::beast::http::status::bad_request;
        } else {
            auto ret = IdsTraverse(idsVec, instances);
            if (ret != 0) {
                res.state = boost::beast::http::status::bad_request;
            }
        }
        bool currentStatus = instancesRecord->IsAvailable() && (dataReady != nullptr && dataReady->load());
        if (lastStatus != currentStatus) {
            reportCounter = 0;
        }
        if (reportCounter % MAX_CHECK_TIMES_OF_COOR_STATUS == 0) {
            if (currentStatus) {
                LOG_I("MindIE-MS coordinator is ready!!!");
            } else {
                LOG_I("MindIE-MS coordinator is not ready...");
            }
        }
        ++reportCounter;
        lastStatus = currentStatus;
        CheckAndHandleCoordinatorStateAlarm();
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [Configure] Failed to read controller instance refresh request: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            body.c_str(), e.what());
        res.state = boost::beast::http::status::bad_request;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ControllerListener] Exception occurred while handling instance refresh request. Error is %s\n",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), e.what());
        res.state = boost::beast::http::status::bad_request;
    }
    connection->SendRes(res);
}

void ControllerListener::CoordinatorInfoHandler(std::shared_ptr<ServerConnection> connection)
{
    LOG_D("[ControllerListener] Handling coordinator information.");
    if (!init) {
        SendErrorRes(connection, boost::beast::http::status::service_unavailable, "");
        return;
    }
    std::vector<MINDIE::MS::DIGSInstanceScheduleInfo> schedulerInfo;
    scheduler->QueryInstanceScheduleInfo(schedulerInfo);
    instancesRecord->ProcSchedulerInfoUnderFlexSituation(schedulerInfo);
    MINDIE::MS::DIGSRequestSummary summary;
    scheduler->QueryRequestSummary(summary);
    nlohmann::json body;
    body["schedule_info"] = nlohmann::json::array();
    for (const auto &elem : schedulerInfo) {
        nlohmann::json elemBody;
        elemBody["id"] = elem.id;
        elemBody["allocated_slots"] = elem.allocatedSlots;
        elemBody["allocated_blocks"] = elem.allocatedBlocks;
        body["schedule_info"].emplace_back(elemBody);
        body["request_num"] = reqManage->GetReqCount();
    }
    nlohmann::json summaryBody;
    if (summary.inputLength != 0) {
        inputLen = static_cast<size_t>(static_cast<float>(summary.inputLength) / Configure::Singleton()->strTokenRate);
    }
    if (summary.outputLength != 0) {
        outputLen = summary.outputLength;
    }
    summaryBody["input_len"] = inputLen.load();
    summaryBody["output_len"] = outputLen.load();
    body["request_length_info"] = summaryBody;

    ServerRes res;
    res.contentType = "application/json";
    res.body = body.dump();
    connection->SendRes(res);
}

void ControllerListener::InstancesOfflineHandler(std::shared_ptr<ServerConnection> connection)
{
    LOG_D("[ControllerListener] Handling instances offline.");
    if (!init) {
        SendErrorRes(connection, boost::beast::http::status::service_unavailable, "");
        return;
    }
    auto &req = connection->GetReq();
    auto body = boost::beast::buffers_to_string(req.body().data());
    ServerRes res;
    try {
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        auto ids = bodyJson.at("ids").template get<std::vector<uint64_t>>();
        instancesRecord->ProcInstanceIdsUnderFlexSituation(ids);
        auto insNumMax = instancesRecord->GetInsNumMax();
        if (ids.size() > insNumMax) {
            LOG_E("[%s] [ControllerListener] Failed to handle instances offline, ids is out of range[0, %u].",
                GetErrorCode(ErrorType::OUT_OF_RANGE, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), insNumMax);
            res.state = boost::beast::http::status::bad_request;
        } else {
            scheduler->CloseInstance(ids);
            for (size_t i = 0; i < ids.size(); ++i) {
                auto ip = instancesRecord->GetIp(ids[i]);
                auto port = instancesRecord->GetPort(ids[i]);
                LOG_M("[Offline] Instance offline ip: %s:%s", ip.c_str(), port.c_str());
            }
        }
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [Configure] Failed to read controller instance offline request: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            body.c_str(), e.what());
        res.state = boost::beast::http::status::bad_request;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ControllerListener] Failed to handle instances offline, error is %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            e.what());
        res.state = boost::beast::http::status::bad_request;
    }
    connection->SendRes(res);
}

// 将断流的实例恢复调度
void ControllerListener::InstancesOnlineHandler(std::shared_ptr<ServerConnection> connection)
{
    LOG_D("[ControllerListener] Handling instances online.");
    if (!init) {
        SendErrorRes(connection, boost::beast::http::status::service_unavailable, "");
        return;
    }
    auto &req = connection->GetReq();
    auto body = boost::beast::buffers_to_string(req.body().data());
    ServerRes res;
    try {
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        auto ids = bodyJson.at("ids").template get<std::vector<uint64_t>>();
        auto insNumMax = instancesRecord->GetInsNumMax();
        if (ids.size() > insNumMax) {
            LOG_E("[%s] [ControllerListener] Handle instances online failed, IDs is out of range[0, %u].",
                GetErrorCode(ErrorType::OUT_OF_RANGE, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), insNumMax);
            res.state = boost::beast::http::status::bad_request;
        } else {
            for (size_t i = 0; i < ids.size(); ++i) {
                auto id = ids[i];
                scheduler->ActivateInstance({id});
                auto ip = instancesRecord->GetIp(id);
                auto port = instancesRecord->GetPort(id);
                LOG_M("[Online] Instance online ip: %s:%s", ip.c_str(), port.c_str());
            }
        }
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [Configure] Failed to read controller instance online request: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            body.c_str(), e.what());
        res.state = boost::beast::http::status::bad_request;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ControllerListener] Handle instances online failed, exception is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            e.what());
        res.state = boost::beast::http::status::bad_request;
    }
    connection->SendRes(res);
}

void ControllerListener::InstancesTasksHandler(std::shared_ptr<ServerConnection> connection)
{
    LOG_D("[ControllerListener] Handling instance tasks.");
    nlohmann::json tasksJson;
    tasksJson["tasks"] = nlohmann::json::array();
    auto &req = connection->GetReq();
    auto url = req.target();
    auto queryParm = ParseQuery(url);
    for (auto it = queryParm.begin(); it != queryParm.end(); ++it) {
        std::string key = it->first;
        std::string value = it->second;
        if (key != "id") {
            continue;
        }
        uint64_t nodeId;
        try {
            nodeId = std::stoul(std::string(value));
        } catch (const std::exception &e) {
            LOG_E("[%s] [ControllerListener] Handle instance tasks failed, exception is %s.",
                GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
                e.what());
            tasksJson["tasks"].emplace_back(-1);
            continue;
        }
        // 考虑Flex节点，这里返回的Task数量要是Flex对应所有实例的和（可能是分裂后的P+D）
        auto task = instancesRecord->GetInstanceTaskNumUnderFlexSituation(nodeId);
        tasksJson["tasks"].emplace_back(task);
    }
    ServerRes res;
    res.contentType = "application/json";
    res.body = tasksJson.dump();
    connection->SendRes(res);
}

void ControllerListener::RecvsUpdater(std::shared_ptr<ServerConnection> connection)
{
    ServerRes reply;
    nlohmann::json recvsAndIsMaster;
    recvsAndIsMaster["is_master"] = Configure::Singleton()->IsMaster();
    recvsAndIsMaster["recv_flow"] = reqManage->GetReqArriveNum();
    reply.body = recvsAndIsMaster.dump();
    connection->SendRes(reply);
}

void ControllerListener::AbnormalStatusHandler(std::shared_ptr<ServerConnection> connection)
{
    LOG_D("[ControllerListener] Handling abnormal and master information from controller.");
    if (!init) {
        SendErrorRes(connection, boost::beast::http::status::service_unavailable, "");
        LOG_E("[%s] [ControllerListener] Failed to handle instance from controller, because it is not init.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        return;
    }
    auto &req = connection->GetReq();
    auto body = boost::beast::buffers_to_string(req.body().data());
    ServerRes res;
    nlohmann::json abnormalStatusReply;
    try {
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        bool isMaster = bodyJson.at("is_master").get<bool>();
        bool isAbnormal = bodyJson.at("is_abnormal").get<bool>();
        res.state = boost::beast::http::status::ok;
        AbnormalStatusUpdate(isAbnormal, isMaster);
        abnormalStatusReply["update_successfully"] = true;
        res.body = abnormalStatusReply.dump();
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [Configure] Failed to read controller instance update request: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            body.c_str(), e.what());
        res.state = boost::beast::http::status::bad_request;
        abnormalStatusReply["update_successfully"] = false;
        res.body = abnormalStatusReply.dump();
    } catch (const std::exception &e) {
        LOG_E("[%s] [ControllerListener] Failed to status update, error is %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), e.what());
        res.state = boost::beast::http::status::bad_request;
        abnormalStatusReply["update_successfully"] = false;
        res.body = abnormalStatusReply.dump();
    }
    connection->SendRes(res);
}

void ControllerListener::AbnormalStatusUpdate(bool abnormal, bool master)
{
    Configure::Singleton()->SetAbnormal(abnormal);
    LOG_D("[ControllerListener] AbnormalStatusUpdate abnormal: %d, master: %d.", abnormal, master);
}

void ControllerListener::IdsAddOrUpdate(const std::vector<uint64_t> &addVec, const std::vector<uint64_t> &updateVec,
    const nlohmann::json::const_iterator &it, uint64_t id)
{
    if (instancesRecord->IsFaultyNode(id)) {
        auto deleteTime = instancesRecord->GetDeleteTime(id);
        auto timeSinceDeletion = system_clock::now() - deleteTime;
        const minutes expirationTime = minutes(2);
        if (timeSinceDeletion > expirationTime) {
            // 超过两分钟，从map中删除
            instancesRecord->RemoveFaultNode(id);
            LOG_D("[ControllerListener] Instance %lu is expired, removed from fault tracking.", id);
        } else {
            return;
        }
    }
    int32_t ret;
    auto iter = std::find(addVec.begin(), addVec.end(), id);
    if (iter != addVec.end()) {
        ret = Add(it); // 新增实例
        LOG_D("[ControllerListener] Instance %lu is added.", id);
        if (ret != 0) {
            LOG_E("[%s] [ControllerListener] Add instance failed",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
            return;
        }
    } else {
        iter = std::find(updateVec.begin(), updateVec.end(), id);
        if (iter == updateVec.end()) {
            return;
        }
        LOG_D("[ControllerListener] Instance %lu is updated.", id);
        ret = Update(it); // 更新实例
        if (ret != 0) {
            LOG_E("[%s] [ControllerListener] Update instance failed",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
            return;
        }
    }
}

int32_t ControllerListener::IdsTraverse(const std::vector<uint64_t> &ids, const nlohmann::json &instances)
{
    auto insNumMax = instancesRecord->GetInsNumMax();
    if (ids.size() > insNumMax) {
        LOG_E("[%s] [ControllerListener] The number of instance IDs exceeds the allowed range [0, %u]\n",
            GetErrorCode(ErrorType::OUT_OF_RANGE, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), insNumMax);
        return -1;
    }
    auto rollList = instancesRecord->Roll(ids);
    auto &addVec = std::get<0>(rollList);
    auto &updateVec = std::get<1>(rollList);
    auto &removeVec = std::get<2>(rollList); // 2是索引值
    try {
        if (instances.size() > insNumMax) {
            LOG_E("[%s] [ControllerListener] The number of instances in the JSON exceeds the allowed range [0, %u].\n",
                GetErrorCode(ErrorType::OUT_OF_RANGE, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), insNumMax);
            return -1;
        }
        for (auto it = instances.begin(); it != instances.end(); ++it) {
            auto id = it->at("id").template get<uint64_t>();
            IdsAddOrUpdate(addVec, updateVec, it, id);
        }
        Remove(removeVec);
        std::pair<size_t, size_t> pdNodeNum = {0, 0};
        const auto& instanceMap = instancesRecord->GetInstanceInfos();
        for (const auto &[id, instance] : instanceMap) {
            if (instance->role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
                pdNodeNum.first++;
            } else if (instance->role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE) {
                pdNodeNum.second++;
            }
            LOG_D("[ControllerListener] instance %zu is %c.\n", id, instance->role);
        }
        LOG_I("[ControllerListener] instance update success, add %zu instance, update %zu instance, "
                "remove %zu instance.(%zu P + %zu D)\n", addVec.size(), updateVec.size(), removeVec.size(),
                pdNodeNum.second, pdNodeNum.first);
        return 0;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ControllerListener] Exception while processing instance identifiers: %s.\n",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), e.what());
        return -1;
    }
}

bool IsPDSeparateMode(const std::string &deployMode)
{
    return (deployMode == "pd_separate" || deployMode == "pd_disaggregation" ||
        deployMode == "pd_disaggregation_single_container");
}

int32_t CheckInstanceReturn(const MINDIE::MS::DIGSInstanceStaticInfo &newInstance,
                            std::string deployMode)
{
    if (newInstance.totalBlockNum == 0) {
        LOG_E("[%s] [ControllerListener] Invalid total block count, expected a positive integer, but received 0.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        return -1;
    }
    if (newInstance.blockSize < 1 || newInstance.blockSize > 128) { // 上限128
        LOG_E("[%s] [ControllerListener] Invalid block size for instance, expected a value between 1 and 128, "
            "but received %zu.", GetErrorCode(ErrorType::INVALID_INPUT,
            CoordinatorFeature::CONTROLLER_LISTENER).c_str(), newInstance.blockSize);
        return -1;
    }
    if (IsPDSeparateMode(deployMode) && newInstance.label != MINDIE::MS::DIGSInstanceLabel::PREFILL_STATIC &&
        newInstance.label != MINDIE::MS::DIGSInstanceLabel::DECODE_STATIC &&
        newInstance.label != MINDIE::MS::DIGSInstanceLabel::FLEX_STATIC) {
        LOG_E("[%s] [ControllerListener] Invalid instance label in 'pd_separate' mode.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        return -1;
    }
    return 0;
}

int32_t ControllerListener::CheckInstance(const MINDIE::MS::DIGSInstanceStaticInfo &newInstance) const
{
    std::string deployMode = Configure::Singleton()->schedulerConfig["deploy_mode"]; // 部署模式
    if (IsPDSeparateMode(deployMode) && newInstance.role != MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE &&
        newInstance.role != MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        // 注意:这里的实例组中的Flex已被转化为PD的组合，因此校验仍按照P和D两种校验
        if (newInstance.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
            LOG_E("[%s] [ControllerListener] deployMode is pd_separate but get unprocessed flex instance",
                GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        } else {
            LOG_E("[%s] [ControllerListener] deployMode is pd_separate but get undef instance",
                  GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        }
        return -1;
    }
    if (deployMode == "single_node" && newInstance.role != MINDIE::MS::DIGSInstanceRole::UN_DEF_INSTANCE) {
        LOG_E("[%s] [ControllerListener] Deploy mode is 'single_node' but get not undefined instance.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        return -1;
    }
    if (newInstance.maxSeqLen == 0) {
        LOG_E("[%s] [ControllerListener] Invalid sequence length for instance, expected a positive integer, but "
            "received 0.", GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        return -1;
    }
    if (newInstance.maxOutputLen < 1 || newInstance.maxOutputLen > newInstance.maxSeqLen) {
        LOG_E("[%s] [ControllerListener] Invalid max output length for instance, expected a value in range [1, %zu], "
            "but received %zu.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            newInstance.maxSeqLen, newInstance.maxOutputLen);
        return -1;
    }
    if (newInstance.totalSlotsNum < 1 || newInstance.totalSlotsNum > 5000) { // 上限5000
        LOG_E("[%s] [ControllerListener] Invalid total slots number for instance, expected a value in range [1, %d], "
            "but received %zu.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            5000, newInstance.totalSlotsNum); // 上限5000
        return -1;
    }
        return CheckInstanceReturn(newInstance, deployMode);
}

int32_t ControllerListener::ParseInstance(
    const nlohmann::json::const_iterator &it,
    MINDIE::MS::DIGSInstanceStaticInfo &newInstance,
    std::string &ip,
    std::string &port,
    std::string &modelName) const
{
    try {
        auto &staticInfo = it->at("static_info");
        newInstance.groupId = staticInfo.at("group_id").template get<uint64_t>();
        newInstance.role = staticInfo.at("role").template get<MINDIE::MS::DIGSInstanceRole>();
        newInstance.flexPRatio = staticInfo.at("p_percentage").template get<uint64_t>();
        newInstance.maxSeqLen = staticInfo.at("max_seq_len").template get<uint32_t>();
        newInstance.maxOutputLen = staticInfo.at("max_output_len").template get<uint32_t>();
        newInstance.totalSlotsNum = staticInfo.at("total_slots_num").template get<uint32_t>();
        newInstance.totalBlockNum = staticInfo.at("total_block_num").template get<uint32_t>();
        newInstance.blockSize = staticInfo.at("block_size").template get<uint32_t>();
        newInstance.label = staticInfo.at("label").template get<MINDIE::MS::DIGSInstanceLabel>();
        newInstance.virtualId = staticInfo.at("virtual_id").template get<uint64_t>();
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [Configure] Failed to parse instance info: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            it->dump().c_str(), e.what());
        return -1;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ControllerListener] Failed to parse instance info, error is %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), e.what());
        return -1;
    }
    if (CheckInstance(newInstance) != 0) {
        return -1;
    }
    ip = it->at("ip").template get<std::string>();
    if (!IsValidIp(ip)) {
        LOG_E("[%s] [ControllerListener] Parsing instance, invalid IP detected!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        return -1;
    }
    port = it->at("port").template get<std::string>();
    if (!IsValidPortString(port)) {
        LOG_E("[%s] [ControllerListener] Parsing instance, invalid port detected!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        return -1;
    }
    modelName = it->at("model_name").template get<std::string>();
    newInstance.maxConnectionNum = Configure::Singleton()->reqLimit.singleNodeMaxReqs;
    return 0;
}

int32_t ControllerListener::AddIns(uint64_t id, const HttpParam &httpParam,
    const MINDIE::MS::DIGSInstanceStaticInfo &newInstance,
    const std::string &modelName,
    const nlohmann::json::const_iterator &it)
{
    if (!instancesRecord->AddInstance(id, httpParam.ip, httpParam.port, newInstance.role, modelName)) {
        LOG_E("[%s] [ControllerListener] Add instance failed.",
            GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        return -1;
    }
    instancesRecord->UpdateExtraInfo(id, std::make_pair(httpParam.metricPort, httpParam.interCommPort),
        newInstance.totalBlockNum, newInstance.totalSlotsNum, newInstance.virtualId);
    scheduler->RegisterInstance({newInstance});
    return Update(it);
}

int32_t ControllerListener::Add(const nlohmann::json::const_iterator &it)
{
    MINDIE::MS::DIGSInstanceStaticInfo newInstance;
    std::string ip;
    std::string port;
    std::string metricPort;
    std::string interCommPort;
    uint64_t id;
    std::string modelName;
    try {
        id = it->at("id").template get<uint64_t>();
        LOG_D("[ControllerListener] Adding new instance with ID %lu.", id);
        metricPort = it->at("metric_port").template get<std::string>();
        interCommPort = it->at("inter_comm_port").template get<std::string>();
        if (!IsValidPortString(metricPort)) {
            LOG_E("[%s] [ControllerListener] Got invalid metric port while adding instance.",
                GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
            return -1;
        }
        if (Configure::Singleton()->schedulerConfig["deploy_mode"] == "pd_disaggregation_single_container"
            && !IsValidPortString(interCommPort)) {
            LOG_E("[%s] [ControllerListener] The provided inter-communication port is invalid, received '%s'.",
                GetErrorCode(ErrorType::WARNING, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
                interCommPort.c_str());
            return -1;
        }
        newInstance.id = id;
        if (ParseInstance(it, newInstance, ip, port, modelName) != 0) {
            return -1;
        }
    } catch (const std::exception &e) {
        LOG_E("[%s] [ControllerListener] Exception occurred while adding instance, error is %s\n",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(), e.what());
        return -1;
    }
    HttpParam httpParam(ip, port, metricPort, interCommPort);
    if (Configure::Singleton()->CheckBackup()) {
        return AddIns(id, httpParam, newInstance, modelName, it);
    } else {
        return AddInsWithoutBackup(it, newInstance, httpParam);
    }
}

int32_t ControllerListener::AddInsWithoutBackup(const nlohmann::json::const_iterator &it,
    MINDIE::MS::DIGSInstanceStaticInfo newInstance, HttpParam &httpParam)
{
    uint64_t id = it->at("id").template get<uint64_t>();
    std::string ip;
    std::string port;
    std::string modelName;
    if (ParseInstance(it, newInstance, ip, port, modelName) != 0) {
        LOG_E("[ControllerListener] Failed to parse instance info for id %lu in AddInsWithoutBackup.", id);
        return -1;
    }
    if (newInstance.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE) {
        for (size_t i = 0; i < Configure::Singleton()->exceptionConfig.maxRetry + 1; ++i) {
            if (requestRepeater->LinkWithDNode(ip, port) != 0) {
                LOG_D("[ControllerListener] AddInsWithoutBackup Successfully add link with decode node at %s:%s.",
                    ip.c_str(), port.c_str());
                continue;
            }
            LOG_I("[ControllerListener] Successfully add link with decode node at %s:%s.",
                ip.c_str(), port.c_str());
            return AddIns(id, httpParam, newInstance, modelName, it);
        }
        LOG_E("[%s] [ControllerListener] Failed to add link with decode node at %s:%s.",
            GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            ip.c_str(), port.c_str());
        return -1;
    } else {
        return AddIns(id, httpParam, newInstance, modelName, it);
    }
}

int32_t ControllerListener::Update(const nlohmann::json::const_iterator &it)
{
    try {
        MINDIE::MS::DIGSInstanceDynamicInfo updateStatus;
        auto id = it->at("id").template get<uint64_t>();
        LOG_D("[ControllerListener] Updating instance with ID %lu", id);
        auto nowRole = instancesRecord->GetRole(id);
        auto newRole = it->at("static_info").at("role").template get<MINDIE::MS::DIGSInstanceRole>();
        if (newRole != nowRole) { // 身份切换
            Remove({id});
            return Add(it);
        }
        updateStatus.id = id;
        auto &dynamicInfo = it->at("dynamic_info");
        updateStatus.totalSlotsNum = it->at("static_info").at("total_slots_num").template get<uint32_t>();
        updateStatus.totalBlockNum = it->at("static_info").at("total_block_num").template get<uint32_t>();
        updateStatus.availSlotsNum = dynamicInfo.at("avail_slots_num").template get<uint32_t>();
        updateStatus.availBlockNum = dynamicInfo.at("avail_block_num").template get<uint32_t>();
        LOG_D("[ControllerListener] Parsed instance %lu resource info - total_slots:%u, total_blocks:%u, "
              "avail_slots:%u, avail_blocks:%u", id, updateStatus.totalSlotsNum,
              updateStatus.totalBlockNum, updateStatus.availSlotsNum, updateStatus.availBlockNum);
        if (dynamicInfo.contains("peers")) {
            updateStatus.peers = dynamicInfo.at("peers").template get<std::vector<uint64_t>>();
        }
        if (dynamicInfo.contains("prefix_hash")) {
            updateStatus.prefixHash = dynamicInfo.at("prefix_hash").template get<std::vector<uint64_t>>();
        }
        scheduler->UpdateInstance({updateStatus});
        return 0;
    } catch (const std::exception &e) {
        LOG_E("[%s] [ControllerListener] Failed to update instance, exception is %s\n",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            e.what());
        return -1;
    }
}

void ControllerListener::Remove(const std::vector<uint64_t> &removeVec)
{
    if (removeVec.empty()) {
        return;
    }
    scheduler->RemoveInstance(removeVec);
    for (auto id : removeVec) {
        auto requests = reqManage->GetInsRequests(id);
        for (std::string reqId : requests) {
            LOG_I("[ControllerListener] Instance has disappear, update request %s to EXCEPTION state", reqId.c_str());
            reqManage->UpdateState(reqId, ReqState::EXCEPTION);
        }
        auto ip = instancesRecord->GetIp(id);
        auto port = instancesRecord->GetPort(id);
        instancesRecord->RemoveInstance(id);
        LOG_I("[ControllerListener] Removed instance %lu at %s:%s from cluster.", id, ip.c_str(), port.c_str());
    }
}

/**
* @brief Parse query parameters from a URL.
*
* Extracts query parameters (key-value pairs) from the query part of a URL
* Parsing stops and returns the already parsed query parameters when the maximum allowed
* parameter count (MAX_ALLOWED_PARAM_NUM_IN_SINGLE_URL) is exceeded.
*
* Supported URL format:
*   protocol://hostname[:port]/path[;parameters][?query]#fragment
*
* @param url The input URL string.
* @return Parsed query parameters as <key, value> pairs.
*/
std::vector<std::pair<std::string, std::string>> ControllerListener::ParseQuery(const std::string& url) const
{
    std::vector<std::pair<std::string, std::string>> queryParams;
    std::string::size_type start = url.find('?');
    if (start != std::string::npos) {
        start++;
        std::string::size_type end = url.find('#', start);
        if (end == std::string::npos) {
            end = url.length();
        }
        std::string queryString = url.substr(start, end - start);
        std::string::size_type pos = 0;
        while (pos < queryString.length()) {
            /*
            Limit the number of parsed query parameters
            to mitigate potential DoS attacks caused by excessively large URLs.
            */
            if (queryParams.size() >= MAX_ALLOWED_PARAM_NUM_IN_SINGLE_URL) {
                LOG_W("[%s] [ControllerListener] The number of query parameters in the URL exceeds %u "
 	                "while executing ParseQuery; parsing has been truncated.",
                    GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
                    MAX_ALLOWED_PARAM_NUM_IN_SINGLE_URL);
                return queryParams;
            }

            std::string::size_type separator = queryString.find('&', pos);
            if (separator == std::string::npos) {
                separator = queryString.length();
            }
            std::string keyValue = queryString.substr(pos, separator - pos);
            std::string::size_type equals = keyValue.find('=');
            if (equals != std::string::npos) {
                std::string key = keyValue.substr(0, equals);
                std::string value = keyValue.substr(equals + 1);
                queryParams.emplace_back(std::make_pair(key, value));
            }
            pos = separator + 1;
        }
    }
    return queryParams;
}

void ControllerListener::StartUpProbeHandler(std::shared_ptr<ServerConnection> connection) const
{
    ServerRes res;
    connection->SendRes(res);
}

void ControllerListener::ReadinessProbeHandler(std::shared_ptr<ServerConnection> connection)
{
    ServerRes res;
    if (!instancesRecord->IsAvailable() || !(dataReady != nullptr && dataReady->load())) {
        res.state = boost::beast::http::status::service_unavailable;
    }
    connection->SendRes(res);
}

void ControllerListener::CoordinatorReadinessProbeHandler(std::shared_ptr<ServerConnection> connection)
{
    ServerRes res;
    bool isUnavailable = !instancesRecord->IsAvailable() || !(dataReady != nullptr && dataReady->load());
    if (Configure::Singleton()->CheckBackup()) {
        // 开启主备,需检测是否是master节点
        isUnavailable = isUnavailable || !Configure::Singleton()->IsMaster();
    }
    if (isUnavailable) {
        res.state = boost::beast::http::status::service_unavailable;
    }
    connection->SendRes(res);
}

void ControllerListener::HealthProbeHandler(std::shared_ptr<ServerConnection> connection) const
{
    LOG_D("[ControllerListener] Coordinator is healthy. Start to send health probe response.");
    ServerRes res;
    connection->SendRes(res);
}

void ControllerListener::TritonModelsReadyHandler(std::shared_ptr<ServerConnection> connection)
{
    ServerRes res;
    if (!instancesRecord->IsAvailable() || !(dataReady != nullptr && dataReady->load())) {
        res.state = boost::beast::http::status::service_unavailable;
    }
    auto &req = connection->GetReq();
    auto url = req.target();
    auto pos1 = url.find("models/");
    if (pos1 == std::string::npos) {
        res.state = boost::beast::http::status::service_unavailable;
    } else {
        auto subUrl = url.substr(pos1 + 7); // 7是"models/"的字节数
        auto pos2 = subUrl.find("/");
        if (pos2 == std::string::npos) {
            res.state = boost::beast::http::status::service_unavailable;
        } else {
            auto modelName = subUrl.substr(0, pos2);
            if (!instancesRecord->HasModelName(modelName)) {
                res.state = boost::beast::http::status::service_unavailable;
            }
        }
    }
    connection->SendRes(res);
}

static void InstancesQueryTasksHandlerError(std::shared_ptr<ServerConnection> connection, ServerRes res,
    boost::beast::http::status state)
{
    nlohmann::json resJson;
    resJson["is_end"] = true;
    res.body = resJson.dump();
    res.state = state;
    connection->SendRes(res);
}

// 用于查询P-D之间有没有存在请求正在处理
void ControllerListener::InstancesQueryTasksHandler(std::shared_ptr<ServerConnection> connection)
{
    ServerRes res;
    if (!instancesRecord->IsAvailable()) {
        return InstancesQueryTasksHandlerError(connection, res, boost::beast::http::status::ok);
    }
    auto &req = connection->GetReq();
    auto body = boost::beast::buffers_to_string(req.body().data());
    uint64_t pId;
    uint64_t dId;
    MINDIE::MS::DIGSRoleChangeType roleChangeType;
    try {
        auto bodyJson = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        pId = bodyJson.at("p_id").template get<uint64_t>();
        dId = bodyJson.at("d_id").template get<uint64_t>();
        roleChangeType = bodyJson.at("role_change_type").template get<MINDIE::MS::DIGSRoleChangeType>();
        // 查询任务数量时，对于Flex做D的场景，需要修改其ID为特殊值
        instancesRecord->ProcTaskQuaryDInstanceIdUnderFlexSituation(dId);
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [Configure] Failed to read controller instance query request: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            body.c_str(), e.what());
        return InstancesQueryTasksHandlerError(connection, res, boost::beast::http::status::bad_request);
    } catch (const std::exception &e) {
        LOG_E("[%s] [ControllerListener] Handle instance query task failed, exception is %s",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str(),
            e.what());
        return InstancesQueryTasksHandlerError(connection, res, boost::beast::http::status::bad_request);
    }
    InstancesQueryTasksProc(connection, pId, dId, roleChangeType);
}

void ControllerListener::InstancesQueryTasksProc(std::shared_ptr<ServerConnection> connection, uint64_t pId,
                                                 uint64_t dId, MINDIE::MS::DIGSRoleChangeType roleChangeType)
{
    ServerRes res;
    nlohmann::json resJson;
    auto pTasksSize = instancesRecord->GetTask(pId);
    auto dTasksSize = instancesRecord->GetTask(dId);
    if (pTasksSize == -1 || dTasksSize == -1) {  // 任一实例不存在了
        return InstancesQueryTasksHandlerError(connection, res, boost::beast::http::status::ok);
    }
    uint64_t tasksSizeOfInsToChangeRole;
    uint64_t idOfInsToChangRole;
    uint64_t routeIndex;
    std::unordered_set<std::string> tasksOfPeerIns;
    if (roleChangeType == MINDIE::MS::DIGSRoleChangeType::DIGS_ROLE_CHANGE_P2D) {
        tasksSizeOfInsToChangeRole = static_cast<uint64_t>(pTasksSize); // 为负的情况已经在前面保护
        tasksOfPeerIns = instancesRecord->GetTasksById(dId);
        idOfInsToChangRole = pId;
        routeIndex = 0;
    } else if (roleChangeType == MINDIE::MS::DIGSRoleChangeType::DIGS_ROLE_CHANGE_D2P) {
        tasksSizeOfInsToChangeRole = static_cast<uint64_t>(dTasksSize); // 为负的情况已经在前面保护
        tasksOfPeerIns = instancesRecord->GetTasksById(pId);
        idOfInsToChangRole = dId;
        routeIndex = 1;
    } else {
        LOG_E("ControllerListener] InstancesQueryTasksHandler: Invalid role change type %s.", roleChangeType);
        return InstancesQueryTasksHandlerError(connection, res, boost::beast::http::status::bad_request);
    }
    if (tasksSizeOfInsToChangeRole > 0) { // 待变换身份实例上还有任务，那PD之间的任务肯定没结束，直接返回false
        resJson["is_end"] = false;
    } else { // 待变换身份实例上没有任务了，需要确认对端是否还有未处理完的经过该实例的任务
        resJson["is_end"] = InstancesQueryTasksCheck(tasksOfPeerIns, idOfInsToChangRole, routeIndex);
    }
    res.body = resJson.dump();
    connection->SendRes(res);
}

bool ControllerListener::InstancesQueryTasksCheck(std::unordered_set<std::string> tasksOfPeerIns,
                                                  uint64_t idOfInsToChangRole, uint64_t routeIndex)
{
    if (routeIndex > 1) {
        return false;
    }
    if (tasksOfPeerIns.empty()) {
        return true;
    }
    for (auto &reqId : std::as_const(tasksOfPeerIns)) {  // 遍历待转换实例对端上面正在处理的所有请求
        auto reqInfo = reqManage->GetReqInfo(reqId);
        if (reqInfo == nullptr) {
            continue;
        }
        auto route = reqInfo->GetRoute();
        if (route[routeIndex] != idOfInsToChangRole) {  // 判断请求的路径是否经过待转换身份实例
            continue;                                   // 请求没有经过待转换身份实例，忽略
        }
        if (reqInfo->HasState(ReqState::FINISH) || reqInfo->HasState(ReqState::EXCEPTION) ||
            reqInfo->HasState(ReqState::TIMEOUT)) {
            continue; // 请求已经结束了，或者已经异常，超时，都认为这个请求在PD之间已经结束了
        }
        return false; // 不属于上面几种情况，说明请求正在处理，没必要再遍历了，PD之间还有任务
    }
    return true; // 全部遍历完，没有触发444行的false，说明PD之间没有任务了
}

void ControllerListener::CheckAndHandleCoordinatorStateAlarm()
{
    bool isInstanceAvailable = instancesRecord->IsAvailable();
    bool isCoordServerReady = (dataReady != nullptr && dataReady->load());
    bool inInstanceMissAlarm = inCoordReadyAlarmStateInstanceMiss.load(std::memory_order_relaxed);
    bool inCoordExceptionAlarm = inCoordReadyAlarmStateCoordException.load(std::memory_order_relaxed);

    if (!isInstanceAvailable && !inInstanceMissAlarm) {
        inCoordReadyAlarmStateInstanceMiss.store(true, std::memory_order_release);
        std::string alarmMsg = AlarmRequestHandler::GetInstance()->FillCoordinatorExceptionAlarmInfo(
            AlarmCategory::ALARM_CATEGORY_ALARM, CoordinatorExceptionReason::INSTANCE_MISSING);
        if (AlarmRequestHandler::GetInstance()->SendAlarmToAlarmManager(alarmMsg) != 0) {
            LOG_W("[%s] [ControllerListener] Coordinator Exception Alarm failed to write shared Memory.",
                GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        }
    } else if (isInstanceAvailable && inInstanceMissAlarm) {
        inCoordReadyAlarmStateInstanceMiss.store(false, std::memory_order_release);
        std::string alarmMsg = AlarmRequestHandler::GetInstance()->FillCoordinatorExceptionAlarmInfo(
            AlarmCategory::ALARM_CATEGORY_CLEAR, CoordinatorExceptionReason::INSTANCE_MISSING);
        if (AlarmRequestHandler::GetInstance()->SendAlarmToAlarmManager(alarmMsg) != 0) {
            LOG_W("[%s] [ControllerListener] Coordinator Exception Alarm failed to write shared Memory.",
                GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        }
    }
    if (!isCoordServerReady && !inCoordExceptionAlarm) {
        inCoordReadyAlarmStateCoordException.store(true, std::memory_order_release);
        std::string alarmMsg = AlarmRequestHandler::GetInstance()->FillCoordinatorExceptionAlarmInfo(
            AlarmCategory::ALARM_CATEGORY_ALARM, CoordinatorExceptionReason::COORDINATOR_EXCEPTION);
        if (AlarmRequestHandler::GetInstance()->SendAlarmToAlarmManager(alarmMsg) != 0) {
            LOG_W("[%s] [ControllerListener] Coordinator Exception Alarm failed to write shared Memory.",
                GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        }
    } else if (isCoordServerReady && inCoordExceptionAlarm) {
        inCoordReadyAlarmStateCoordException.store(false, std::memory_order_release);
        std::string alarmMsg = AlarmRequestHandler::GetInstance()->FillCoordinatorExceptionAlarmInfo(
            AlarmCategory::ALARM_CATEGORY_CLEAR, CoordinatorExceptionReason::COORDINATOR_EXCEPTION);
        if (AlarmRequestHandler::GetInstance()->SendAlarmToAlarmManager(alarmMsg) != 0) {
            LOG_W("[%s] [ControllerListener] Coordinator Exception Alarm failed to write shared Memory.",
                GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONTROLLER_LISTENER).c_str());
        }
    }
}
}