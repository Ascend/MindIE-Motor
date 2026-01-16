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
#include <cstdlib>
#include <cctype>
#include <algorithm>
#include "ConfigParams.h"
#include "Util.h"
#include "Logger.h"
#include "common.h"
#include "LogLevelDynamicHandler.h"
#include "Configure.h"

namespace MINDIE::MS {

constexpr auto DEFAULT_CONFIG_FILE_PATH = "../conf/ms_coordinator.json";
constexpr auto CONFIG_FILE_PATH_ENV = "MINDIE_MS_COORDINATOR_CONFIG_FILE_PATH";
constexpr size_t MAX_REQ_NUM = 15 * 6 * 1000;
constexpr auto CONFIG_MAX_REQ_ENV = "MINDIE_MS_COORDINATOR_CONFIG_MAX_REQ";
constexpr auto CONFIG_SINGLE_NODE_MAX_REQ_ENV = "MINDIE_MS_COORDINATOR_CONFIG_SINGLE_NODE_MAX_REQ";
constexpr size_t SINGLE_NODE_MAX_REQ_NUM = 15000;
constexpr size_t MAX_INSTANCES_NUM = 768 * 6;
constexpr auto CONFIG_CHECK_FILES_ENV = "MINDIE_CHECK_INPUTFILES_PERMISSION";
constexpr size_t MAX_INFER_TIME = 65535; // Max end-to-end inference timeout
constexpr size_t ADJUSTMENT_SECONDS = 20;

constexpr int64_t PORT_MAX = 65535; // 65535 maximum prot id
constexpr int64_t PORT_MIN = 1024; // 1024 minimum port id

static bool GetCheckFiles()
{
    bool checkFiles = true;
    auto filesEnv = std::getenv(CONFIG_CHECK_FILES_ENV);
    if (filesEnv != nullptr) {
        std::string filesCheck = filesEnv;
        if (filesCheck == "0") {
            checkFiles = false;
        }
    }
    return checkFiles;
}

int32_t Configure::Init()
{
    auto env = std::getenv(CONFIG_FILE_PATH_ENV);
    std::string configFile = (env == nullptr) ? DEFAULT_CONFIG_FILE_PATH : env;
    this->checkMountedFiles = GetCheckFiles();
    std::string confStr;
    uint32_t mode = (env == nullptr || this->checkMountedFiles) ? 0640 : 0777;  // 校验权限是0640, 不校验是0777
    auto ret = FileToBuffer(configFile, confStr, mode, (env == nullptr || this->checkMountedFiles));
    if (ret != 0) {
        LOG_E("[%s] [Configure] Failed to read the coordinator configuration file %s.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::CONFIGURE).c_str(),
            configFile.c_str());
        return -1;
    }
    try {
        auto confJson = nlohmann::json::parse(confStr, CheckJsonDepthCallBack);
        if (CheckBackupValid(confJson) != 0) {
            return -1;
        }
        if (ReadLogInfo(confJson, configFile) != 0 ||
            ReadHttpConfig(confJson.at("http_config")) != 0 ||
            ReadMetricsConfig(confJson.at("metrics_config")) != 0 ||
            ReadPrometheusMetricsConfig(confJson.at("prometheus_metrics_config")) != 0 ||
            ReadExceptionConfig(confJson.at("exception_config")) != 0 ||
            ReadRequestLimit(confJson.at("request_limit")) != 0 ||
            ReadTlsConfig(confJson.at("tls_config")) != 0 ||
            ReadSchedulerConfig(confJson.at("digs_scheduler_config")) != 0 ||
            ReadStrTokenRateConfig(confJson) != 0) {
            return -1;
        }
        return 0;
    } catch (const nlohmann::json::exception& e) {
        LOG_E("[%s] [Configure] Failed to read Coordinator config: %s, json parse error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONFIGURE).c_str(), confStr.c_str(), e.what());
        return -1;
    } catch (const std::exception& e) {
        LOG_E("[%s] [Configure] Initialize failed, result is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONFIGURE).c_str(), e.what());
        return -1;
    }
}

void Configure::SetScheduleTimeout(size_t scheduleTimeout)
{
    LOG_M("[Configure] Set schedule timeout to %zu", scheduleTimeout);
    exceptionConfig.scheduleTimeout = scheduleTimeout;
}

 void Configure::SetFirstTokenTimeout(size_t firstTokenTimeout)
{
    LOG_M("[Configure] Set first token timeout to %zu", firstTokenTimeout);
    exceptionConfig.firstTokenTimeout = firstTokenTimeout;
}

 void Configure::SetInferTimeout(size_t inferTimeout)
{
    LOG_M("[Configure] Set infer timeout to %zu", inferTimeout);
    exceptionConfig.inferTimeout = inferTimeout;
}

int32_t Configure::CheckBackupValid(nlohmann::json confJson)
{
    if (!confJson.contains("backup_config")) {  // 若不存在此config则默认关闭主备
        backupEnable = false;
        return 0;
    }
    return ReadBackupConfig((confJson).at("backup_config"));
}

bool Configure::IsMaster()
{
    std::unique_lock<std::mutex> lock(mtx);
    return isMaster;
}

int32_t Configure::SetControllerIP(std::string controllerIP)
{
    if (!IsValidIp(controllerIP, httpConfig.allAllZeroIpListening)) {
        LOG_E("[%s] [Configure] IP from controller is invalid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return -1;
    }
    httpConfig.controllerIP = controllerIP;
    return 0;
}

void Configure::SetMaster(bool master)
{
    std::unique_lock<std::mutex> lock(mtx);
    isMaster = master;
}

bool Configure::IsAbnormal()
{
    std::unique_lock<std::mutex> lock(mtx);
    return isAbnormal;
}

void Configure::SetAbnormal(bool abnormal)
{
    std::unique_lock<std::mutex> lock(mtx);
    isAbnormal = abnormal;
}

bool Configure::CheckBackup()
{
    std::unique_lock<std::mutex> lock(mtx);
    return backupEnable;
}

static bool IsNumberStringValid(const std::string &s, int64_t min, int64_t max)
{
    if (!std::all_of(s.begin(), s.end(), [](char c) {
        return std::isdigit(c);
    })) {
        LOG_E("[%s] [Configure] Number in string %s is not valid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str(),
            s.c_str());
        return false;
    }

    try {
        int64_t number = std::stoi(s);
        return (number >= min && number <= max);
    } catch (...) {
        LOG_E("[%s] [Configure] Number in string %s is not valid!",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONFIGURE).c_str(),
            s.c_str());
        return false;
    }
    return false;
}

static bool IsFloatStringValid(const std::string &s, float min, float max)
{
    if (!std::isdigit(s[0])) {
        return false;
    }
    bool foundDot = false;
    for (char c : s) {
        if (c == '.' && foundDot) {
            return false;
        } else if (c == '.') {
            foundDot = true;
        } else if (!std::isdigit(c)) {
            return false;
        }
    }

    try {
        float number = std::stof(s);
        if (number < min || number > max) {
            return false;
        }
    } catch (const std::exception& e) {
        return false;
    }
    return true;
}


int32_t Configure::ReadHttpConfig(const nlohmann::json &config)
{
    if (!IsJsonStringValid(config, "predict_ip") || !IsJsonStringValid(config, "predict_port") ||
        !IsJsonStringValid(config, "manage_ip") || !IsJsonStringValid(config, "manage_port") ||
        !IsJsonStringValid(config, "alarm_port") || !IsJsonStringValid(config, "status_port") ||
        !IsJsonStringValid(config, "external_port") ||
        !IsJsonIntValid(config, "server_thread_num", 1, 10000) ||   // server_thread_num的范围是1~10000
        !IsJsonIntValid(config, "client_thread_num", 1, 10000) ||   // client_thread_num的范围是1~10000
        !IsJsonIntValid(config, "http_timeout_seconds", 0, 3600) || // http_timeout_seconds的范围是0~3600
        !IsJsonIntValid(config, "keep_alive_seconds", 0, 3600) ||   // keep_alive_seconds的范围是0~3600
        !IsJsonStringValid(config, "server_name") || !IsJsonStringValid(config, "user_agent") ||
        !IsJsonBoolValid(config, "allow_all_zero_ip_listening")) {
        LOG_E("[%s] [Configure] Read HTTP configuration from config file failed, some paramters is invalid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return -1;
    }
    httpConfig.allAllZeroIpListening = config.at("allow_all_zero_ip_listening").template get<bool>();
    httpConfig.predIp = config.at("predict_ip").template get<std::string>();
    httpConfig.predPort = config.at("predict_port").template get<std::string>();
    httpConfig.managementIp = config.at("manage_ip").template get<std::string>();
    httpConfig.managementPort = config.at("manage_port").template get<std::string>();
    httpConfig.statusPort = config.at("status_port").template get<std::string>();
    httpConfig.externalPort = config.at("external_port").template get<std::string>();
    httpConfig.alarmPort = config.at("alarm_port").template get<std::string>();
    if (!IsValidIp(httpConfig.predIp, httpConfig.allAllZeroIpListening) ||
        !IsValidIp(httpConfig.managementIp, httpConfig.allAllZeroIpListening)) {
        LOG_E("[%s] [Configure] Read HTTP configuration from config file failed, IP is invalid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return -1;
    }
    if (!IsNumberStringValid(httpConfig.predPort, 1024, 65535) ||       // predPort的范围是1024~65535
        !IsNumberStringValid(httpConfig.managementPort, 1024, 65535) || // managementPort的范围是1024~65535
        !IsNumberStringValid(httpConfig.alarmPort, 1024, 65535) || // alarmPort的范围是1024~65535
        !IsNumberStringValid(httpConfig.statusPort, 1024, 65535) || // statusPort的范围是1024~65535
        !IsNumberStringValid(httpConfig.externalPort, 1024, 65535)) {
        LOG_E("[%s] [Configure] Failed to read HTTP configuration from config file. The port is invalid. "
            "Expected port range: [1024, 65535], but got predPort: %s, managementPort: %s, alarmPort: %s, "
            "statusPort: %s, externalPort: %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str(),
            httpConfig.predPort.c_str(), httpConfig.managementPort.c_str(), httpConfig.alarmPort.c_str(),
            httpConfig.statusPort.c_str(), httpConfig.externalPort.c_str());
        return -1;
    }
    httpConfig.serverThreadNum = config.at("server_thread_num").template get<size_t>();
    httpConfig.clientThreadNum = config.at("client_thread_num").template get<size_t>();
    httpConfig.httpTimeoutS = config.at("http_timeout_seconds").template get<size_t>();
    httpConfig.keepAliveS = config.at("keep_alive_seconds").template get<size_t>();
    httpConfig.serverName = config.at("server_name").template get<std::string>();
    httpConfig.userAgent = config.at("user_agent").template get<std::string>();
    return 0;
}

int32_t Configure::ReadMetricsConfig(const nlohmann::json &config)
{
    if (!IsJsonBoolValid(config, "enable") ||
        !IsJsonIntValid(config, "trigger_size", 1, 10000)) { // trigger_size的范围是1~10000
        LOG_E("[%s] [Configure] Read metrics configuration from file failed, some parameters are invalid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return -1;
    }
    metricsConfig.metrics = config.at("enable").template get<bool>();
    metricsConfig.triggerSize = config.at("trigger_size").template get<size_t>();
    return 0;
}

int32_t Configure::ReadPrometheusMetricsConfig(const nlohmann::json &config)
{
    if (!IsJsonIntValid(config, "reuse_time", 1, 100)) { // reuse_time的范围是1~100
        LOG_E("[%s] [Configure] Read prometheus metrics configuration failed, some parameters are invalid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return -1;
    }
    promMetricsConfig.reuseTime = config.at("reuse_time").template get<size_t>();
    return 0;
}

int32_t Configure::ReadExceptionConfig(const nlohmann::json &config)
{
    if (!IsJsonIntValid(config, "max_retry", 0, 10) ||              // max_retry的范围是0~10
        !IsJsonIntValid(config, "schedule_timeout", 0, 3600) ||     // schedule_timeout的范围是0~3600
        !IsJsonIntValid(config, "first_token_timeout", 0, 3600) ||  // first_token_timeout的范围是0~3600
        !IsJsonIntValid(config, "infer_timeout", 0, MAX_INFER_TIME) ||        // infer_timeout的范围是0~65535
        !IsJsonIntValid(config, "tokenizer_timeout", 0, 3600)) {    // tokenizer_timeout的范围是0~3600
        LOG_E("[%s] [Configure] Read exception config failed, some parameters are invalid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return -1;
    }
    exceptionConfig.maxRetry = config.at("max_retry").template get<size_t>();
    exceptionConfig.scheduleTimeout = config.at("schedule_timeout").template get<size_t>();
    exceptionConfig.firstTokenTimeout = config.at("first_token_timeout").template get<size_t>();
    exceptionConfig.inferTimeout = config.at("infer_timeout").template get<size_t>();
    exceptionConfig.tokenizerTimeout = config.at("tokenizer_timeout").template get<size_t>();
    size_t minTimeout = std::min(exceptionConfig.firstTokenTimeout, exceptionConfig.inferTimeout);
    /*
        调度超时时间需要小于推理超时时间，避免高并发时序问题。
        具体规则：schedule_timeout + ADJUSTMENT_SECONDS(20秒) <= minTimeout
        如果schedule_timeout过大，例如和minTimeout都是60秒，会发生以下时序问题：
        1. 第59.9秒才完成调度，将请求转发到底层组件
        2. 第60秒调度器发现请求超时，发送stop请求
        3. 底层组件的stop thread比infer thread执行要快
        4. 可能会发生stop thread发现推理请求尚未入队，导致stop请求失败
        基于经验值，将schedule_timeout调整为minTimeout - ADJUSTMENT_SECONDS
        确保调度过程有20秒的安全余量，避免调度和推理过程在时间上产生冲突。
    */
    if (exceptionConfig.scheduleTimeout + ADJUSTMENT_SECONDS > minTimeout) {
        size_t originalScheduleTimeout = exceptionConfig.scheduleTimeout;
        exceptionConfig.scheduleTimeout = (minTimeout > ADJUSTMENT_SECONDS) ?
                                         (minTimeout - ADJUSTMENT_SECONDS) : 0;

        LOG_W("[%s] [Configure] schedule_timeout(%zu) should be less than the minimum of inferTimeout(%zu) "
            "and firstTokenTimeout(%zu), automatically adjusted to %zu to avoid high concurrency timing issues!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str(),
            originalScheduleTimeout, exceptionConfig.inferTimeout, exceptionConfig.firstTokenTimeout,
            exceptionConfig.scheduleTimeout);
    }
    return 0;
}

int32_t Configure::ReadLogInfo(const nlohmann::json &config, const std::string& configFile) const
{
    MINDIE::MS::DefaultLogConfig defaultLogConfig;
    defaultLogConfig.option.subModule = SubModule::MS_COORDINATOR;
    int32_t logInitRes = Logger::Singleton()->Init(defaultLogConfig, config, configFile);
    int logLevelIntervalMs = 1000;
    LogLevelDynamicHandler::Init(logLevelIntervalMs, "Coordinator", true);
    return logInitRes;
}

int32_t Configure::ReadBackupConfig(const nlohmann::json &config)
{
    if (!IsJsonBoolValid(config, "function_enable")) {
        LOG_E("[%s] [Configure] Read backup configuration from file failed, some parameters are invalid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return -1;
    }
    backupEnable = config.at("function_enable").template get<bool>();
    coordinatorBackUpConfig.funSw = config.at("function_enable").template get<bool>();
    if (backupEnable) {
        // 如果开启主备，才校验相关参数
        if (!IsJsonStringValid(config, "database_server_dns") ||
            !IsJsonIntValid(config, "database_server_port", PORT_MIN, PORT_MAX)) {
            LOG_E("[%s] [Configure] Read backup configuration from file failed, some parameters are invalid!",
                  GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
            return -1;
        }
        coordinatorBackUpConfig.serverDns = config.at("database_server_dns").get<std::string>();
        coordinatorBackUpConfig.serverPort = config.at("database_server_port").get<uint32_t>();
    }
    return 0;
}

static bool IsCacheAffinityParamsInvalid(const nlohmann::json &config)
{
    return (!IsJsonStringValid(config, "cache_size") || !IsJsonStringValid(config, "slots_thresh") ||
            !IsJsonStringValid(config, "block_thresh") ||
            !IsNumberStringValid(config["cache_size"], 1, 10000) || // cache_size的范围是1~10000
            !IsFloatStringValid(config["slots_thresh"], 0.0, 1.0) ||
            !IsFloatStringValid(config["block_thresh"], 0.0, 1.0));
}

static bool IsLoadBalanceParamsInvalid(const nlohmann::json &config)
{
    return (!IsJsonStringValid(config, "max_schedule_count") || !IsJsonStringValid(config, "reordering_type") ||
            !IsJsonStringValid(config, "max_res_num") ||
            !IsJsonStringValid(config, "res_limit_rate") || !IsJsonStringValid(config, "select_type") ||
            !IsNumberStringValid(config["max_schedule_count"], 1, 90000) || // max_schedule_count的范围是1~90000
            !IsNumberStringValid(config["reordering_type"], 1, 3) ||        // reordering_type的范围是1~3
            !IsNumberStringValid(config["max_res_num"], 1, 10000) ||        // max_res_num的范围是1~10000
            !IsFloatStringValid(config["res_limit_rate"], 0.0, 2000.0) ||   // res_limit_rate的范围是0.0~2000.0
            !IsNumberStringValid(config["select_type"], 1, 2) ||            // select_type的范围是1~2
            !IsJsonStringValid(config, "load_cost_values") ||
            (config["load_cost_values"] != "1, 0" && config["load_cost_values"] != "1, 1") ||
            !IsJsonStringValid(config, "load_cost_coefficient"));
}

static bool CheckSingleNodeValid(const std::string &schedulerType, const std::string &algorithmType)
{
    if (schedulerType.compare("default_scheduler") != 0) {
        LOG_E("[%s] [Configure] Read scheduler config from file failed. "
            "The 'single_node' deployment type only support 'default_scheduler' as the value of 'scheduler_type'!",
            GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::CONFIGURE).c_str());
        return false;
    }
    if (algorithmType.compare("cache_affinity") != 0 && algorithmType.compare("round_robin") != 0) {
        LOG_E("[%s] [Configure] Read scheduler config from file failed. "
            "The 'single_node' deployment type only support 'cache_affinity' or 'round_robin' "
            " as the value of 'algorithm_type'",
            GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::CONFIGURE).c_str());
        return false;
    }
    return true;
}

static bool CheckPDSeparateValid(const std::string &schedulerType, const std::string &algorithmType)
{
    if (schedulerType.compare("digs_scheduler") == 0) {
        if (algorithmType.compare("load_balance") != 0) {
            LOG_E("[%s] [Configure] Read scheduler config from file failed. The 'digs_scheduler' scheduler type "
                "supports 'load_balance' as as the value of 'algorithm_type'!",
                GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::CONFIGURE).c_str());
            return false;
        }
    } else if (schedulerType.compare("default_scheduler") == 0) {
        if (algorithmType.compare("round_robin") != 0) {
            LOG_E("[%s] [Configure] Read scheduler config from file failed. The 'default_scheduler' scheduler type "
                "supports 'round_robin' as as the value of 'algorithm_type'!",
                GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::CONFIGURE).c_str());
            return false;
        }
    } else {
        LOG_E(
            "[%s] [Configure] Read scheduler config from file failed. The 'pd_separate' deployment type only"
            " supports 'digs_scheduler' and 'default_scheduler' as the value of 'scheduler_type'!",
            GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::CONFIGURE).c_str());
        return false;
    }
    return true;
}

static bool IsSchedulerConfigValid(const nlohmann::json &config)
{
    if (!IsJsonStringValid(config, "deploy_mode") || !IsJsonStringValid(config, "scheduler_type") ||
        !IsJsonStringValid(config, "algorithm_type")) {
        LOG_E("[%s] [Configure] Read scheduler config from file failed!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return false;
    }
    std::string deployMode = config["deploy_mode"];
    std::string schedulerType = config["scheduler_type"];
    std::string algorithmType = config["algorithm_type"];
    if (deployMode.compare("single_node") != 0 && deployMode.compare("pd_separate") != 0 &&
        deployMode.compare("pd_disaggregation") != 0 && deployMode.compare("pd_disaggregation_single_container") != 0) {
        LOG_E("[%s] [Configure] Read scheduler config from file failed. 'deploy_mode' only support 'single_node'"
            ", 'pd_disaggregation', 'pd_disaggregation_single_container' and 'pd_separate'!",
            GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::CONFIGURE).c_str());
        return false;
    } else if (deployMode.compare("single_node") == 0) {
        if (!CheckSingleNodeValid(schedulerType, algorithmType)) {
            return false;
        }
    } else {
        if (!CheckPDSeparateValid(schedulerType, algorithmType)) {
            return false;
        }
    }
    if (algorithmType.compare("cache_affinity") == 0) {
        if (IsCacheAffinityParamsInvalid(config)) {
            LOG_E("[%s] [Configure] Read scheduler config from file failed. The value of 'cache_affinity' is invalid!",
                GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
            return false;
        }
    } else if (algorithmType.compare("load_balance") == 0) {
        if (IsLoadBalanceParamsInvalid(config)) {
            LOG_E("[%s] [Configure] Read scheduler config from file failed. The value of 'load_balance' is invalid!",
                GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
            return false;
        }
    }

    return true;
}

int32_t Configure::ReadSchedulerConfig(const nlohmann::json &config)
{
    if (!IsSchedulerConfigValid(config)) {
        return -1;
    }
    try {
        for (auto &elem : config.items()) {
            schedulerConfig.emplace(std::make_pair(elem.key(), elem.value()));
        }
    } catch (const std::exception& e) {
        LOG_E("[%s] [Configure] Read scheduler config from file failed, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONFIGURE).c_str(),
            e.what());
        return -1;
    }
    return 0;
}

static bool IsTlsValid(const nlohmann::json &mainObj, const std::string &tlsEnableKey,
    const std::string &tlsItemName)
{
    if (!IsJsonBoolValid(mainObj, tlsEnableKey)) {
        return false;
    }
    bool serverUseTls = mainObj[tlsEnableKey];
    if (serverUseTls) {
        if (!IsJsonObjValid(mainObj, tlsItemName) ||
            !IsJsonStringValid(mainObj[tlsItemName], "ca_cert") ||
            !IsJsonStringValid(mainObj[tlsItemName], "tls_cert") ||
            !IsJsonStringValid(mainObj[tlsItemName], "tls_key") ||
            !IsJsonStringValid(mainObj[tlsItemName], "tls_passwd") ||
            !IsJsonStringValid(mainObj[tlsItemName], "tls_crl", 0)) {
            return false;
        }
    }
    return true;
}

static bool IsJsonTlsInvalid(const nlohmann::json &config)
{
    return (!IsTlsValid(config, "controller_server_tls_enable", "controller_server_tls_items") ||
            !IsTlsValid(config, "request_server_tls_enable", "request_server_tls_items") ||
            !IsTlsValid(config, "mindie_client_tls_enable", "mindie_client_tls_items") ||
            !IsTlsValid(config, "mindie_mangment_tls_enable", "mindie_mangment_tls_items") ||
            !IsTlsValid(config, "etcd_server_tls_enable", "etcd_server_tls_items") ||
            !IsTlsValid(config, "alarm_client_tls_enable", "alarm_client_tls_items") ||
            !IsTlsValid(config, "external_tls_enable", "external_tls_items") ||
            !IsTlsValid(config, "status_tls_enable", "status_tls_items"));
}

static void ReadTlsItems(const nlohmann::json &config, std::string itemsStr, TlsItems &items)
{
    items.caCert = config[itemsStr]["ca_cert"];
    items.tlsCert = config[itemsStr]["tls_cert"];
    items.tlsKey = config[itemsStr]["tls_key"];
    items.tlsCrl = config[itemsStr]["tls_crl"];
    items.tlsPasswd = config[itemsStr]["tls_passwd"];
}

int32_t Configure::ReadTlsConfig(const nlohmann::json &config)
{
    if (IsJsonTlsInvalid(config)) {
        LOG_E("[%s] [Configure] ReadTlsConfig failed, some params invalid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return -1;
    }

    controllerServerTlsItems.tlsEnable = config["controller_server_tls_enable"];
    if (config["controller_server_tls_enable"]) {
        ReadTlsItems(config, "controller_server_tls_items", controllerServerTlsItems);
        controllerServerTlsItems.checkFiles = checkMountedFiles;
    }

    requestServerTlsItems.tlsEnable = config["request_server_tls_enable"];
    if (config["request_server_tls_enable"]) {
        ReadTlsItems(config, "request_server_tls_items", requestServerTlsItems);
        requestServerTlsItems.checkFiles = checkMountedFiles;
    }

    mindieClientTlsItems.tlsEnable = config["mindie_client_tls_enable"];
    if (config["mindie_client_tls_enable"]) {
        ReadTlsItems(config, "mindie_client_tls_items", mindieClientTlsItems);
        mindieClientTlsItems.checkFiles = checkMountedFiles;
    }

    mindieMgmtTlsItems.tlsEnable = config["mindie_mangment_tls_enable"];
    if (config["mindie_mangment_tls_enable"]) {
        ReadTlsItems(config, "mindie_mangment_tls_items", mindieMgmtTlsItems);
        mindieMgmtTlsItems.checkFiles = checkMountedFiles;
    }

    alarmClientTlsItems.tlsEnable = config["alarm_client_tls_enable"];
    if (config["alarm_client_tls_enable"]) {
        ReadTlsItems(config, "alarm_client_tls_items", alarmClientTlsItems);
        alarmClientTlsItems.checkFiles = checkMountedFiles;
    }

    externalTlsItems.tlsEnable = config["external_tls_enable"];
    if (config["external_tls_enable"]) {
        ReadTlsItems(config, "external_tls_items", externalTlsItems);
        externalTlsItems.checkFiles = checkMountedFiles;
    }

    statusTlsItems.tlsEnable = config["status_tls_enable"];
    if (config["status_tls_enable"]) {
        ReadTlsItems(config, "status_tls_items", statusTlsItems);
        statusTlsItems.checkFiles = checkMountedFiles;
    }
    
    coordinatorEtcdTlsItems.tlsEnable = config["etcd_server_tls_enable"];
    if (config["etcd_server_tls_enable"]) {
        ReadTlsItems(config, "etcd_server_tls_items", coordinatorEtcdTlsItems);
        coordinatorEtcdTlsItems.checkFiles = checkMountedFiles;
    }
    return 0;
}

int32_t Configure::ReadRequestLimit(const nlohmann::json &config)
{
    if (!IsJsonIntValid(config, "single_node_max_requests", 1, 15000) || // single_node_max_requests的范围是1~15000
        !IsJsonIntValid(config, "max_requests", 1, 90000) || // max_requests的范围是1~90000
        !IsJsonIntValid(config, "body_limit", 1, 20)) { // body_limit的范围是1~20
        LOG_E("[%s] [Configure] Read request limit from config file failed, some parameters are invalid!",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
        return -1;
    }
    reqLimit.singleNodeMaxReqs = config.at("single_node_max_requests").template get<size_t>();
    reqLimit.maxReqs = config.at("max_requests").template get<size_t>();
    reqLimit.bodyLimit = config.at("body_limit").template get<size_t>() * 1024 * 1024; // 1024 * 1024表示单位兆
    auto singleNodeMaxReqEnv = std::getenv(CONFIG_SINGLE_NODE_MAX_REQ_ENV);
    if (singleNodeMaxReqEnv != nullptr) {
        std::string singleNodeMaxReqStr = singleNodeMaxReqEnv;
        try {
            reqLimit.singleNodeMaxReqs = std::stoul(singleNodeMaxReqStr);
        } catch (const std::exception& e) {
            LOG_E("[%s] [Configure] ReadRequestLimit: %s", GetErrorCode(ErrorType::EXCEPTION,
                CoordinatorFeature::CONFIGURE).c_str(), e.what());
            return -1;
        }
    }
    auto maxReqEnv = std::getenv(CONFIG_MAX_REQ_ENV);
    if (maxReqEnv != nullptr) {
        std::string maxReqStr = maxReqEnv;
        try {
            reqLimit.maxReqs = std::stoul(maxReqStr);
        } catch (const std::exception& e) {
            LOG_E("[%s] [Configure] ReadRequestLimit: %s",
                GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONFIGURE).c_str(), e.what());
            return -1;
        }
    }
    if (reqLimit.singleNodeMaxReqs < 1 || reqLimit.singleNodeMaxReqs > SINGLE_NODE_MAX_REQ_NUM) {
        LOG_W("[%s] [Configure] Read request limit failed. The value of max number of requests for single node is out"
            "of limit[1-%lu], set %lu now.", GetWarnCode(ErrorType::WARNING, CoordinatorFeature::CONFIGURE).c_str(),
            SINGLE_NODE_MAX_REQ_NUM, SINGLE_NODE_MAX_REQ_NUM);
        reqLimit.singleNodeMaxReqs = SINGLE_NODE_MAX_REQ_NUM;
    }
    if (reqLimit.maxReqs < 1 || reqLimit.maxReqs > MAX_REQ_NUM) {
        LOG_W("[%s] [Configure] Read request limit failed. The value of max requests is out of limit[1-%lu], "
            "set %lu now.", GetWarnCode(ErrorType::WARNING, CoordinatorFeature::CONFIGURE).c_str(),
            MAX_REQ_NUM, MAX_REQ_NUM);
        reqLimit.maxReqs = MAX_REQ_NUM;
    }
    reqLimit.connMaxReqs = reqLimit.maxReqs;
    httpConfig.connectionPoolMaxConn = reqLimit.maxReqs + MAX_INSTANCES_NUM;
    return 0;
}

int32_t Configure::ReadStrTokenRateConfig(const nlohmann::json &config)
{
    try {
        strTokenRate = config.at("string_token_rate").template get<float>();
        if (strTokenRate < 1.0 || strTokenRate > 100.0) { // string_token_rate的范围是1.0~100.0
            LOG_E("[%s] [Configure] The value of field 'string_token_rate' read from config file is out of "
                "range[1.0, 100.0].", GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::CONFIGURE).c_str());
            return -1;
        }
        return 0;
    } catch (const std::exception& e) {
        LOG_E("[%s] [Configure] Read 'string_token_rate' from config file failed, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::CONFIGURE).c_str(), e.what());
        return -1;
    }
}
}