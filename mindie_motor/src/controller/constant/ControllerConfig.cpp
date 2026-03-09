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
#include "ControllerConfig.h"
#include <algorithm>
#include <string>
#include <vector>
#include "nlohmann/json.hpp"
#include "Logger.h"
#include "LogLevelDynamicHandler.h"

namespace MINDIE::MS {
static int64_t g_portMax = 65535; // 65535 maximum prot id
static int64_t g_portMin = 1024; // 1024 minimum port id
static int64_t g_commonIntMin = 0;
static int64_t g_commonIntMax = 65535;
static int64_t g_commonTimePeriodMin = 1;
static int64_t g_commonAttemptMin = 1;
static int64_t g_digsIntMin = 1;
static int32_t g_digsTimePeriodMin = 3600; // 3600表示最小设置为1小时
static int32_t g_digsTimePeriodMax = 3600 * 24 * 15; // 最大设置为15天
static int64_t g_maxIntRate = 767;
static size_t g_maxRate = 767;
static size_t g_minRate = 0;
static size_t g_maxRateSum = 768;  // PD比例的和最大为768
static size_t g_maxTpSize = 16;
static size_t g_maxDpSize = 512;
static size_t g_maxSpSize = 16;
static size_t g_maxMultiNodeSize = 768;
static int32_t g_prefillServer = 2;
static int32_t g_decodeServer = 1;
constexpr int32_t MAX_ENV_LENGTH = 256;

static bool IsConfigJsonDigsIntValid(const nlohmann::json &config)
{
    if (!IsJsonIntValid(config, "digs_request_summary_input_length", g_digsIntMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "digs_request_summary_output_length", g_digsIntMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "digs_prefill_slo", g_digsIntMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "digs_decode_slo", g_digsIntMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "digs_pp", g_digsIntMin, g_commonIntMax)) {
        return false;
    }
    return true;
}

bool ControllerConfig::IsValidDefaultPDRateConfig(const nlohmann::json &config) const
{
    if (!IsJsonIntValid(config, "default_p_rate", g_commonIntMin, g_maxIntRate) ||
        !IsJsonIntValid(config, "default_d_rate", g_commonIntMin, g_maxIntRate)) {
        return false;
    }
    size_t pRate = config.at("default_p_rate").get<size_t>();
    size_t dRate = config.at("default_d_rate").get<size_t>();
    return IsValidPRateAndDRate(pRate, dRate);
}

bool ControllerConfig::IsValidPRateAndDRate(size_t pRate, size_t dRate) const
{
    if (pRate == g_minRate && dRate == g_minRate) {
        return true;
    }
    if ((pRate == g_minRate && dRate > g_minRate) || (pRate > g_minRate && dRate == g_minRate)) {
        LOG_E("[%s] [ControllerConfig] 'default_p_rate' %zu and 'default_d_rate' %zu "
              "should both be 0 or not 0.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str(), pRate, dRate);
        return false;
    }
    if ((pRate + dRate) > g_maxRateSum) {
        LOG_E("[%s] [ControllerConfig] The sum of 'default_p_rate' %zu and default_d_rate' %zu "
              "should be less than %zu.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str(), pRate, dRate, g_maxRateSum);
        return false;
    }
    return true;
}

bool ControllerConfig::IsNodeConfigJsonIntValid(const nlohmann::json &config, std::string key) const
{
    if (!IsJsonIntValid(config["multi_node_infer_config"][key], "node_machine_num", g_digsIntMin, g_maxMultiNodeSize)) {
        return false;
    }

    if (!IsJsonBoolValid(config["multi_node_infer_config"][key], "enable_dist_dp_server")) {
        return false;
    }

    if (!IsJsonIntValid(config["multi_node_infer_config"][key], "tp_size", g_digsIntMin, g_maxTpSize)) {
        return false;
    }

    if (!IsJsonIntValid(config["multi_node_infer_config"][key], "dp_size", g_digsIntMin, g_maxDpSize)) {
        return false;
    }
    if (!IsJsonIntValid(config["multi_node_infer_config"][key], "sp_size", g_digsIntMin, g_maxSpSize)) {
        return false;
    }
    return true;
}

static bool IsConfigJsonIntValid(const nlohmann::json &config)
{
    if (!IsJsonIntValid(config, "cluster_synchronization_seconds", g_commonTimePeriodMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "rank_table_detecting_seconds", g_commonTimePeriodMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "disappeared_server_waiting_seconds", g_commonTimePeriodMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "mindie_server_control_port", g_portMin, g_portMax) ||
        !IsJsonIntValid(config, "mindie_server_port", g_portMin, g_portMax) ||
        !IsJsonIntValid(config, "mindie_server_metric_port", g_portMin, g_portMax) ||
        !IsJsonIntValid(config, "mindie_ms_coordinator_port", g_portMin, g_portMax) ||
        !IsJsonIntValid(config, "node_manager_port", g_portMin, g_portMax) ||
        !IsJsonIntValid(config, "mindie_ms_coordinator_external_port", g_portMin, g_portMax) ||
        !IsJsonIntValid(config, "controller_alarm_port", g_portMin, g_portMax) ||
        !IsJsonIntValid(config, "initial_dist_server_port", g_portMin, g_portMax) ||
        !IsJsonObjValid(config, "http_server") ||
        !IsJsonIntValid(config["http_server"], "port", g_portMin, g_portMax) ||
        !IsJsonIntValid(config, "server_online_attempt_times", g_commonAttemptMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "server_online_wait_seconds", g_commonTimePeriodMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "init_role_attempt_times", g_commonAttemptMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "check_role_attempt_times", g_commonAttemptMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "check_role_wait_seconds", g_commonTimePeriodMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "http_timeout_seconds", g_commonTimePeriodMin, g_commonIntMax) ||
        !IsJsonIntValid(config, "http_retries", g_commonIntMin, g_commonIntMax) ||
        !IsConfigJsonDigsIntValid(config)) {
        return false;
    }
    return true;
}

static bool IsTlsConfigJsonStringValid(const nlohmann::json &config, const std::string &tlsEnableName,
    const std::string &certName)
{
    bool tlsEnable = config.at(tlsEnableName).get<bool>();
    if (!tlsEnable) {
        return true;
    }
    int tlsCrlNameMaxLen = 4096;
    if (!IsJsonObjValid(config, certName) || !IsJsonStringValid(config[certName], "ca_cert") ||
        !IsJsonStringValid(config[certName], "tls_cert") || !IsJsonStringValid(config[certName], "tls_key") ||
        !IsJsonStringValid(config[certName], "tls_passwd") ||
        !IsJsonStringValid(config[certName], "tls_crl", 0, tlsCrlNameMaxLen)) { // tls_crl有效长度为0~4096
        return false;
    }
    return true;
}

static bool IsProcessManagerValid(const nlohmann::json &config)
{
    if (!config["process_manager"].at("to_file").get<bool>()) {
        return true;
    }
    if (!IsJsonStringValid(config["process_manager"], "file_path")) {
        return false;
    }
    return true;
}

static bool IsClusterStatusValid(const nlohmann::json &config)
{
    if (!config["cluster_status"].at("to_file").get<bool>()) {
        return true;
    }
    if (!IsJsonStringValid(config["cluster_status"], "file_path")) {
        return false;
    }
    return true;
}

static bool IsHasFlexValid(const nlohmann::json &config)
{
    if (!config.contains("has_flex")) {
        return true;
    }
    try {
        std::ignore = config.at("has_flex").get<bool>();
        return true;
    } catch (const nlohmann::json::type_error& e) {
        return false;
    }
}

bool ControllerConfig::IsConfigJsonStringValid(const nlohmann::json &config,
    const std::vector<std::pair<std::string, std::string>> &tlsConfigPairs) const
{
    if (!IsJsonStringValid(config, "deploy_mode") ||
        !IsJsonStringValid(config, "global_rank_table_file_path") ||
        !IsJsonStringValid(config, "role_decision_methods") ||
        !IsJsonStringValid(config, "digs_model_config_path") ||
        !IsJsonStringValid(config, "digs_machine_config_path") || !IsJsonStringValid(config, "model_type") ||
        !IsJsonStringValid(config, "transfer_type")) {
        return false;
    }
    if (!IsJsonStringValid(config["http_server"], "ip") ||
        !IsValidIp(config["http_server"]["ip"], config.at("allow_all_zero_ip_listening").get<bool>())) {
        return false;
    }

    for (auto &pair : std::as_const(tlsConfigPairs)) {
        if (!IsTlsConfigJsonStringValid(config["tls_config"], pair.first, pair.second)) {
            return false;
        }
    }
    if (!IsProcessManagerValid(config) || !IsClusterStatusValid(config)) {
        return false;
    }

    if (!IsHasFlexValid(config)) {
        return false;
    }

    if (config.contains("has_flex") && config.at("has_flex").get<bool>()) {
        LOG_E("[%s] [ControllerConfig] Feature has_flex is currently not supported, please set it to false.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str());
        return false;
    }
    return true;
}

bool ControllerConfig::IsAutoSwitchingValid(const nlohmann::json &config) const
{
    if (!IsJsonObjValid(config, "digs_periodic_role_decision") ||
        !IsJsonBoolValid(config["digs_periodic_role_decision"], "auto_pd_role_switching_enable") ||
        !IsJsonIntValid(config["digs_periodic_role_decision"], "role_decision_time_period",
            g_digsTimePeriodMin, g_digsTimePeriodMax) ||
        !IsJsonIntValid(config["digs_periodic_role_decision"], "tasks_end_wait_seconds",
            g_commonTimePeriodMin, g_commonIntMax)) {
        return false;
    }
    return true;
}

bool ControllerConfig::IsMultiNodeInferConfigValid(const nlohmann::json &config) const
{
    if (!config.contains("multi_node_infer_config")) {
        LOG_W("[%s] [ControllerConfig] The parameter multi_node_infer_config is empty.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::CONSTANT).c_str());
        return true;
    }
    if (!IsJsonBoolValid(config["multi_node_infer_config"], "multi_node_infer_enable") ||
        !IsJsonObjValid(config["multi_node_infer_config"], "p_node_config") ||
        !IsJsonObjValid(config["multi_node_infer_config"], "d_node_config")) {
        LOG_W("[%s] [ControllerConfig] Check multi_node_infer_config failed.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::CONSTANT).c_str());
        return false;
    }
    if (!IsNodeConfigJsonIntValid(config, "p_node_config") ||
        !IsNodeConfigJsonIntValid(config, "d_node_config")) {
        LOG_W("[%s] [ControllerConfig] Check node_config failed.",
            GetWarnCode(ErrorType::WARNING, ControllerFeature::CONSTANT).c_str());
        return false;
    }
    mIsMultiNodeMode = config["multi_node_infer_config"]["multi_node_infer_enable"].get<bool>();
    return true;
}

bool ControllerConfig::IsConfigJsonValid(const nlohmann::json &config,
    const std::vector<std::pair<std::string, std::string>> &tlsConfigPairs) const
{
    if (!IsJsonBoolValid(config, "allow_all_zero_ip_listening") ||
        !IsJsonBoolValid(config, "is_heterogeneous") ||
        !IsJsonObjValid(config, "tls_config") ||
        !IsJsonBoolValid(config["tls_config"], "request_coordinator_tls_enable") ||
        !IsJsonBoolValid(config["tls_config"], "request_server_tls_enable") ||
        !IsJsonBoolValid(config["tls_config"], "http_server_tls_enable") ||
        !IsJsonBoolValid(config["tls_config"], "external_coordinator_tls_enable") ||
        !IsJsonObjValid(config, "process_manager") ||
        !IsJsonBoolValid(config["process_manager"], "to_file") ||
        !IsJsonObjValid(config, "cluster_status") ||
        !IsJsonBoolValid(config["cluster_status"], "to_file")) {
        return false;
    }
    if (!IsValidDefaultPDRateConfig(config)) {
        LOG_E("[%s] [ControllerConfig] In controller config JSON file, default P rate or default D rate is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str());
        return false;
    }

    if (!IsConfigJsonStringValid(config, tlsConfigPairs)) {
        return false;
    }

    if (!IsConfigJsonIntValid(config)) {
        return false;
    }
    if (!IsMultiNodeInferConfigValid(config)) {
        return false;
    }

    std::vector<std::string> deployMode {"pd_separate", "pd_disaggregation",
        "pd_disaggregation_single_container", "single_node"};
    if (std::find(deployMode.begin(), deployMode.end(), config["deploy_mode"]) == deployMode.end()) {
        LOG_E("[%s] [ControllerConfig] In controller config JSON file, deploy mode is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str());
        return false;
    }
    if (config["role_decision_methods"] != "digs") {
        LOG_E("[%s] [ControllerConfig] In controller config JSON file, 'role_decision_methods' only supports 'digs'.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str());
        return false;
    }
    if (config["transfer_type"] != "D2DTransfer") {
        LOG_E("[%s] [ControllerConfig] In controller config JSON file, 'transfer_type' only supports 'D2DTransfer'.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str());
        return false;
    }
    return IsAutoSwitchingValid(config);
}

static bool GetCheckFiles(const std::string &checkEnvParam)
{
    bool checkMountedFiles = true;
    auto checkEnv = std::getenv(checkEnvParam.c_str());
    if (checkEnv != nullptr && std::strlen(checkEnv) <= MAX_ENV_LENGTH) {
        std::string filesCheck = checkEnv;
        if (filesCheck == "0") {
            checkMountedFiles = false;
        }
    } else {
        LOG_W("[%s] [ControllerConfig] Env variable %s content is nullptr or length exceeds %d",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str(),
              checkEnvParam.c_str(), MAX_ENV_LENGTH);
    }
    return checkMountedFiles;
}

void ControllerConfig::InitMultiNodeConfig(const nlohmann::json &rawConfig)
{
    if (!mIsMultiNodeMode) {
        return;
    }
    mInitialDpServerPort = rawConfig["initial_dist_server_port"].get<uint32_t>();
    enablePDistribute = rawConfig["multi_node_infer_config"]["p_node_config"]["enable_dist_dp_server"].get<bool>();
    enableDDistribute = rawConfig["multi_node_infer_config"]["d_node_config"]["enable_dist_dp_server"].get<bool>();
    mPTpSize = rawConfig["multi_node_infer_config"]["p_node_config"]["tp_size"].get<uint32_t>();
    mDTpSize = rawConfig["multi_node_infer_config"]["d_node_config"]["tp_size"].get<uint32_t>();
    mPDpSize = rawConfig["multi_node_infer_config"]["p_node_config"]["dp_size"].get<uint32_t>();
    mDDpSize = rawConfig["multi_node_infer_config"]["d_node_config"]["dp_size"].get<uint32_t>();
    mPSpSize = rawConfig["multi_node_infer_config"]["p_node_config"]["sp_size"].get<uint32_t>();
    mDSpSize = rawConfig["multi_node_infer_config"]["d_node_config"]["sp_size"].get<uint32_t>();
    mPCpSize = rawConfig["multi_node_infer_config"]["p_node_config"]["cp_size"].get<uint32_t>();
    mDCpSize = rawConfig["multi_node_infer_config"]["d_node_config"]["cp_size"].get<uint32_t>();
    mPNodeSize = rawConfig["multi_node_infer_config"]["p_node_config"]["node_machine_num"].get<uint32_t>();
    mDNodeSize = rawConfig["multi_node_infer_config"]["d_node_config"]["node_machine_num"].get<uint32_t>();
    
    // 校验多节点配置
    if (ValidateMultiNodeConfig() != 0) {
        LOG_E("[%s] [ControllerConfig] InitMultiNodeConfig: Multi-node configuration validation failed",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str());
        throw std::runtime_error("Multi-node configuration validation failed");
    }
}

int32_t ControllerConfig::ValidateMultiNodeConfig() const
{
    // 校验DP、CP、TP、SP大小不能为0
    if (mPDpSize == 0 || mPCpSize == 0 || mPTpSize == 0 || mPSpSize == 0) {
        LOG_E("[%s] [ControllerConfig] ValidateMultiNodeConfig: Prefill configuration values cannot be zero. "
              "DP size: %u, CP size: %u, TP size: %u, SP size: %u",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str(),
              mPDpSize, mPCpSize, mPTpSize, mPSpSize);
        return -1;
    }
    
    if (mDDpSize == 0 || mDCpSize == 0 || mDTpSize == 0 || mDSpSize == 0) {
        LOG_E("[%s] [ControllerConfig] ValidateMultiNodeConfig: Decode configuration values cannot be zero. "
              "DP size: %u, CP size: %u, TP size: %u, SP size: %u",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str(),
              mDDpSize, mDCpSize, mDTpSize, mDSpSize);
        return -1;
    }
    
    // 校验DP和CP不能同时大于1
    if ((mPDpSize > 1 && mPCpSize > 1) || (mDDpSize > 1 && mDCpSize > 1)) {
        LOG_E("[%s] [ControllerConfig] ValidateMultiNodeConfig: DP and CP cannot be enabled simultaneously. "
              "Prefill DP size: %u, CP size: %u; Decode DP size: %u, CP size: %u. "
              "When DP > 1, CP must equal 1, and vice versa.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str(),
              mPDpSize, mPCpSize, mDDpSize, mDCpSize);
        return -1;
    }
    
    return 0;
}

void ControllerConfig::InitModelIDConfig(const nlohmann::json & /* rawConfig */) // 未使用的参数注释
{
    size_t numOfModels = 1; // currently only support generating 1 model, will support generate for more replica
    for (size_t i = 0; i < numOfModels; i++) {
        mModelIDVec.emplace_back(GetUUID());
    }
}

void ControllerConfig::InitConfig(const nlohmann::json &rawConfig,
    const std::vector<std::pair<std::string, std::string>> &tlsConfigPairs)
{
    InitNameToStringConfig(rawConfig);
    InitTlsItems(rawConfig, tlsConfigPairs);
    InitProcessManagerConfig(rawConfig);
    InitClusterStatusConfig(rawConfig);
    InitCtrlBackupConfig(rawConfig);
    InitModelIDConfig(rawConfig);
    InitCCAE(rawConfig);
    InitClusterD(rawConfig);
    InitAlarm(rawConfig);
    InitFaultRecoveryFunction(rawConfig);
    auto deployModeStr = rawConfig.at("deploy_mode").get<std::string>();
    mDeployMode = ControllerConstant::GetInstance()->GetDeployModeByStr(deployModeStr);
    mServerOnlineAttemptTimes = rawConfig.at("server_online_attempt_times").get<uint32_t>();
    mServerOnlineWaitSeconds = rawConfig.at("server_online_wait_seconds").get<uint32_t>();
    mInitRoleAttemptTimes = rawConfig.at("init_role_attempt_times").get<uint32_t>();
    mCheckRoleAttemptTimes = rawConfig.at("check_role_attempt_times").get<uint32_t>();
    mCheckRoleWaitSeconds = rawConfig.at("check_role_wait_seconds").get<uint32_t>();
    mHttpRetries = rawConfig.at("http_retries").get<uint32_t>();
    mHttpTimeoutSeconds = rawConfig.at("http_timeout_seconds").get<uint32_t>();
    mPRate = rawConfig.at("default_p_rate").get<size_t>();
    mDRate = rawConfig.at("default_d_rate").get<size_t>();
    mDigsRequestInputLength = rawConfig.at("digs_request_summary_input_length").get<size_t>();
    mDigsRequestOutputLength = rawConfig.at("digs_request_summary_output_length").get<size_t>();
    mClusterSynchronizationSeconds = rawConfig.at("cluster_synchronization_seconds").get<uint32_t>();
    mRankTableDetectingSeconds = rawConfig.at("rank_table_detecting_seconds").get<uint32_t>();
    mDisappearedServerWaitingSeconds = rawConfig.at("disappeared_server_waiting_seconds").get<uint32_t>();
    mTasksEndWaitSeconds = rawConfig["digs_periodic_role_decision"].at("tasks_end_wait_seconds").get<uint32_t>();
    mIsAutoSwitching = rawConfig["digs_periodic_role_decision"].at("auto_pd_role_switching_enable").get<bool>();
    mAutoSwitchingSeconds = rawConfig["digs_periodic_role_decision"].at("role_decision_time_period").get<uint32_t>();
    mIsHeterogeneous = rawConfig.at("is_heterogeneous").get<bool>();
    mAllowAllZeroIpListening = rawConfig.at("allow_all_zero_ip_listening").get<bool>();
    mIsSingleContainer = ControllerConstant::GetInstance()->IsSingleContainer(deployModeStr);
    mPort = rawConfig["http_server"].at("port").get<int64_t>();
    mStaticElasticTemplatePath = rawConfig.at("scaling_rule_file_path").get<std::string>();
    InitMultiNodeConfig(rawConfig);
    // has_flex键不存在，设默认值为false
    mHasFlex = rawConfig.value("has_flex", false);
    // 大EP场景不开启flex，用 mIsMultiNodeMode 判断
    if (mIsMultiNodeMode) {
        mHasFlex = false;
    }
}

bool ContainsAlarmConfig(const nlohmann::json &rawConfig)
{
    if (!rawConfig.at("tls_config").contains("alarm_tls_enable") ||
        !rawConfig.at("tls_config").contains("alarm_tls_items")) {
        LOG_I("[ControllerConfig] Alarm manager is not enable.");
        return false;
    }
    bool containsAlarm = IsJsonBoolValid(rawConfig.at("tls_config"), "alarm_tls_enable") &&
        IsTlsConfigJsonStringValid(rawConfig.at("tls_config"), "alarm_tls_enable", "alarm_tls_items");
    return containsAlarm;
}

void ControllerConfig::InitAlarm(const nlohmann::json &rawConfig)
{
    if (!ContainsAlarmConfig(rawConfig)) {
        mAlarmTlsItems.tlsEnable = false;
        return;
    }
    mAlarmTlsItems = GetInitTlsItems(rawConfig["tls_config"], "alarm_tls_enable", "alarm_tls_items");
}
bool ContainsCCAEConfig(const nlohmann::json &rawConfig)
{
    if (!rawConfig.contains("ccae")) {
        LOG_I("[ControllerConfig] The ccae function is not enable.");
        return false;
    }
    bool containsCCAE = IsJsonObjValid(rawConfig, "ccae") &&
        IsJsonStringValid(rawConfig.at("ccae"), "ip") &&
        IsJsonIntValid(rawConfig.at("ccae"), "port", g_portMin, g_portMax);
    return containsCCAE;
}

void ControllerConfig::InitCCAE(const nlohmann::json &rawConfig)
{
    mCCAETlsItems.tlsEnable = false;
    if (!ContainsCCAEConfig(rawConfig)) {
        return;
    }
    // Get ccae ip
    auto keyName = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_IP);
    auto ccaeKeyName = "ccae_" + keyName;
    mNameToStringConfig[ccaeKeyName] = rawConfig["ccae"].at(keyName).get<std::string>();
    
    // Get ccae port
    mCCAEPort = rawConfig["ccae"].at("port").get<int64_t>();
}

bool ContainsClusterDConfig(const nlohmann::json &rawConfig)
{
    if (!rawConfig.contains("cluster_port") ||
        !rawConfig.at("tls_config").contains("cluster_tls_enable") ||
        !rawConfig.at("tls_config").contains("cluster_tls_items")) {
        LOG_I("[ControllerConfig] Communication with MindCluster is not enable.");
        return false;
    }
    bool containsClusterD =  IsJsonIntValid(rawConfig, "cluster_port", g_portMin, g_portMax) &&
        IsJsonBoolValid(rawConfig.at("tls_config"), "cluster_tls_enable") &&
        IsTlsConfigJsonStringValid(rawConfig.at("tls_config"), "cluster_tls_enable", "cluster_tls_items");
    return containsClusterD;
}

void ControllerConfig::InitClusterD(const nlohmann::json &rawConfig)
{
    if (!ContainsClusterDConfig(rawConfig)) {
        mClusterTlsItems.tlsEnable = false;
        return;
    }
    mClusterPort = rawConfig.at("cluster_port").get<std::uint32_t>();
    mClusterTlsItems = GetInitTlsItems(rawConfig["tls_config"], "cluster_tls_enable", "cluster_tls_items");
}

int32_t ControllerConfig::Init()
{
    auto envParam = ControllerConstant::GetInstance()->GetEnvParam(EnvParam::CONTROLLER_CONFIG_FILE_PATH);
    auto env = std::getenv(envParam.c_str());
    std::string path = (env == nullptr) ?
        ControllerConstant::GetInstance()->GetFilePath(FilePath::CONFIG_PATH) : env;
    auto checkEnvParam = ControllerConstant::GetInstance()->GetEnvParam(EnvParam::CHECK_FILES);
    mCheckMountedFiles = GetCheckFiles(checkEnvParam);
    uint32_t mode = (env == nullptr || mCheckMountedFiles) ? 0640 : 0777;   // ms_controller.json的权限要求是0640, 不校验是0777
    auto rawConfig = FileToJsonObj(path, mode, (env == nullptr || mCheckMountedFiles)); // ms_controller.json的权限要求是0640
    if (rawConfig.empty()) {
        LOG_E("[%s] [ControllerConfig] Initialize controller configuration failed, config JSON file is empty.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str());
        return -1;
    }
    auto tlsConfigPairs = GenerateTlsConfigPairs();
    if (!IsConfigJsonValid(rawConfig, tlsConfigPairs)) {
        LOG_E("[%s] [ControllerConfig] Initialize controller configuration failed, config JSON is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str());
        return -1;
    }
    try {
        InitConfig(rawConfig, tlsConfigPairs);
        return InitLog(rawConfig, path);
    } catch (const std::exception& e) {
        LOG_E("[%s] [ControllerConfig] Initialize controller configuration failed, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::CONSTANT).c_str(), e.what());
        return -1;
    }
}

std::vector<std::pair<std::string, std::string>> ControllerConfig::GenerateTlsConfigPairs() const
{
    std::vector<std::pair<std::string, std::string>> tlsConfigPairs;
    tlsConfigPairs.push_back(std::make_pair("request_coordinator_tls_enable", "request_coordinator_tls_items"));
    tlsConfigPairs.push_back(std::make_pair("request_server_tls_enable", "request_server_tls_items"));
    tlsConfigPairs.push_back(std::make_pair("http_server_tls_enable", "http_server_tls_items"));
    tlsConfigPairs.push_back(std::make_pair("external_coordinator_tls_enable", "external_coordinator_tls_items"));
    return tlsConfigPairs;
}

size_t ControllerConfig::GetModelNum() const
{
    return mModelIDVec.size();
}

std::string ControllerConfig::GetModelID(size_t it) const
{
    if (it < mModelIDVec.size()) {
        return mModelIDVec.at(it);
    }
    LOG_E("[ControllerConfig] Getting ModelID out of range.");
    return "";
}

uint32_t ControllerConfig::GetServerOnlineAttemptTimes() const
{
    return mServerOnlineAttemptTimes;
}

uint32_t ControllerConfig::GetServerOnlineWaitSeconds() const
{
    return mServerOnlineWaitSeconds;
}

uint32_t ControllerConfig::GetInitRoleAttemptTimes() const
{
    return mInitRoleAttemptTimes;
}

uint32_t ControllerConfig::GetCheckRoleAttemptTimes() const
{
    return mCheckRoleAttemptTimes;
}

uint32_t ControllerConfig::GetCheckRoleWaitSeconds() const
{
    return mCheckRoleWaitSeconds;
}

uint32_t ControllerConfig::GetTasksEndWaitSeconds() const
{
    return mTasksEndWaitSeconds;
}

uint32_t ControllerConfig::GetClusterPort() const
{
    return mClusterPort;
}

uint64_t ControllerConfig::GetInitialDpServerPort() const
{
    return mInitialDpServerPort;
}

MINDIE::MS::DIGSInstanceRole ControllerConfig::GetPDInitRole(int32_t deployMode) const
{
    if (enablePDistribute == enableDDistribute) {
        // Since it could be possible that both P and D could be distributed,
        // We use g_prefillServer and g_decodeServer to judge P and D
        if (deployMode == g_prefillServer) {
            return MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
        } else if (deployMode == g_decodeServer) {
            return MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE;
        }
        return MINDIE::MS::DIGSInstanceRole::UN_DEF_INSTANCE;
    }
    if (static_cast<bool>(deployMode) == enablePDistribute) {
        return MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
    } else {
        return MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE;
    }
}

void ControllerConfig::InitNameToStringConfig(const nlohmann::json &rawConfig)
{
    auto keyName = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_IP);
    mNameToStringConfig[keyName] = rawConfig["http_server"].at(keyName).get<std::string>();

    std::vector<ConfigKey> keys = { ConfigKey::CONFIG_KEY_GLOBAL_RANK_TABLE_PATH,
        ConfigKey::CONFIG_KEY_MODEL_CONFIG_FILE_PATH,
        ConfigKey::CONFIG_KEY_MACHINE_CONFIG_FILE_PATH,
        ConfigKey::CONFIG_KEY_MODEL_TYPE,
        ConfigKey::CONFIG_KEY_TRANSFER_TYPE,
        ConfigKey::CONFIG_KEY_ROLE_DECISION_METHODS
    };
    for (auto &key: keys) {
        keyName = ControllerConstant::GetInstance()->GetConfigKey(key);
        mNameToStringConfig[keyName] = rawConfig.at(keyName).get<std::string>();
    }

    std::vector<ConfigKey> intKeys = { ConfigKey::CONFIG_KEY_SERVER_PORT,
        ConfigKey::CONFIG_KEY_SERVER_CONTROL_PORT,
        ConfigKey::CONFIG_KEY_SERVER_METRIC_PORT,
        ConfigKey::CONFIG_KEY_COORDINATOR_PORT,
        ConfigKey::CONFIG_KEY_NODE_MANAGER_PORT,
        ConfigKey::CONFIG_KEY_COORDINATOR_EXTERNAL_PORT,
        ConfigKey::CONFIG_KEY_CONTROLLER_ALARM_PORT,
        ConfigKey::CONFIG_KEY_DIGS_PREFILL_SLO,
        ConfigKey::CONFIG_KEY_DIGS_DECODE_SLO,
        ConfigKey::CONFIG_KEY_DIGS_PP
    };
    for (auto &key : intKeys) {
        keyName = ControllerConstant::GetInstance()->GetConfigKey(key);
        mNameToStringConfig[keyName] = std::to_string(rawConfig.at(keyName).get<int32_t>());
    }
}

TlsItems ControllerConfig::GetInitTlsItems(const nlohmann::json &rawConfig, const std::string &tlsEnableName,
    const std::string &certName) const
{
    TlsItems tlsItems;
    tlsItems.tlsEnable = rawConfig.at(tlsEnableName).get<bool>();
    if (!tlsItems.tlsEnable) {
        LOG_I("[ControllerConfig] Getting TLS items, %s is disabled.", tlsEnableName.c_str());
        return tlsItems;
    }
    tlsItems.caCert = rawConfig[certName].at("ca_cert").get<std::string>();
    tlsItems.tlsCert = rawConfig[certName].at("tls_cert").get<std::string>();
    tlsItems.tlsKey = rawConfig[certName].at("tls_key").get<std::string>();
    tlsItems.tlsPasswd = rawConfig[certName].at("tls_passwd").get<std::string>();
    tlsItems.tlsCrl = rawConfig[certName].at("tls_crl").get<std::string>();
    tlsItems.checkFiles = mCheckMountedFiles;
    return tlsItems;
}

int32_t ControllerConfig::InitLog(const nlohmann::json &rawConfig, const std::string& configFilePath) const
{
    MINDIE::MS::DefaultLogConfig defaultLogConfig;
    defaultLogConfig.option.subModule = SubModule::MS_CONTROLLER;
    int32_t logInitRes = Logger::Singleton()->Init(defaultLogConfig, rawConfig, configFilePath);
    int logLevelIntervalMs = 1000;
    LogLevelDynamicHandler::Init(logLevelIntervalMs, "Controller", true);
    return logInitRes;
}

void ControllerConfig::InitTlsItems(const nlohmann::json &rawConfig,
    const std::vector<std::pair<std::string, std::string>> &tlsConfigPairs)
{
    mRequestCoordinatorTlsItems = GetInitTlsItems(rawConfig["tls_config"],
        tlsConfigPairs[0].first, tlsConfigPairs[0].second); // 0表示coordinator的客户端证书
    mRequestServerTlsItems = GetInitTlsItems(rawConfig["tls_config"],
        tlsConfigPairs[1].first, tlsConfigPairs[1].second); // 1表示server的客户端证书
    mHttpServerTlsItems = GetInitTlsItems(rawConfig["tls_config"],
        tlsConfigPairs[2].first, tlsConfigPairs[2].second); // 2表示controller的服务端证书
    mExternalCoordinatorTlsItems = GetInitTlsItems(rawConfig["tls_config"],
        tlsConfigPairs[3].first, tlsConfigPairs[3].second); // 3表示coordinator的外部服务接口证书
}

void ControllerConfig::InitProcessManagerConfig(const nlohmann::json &rawConfig)
{
    mProcessManagerConfig.toFile = rawConfig["process_manager"].at("to_file").get<bool>();
    if (mProcessManagerConfig.toFile) {
        mProcessManagerConfig.filePath = rawConfig["process_manager"].at("file_path").get<std::string>();
    }
}

void ControllerConfig::InitClusterStatusConfig(const nlohmann::json &rawConfig)
{
    mClusterStatusConfig.toFile = rawConfig["cluster_status"].at("to_file").get<bool>();
    if (mClusterStatusConfig.toFile) {
        mClusterStatusConfig.filePath = rawConfig["cluster_status"].at("file_path").get<std::string>();
    }
}

void ControllerConfig::InitCtrlBackupConfig(const nlohmann::json &rawConfig)
{
    // TLS 合法性校验
    if (!rawConfig.at("tls_config").contains("etcd_server_tls_enable") ||
        !rawConfig.at("tls_config").contains("etcd_server_tls_items")) {
        LOG_I("[InitCtrlBackupConfig] Communication with ETCD is not configured.");
        mEtcdTlsItems.tlsEnable = false;
    } else {
        if (!IsTlsConfigJsonStringValid(rawConfig["tls_config"], "etcd_server_tls_enable", "etcd_server_tls_items")) {
            mEtcdTlsItems.tlsEnable = false;
        } else {
            mEtcdTlsItems = GetInitTlsItems(rawConfig["tls_config"],
            "etcd_server_tls_enable", "etcd_server_tls_items");
        }
    }

    // controller_backup_cfg 参数存在及合法性校验
    if (!rawConfig.contains("controller_backup_cfg") || !rawConfig["controller_backup_cfg"].contains("function_sw")) {
        LOG_I("[InitCtrlBackupConfig] Controller backup function is not configured.");
        mCtrlerBackUpConfig.funSw = false;
        return;
    }
    if (!IsJsonIntValid(rawConfig["controller_backup_cfg"], "database_server_port", g_portMin, g_portMax) ||
        !IsJsonBoolValid(rawConfig["controller_backup_cfg"], "function_sw") ||
        !IsJsonStringValid(rawConfig["controller_backup_cfg"], "database_server_dns")) {
            LOG_I("[InitCtrlBackupConfig] fun cfg is invalid, funsw is false.");
            mCtrlerBackUpConfig.funSw = false;
            return;
    }
    mCtrlerBackUpConfig.funSw = rawConfig["controller_backup_cfg"].at("function_sw").get<bool>();
    if (mCtrlerBackUpConfig.funSw) {
        mCtrlerBackUpConfig.serverDns = rawConfig["controller_backup_cfg"].at("database_server_dns").get<std::string>();
        mCtrlerBackUpConfig.serverPort = rawConfig["controller_backup_cfg"].at("database_server_port").get<uint32_t>();
    }
}

void ControllerConfig::InitFaultRecoveryFunction(const nlohmann::json &rawConfig)
{
    mFaultRecoverySw.clear();
    if (!rawConfig.contains("fault_recovery_func_dict") || !rawConfig["fault_recovery_func_dict"].is_object()) {
        LOG_I("[InitFaultRecoveryFunction] fault_recovery_func_dict is not configured or not an object.");
        return;
    }
    const auto& dict = rawConfig["fault_recovery_func_dict"];
    for (auto it = dict.begin(); it != dict.end(); ++it) {
        const std::string &key = it.key();
        if (!IsJsonBoolValid(dict, key)) {
            LOG_I("[InitFaultRecoveryFunction] %s recovery function is disabled because "
                "configuration value is not a bool type.", key.c_str());
            mFaultRecoverySw[key] = false;
        } else {
            mFaultRecoverySw[key] = dict.at(key).get<bool>();
        }
    }
}

std::string ControllerConfig::GetPodIP() const
{
    auto envParam = ControllerConstant::GetInstance()->GetEnvParam(EnvParam::POD_IP);
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_IP);
    auto ip = GetConfigByEnvThenJson(envParam, configParam);
    if (!IsValidIp(ip, mAllowAllZeroIpListening)) {
        LOG_E("[%s] [ControllerConfig] POD IP %s is invalid, using HTTP server IP.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str(),
            ip.c_str());
        return GetStringConfig(configParam);
    }
    return ip;
}

std::string ControllerConfig::GetCCAEIP() const
{
    auto ip = GetStringConfig("ccae_ip");
    if (!IsValidIp(ip, mAllowAllZeroIpListening)) {
        LOG_W("[%s] [ControllerConfig] CCAE IP %s is invalid, or CCAE will not be used.",
              GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str(),
              ip.c_str());
        return "";
    }
    return ip;
}

DeployMode ControllerConfig::GetDeployMode() const
{
    return mDeployMode;
}

const ProcessManagerConfig& ControllerConfig::GetProcessManagerConfig()
{
    return mProcessManagerConfig;
}

const ClusterStatusConfig& ControllerConfig::GetClusterStatusConfig()
{
    return mClusterStatusConfig;
}

const CtrlerBackUpConfig& ControllerConfig::GetCtrlBackUpConfig()
{
    return mCtrlerBackUpConfig;
}

std::string ControllerConfig::GetGlobalRankTablePath() const
{
    auto envParam = ControllerConstant::GetInstance()->GetEnvParam(EnvParam::GLOBAL_RANK_TABLE);
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_GLOBAL_RANK_TABLE_PATH);
    return GetConfigByEnvThenJson(envParam, configParam);
}

std::string ControllerConfig::GetMindIEServerPort() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_SERVER_PORT);
    return GetStringConfig(configParam);
}

int64_t ControllerConfig::GetPort() const
{
    return mPort;
}

int64_t ControllerConfig::GetCCAEPort() const
{
    return mCCAEPort;
}

bool ControllerConfig::GetHasFlex() const
{
    return mHasFlex;
}

bool ControllerConfig::GetFaultRecoveryEnableByConfigKey(const std::string &key) const
{
    if (mFaultRecoverySw.find(key) != mFaultRecoverySw.end()) {
        auto it = mFaultRecoverySw.find(key);
        return it->second;
    }
    return false;
}

// 判断是否存在快恢功能开启
bool ControllerConfig::IsAnyFaultRecoveryEnable() const
{
    return std::any_of(mFaultRecoverySw.begin(), mFaultRecoverySw.end(),
        [](const auto& p) { return p.second; });
}

std::string ControllerConfig::GetMindIEServerControlPort() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_SERVER_CONTROL_PORT);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetMindIEServerMetricPort() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_SERVER_METRIC_PORT);
    return GetStringConfig(configParam);
}

uint32_t ControllerConfig::GetClusterSynchronizationSeconds() const
{
    return mClusterSynchronizationSeconds;
}

uint32_t ControllerConfig::GetRankTableDetectingSeconds() const
{
    return mRankTableDetectingSeconds;
}

uint32_t ControllerConfig::GetDisappearedServerWaitingSeconds() const
{
    return mDisappearedServerWaitingSeconds;
}

uint32_t ControllerConfig::GetHttpTimeoutSeconds() const
{
    return mHttpTimeoutSeconds;
}

uint32_t ControllerConfig::GetHttpRetries() const
{
    return mHttpRetries;
}

std::string ControllerConfig::GetNodeManagerPort() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_NODE_MANAGER_PORT);
    return GetStringConfig(configParam);
}

size_t ControllerConfig::GetDIGSRequestInputLength() const
{
    return mDigsRequestInputLength;
}

size_t ControllerConfig::GetDIGSRequestOutputLength() const
{
    return mDigsRequestOutputLength;
}

std::string ControllerConfig::GetCoordinatorPort() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_COORDINATOR_PORT);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetCoordinatorExternalPort() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_COORDINATOR_EXTERNAL_PORT);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetControllerAlarmPort() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_CONTROLLER_ALARM_PORT);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetModelConfigFilePath() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_MODEL_CONFIG_FILE_PATH);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetMachineConfigFilePath() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_MACHINE_CONFIG_FILE_PATH);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetDIGSPrefillSLO() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_DIGS_PREFILL_SLO);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetDIGSDecodeSLO() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_DIGS_DECODE_SLO);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetDIGSTimePeriod() const
{
    return std::to_string(mAutoSwitchingSeconds);
}

