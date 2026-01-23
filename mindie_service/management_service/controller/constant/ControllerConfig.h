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
#ifndef MINDIE_MS_CONTROLLER_CONFIG
#define MINDIE_MS_CONTROLLER_CONFIG

#include <string>
#include <cstdint>
#include <vector>
#include <unordered_map>
#include <atomic>
#include "nlohmann/json.hpp"
#include "JsonFileLoader.h"
#include "ControllerConstant.h"
#include "ConfigParams.h"
namespace MINDIE::MS {

struct ProcessManagerConfig {
    bool toFile = false;
    std::string filePath {};
};

struct ClusterStatusConfig {
    bool toFile = false;
    std::string filePath {};
};

struct CtrlerBackUpConfig {
    bool funSw = false;
    std::string serverDns {};
    uint32_t serverPort = 0;
};

class ControllerConfig {
public:
    static ControllerConfig *GetInstance()
    {
        static ControllerConfig instance;
        return &instance;
    }
    int32_t Init();
    bool IsValidPRateAndDRate(size_t pRate, size_t dRate) const;
    DeployMode GetDeployMode() const;
    std::string GetPodIP() const;
    std::string GetCCAEIP() const;
    int64_t GetPort() const;
    int64_t GetCCAEPort() const;
    size_t GetModelNum() const;
    std::string GetModelID(size_t it) const;
    std::string GetGlobalRankTablePath() const;
    std::string GetMindIEServerPort() const;
    std::string GetMindIEServerControlPort() const;
    std::string GetMindIEServerMetricPort() const;
    const ProcessManagerConfig& GetProcessManagerConfig();
    const ClusterStatusConfig& GetClusterStatusConfig();
    const CtrlerBackUpConfig& GetCtrlBackUpConfig();
    std::string GetCoordinatorPort() const;
    std::string GetCoordinatorExternalPort() const;
    std::string GetControllerAlarmPort() const;
    std::string GetNodeManagerPort() const;
    std::string GetPDRatio() const;
    std::string GetModelConfigFilePath() const;
    std::string GetMachineConfigFilePath() const;
    std::string GetDIGSPrefillSLO() const;
    std::string GetDIGSDecodeSLO() const;
    std::string GetDIGSTimePeriod() const;
    bool GetNPURecoveryEnableConfig() const;
    bool GetDIGSIsAutoSwitching() const;
    bool GetDIGSIsHeterogeneous() const;
    bool GetDIGSIsSingleContainer() const;
    bool GetPIsDistribute() const;
    bool GetDIsDistribute() const;
    std::string GetModelType() const;
    uint32_t GetClusterPort() const;
    std::string GetDIGSTransferType() const;
    std::string GetDIGSPP() const;
    std::string GetRoleDecisionMethod() const;
    bool IsMultiNodeMode() const;
    uint32_t GetPNodeNum() const;
    uint32_t GetDNodeNum() const;
    uint32_t GetPTpSize() const;
    uint32_t GetDTpSize() const;
    uint32_t GetPDpSize() const;
    uint32_t GetDDpSize() const;
    uint32_t GetPSpSize() const;
    uint32_t GetDSpSize() const;
    uint32_t GetPCpSize() const;
    uint32_t GetDCpSize() const;
    uint32_t GetLimitOfNodesPerTypeInGroup() const;
    uint32_t GetHttpTimeoutSeconds() const;
    uint32_t GetHttpRetries() const;
    size_t GetDIGSRequestInputLength() const;
    size_t GetDIGSRequestOutputLength() const;
    uint32_t GetServerOnlineAttemptTimes() const;
    uint32_t GetServerOnlineWaitSeconds() const;
    uint32_t GetInitRoleAttemptTimes() const;
    uint32_t GetCheckRoleAttemptTimes() const;
    uint32_t GetCheckRoleWaitSeconds() const;
    uint32_t GetTasksEndWaitSeconds() const;
    uint32_t GetClusterSynchronizationSeconds() const;
    uint32_t GetRankTableDetectingSeconds() const;
    uint32_t GetDisappearedServerWaitingSeconds() const;

    std::string GetStaticElasticTemplatePath() const;
    void SetStaticElasticTemplatePath(std::string templatePath);

    const TlsItems& GetRequestCoordinatorTlsItems();
    const TlsItems& GetExternalCoordinatorTlsItems();
    const TlsItems& GetRequestServerTlsItems();
    const TlsItems& GetHttpServerTlsItems();
    const TlsItems& GetCCAETlsItems();
    const TlsItems& GetClusterTlsItems();
    const TlsItems& GetEtcdTlsItems();
    const TlsItems& GetAlarmTlsItems();
    bool GetCheckMountedFiles() const;
    bool GetHasFlex() const;
    void ParsePDRate(size_t &pRatio, size_t &dRatio) const;
    uint64_t GetInitialDpServerPort() const;
    MINDIE::MS::DIGSInstanceRole GetPDInitRole(int32_t deployMode) const;
    int32_t ValidateMultiNodeConfig() const;

    bool IsLeader() const; // 获取当前节点是否为leader
    void SetLeader(bool isLeader); // 设置当前节点是否为leader

