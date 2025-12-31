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
#include <set>
#include <iostream>
#include "ServerFactory.h"
#include "HttpClient.h"
#include "Logger.h"
#include "KubeResourceDef.h"
#include "Util.h"

namespace MINDIE::MS {
namespace {
    uint32_t g_maxInstanceNum = 256; // 256 当前限制最大实例规格，理论上可拓展，用户需要考虑系统资源是否充足
    int64_t g_maxPort = 65535; // 65535 最大端口号
    int64_t g_minPort = 1024; // 1024 最小端口号
};
class SingleNodeServer : public InferServerBase {
public:
    SingleNodeServer() = default;
    ~SingleNodeServer() override {}
    SingleNodeServer(const SingleNodeServer&) = delete;
    SingleNodeServer& operator=(const SingleNodeServer&) = delete;
    std::pair<int32_t, std::string> Init(HttpClientParams httpClientConfig, const std::string &statusPath) override;
    std::pair<int32_t, std::string> Deploy(const std::string &config) override;
    std::pair<int32_t, std::string> Update(const std::string &config) override;
    std::pair<int32_t, std::string> Unload() override;
    int32_t FromJson(__attribute__((unused)) const nlohmann::json &serverStatus) override {return 0;}
    std::pair<int32_t, std::string> GetDeployStatus() override;
private:
    int32_t GetPodInfo(nlohmann::json &modelInfo, std::map<std::string, std::string>::const_iterator &map,
        nlohmann::json &instanceStatus);
    int32_t HandleLoadReq(const std::string &resourceType);
    int32_t HandleDeleteReq();
    int32_t CreateClientReq(const std::string &url, boost::beast::http::verb type,
        const std::string &inputStr, std::string &response, HttpClient &client) const;
    int32_t ParseLoadParams(const std::string &str);
    std::string CreateDeployJson(const LoadServiceParams &params) const;
    std::string CreateServiceJson();
    int32_t CheckHealth(nlohmann::json &instanceStatus);
    HttpClient mKubeHttpClient;
    HttpClient mMindieHttpClient;
    HttpClientParams mClientParams;
    LoadServiceParams serviceParams;
    std::string mStatusFile;
    ResourcesInfo mKubeResources;
    uint32_t mTimeoutSeconds = 3;
    uint32_t mRetryTimes = 3;
};
std::pair<int32_t, std::string> SingleNodeServer::Init(HttpClientParams httpClientConfig, const std::string &statusPath)
{
    mClientParams = httpClientConfig;
    if (this->mKubeHttpClient.Init(httpClientConfig.k8sIP, std::to_string(httpClientConfig.k8sPort),
        httpClientConfig.k8sClientTlsItems) != 0 ||
        this->mMindieHttpClient.Init("", "", httpClientConfig.mindieClientTlsItems) != 0) {
        return std::make_pair(-1, "Init client failed");
    }
    this->mStatusFile = statusPath;
    return std::make_pair(0, "Init client success");
}

int32_t SingleNodeServer::CreateClientReq(const std::string &url, boost::beast::http::verb type,
    const std::string &inputStr, std::string &response, HttpClient &client) const
{
    std::map<boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    if (type == boost::beast::http::verb::patch) {
        map[boost::beast::http::field::content_type] = "application/strategic-merge-patch+json";
    } else {
        map[boost::beast::http::field::content_type] = "application/json";
    }
    Request req = {url, type, map, inputStr};
    int32_t code = 0;
    int32_t ret = client.SendRequest(req, mTimeoutSeconds, mRetryTimes, response, code);
    if (ret != 0) {
        return ret;
    }
    return code;
}

int32_t ParseResourceRequestParams(const nlohmann::json &mainObj, LoadServiceParams &serverParams)
{
    if (!mainObj.contains("resource_requests") || !mainObj["resource_requests"].contains("memory") ||
        !mainObj["resource_requests"].contains("cpu_core") || !mainObj["resource_requests"].contains("npu_type") ||
        !mainObj["resource_requests"].contains("npu_chip_num")) {
        LOG_E("[%s] [Deployer] Missing required keys in 'resource_requests': 'memory', 'cpu_core', 'npu_type', "
            "'npu_chip_num'.", GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }

    if (!mainObj["resource_requests"]["memory"].is_number_integer() ||
        !mainObj["resource_requests"]["npu_chip_num"].is_number_integer() ||
        !mainObj["resource_requests"]["npu_type"].is_string() ||
        !mainObj["resource_requests"]["cpu_core"].is_number_integer()) {
        LOG_E("[%s] [Deployer] Invalid value in 'resource_requests'. Ensure memory, cpu_core, npu_chip_num, "
            "and npu_type are of correct types.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    serverParams.npuType = mainObj["resource_requests"]["npu_type"];
    if (serverParams.npuType != "Ascend910" && serverParams.npuType != "Ascend310P") {
        LOG_E("[%s] [Deployer] Invalid 'npu_type' value for singel node server."
            " Supported types: 'Ascend910' or 'Ascend310P'",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }

    serverParams.cpuRequest = mainObj["resource_requests"]["cpu_core"];
    if (serverParams.cpuRequest < 1000 || serverParams.cpuRequest > 256000) { // 1000, 256000 cpu range
        LOG_E("[%s] [Deployer] CPU request 'cpu_core' value must in the range of [1000, 256000].",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    serverParams.npuNum = mainObj["resource_requests"]["npu_chip_num"];
    if (serverParams.npuNum < 1 || serverParams.npuNum > 8) { // 1 8 当前约束单机[1, 8]卡
        LOG_E("[%s] [Deployer] 'npu_chip_num' must be [1, 8].",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    serverParams.memRequest = mainObj["resource_requests"]["memory"];
    if (serverParams.memRequest < 1000 || serverParams.memRequest > 256000) { // 1000, 256000 memory range
        LOG_E("[%s] [Deployer] Memory request 'memory' must in the range of [1000MB, 256000MB].",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    return 0;
}

static bool IsValidPercentage(const std::string &percentage)
{
    if (percentage.size() <= 1 || percentage.back() != '%') {
        return false;
    }
    std::string numberPart = percentage.substr(0, percentage.size() - 1);
    int64_t number = 0;
    try {
        // 转换为整数并检查范围
        number = std::stoi(numberPart);
    } catch (...) {
        LOG_E("[%s] [Deployer] Number in percentage string %s is not valid.",
            GetErrorCode(ErrorType::EXCEPTION, DeployerFeature::SINGLENODE_SERVER).c_str(),
            numberPart.c_str());
        return false;
    }
    return number >= 0 && number <= 100; // 100 最大百分比
}

static int32_t ParseResourceParams(const nlohmann::json &mainObj, LoadServiceParams &serverParams)
{
    if (ParseResourceRequestParams(mainObj, serverParams) != 0) {
        return -1;
    }

    if (!mainObj.contains("replicas") || !mainObj["replicas"].is_number_integer()) {
        LOG_E("[%s] [Deployer] Lack of key 'replicas' or the value is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    serverParams.replicas = mainObj["replicas"];

    if (serverParams.replicas > g_maxInstanceNum || serverParams.replicas <= 0) {
        LOG_E("[%s] [Deployer] The 'replicas' value should be an integer in the range [1, %d], but it is %d.",
            GetErrorCode(ErrorType::RESOURCE_LIMIT, DeployerFeature::SINGLENODE_SERVER).c_str(),
            g_maxInstanceNum, serverParams.replicas);
        return -1;
    }

    if (!IsJsonIntValid(mainObj, "init_delay", 10, 1800)) { // 10 1800 存活探针容忍的启动时间
        return -1;
    }

    serverParams.initDealy = mainObj["init_delay"];

    if (ParseProbeSetting(mainObj, serverParams) != 0) {
        return -1;
    }
    if (!IsJsonIntValid(mainObj, "termination_grace_period_seconds", 0, 3600)) { // 0 3600 优雅退出容忍时间范围
        return -1;
    }

    serverParams.terminationGracePeriodSeconds = mainObj["termination_grace_period_seconds"];

    if (!IsJsonStringValid(mainObj, "max_surge")) {
        return -1;
    }
    if (!IsValidPercentage(mainObj["max_surge"])) {
        LOG_E("[%s] [Deployer] 'max_surge' is not a valid percentage.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    serverParams.maxSurge = mainObj["max_surge"];

    if (!IsJsonStringValid(mainObj, "max_unavailable")) {
        return -1;
    }
    if (!IsValidPercentage(mainObj["max_unavailable"])) {
        LOG_E("[%s] [Deployer] 'max_unavailable' is not a valid percentage.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    serverParams.maxUnavailable = mainObj["max_unavailable"];
    return 0;
}

int32_t ParseMindIEConfigParams(nlohmann::json &mainObj, bool mindieInitTlsSwitch, LoadServiceParams &serverParams)
{
    if (!IsJsonObjValid(mainObj, "mindie_server_config")) {
        return -1;
    }
    if (!IsJsonIntValid(mainObj["mindie_server_config"], "infer_port", g_minPort, g_maxPort)) {
        return -1;
    }
    serverParams.mindieServerPort = mainObj["mindie_server_config"]["infer_port"];
    if (!IsJsonIntValid(mainObj["mindie_server_config"], "management_port", g_minPort, g_maxPort)) {
        return -1;
    }
    serverParams.mindieServerMngPort = mainObj["mindie_server_config"]["management_port"];
    if (!IsJsonBoolValid(mainObj["mindie_server_config"], "enable_tls")) {
        return -1;
    }
    serverParams.mindieUseHttps = mainObj["mindie_server_config"]["enable_tls"];
    if (serverParams.mindieUseHttps != mindieInitTlsSwitch) {
        LOG_E("[%s] [Deployer] Dynamic loaded MindIE Server TLS switch must be consistent with the one that initialize "
            "the process", GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    if (!IsJsonStringValid(mainObj["mindie_server_config"], "mies_install_path")) {
        return -1;
    }
    serverParams.miesInstallPath = mainObj["mindie_server_config"]["mies_install_path"];
    if (!IsAbsolutePath(serverParams.miesInstallPath)) {
        return -1;
    }
    return 0;
}

int32_t SingleNodeServer::ParseLoadParams(const std::string &str)
{
    if (!CheckJsonStringSize(str) || !nlohmann::json::accept(str)) {
        printf("deploying json accept error!\r\n");
        return -1;
    }
    auto mainObj = nlohmann::json::parse(str, CheckJsonDepthCallBack);
    if (!IsJsonStringValid(mainObj, "server_name", 1, 48) || // 48 最大长度，超过k8s不支持
        !IsJsonStringValid(mainObj, "scheduler") || !IsJsonStringValid(mainObj, "service_type")) {
        return -1;
    }
    if (mainObj["scheduler"] != "default" && mainObj["scheduler"] != "volcano") {
        LOG_E("[%s] [Deployer] Scheduler must be \"default\" or \"volcano\"",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    serviceParams.scheduler = mainObj["scheduler"];
    serviceParams.name = mainObj["server_name"];
    serviceParams.nameSpace = "mindie";

    serviceParams.serverImage = "mindie:server";
    serviceParams.startupCmd = "/mnt/mindie-service/ms/run/run.sh";
    serviceParams.serverType = mainObj["service_type"];

    if (!IsJsonBoolValid(mainObj, "npu_fault_reschedule")) {
        return -1;
    }
    serviceParams.npuFaultReschedule = mainObj["npu_fault_reschedule"];
    if (serviceParams.serverType == "NodePort") {
        if (!IsJsonIntValid(mainObj, "service_port", 30000, 32767)) { // 30000, 32767, k8s Service port范围
            return -1;
        }
        serviceParams.servicePort = mainObj["service_port"];
    }
    if (serviceParams.serverType != "NodePort") {
        LOG_E("[%s] [Deployer] Only support 'NodePort' for 'service_port' field.",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }
    if (ParseResourceParams(mainObj, serviceParams) != 0) {
        return -1;
    }
    SetMountPath(serviceParams);
    if (ParseMindIEConfigParams(mainObj, mClientParams.mindieClientTlsItems.tlsEnable, serviceParams) != 0) {
        return -1;
    }
    return 0;
}

std::string SingleNodeServer::CreateServiceJson()
{
    nlohmann::json j;
    j["kind"] = "Service";
    j["apiVersion"] = "v1";
    j["metadata"] = {{"labels", {{"app", serviceParams.name}}}, {"name", serviceParams.name},
            {"namespace", serviceParams.nameSpace}};
    j["spec"] = {{"type", serviceParams.serverType}, {"ports", {{{"port", serviceParams.mindieServerPort},
            {"targetPort", serviceParams.mindieServerPort}, {"nodePort", serviceParams.servicePort}}}},
            {"selector", {{"app", serviceParams.name}}}};
    std::string jsonString = j.dump();
    return jsonString;
}
static void SetEnv(nlohmann::json &containers, std::string scheduler, std::string npuType,
    const LoadServiceParams &params)
{
    const std::vector<std::pair<std::string, nlohmann::json>> envVars = {
        {"HOST_IP", {{"valueFrom", {{"fieldRef", {{"fieldPath", "status.hostIP"}}}}}}},
        {"MIES_CONTAINER_IP", {{"valueFrom", {{"fieldRef", {{"fieldPath", "status.podIP"}}}}}}},
        {"MIES_CONTAINER_MANAGEMENT_IP", {{"valueFrom", {{"fieldRef", {{"fieldPath", "status.podIP"}}}}}}},
        {"MIES_CONFIG_JSON_PATH", {{"value", "/mnt/mindie-service/ms/config/config.json"}}},
        {"MIES_INSTALL_PATH", {{"value", params.miesInstallPath}}},
        {"MINDIE_USE_HTTPS", {{"value", params.mindieUseHttps ? "true" : "false"}}}
    };

    containers["env"] = nlohmann::json::array();
    for (const auto& [name, value] : envVars) {
        nlohmann::json envVar;
        envVar["name"] = name;
        for (auto it = value.begin(); it != value.end(); ++it) {
            envVar[it.key()] = it.value();
        }
        containers["env"].push_back(envVar);
    }

    if (scheduler == "volcano") {
        // volcano需要，k8s默认调度器不需要
        nlohmann::json j1 = {{"name", "ASCEND_VISIBLE_DEVICES"}, {"valueFrom", {{"fieldRef",
            {{"fieldPath", "metadata.annotations['huawei.com/" + npuType + "']"}}}}}};
        containers["env"].emplace_back(j1);
    }
}

void SetProbe(nlohmann::json &containers, const LoadServiceParams &params)
{
    std::string exportEnv = "export HSECEASY_PATH=$MIES_INSTALL_PATH/lib;"
            "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$MIES_INSTALL_PATH/lib;"
            "export MINDIE_UTILS_HTTP_CLIENT_CTL_CONFIG_FILE_PATH=$MIES_INSTALL_PATH/conf/http_client_ctl.json;";
    containers["startupProbe"]["exec"]["command"] = {"/bin/bash", "-c", exportEnv +
        " $MIES_INSTALL_PATH/bin/http_client_ctl $MIES_CONTAINER_MANAGEMENT_IP " +
        std::to_string(params.mindieServerMngPort) + " /v2/health/live 3 0"};
    containers["startupProbe"]["periodSeconds"] = 1; // 1 执行探测的时间间隔
    containers["startupProbe"]["failureThreshold"] = params.initDealy; // 1 ready可出现问题次数阈值
    std::string readyUrl = params.detectInnerError ? "/health/timed-" +
        std::to_string(params.readinessTimeout) : "/v2/health/ready";
    containers["readinessProbe"]["exec"]["command"] = {"/bin/bash", "-c",  exportEnv +
        " $MIES_INSTALL_PATH/bin/http_client_ctl $MIES_CONTAINER_MANAGEMENT_IP " +
        std::to_string(params.mindieServerMngPort) + " " + readyUrl +
        " " + std::to_string(params.readinessTimeout) + " 0"};

    containers["readinessProbe"]["periodSeconds"] = 5; // 5 执行探测的时间间隔
    containers["readinessProbe"]["timeoutSeconds"] = params.readinessTimeout; // 执行探测的时间间隔
    containers["readinessProbe"]["failureThreshold"] = params.readinessFailureThreshold; // ready可出现问题次数阈值
    std::string liveUrl = params.detectInnerError ? "/health/timed-" +
        std::to_string(params.livenessTimeout) : "/v2/health/live";
    containers["livenessProbe"]["exec"]["command"] = {"/bin/bash", "-c", exportEnv +
        " $MIES_INSTALL_PATH/bin/http_client_ctl $MIES_CONTAINER_MANAGEMENT_IP " +
        std::to_string(params.mindieServerMngPort) + " " +
        liveUrl + " " + std::to_string(params.livenessTimeout) + " 0"};
    containers["livenessProbe"]["periodSeconds"] = 3; // 3 执行探测的时间间隔
    containers["livenessProbe"]["timeoutSeconds"] = params.livenessTimeout; // 执行探测的超时时间
    containers["livenessProbe"]["failureThreshold"] = params.livenessFailureThreshold; // live可出现问题次数阈值
}

void SetShareMemVolumn(nlohmann::json &podSpec, nlohmann::json &containers)
{
    nlohmann::json volumeShm = nlohmann::json::object();  // 共享内存设置
    volumeShm["name"] = "dshm";
    volumeShm["emptyDir"] = nlohmann::json::object();
    volumeShm["emptyDir"]["medium"] = "Memory";
    volumeShm["emptyDir"]["sizeLimit"] = "1Gi";
    podSpec["volumes"].emplace_back(volumeShm);
    nlohmann::json mountShm = nlohmann::json::object(); // 共享内存设置
    mountShm["name"] = "dshm";
    mountShm["mountPath"] = "/dev/shm";
    containers["volumeMounts"].emplace_back(mountShm);
}

nlohmann::json SetPodSpec(const LoadServiceParams &params)
{
    nlohmann::json firstJ;
    nlohmann::json secondJ;
    nlohmann::json thirdJ;
    secondJ["image"] = params.serverImage;
    secondJ["imagePullPolicy"] = "IfNotPresent";
    secondJ["name"] = params.name;
    thirdJ["allowPrivilegeEscalation"] = false;
    thirdJ["capabilities"]["drop"] = {"ALL"};
    thirdJ["seccompProfile"]["type"] = "RuntimeDefault";
    secondJ["securityContext"] = thirdJ;
    SetEnv(secondJ, params.scheduler, params.npuType, params);
    SetProbe(secondJ, params);

    secondJ["volumeMounts"] = nlohmann::json::array();
    SetShareMemVolumn(firstJ, secondJ);
    SetHostPathVolumns(firstJ, secondJ, params);
    secondJ["command"] =  {"/bin/bash", "-c", params.startupCmd};
    secondJ["ports"] = {{{"containerPort", params.mindieServerPort}}};
    secondJ["resources"] = {{"requests", {{"huawei.com/" + params.npuType, params.npuNum},
            {"memory", std::to_string(params.memRequest) + "Mi"}, {"cpu", std::to_string(params.cpuRequest) + "m"}}},
            {"limits", {{"huawei.com/" + params.npuType, params.npuNum},
            {"memory", std::to_string(params.memRequest * 2) + "Mi"},
            {"cpu", std::to_string(params.cpuRequest * 2) + "m"}}}};

    std::string exportEnv = "export HSECEASY_PATH=$MIES_INSTALL_PATH/lib;"
            "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$MIES_INSTALL_PATH/lib;"
            "export MINDIE_UTILS_HTTP_CLIENT_CTL_CONFIG_FILE_PATH=$MIES_INSTALL_PATH/conf/http_client_ctl.json;";

    secondJ["lifecycle"]["preStop"]["exec"]["command"] = {"/bin/bash", "-c", exportEnv +
        "sleep 1; $MIES_INSTALL_PATH/bin/http_client_ctl $MIES_CONTAINER_MANAGEMENT_IP " +
        std::to_string(params.mindieServerMngPort) + std::string(" /stopService 3 0")};
    if (params.scheduler == "volcano") {
        firstJ["schedulerName"] = "volcano";
    }
    if (params.npuType == "Ascend910") {
        firstJ["nodeSelector"] = {{"accelerator", "huawei-Ascend910"}};
    } else {
        firstJ["nodeSelector"] = {{"accelerator", "huawei-Ascend310P"}};
    }
    firstJ["containers"] = {secondJ};
    firstJ["automountServiceAccountToken"] = false;
    firstJ["terminationGracePeriodSeconds"] = params.terminationGracePeriodSeconds;
    return firstJ;
}

std::string SingleNodeServer::CreateDeployJson(const LoadServiceParams &params) const
{
    auto podSpec = SetPodSpec(params);
    std::string label = params.npuType == "Ascend910" ? "ascend-910" : "ascend-310P";
    nlohmann::json firstJ;
    firstJ["metadata"] = {{"labels", {{"app", params.name}, {"pod-rescheduling", "on"},
        {"ring-controller.atlas", label}}}};
    if (params.npuFaultReschedule) {
        firstJ["metadata"]["labels"]["fault-scheduling"] = "grace";
    }
    firstJ["spec"] = podSpec;
    nlohmann::json secondJ;
    secondJ["replicas"] = params.replicas;
    secondJ["strategy"] = {{"type", "RollingUpdate"},
        {"rollingUpdate", {{"maxUnavailable", params.maxUnavailable}, {"maxSurge", params.maxSurge}}}};
    secondJ["selector"] = {{"matchLabels", {{"app", params.name}}}};
    secondJ["template"] = firstJ;
    nlohmann::json deployJ;
    deployJ["apiVersion"] = "apps/v1";
    deployJ["kind"] = "Deployment";
    deployJ["metadata"] = {{"name", params.name}, {"namespace", params.nameSpace}, {"labels", {{"app", params.name}}}};
    deployJ["spec"] = secondJ;
    std::string jsonString = deployJ.dump();
    return jsonString;
}

int32_t SingleNodeServer::HandleLoadReq(const std::string &resourceType)
{
    std::set<int32_t> statusOk = {200, 201, 202, 203}; // 200 201 202 203: status ok
    std::string response = "";
    std::string prefix = resourceType == "deployments" ? "/apis/apps/v1/namespaces/" : "/api/v1/namespaces/";
    std::string url = prefix + serviceParams.nameSpace + "/" + resourceType + "/" + serviceParams.name;
    int32_t ret = CreateClientReq(url, boost::beast::http::verb::get, "", response, mKubeHttpClient);
    if (statusOk.count(ret) != 0) {
        HandleDeleteReq(); // 这里不判断异常是必须把loadedResource里的资源全部删除
        LOG_E("[%s] [Deployer] Server has been created before.",
            GetErrorCode(ErrorType::OPERATION_REPEAT, DeployerFeature::SINGLENODE_SERVER).c_str());
        return -1;
    }

    std::string jsonString = "";
    if (resourceType == "deployments") {
        jsonString = CreateDeployJson(serviceParams);
    } else {
        jsonString = CreateServiceJson();
    }
    url = prefix + serviceParams.nameSpace + "/" + resourceType;
    ret = CreateClientReq(url, boost::beast::http::verb::post, jsonString, response, mKubeHttpClient);
    if (statusOk.count(ret) == 0) {
        HandleDeleteReq(); // 这里不判断异常是必须把loadedResource里的资源全部删除
        LOG_E("[%s] [Deployer] Load %s failed which leads to load service failed.",
              GetErrorCode(ErrorType::OPERATION_REPEAT, DeployerFeature::SINGLENODE_SERVER).c_str(), resourceType);
        return -1;
    }
    return 0;
}

std::pair<int32_t, std::string> SingleNodeServer::Deploy(const std::string &config)
{
    if (ParseLoadParams(config) != 0) {
        return std::make_pair(-1, "ParseLoadParams failed");
    }

    if (HandleLoadReq("deployments") != 0) {
        return std::make_pair(-1, "Deploy deployments failed");
    }
    mKubeResources.deploymentNames.emplace_back(serviceParams.name);

    if (serviceParams.serverType == "NodePort") {
        if (HandleLoadReq("services") != 0) {
            return std::make_pair(-1, "deploy services failed");
        }
        mKubeResources.serviceName = serviceParams.name;
    }
    serviceParams.createTime = GetCurrentDateTimeString();
    serviceParams.updateTime = "";
    LOG_M("[Create] Success to create Deployment and Service!");
    return std::make_pair(0, "Creating the server!");
}

int32_t SingleNodeServer::HandleDeleteReq()
{
    std::set<int32_t> statusOk = {200, 201, 202, 203}; // 200 201 202 203: status ok
    int32_t error = 0;
    std::string response = "";
    if (mKubeResources.serviceName.length() != 0) {
        std::string url = "/api/v1/namespaces/" + serviceParams.nameSpace + "/services/" + mKubeResources.serviceName;
        int32_t ret = CreateClientReq(url, boost::beast::http::verb::delete_, "", response, mKubeHttpClient);
        if (statusOk.count(ret) == 0) {
            std::cout<<"delete the services with " + mKubeResources.serviceName + " failed"<<std::endl;
            error = -1;
        }
    }
    for (uint32_t i = 0; i < mKubeResources.deploymentNames.size(); i++) {
        std::string url =
            "/apis/apps/v1/namespaces/" + serviceParams.nameSpace + "/deployments/" + mKubeResources.deploymentNames[i];
        int32_t ret = CreateClientReq(url, boost::beast::http::verb::delete_, "", response, mKubeHttpClient);
        if (statusOk.count(ret) == 0) {
            std::cout<<"delete the deployments with " + mKubeResources.deploymentNames[i] + " failed"<<std::endl;
            error = -1;
        }
    }
    return error;
}

std::pair<int32_t, std::string> SingleNodeServer::Unload()
{
    auto ret = HandleDeleteReq();
    if (ret != 0) {
        LOG_E("[%s] [Deployer] Unload server failed!",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::SINGLENODE_SERVER).c_str());
        return std::make_pair(ret, "unload server failed!");
    }
    LOG_M("[Clear] Succeed to clear resources.");
    return std::make_pair(ret, "succeed to clear resources");
}

std::pair<int32_t, std::string> SingleNodeServer::Update(const std::string &config)
{
    std::set<int32_t> statusOk = {200, 201, 202, 203}; // 200 201 202 203: status ok
    LoadServiceParams params = serviceParams;
    if (!CheckJsonStringSize(config) || !nlohmann::json::accept(config)) {
        return std::make_pair(-1, "json accept error in UpdateServer.");
    }
    auto mainObj = nlohmann::json::parse(config, CheckJsonDepthCallBack);
    std::string nameSpace = serviceParams.nameSpace;
    std::string name = mainObj["server_name"];
    std::string response = "";

    std::string url =
        "/apis/apps/v1/namespaces/" + nameSpace + "/deployments/" + name + "?fieldManager=kubectl-rollout";
    nlohmann::json j = {{"spec", {{"template", {{"metadata", {{"annotations",
        {{"kubectl.kubernetes.io/restartedAt", GetCurrentDateTimeString()}}}}}}}}}};
    std::string inStr = j.dump();
    int32_t ret = CreateClientReq(url, boost::beast::http::verb::patch, inStr, response, mKubeHttpClient);
    if (statusOk.count(ret) == 0) {
        return std::make_pair(-1, "update with model or images changed failed.");
    }

    serviceParams = std::move(params);
    serviceParams.updateTime = GetCurrentDateTimeString();
    LOG_M("[Update] Update server success!");
    return std::make_pair(0, "update server success.");
}

int32_t SingleNodeServer::CheckHealth(nlohmann::json &instanceStatus)
{
    std::string outStr;
    std::set<int32_t> statusOk = {200, 201, 202, 203}; // 200 201 202 203: status ok
    int32_t ret = CreateClientReq("/v2/health/ready", boost::beast::http::verb::get, "", outStr, mMindieHttpClient);
    if (statusOk.count(ret) == 0) {
        instanceStatus["readiness"] = false;
        instanceStatus["message"] = "this mindie_server is not ready.";
    } else {
        instanceStatus["readiness"] = true;
    }

    ret = CreateClientReq("/v2/health/live", boost::beast::http::verb::get, "", outStr, mMindieHttpClient);
    if (statusOk.count(ret) == 0) {
        instanceStatus["liveness"] = false;
        instanceStatus["message"] = "this mindie_server is not live.";
    } else {
        instanceStatus["liveness"] = true;
    }
    return 0;
}

int32_t SingleNodeServer::GetPodInfo(nlohmann::json &modelInfo, std::map<std::string, std::string>::const_iterator &map,
    nlohmann::json &instanceStatus)
{
    std::set<int32_t> statusOk = {200, 201, 202, 203}; // 200 201 202 203: status ok

    instanceStatus["pod_name"] = map->first;
    if (!CheckJsonStringSize(map->second) || !nlohmann::json::accept(map->second)) {
        instanceStatus["message"] = "status in GetDeployStatus is not json.";
        return -1;
    }
    auto mainObj = nlohmann::json::parse(map->second, CheckJsonDepthCallBack);
    if (!IsJsonStringValid(mainObj, "phase")) {
        instanceStatus["message"] = "phase value is not string.";
        return -1;
    }
    if (mainObj.at("phase") != "Running") {
        instanceStatus["message"] = "This server is not Running now.";
        return -1;
    }
    if (!IsJsonStringValid(mainObj, "podIP")) {
        instanceStatus["message"] = "podIP is not string.";
        return -1;
    }
    if (!IsValidIp(mainObj["podIP"])) {
        instanceStatus["message"] = "podIP is not valid ipv4 address.";
        return -1;
    }
    this->mMindieHttpClient.SetHostAndPort(mainObj["podIP"], std::to_string(serviceParams.mindieServerMngPort));
    auto ret = CheckHealth(instanceStatus);
    if (ret != 0) {
        return ret;
    }
    std::string response = "";
    ret = CreateClientReq("/info", boost::beast::http::verb::get, "", response, mMindieHttpClient);
    if (statusOk.count(ret) == 0) {
        instanceStatus["message"] = "get mindie_server info failed.";
        return -1;
    }
    if (!CheckJsonStringSize(response) || !nlohmann::json::accept(response)) {
        instanceStatus["message"] = "info from mindie-server is not a valid json.";
        return -1;
    }
    modelInfo = nlohmann::json::parse(response, CheckJsonDepthCallBack);
    return 0;
}

std::pair<int32_t, std::string> SingleNodeServer::GetDeployStatus()
{
    std::set<int32_t> statusOk = {200, 201, 202, 203}; // 200 201 202 203: status ok
    std::string response = "";
    std::string url = "/api/v1/namespaces/" + serviceParams.nameSpace + "/pods?labelSelector=app=" +
        serviceParams.name;
    int32_t ret = CreateClientReq(url, boost::beast::http::verb::get, "", response, mKubeHttpClient);
    if (statusOk.count(ret) == 0) {
        return std::make_pair(-1, "Service is not exist.");
    }
    if (!CheckJsonStringSize(response) || !nlohmann::json::accept(response)) {
        return std::make_pair(-1, "Response in GetDeployStatus is not json.");
    }
    auto mainObj = nlohmann::json::parse(response, CheckJsonDepthCallBack);
    std::string status;
    std::map<std::string, std::string> podStatus;
    nlohmann::json serverStatus;
    serverStatus["instances_status"] = nlohmann::json::array();

    if (!IsJsonArrayValid(mainObj, "items", 1, g_maxInstanceNum)) {
        return std::make_pair(-1, "Pod items is invalid");
    }

    auto items = mainObj.at("items");
    std::string name = serviceParams.name;
    for (uint32_t i = 0; i < items.size(); i++) {
        if (!items[i].contains("metadata")) {
            return std::make_pair(-1, "Pod item does not contain metadata");
        }
        if (!IsJsonStringValid(items[i]["metadata"], "name")) {
            return std::make_pair(-1, "Pod item metadata does not contain name");
        }
        std::string podName = items[i].at("metadata").at("name");
        if (strncmp(podName.c_str(), name.c_str(), name.length()) == 0 && IsJsonObjValid(items[i], "status")) {
            status = items[i].at("status").dump();
            podStatus[podName] = status;
        }
    }
    nlohmann::json modelConfig;
    for (auto iter = podStatus.cbegin(); iter != podStatus.cend(); ++iter) {
        nlohmann::json instanceStatus;
        auto retVal = GetPodInfo(modelConfig, iter, instanceStatus);
        (void)retVal;
        serverStatus["instances_status"].push_back(instanceStatus);
    }
    serverStatus["model_info"] = modelConfig;
    serverStatus["server_name"] = serviceParams.name;
    LOG_M("[Get] Success to get deploy status of the server %s.", serviceParams.name.c_str());
    return std::make_pair(0, serverStatus.dump());
}

REGISTER_INFER_SERVER(SingleNodeServer, mindie_single_node);
}