bool ControllerConfig::GetDIGSIsAutoSwitching() const
{
    return mIsAutoSwitching;
}

bool ControllerConfig::GetDIGSIsHeterogeneous() const
{
    return mIsHeterogeneous;
}

bool ControllerConfig::GetDIGSIsSingleContainer() const
{
    return mIsSingleContainer;
}


bool ControllerConfig::GetPIsDistribute() const
{
    return enablePDistribute;
}

bool ControllerConfig::GetDIsDistribute() const
{
    return enableDDistribute;
}

std::string ControllerConfig::GetModelType() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_MODEL_TYPE);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetDIGSTransferType() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_TRANSFER_TYPE);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetDIGSPP() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_DIGS_PP);
    return GetStringConfig(configParam);
}

std::string ControllerConfig::GetRoleDecisionMethod() const
{
    auto configParam = ControllerConstant::GetInstance()->GetConfigKey(ConfigKey::CONFIG_KEY_ROLE_DECISION_METHODS);
    return GetStringConfig(configParam);
}

bool ControllerConfig::IsMultiNodeMode() const
{
    return mIsMultiNodeMode;
}

uint32_t ControllerConfig::GetPNodeNum() const
{
    return mPNodeSize;
}

uint32_t ControllerConfig::GetDNodeNum() const
{
    return mDNodeSize;
}

