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
#include <thread>
#include <string>
#include <optional>
#include "Logger.h"
#include "Util.h"
#include "ConfigParams.h"
#include "ControllerConfig.h"
#include "RankTableLoader.h"
#include "FaultManager.h"
#include "AlarmRequestHandler.h"
#include "AlarmManager.h"
#include "AlarmConfig.h"
#include "SecurityUtils.h"
#include "GrpcClusterClient.h"
#include "NPURecoveryManager.h"
#include "ClusterClient.h"

namespace MINDIE::MS {
constexpr uint64_t NOT_FOUNDED_NODEID = std::numeric_limits<uint64_t>::max();
void PrintRankTable(const std::string &response)
{
    uint32_t maxLoggerStrLen = Logger::Singleton()->GetMaxLogStrSize();
    uint32_t msgLen = response.length() > LOG_MSG_STR_SIZE_MAX ? LOG_MSG_STR_SIZE_MAX : response.length(); // 限制最大打印上限
    LOG_I("[ClusterClient] Rank table length is %d, Logger max log str is %d.", msgLen, maxLoggerStrLen);
    uint32_t safetyMargin = LOG_MSG_STR_SIZE_MIN + 50; // 50: 预留的安全余量
    const std::string rankTablePrefix = "[ClusterClient] Rank table info ";
    uint32_t prefixLen = rankTablePrefix.length();
    int32_t maxContentLen = maxLoggerStrLen - prefixLen - safetyMargin;
    if (maxContentLen <= 0) {
        LOG_W("[PrintRankTable] Logger maxLogStrSize is too small to print rank table");
        return;
    }
    if (msgLen <= static_cast<uint32_t>(maxContentLen)) {
        std::string allContent = rankTablePrefix + response;
        LOG_I("%s.", allContent.c_str());
    } else {
        uint32_t start = 0;
        int segment = 1;
        while (start < msgLen) {
            uint32_t end = start + maxContentLen;
            if (end > msgLen) {
                end = msgLen;
            }
            std::string segmentContent = rankTablePrefix + "(part " + std::to_string(segment) + ") " +
                response.substr(start, end - start);
            LOG_I("%s", segmentContent.c_str());
            start = end;
            segment++;
        }
    }
}

void SaveRankTableCallback(std::string &response)
{
    PrintRankTable(response);
    std::unique_ptr<RankTableLoader> rankTable = std::make_unique<RankTableLoader>();
    if (rankTable->WriteRankTable(response) != 0) {
        LOG_E("[%s] [ClusterClient] Write rank table failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
    } else {
        LOG_I("[ClusterClient] Write rank table success.");
    }
}

void DealwithFaultMsgCallback(fault::FaultMsgSignal &response)
{
    // 验证信号类型
    std::string signalType = response.signaltype();
    if (!IsValidFaultSignalType(signalType)) {
        LOG_E("[%s] [ClusterClient] Invalid fault signal type received: %s",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str(),
            signalType.c_str());
        return;
    }
    
    if (signalType == "normal") {
        LOG_I("[ClusterClient] read fault message signalType is %s.", signalType.c_str());
        return;
    }
    auto nodeFaultInfo = response.nodefaultinfo();
    for (auto it = nodeFaultInfo.begin(); it != nodeFaultInfo.end(); ++it) {
        ClusterClient::GetInstance()->AddFaultNodeByNodeIp(*it);
    }
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(response);
}

namespace {
    inline const std::string& ErrCode()
    {
        static const std::string kEc =
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER);
        return kEc;
    }

    inline void LogInvalid(const char* what, const std::string& v)
    {
        LOG_E("[%s] [ClusterClient] Invalid %s: %s",
              ErrCode().c_str(), what, v.c_str());
    }
    
    inline bool CheckSignalType(const std::string& t)
    {
        if (!IsValidFaultSignalType(t)) {
            LogInvalid("fault signal type", t);
            return false;
        }
        return true;
    }
    inline bool CheckNodeName(const std::string& n)
    {
        if (!IsValidNodeName(n)) {
            LogInvalid("node name", n);
            return false;
        }
        return true;
    }
    inline bool CheckFaultLevel(const std::string& lv)
    {
        if (!IsValidFaultLevel(lv)) {
            LogInvalid("fault level", lv);
            return false;
        }
        return true;
    }
    inline std::optional<std::string> GetSanitizedIp(const std::string& raw)
    {
        std::string v = ValidateAndSanitizeIP(raw);
        if (v.empty()) {
            LogInvalid("node IP", raw);
            return std::nullopt;
        }
        return v;
    }
    inline std::optional<std::string> GetSanitizedDeviceId(const std::string& raw)
    {
        std::string v = ValidateAndSanitizeDeviceId(raw);
        if (v.empty()) {
            LogInvalid("device ID", raw);
            return std::nullopt;
        }
        return v;
    }
    } // namespace
    

ClusterClient* ClusterClient::GetInstance()
{
    static ClusterClient instance;
    return &instance;
}

int32_t ClusterClient::Init()
{
    if (mIsInit.load()) {
        return 0;
    }
    auto jobId = std::getenv("MINDX_TASK_ID");
    if (jobId == nullptr || jobId[0] == '\0') {
        LOG_E("[%s] [ClusterClient] Get job id failed or job id is null.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    mTlsConfig = ControllerConfig::GetInstance()->GetClusterTlsItems();
    mRequest.set_jobid(jobId);
    mRequest.set_role("mindie-ms-controller");

    mFaultInfo.set_jobid(jobId);
    mFaultInfo.set_role("mindie-ms-controller");
    mIsInit.store(true);
    return 0;
}

void ClusterClient::AddFaultNodeByNodeIp(fault::NodeFaultInfo& nodeInfo)
{
    // 验证故障级别
    std::string faultLevel = nodeInfo.faultlevel();
    if (!IsValidFaultLevel(faultLevel)) {
        LOG_E("[%s] [AddFaultNodeByNodeIp] Invalid fault level: %s",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str(), faultLevel.c_str());
        return;
    }
    
    // 验证节点名称
    std::string nodeName = nodeInfo.nodename();
    if (!IsValidNodeName(nodeName)) {
        LOG_E("[%s] [AddFaultNodeByNodeIp] Invalid node name: %s",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str(), nodeName.c_str());
        return;
    }

    std::vector<uint64_t> nodeIds = mNodeStatus->GetAllNodeIds();
    if (nodeIds.empty()) {
        LOG_W("[AddFaultNodeByNodeIp] Find no node IDs in mNodeStatus.");
    }
    bool isFoundNode = false;
    for (auto id : std::as_const(nodeIds)) {
        auto node = mNodeStatus->GetNode(id);
        if (node == nullptr) {
            LOG_I("[AddFaultNodeByNodeIp] Ignore node id: %zu.", id);
            continue;
        }
        for (auto &serverInfo : node->serverInfoList) {
            if (serverInfo.hostId != nodeInfo.nodeip()) {
                continue;
            }
            isFoundNode = true;
            LOG_I("[AddFaultNodeByNodeIp] Find one node id: %zu, ip:%s", id, nodeInfo.nodeip().c_str());
            if (nodeInfo.faultlevel() == "UnHealthy" &&
                NPURecoveryManager::GetInstance()->HasCriticalFaultLevel(nodeInfo)) {
                    FaultManager::GetInstance()->RecordHardwareFaultyNode(id, HardwareFaultType::UNHEALTHY);
                    break;
            } else if (nodeInfo.faultlevel() == "SubHealthy") {
                FaultManager::GetInstance()->RecordHardwareFaultyNode(id, HardwareFaultType::SUBHEALTHY);
                break;
            }
            LOG_W("[%s] [AddFaultNodeByNodeIp] Get unknown fault level: %s",
                GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str(),
                nodeInfo.faultlevel().c_str());
        }
    }
    if (!isFoundNode && nodeInfo.faultlevel() == "UnHealthy" &&
        NPURecoveryManager::GetInstance()->HasCriticalFaultLevel(nodeInfo)) {
        LOG_W("[AddFaultNodeByNodeIp] No node found with IP: %s, just record node id with MAX_UINT64_VALUE.",
              nodeInfo.nodeip().c_str());
        FaultManager::GetInstance()->RecordHardwareFaultyNode(NOT_FOUNDED_NODEID, HardwareFaultType::UNHEALTHY);
    }
}

int32_t ClusterClient::Register()
{
    if (Init() != 0) {
        LOG_E("[%s] [ClusterClient] client init failed, cannot register.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    if (mIsRegister.load()) {
        return 0;
    }
    auto clusterIp = std::getenv("MINDX_SERVER_IP");
    auto clusterPort = ControllerConfig::GetInstance()->GetClusterPort();
    if (clusterIp == nullptr || !IsValidIp(std::string(clusterIp))) {
        LOG_E("[%s] [ClusterClient] Get cluster IP failed or cluster IP is invalid.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    LOG_I("[ClusterClient] get ClusterDomain %s, ClusterPort %d success.",
        clusterIp, clusterPort);
    auto serverAddr = FormatHostPort(clusterIp, clusterPort);
    mGrpcChannel = CreateGrpcChannel(serverAddr, mTlsConfig);
    if (mGrpcChannel == nullptr) {
        LOG_E("[%s] [ClusterClient] Create channel failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    mIsRegister.store(true);
    return 0;
}

bool ClusterClient::RegisterRankTable(std::unique_ptr<config::Config::Stub>& configStub)
{
    if (!configStub) {
        LOG_E("[%s] [ClusterClient] Invalid null configStub in RegisterRankTable.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::CONTROLLER).c_str());
        return false;
    }
    uint32_t retryTimes = 0;
    bool rankTableIsRegister = false;
    while (!rankTableIsRegister) {
        ClientContext ctxRegister;
        Status statusRegister;
        config::Status responseStatus;
        statusRegister = configStub->Register(&ctxRegister, mRequest, &responseStatus);
        if (!statusRegister.ok()) {
            if (retryTimes++ > MAX_RETRY_TIMES) { // 重试次数超过最大次数，退出循环
                // 上报订阅失败
                ReportAlarm(ClusterConnectionReason::RANKTABLE_SUBSCRIBE_FAILED);
                LOG_E("[%s] [ClusterClient] RankTable Register failed, retry times is %d.",
                    GetErrorCode(ErrorType::UNAUTHENTICATED, ControllerFeature::CONTROLLER).c_str(), retryTimes);
                return false;
            }
            std::this_thread::sleep_for(std::chrono::seconds(1));
            continue;
        }
        rankTableIsRegister = true;
    }
    return true;
}

int32_t ClusterClient::SubscribeRankTable(const RankTableCallback &callback)
{
    if (Register() != 0) {
        LOG_E("[%s] [ClusterClient] Subscribe rank table failed.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        mIsRegister.store(false);
        return -1;
    }
    std::unique_ptr<config::Config::Stub> configStub = config::Config::NewStub(mGrpcChannel);

    // Register RankTable in MAX_RETRY_TIMES times
    if (!RegisterRankTable(configStub)) {
        return -1;
    }
    LOG_I("[ClusterClient] SubscribeRankTable register success.");
    // 如果之前上报过RankTable订阅失败告警，现在注册成功了，上报恢复
    if (mRankTableAlarmReported.load()) {
        ClearAlarm(ClusterConnectionReason::RANKTABLE_SUBSCRIBE_FAILED);
    }
    ClientContext ctxSubscribe;
    Status statusSubscribe;
    config::RankTableStream response;
    std::string rankTable;
    std::unique_ptr<grpc::ClientReader<config::RankTableStream>> reader(
        configStub->SubscribeRankTable(&ctxSubscribe, mRequest));
    // true：表示成功从流中读取到一条消息。false：表示流已结束或发生了错误。
    bool firstRead = true;
    while (reader->Read(&response)) {
        if (firstRead && mConnectionAlarmReported.load()) {
            ClearAlarm(ClusterConnectionReason::CONNECTION_INTERRUPTED);
            firstRead = false;
        }
        rankTable += response.ranktable();
        if (!rankTable.empty()) {
            callback(rankTable);
            // 第一次成功保存ClusterD传输过来的global ranktable后，置为true
            mWaitClusterDGRTSave->store(true);
            LOG_I("[ClusterClient] Subscribe rank table success.");
            rankTable.clear();
        }
    }
    statusSubscribe = reader->Finish();
    if (!statusSubscribe.ok()) {
        // 上报链接中断
        ReportAlarm(ClusterConnectionReason::CONNECTION_INTERRUPTED);
        LOG_E("[%s] [ClusterClient] Subscribe rank table failed, error is %s.",
            GetErrorCode(ErrorType::UNAUTHENTICATED, ControllerFeature::CONTROLLER).c_str(),
            statusSubscribe.error_message().c_str());
        return -1;
    }
    return 0;
}

void ClusterClient::PrintFaultSignal(const fault::FaultMsgSignal& signal) const
{
    if (!CheckSignalType(signal.signaltype())) {
        return;
    }

    LOG_I("[ClusterClient] Fault Message Signal:");
    LOG_I("UUID: %s, Job ID: %s, Signal Type: %s",
          signal.uuid().c_str(), signal.jobid().c_str(), signal.signaltype().c_str());

    for (const auto& nodeInfo : signal.nodefaultinfo()) {
        auto nodeIp = GetSanitizedIp(nodeInfo.nodeip());
        if (!nodeIp) {
            continue;
        }
        if (!CheckNodeName(nodeInfo.nodename())) {
            continue;
        }
        if (!CheckFaultLevel(nodeInfo.faultlevel())) {
            continue;
        }

        LOG_I("[Node] %s (%s), SN: %s, Fault Level: %s",
              nodeInfo.nodename().c_str(), nodeIp->c_str(),
              nodeInfo.nodesn().c_str(), nodeInfo.faultlevel().c_str());

        for (const auto& device : nodeInfo.faultdevice()) {
            auto deviceId = GetSanitizedDeviceId(device.deviceid());
            if (!deviceId) {
                continue;
            }

            std::string codesStr   = "Codes:";
            std::string typesStr   = "Types:";
            std::string reasonsStr = "Reasons:";

            for (const auto& code : device.faultcodes()) {
                codesStr   += ", " + SanitizeStringForJson(code);
            }
            for (const auto& typ  : device.faulttype()) {
                typesStr   += ", " + SanitizeStringForJson(typ);
            }
            for (const auto& why  : device.faultreason()) {
                reasonsStr += ", " + SanitizeStringForJson(why);
            }

            LOG_I("[ClusterClient] Fault Message Device ID: %s, Type: %s, Level: %s, %s, %s, %s",
                  deviceId->c_str(), device.devicetype().c_str(), device.faultlevel().c_str(),
                  codesStr.c_str(), typesStr.c_str(), reasonsStr.c_str());
        }
    }
}

bool ClusterClient::RegisterFaultMsg(std::unique_ptr<fault::Fault::Stub>& faultStub)
{
    if (!faultStub) {
        LOG_E("[%s] [ClusterClient] Invalid null faultStub in RegisterFaultMsg.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, ControllerFeature::CONTROLLER).c_str());
        return false;
    }

    uint32_t retryTimes = 0;
    bool faultIsRegister = false;
    while (!faultIsRegister) {
        ClientContext ctxRegister;
        Status statusRegister;
        fault::Status responseStatus;
        statusRegister = faultStub->Register(&ctxRegister, mFaultInfo, &responseStatus);
        if (!statusRegister.ok()) {
            if (retryTimes++ > MAX_RETRY_TIMES) { // 重试次数超过最大次数，退出循环
                // 上报订阅失败
                ReportAlarm(ClusterConnectionReason::FAULT_SUBSCRIBE_FAILED);
                LOG_E("[%s] [ClusterClient] FaultMsg Register failed, retry times is %d.",
                    GetErrorCode(ErrorType::UNAUTHENTICATED, ControllerFeature::CONTROLLER).c_str(), retryTimes);
                return false;
            }
            std::this_thread::sleep_for(std::chrono::seconds(1));
            continue;
        }
        faultIsRegister = true;
    }
    return true;
}


int32_t ClusterClient::SubscribeFaultMsgSignal(const FaultMsgCallback &callback)
{
    if (Register() != 0) {
        LOG_E("[%s] [ClusterClient] Subscribe fault msg signal failed",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        mIsRegister.store(false);
        return -1;
    }
    std::unique_ptr<fault::Fault::Stub> faultStub = fault::Fault::NewStub(mGrpcChannel);

    // 注册服务
    if (!RegisterFaultMsg(faultStub)) {
        return -1;
    }
    LOG_I("[ClusterClient] SubscribeFaultMsgSignal register success.");
    // 如果之前上报过FaultMsg订阅失败告警，现在注册成功了，上报恢复
    if (mFaultMsgAlarmReported.load()) {
        ClearAlarm(ClusterConnectionReason::FAULT_SUBSCRIBE_FAILED);
    }
    ClientContext ctxSubscribe;
    Status statusSubscribe;
    // 读取错误信息
    fault::FaultMsgSignal response;
    std::unique_ptr<grpc::ClientReader<fault::FaultMsgSignal>> reader(
        faultStub->SubscribeFaultMsgSignal(&ctxSubscribe, mFaultInfo));
    bool firstRead = true;
    while (reader->Read(&response)) {
        if (firstRead && mConnectionAlarmReported.load()) {
            ClearAlarm(ClusterConnectionReason::CONNECTION_INTERRUPTED);
            firstRead = false;
        }
        PrintFaultSignal(response);
        callback(response);
        LOG_I("[ClusterClient] Subscribe fault msg success!");
    }
    statusSubscribe = reader->Finish();
    if (!statusSubscribe.ok()) {
        LOG_E("[%s] [ClusterClient] Subscribe fault msg signal failed, error is %s.",
            GetErrorCode(ErrorType::UNAUTHENTICATED, ControllerFeature::CONTROLLER).c_str(),
            statusSubscribe.error_message().c_str());
        return -1;
    }
    return 0;
}

void ClusterClient::CreatDealwithThread()
{
    if (mRankTableThread == nullptr) {
        mRankTableThread = std::make_unique<std::thread>([this]() {
            if (SubscribeRankTable(SaveRankTableCallback) != 0) {
                LOG_E("[%s] [ClusterClient] Subscribe rank table failed.",
                    GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
                sleep(1);
            }
            mRankTableStop.store(true);
        });
    }

    if (mFaultThread == nullptr) {
        mFaultThread = std::make_unique<std::thread>([this]() {
            if (SubscribeFaultMsgSignal(DealwithFaultMsgCallback) != 0) {
                LOG_E("[%s] [ClusterClient] Fault message dealwith failed.",
                    GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
                sleep(1);
            }
            mFaultStop.store(true);
        });
    }
}

int32_t ClusterClient::Start(std::shared_ptr<NodeStatus> nodeStatus)
{
    try {
        if (nodeStatus == nullptr) {
            LOG_E("[%s] [ClusterClient] Register failed, nodeStatus is nullptr.",
                GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str());
            return -1;
        }
        mNodeStatus = nodeStatus;
        mClientThread = std::make_unique<std::thread>([this]() {
            uint32_t retryTimes = 0;
            while (true) {
                if (!ControllerConfig::GetInstance()->IsLeader()) {
                    sleep(1);
                    continue;
                }
                if (retryTimes > REGISTER_MAX_RETRY_TIMES) {
                    // 上报建链异常告警--此告警不可恢复，因为这里退出后就会退出主线程
                    ReportAlarm(ClusterConnectionReason::REGISTER_FAILED);
                    LOG_E("[%s] [ClusterClient] Register Exceed max retry times.",
                        GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
                    break;
                }
                if (!mIsRegister.load() && Register() != 0) {
                    LOG_E("[%s] [ClusterClient] Register failed, retry times is %u.",
                        GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str(), retryTimes);
                    retryTimes++;
                    sleep(1);
                    continue;
                }

                retryTimes = 0;
                CreatDealwithThread();

                // 判断是否需要重启线程
                if (mFaultStop.load() && mFaultThread) {
                    mFaultThread->join(); // 等待线程结束
                    mFaultThread.reset(); // 重置线程指针
                    LOG_I("[ClusterClient] Fault thread restarted.");
                }

                if (mRankTableStop.load() && mRankTableThread) {
                    mRankTableThread->join(); // 等待线程结束
                    mRankTableThread.reset(); // 重置线程指针
                    LOG_I("[ClusterClient] RankTable thread restarted.");
                }
            }
        });
    } catch (const std::exception& e) {
        LOG_E("[%s] [ClusterClient] Failed to create main thread.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, ControllerFeature::CONTROLLER).c_str());
        return -1;
    }
    return 0;
}

void ClusterClient::ReportAlarm(ClusterConnectionReason reason)
{
    // 先检查是否已经上报过同类型告警，避免重复上报
    bool alreadyReported = false;
    switch (reason) {
        case ClusterConnectionReason::RANKTABLE_SUBSCRIBE_FAILED:
            alreadyReported = mRankTableAlarmReported.load();
            break;
        case ClusterConnectionReason::FAULT_SUBSCRIBE_FAILED:
            alreadyReported = mFaultMsgAlarmReported.load();
            break;
        case ClusterConnectionReason::CONNECTION_INTERRUPTED:
            alreadyReported = mConnectionAlarmReported.load();
            break;
        case ClusterConnectionReason::REGISTER_FAILED:
            // REGISTER_FAILED 告警是不可恢复的，每次都上报
            alreadyReported = false;
            break;
        default:
            LOG_E("[ClusterClient] Unknown connection reason: %d", static_cast<int>(reason));
            return;
    }
    
    if (alreadyReported) {
        LOG_I("[ClusterClient] Alarm already reported for reason: %d, skipping duplicate report.",
              static_cast<int>(reason));
        return;
    }
    
    // 上报告警
    std::string alarmMsg = AlarmRequestHandler::GetInstance()->FillClusterConnectionAlarmInfo(
        AlarmCategory::ALARM_CATEGORY_ALARM, reason);
    AlarmManager::GetInstance()->AlarmAdded(alarmMsg);
    
    // 根据不同的告警原因设置对应的标志位
    switch (reason) {
        case ClusterConnectionReason::RANKTABLE_SUBSCRIBE_FAILED:
            mRankTableAlarmReported.store(true);
            LOG_E("[ClusterClient] RankTable subscription alarm reported.");
            break;
        case ClusterConnectionReason::FAULT_SUBSCRIBE_FAILED:
            mFaultMsgAlarmReported.store(true);
            LOG_E("[ClusterClient] FaultMsg subscription alarm reported.");
            break;
        case ClusterConnectionReason::CONNECTION_INTERRUPTED:
            mConnectionAlarmReported.store(true);
            LOG_E("[ClusterClient] Connection interrupted alarm reported.");
            break;
        case ClusterConnectionReason::REGISTER_FAILED:
            LOG_E("[ClusterClient] Register failed alarm reported.");
            break;
        default:
            LOG_E("[ClusterClient] Unknown connection reason.");
            break;
    }
}

void ClusterClient::ClearAlarm(ClusterConnectionReason reason)
{
    std::string clearMsg = AlarmRequestHandler::GetInstance()->FillClusterConnectionAlarmInfo(
        AlarmCategory::ALARM_CATEGORY_CLEAR, reason);
    AlarmManager::GetInstance()->AlarmAdded(clearMsg);
    
    // 清除对应的标志位
    switch (reason) {
        case ClusterConnectionReason::RANKTABLE_SUBSCRIBE_FAILED:
            mRankTableAlarmReported.store(false);
            LOG_I("[ClusterClient] RankTable subscription alarm cleared.");
            break;
        case ClusterConnectionReason::FAULT_SUBSCRIBE_FAILED:
            mFaultMsgAlarmReported.store(false);
            LOG_I("[ClusterClient] FaultMsg subscription alarm cleared.");
            break;
        case ClusterConnectionReason::CONNECTION_INTERRUPTED:
            mConnectionAlarmReported.store(false);
            LOG_I("[ClusterClient] Connection interrupted alarm cleared.");
            break;
        default:
            LOG_E("[ClusterClient] Unknown connection reason.");
            break;
    }
}

#ifdef UT_FLAG
void ClusterClient::SetNodeStatus(std::shared_ptr<NodeStatus> mock) { mNodeStatus = mock; }
void ClusterClient::Reset()
{
    mIsRegister = false;
    mIsInit = false;
    if (mClientThread != nullptr) {
        mClientThread->join();
        mClientThread.reset();
    }
}
#endif // UT_FLAG
}