    ControllerConfig(const ControllerConfig &obj) = delete;
    ControllerConfig &operator=(const ControllerConfig &obj) = delete;
    ControllerConfig(ControllerConfig &&obj) = delete;
    ControllerConfig &operator=(ControllerConfig &&obj) = delete;

#ifndef UT_FLAG
private:
#endif
    ControllerConfig() = default;
    ~ControllerConfig() = default;
    DeployMode mDeployMode = DeployMode::SINGLE_NODE;
    uint32_t mClusterPort = 8899;
    size_t mPRate = 1;
    size_t mDRate = 1;
    int64_t mPort = 1026;
    int64_t mCCAEPort = 0;
    ProcessManagerConfig mProcessManagerConfig;
    ClusterStatusConfig mClusterStatusConfig;
    CtrlerBackUpConfig mCtrlerBackUpConfig;
    uint32_t mServerOnlineAttemptTimes = 36;
    uint32_t mServerOnlineWaitSeconds = 5;
    uint32_t mInitRoleAttemptTimes = 5;
    uint32_t mCheckRoleAttemptTimes = 60;
    uint32_t mCheckRoleWaitSeconds = 5;
    uint32_t mTasksEndWaitSeconds = 300;
    uint32_t mHttpRetries = 1;
    uint32_t mHttpTimeoutSeconds = 10;
    size_t mDigsRequestInputLength = 3000;
    size_t mDigsRequestOutputLength = 200;
    uint32_t mClusterSynchronizationSeconds = 60;
    uint32_t mRankTableDetectingSeconds = 1;
    uint32_t mDisappearedServerWaitingSeconds = 120;
    uint32_t mAutoSwitchingSeconds = 86400;
    bool mIsAutoSwitching = false;
    bool mIsHeterogeneous = false;
    bool mAllowAllZeroIpListening = false;
    bool mIsSingleContainer = false;
    bool recoverySw = false;
    TlsItems mRequestCoordinatorTlsItems;
    TlsItems mRequestServerTlsItems;
    TlsItems mHttpServerTlsItems;
    TlsItems mExternalCoordinatorTlsItems;
    TlsItems mCCAETlsItems;
    TlsItems mClusterTlsItems;
    TlsItems mEtcdTlsItems;
    TlsItems mAlarmTlsItems;
    bool mCheckMountedFiles = true;
    bool mHasFlex = false;
    bool enablePDistribute = false;
    bool enableDDistribute = false;
    mutable bool mIsMultiNodeMode = false;
    uint32_t mPNodeSize = 1;
    uint32_t mDNodeSize = 1;
    uint32_t mPTpSize = 1;
    uint32_t mDTpSize = 1;
    uint32_t mPDpSize = 1;
    uint32_t mDDpSize = 1;
    uint32_t mPSpSize = 1;
    uint32_t mDSpSize = 1;
    uint32_t mPCpSize = 1;
    uint32_t mDCpSize = 1;
    uint64_t mInitialDpServerPort = 10000;
    std::string mStaticElasticTemplatePath;
    std::unordered_map<std::string, std::string> mNameToStringConfig {};
    std::vector<std::string> mModelIDVec {};
    std::atomic<bool> mIsLeader = {false}; // controller 是否是leader
    std::string GetStringConfig(const std::string &config) const;
    std::string GetConfigByEnvThenJson(const std::string &envParam, const std::string &configParam) const;
    void InitNameToStringConfig(const nlohmann::json &rawConfig);
    void InitTlsItems(const nlohmann::json &rawConfig,
                      const std::vector<std::pair<std::string, std::string>> &tlsConfigPairs);
    void InitProcessManagerConfig(const nlohmann::json &rawConfig);
    void InitClusterStatusConfig(const nlohmann::json &rawConfig);
    void InitCtrlBackupConfig(const nlohmann::json &rawConfig);
    void InitMultiNodeConfig(const nlohmann::json &rawConfig);
    void InitModelIDConfig(const nlohmann::json &rawConfig);
    void InitConfig(const nlohmann::json &rawConfig,
        const std::vector<std::pair<std::string, std::string>> &tlsConfigPairs);
    void InitCCAE(const nlohmann::json &rawConfig);
    void InitAlarm(const nlohmann::json &rawConfig);
    void InitClusterD(const nlohmann::json &rawConfig);
    void InitNPURecoveryEnable(const nlohmann::json &rawConfig);
    int32_t InitLog(const nlohmann::json &rawConfig, const std::string& configFilePath) const;
    bool IsValidDefaultPDRateConfig(const nlohmann::json &config) const;
    bool IsConfigJsonValid(const nlohmann::json &config,
        const std::vector<std::pair<std::string, std::string>> &tlsConfigPairs) const;
    bool IsConfigJsonStringValid(const nlohmann::json &config,
        const std::vector<std::pair<std::string, std::string>> &tlsConfigPairs) const;
    bool IsNodeConfigJsonIntValid(const nlohmann::json &config, std::string key) const;
    bool IsMultiNodeInferConfigValid(const nlohmann::json &config) const;
    bool IsAutoSwitchingValid(const nlohmann::json &config) const;
    size_t GetPRate() const;
    size_t GetDRate() const;
    TlsItems GetInitTlsItems(const nlohmann::json &rawConfig, const std::string &tlsEnableName,
                             const std::string &certName) const;
    std::vector<std::pair<std::string, std::string>> GenerateTlsConfigPairs() const;
};
}
#endif // MINDIE_MS_CONTROLLER_CONFIG