uint32_t ControllerConfig::GetPTpSize() const
{
    return mPTpSize;
}

uint32_t ControllerConfig::GetDTpSize() const
{
    return mDTpSize;
}

uint32_t ControllerConfig::GetPDpSize() const
{
    return mPDpSize;
}

uint32_t ControllerConfig::GetDDpSize() const
{
    return mDDpSize;
}

uint32_t ControllerConfig::GetPSpSize() const
{
    return mPSpSize;
}

uint32_t ControllerConfig::GetDSpSize() const
{
    return mDSpSize;
}

uint32_t ControllerConfig::GetPCpSize() const
{
    return mPCpSize;
}

uint32_t ControllerConfig::GetDCpSize() const
{
    return mDCpSize;
}

bool ControllerConfig::IsLeader() const
{
    if (!mCtrlerBackUpConfig.funSw) {
        return true;
    }
    return mIsLeader.load();
}
void ControllerConfig::SetLeader(bool isLeader)
{
    mIsLeader.store(isLeader);
}

std::string ControllerConfig::GetStaticElasticTemplatePath() const
{
    return mStaticElasticTemplatePath;
}

void ControllerConfig::SetStaticElasticTemplatePath(std::string templatePath)
{
    mStaticElasticTemplatePath = templatePath;
}

std::string ControllerConfig::GetStringConfig(const std::string &config) const
{
    auto iter = mNameToStringConfig.find(config);
    if (iter == mNameToStringConfig.end()) {
        LOG_E("[%s] [ControllerConfig] String config %s is not found.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONSTANT).c_str(),
            config.c_str());
        return {};
    }
    return iter->second;
}

size_t ControllerConfig::GetPRate() const
{
    auto envParam = ControllerConstant::GetInstance()->GetEnvParam(EnvParam::P_RATE);
    auto env = std::getenv(envParam.c_str());
    std::string pRateStr = (env == nullptr) ? "" : env;
    if (pRateStr.empty()) {
        return mPRate;
    }
    LOG_D("[ControllerConfig] Get P rate using environment variable %s.", envParam.c_str());
    try {
        size_t pRate = static_cast<size_t>(std::stoul(pRateStr));
        if (pRate < g_minRate || pRate > g_maxRate) {
            LOG_E("[%s] [ControllerConfig] P rate value %zu must be in range of [%zu, %zu], use default P rate %zu.",
                GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str(), pRate,
                g_minRate, g_maxRate, mPRate);
            return mPRate;
        }
        return pRate;
    } catch (const std::exception& e) {
        LOG_E("[%s] [ControllerConfig] Parse P rate failed, use default P rate %zu.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::CONSTANT).c_str(),
            mPRate);
        return mPRate;
    }
}

size_t ControllerConfig::GetDRate() const
{
    auto envParam = ControllerConstant::GetInstance()->GetEnvParam(EnvParam::D_RATE);
    auto env = std::getenv(envParam.c_str());
    std::string dRateStr = (env == nullptr) ? "" : env;
    if (dRateStr.empty()) {
        return mDRate;
    }
    LOG_D("[ControllerConfig]GetDRate: using env %s", envParam.c_str());
    try {
        size_t dRate = static_cast<size_t>(std::stoul(dRateStr));
        if (dRate < g_minRate || dRate > g_maxRate) {
            LOG_E("[%s] [ControllerConfig] D rate value %zu must be in range of [%zu, %zu], use default d rate %zu",
                  GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONSTANT).c_str(), dRate,
                  g_minRate, g_maxRate, mDRate);
            return mDRate;
        }
        return dRate;
    } catch (const std::exception& e) {
        LOG_E("[%s] [ControllerConfig] Parse D rate failed, use default D rate %zu.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::CONSTANT).c_str(),
            mDRate);
        return mDRate;
    }
}

void ControllerConfig::ParsePDRate(size_t &pRatio, size_t &dRatio) const
{
    pRatio = GetPRate();
    dRatio = GetDRate();
    LOG_I("[ControllerConfig] Parseed PD rate: P rate is %zu, D rate is %zu.", pRatio, dRatio);
}

const TlsItems& ControllerConfig::GetRequestCoordinatorTlsItems()
{
    return mRequestCoordinatorTlsItems;
}

const TlsItems& ControllerConfig::GetRequestServerTlsItems()
{
    return mRequestServerTlsItems;
}

const TlsItems& ControllerConfig::GetHttpServerTlsItems()
{
    return mHttpServerTlsItems;
}

const TlsItems& ControllerConfig::GetExternalCoordinatorTlsItems()
{
    return mExternalCoordinatorTlsItems;
}

const TlsItems& ControllerConfig::GetCCAETlsItems()
{
    return mCCAETlsItems;
}

const TlsItems& ControllerConfig::GetClusterTlsItems()
{
    return mClusterTlsItems;
}

const TlsItems& ControllerConfig::GetEtcdTlsItems()
{
    return mEtcdTlsItems;
}

const TlsItems& ControllerConfig::GetAlarmTlsItems()
{
    return mAlarmTlsItems;
}
std::string ControllerConfig::GetConfigByEnvThenJson(const std::string &envParam, const std::string &configParam) const
{
    auto env = std::getenv(envParam.c_str());
    std::string config = (env == nullptr) ? "" : env;
    if (!config.empty()) {
        LOG_D("[ControllerConfig] Get Config using environment variable %s.", envParam.c_str());
        return config;
    }
    return GetStringConfig(configParam);
}

bool ControllerConfig::GetCheckMountedFiles() const
{
    return mCheckMountedFiles;
}
}