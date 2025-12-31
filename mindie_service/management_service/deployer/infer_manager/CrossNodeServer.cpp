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
#include <sys/types.h>
#include <arpa/inet.h>
#include <chrono>
#include <iomanip>
#include <chrono>
#include <set>
#include "nlohmann/json.hpp"
#include "boost/asio.hpp"
#include "boost/beast/version.hpp"
#include "boost/beast/http.hpp"
#include "ConfigParams.h"
#include "HttpClient.h"
#include "ServerFactory.h"
#include "Logger.h"
#include "StatusHandler.h"
#include "KubeResourceDef.h"
#include "Util.h"

namespace MINDIE::MS {

enum class DeployStatus {
    CREATING,
    CREATED,
    FAILED,
    STOPPING
};

enum class InstanceStatus {
    UNREADY,
    READY,
    ABNORMAL,
};

enum class RestoreState {
    NONE,
    RECREATING,
    PENDING,
};

struct InferInstance {
    RestoreState restoreState;
    std::string nameSpace;
    std::chrono::seconds masterPodCreateTime;
    std::string masterPodIp;
    std::string deploymentName;
    std::string ranktableName;
    InstanceStatus status;
};

namespace {
    uint32_t g_findMaterPodInterval = 3;
    uint32_t g_monitorInstanceInterval = 3;  // 3 监控实例健康状态间隔时间，单位秒
    uint64_t g_minCrossNodeNum = 2; // 2 最小跨节点数
    uint64_t g_maxCrossNodeNum = 4; // 4 最大跨节点数
    uint32_t g_maxInstanceNum = 1; // 1 当前限制最大实例规格，理论上可拓展
    int64_t g_maxPort = 65535; // 65535 最大端口号
    int64_t g_minPort = 1024; // 1024 最小端口号
};

class CrossNodeServer : public InferServerBase {
public:
    CrossNodeServer() = default;
    ~CrossNodeServer() override
    {
        this->mDeployStatus = DeployStatus::STOPPING;
        if (this->mCreateServiceThread != nullptr) {
            this->mCreateServiceThread->join();
            this->mCreateServiceThread = nullptr;
        }
        if (this->mMonitoringThread != nullptr) {
            this->mMonitoringThread->join();
            this->mMonitoringThread = nullptr;
        }
    }
    CrossNodeServer(const CrossNodeServer&) = delete;
    CrossNodeServer& operator=(const CrossNodeServer&) = delete;
    std::pair<int32_t, std::string> Init(HttpClientParams httpClientCfg, const std::string &statusPath) override;
    std::pair<int32_t, std::string> Deploy(const std::string &config) override;
    std::pair<int32_t, std::string> Update(const std::string &config) override;
    std::pair<int32_t, std::string> Unload() override;
    std::pair<int32_t, std::string> GetDeployStatus() override;
    int32_t FromJson(const nlohmann::json &serverStatus) override;
private:
    std::pair<int32_t, std::string> ClearResources();
    int32_t SendKubeHttpRequest(const std::string& target, Http::verb method,
        const std::string &header, const std::string &reqBody, std::string& responseBody);
    int32_t GetFromMindIEServer(HttpClient &httpClient, const std::string& target, std::string& responseBody,
        uint32_t timeoutSeconds, uint32_t retryTimes) const;
    int32_t ModifyRanktable(const nlohmann::json &ranktable, uint32_t ind, bool &isLabeled);
    int32_t FindAndLabelMasterPod(uint32_t ind, bool &isLabeled);
    int32_t LabelMasterPod(const std::string &masterContainerIp, uint32_t ind, bool &isLabeled);
    int32_t CreateService(const LoadServiceParams &serverParams);
    void AssociateMasterPod(const LoadServiceParams &serverParams);
    int32_t CreateInstanceResource(uint32_t ind, const std::string &nameSpace,
        const std::string &ranktableName, const std::string &deplymentName);
    int32_t RetryLabelMasterPod(uint32_t ind, RestoreState &restoreState);
    int32_t RecoverInstance(uint32_t ind, InferInstance &instacne);
    void CheckInstanceStatus(uint32_t ind, InferInstance &instance);
    int32_t FromDeployment(const std::string &deploymentName);
    int32_t FromReplicaResource(uint32_t ind);
    int32_t GetMasterPodIPByLabel(const std::string &serverName, const std::string &deploymentName,
        std::string &podIP);
    void MinitoringInstance();
    void UpdateInstanceStatus();
    int GetModelInfo(nlohmann::json &modelInfo);
    int GetServerStatus(const std::string &url, bool &isOk);
    std::pair<int32_t, std::string> CreateServiceThread(std::string &logCode);
    HttpClientParams mClientParams;
    HttpClient mKubeHttpClient;
    HttpClient mMointerMindieHttpClient;
    HttpClient mMindieGetStatusHttpClient;
    uint32_t mTimeoutSeconds = 3;
    uint32_t mRetryTimes = 2;
    std::string mServerName;
    ResourcesInfo mKubeResources;
    LoadServiceParams mServiceParams;
    bool mUseService = false;
    std::map<uint32_t, InferInstance> mInferInstance;
    std::string mStatusFile;
    DeployStatus mDeployStatus = DeployStatus::CREATING;
    std::string mStatusMessage = "start creating the app";
    std::unique_ptr<std::thread> mCreateServiceThread = nullptr;
    std::unique_ptr<std::thread> mMonitoringThread = nullptr;
    std::string mServiceNameSuffix = "-service";
    std::string mDeploymentNameMiddle = "-deployment-";
    std::string mRanktableNamePrefix = "rings-config-";
};

std::pair<int32_t, std::string> CrossNodeServer::Init(HttpClientParams httpClientCfg,
    const std::string &statusPath)
{
    mClientParams = httpClientCfg;
    if (this->mKubeHttpClient.Init(httpClientCfg.k8sIP, std::to_string(httpClientCfg.k8sPort),
        httpClientCfg.k8sClientTlsItems) != 0 ||
        this->mMointerMindieHttpClient.Init("", "", httpClientCfg.mindieClientTlsItems) != 0
        || this->mMindieGetStatusHttpClient.Init("", "", httpClientCfg.mindieClientTlsItems) != 0) {
        return std::make_pair(-1, "Init client failed");
    }
    this->mStatusFile = statusPath;
    return std::make_pair(0, "");
}

static std::string CreateConfigMapJson(std::string nameSpace, std::string name, std::string dataName,
    std::string config, ResourceLabel label)
{
    nlohmann::json jsonObj;
    jsonObj["kind"] = "ConfigMap";
    jsonObj["apiVersion"] = "v1";
    jsonObj["metadata"] = {{"name", name}, {"namespace", nameSpace}};
    jsonObj["data"] = {{dataName, config}};
    if (label.key.size() != 0) {
        jsonObj["metadata"]["labels"][label.key] = label.value;
    }
    std::string jsonString = jsonObj.dump();
    return jsonString;
}

std::pair<int32_t, std::string> CrossNodeServer::GetDeployStatus()
{
    std::map<RestoreState, std::string> restartStateMap = {
        { RestoreState::NONE, "none" },
        { RestoreState::RECREATING, "recreating" },
        { RestoreState::PENDING, "pending" }
    };

    std::map<DeployStatus, std::string> serverStatusStr = {
        {DeployStatus::CREATING, "creating"},
        {DeployStatus::CREATED, "created"},
        {DeployStatus::FAILED, "failed"},
        {DeployStatus::STOPPING, "stopping"}
    };

    nlohmann::json jsonObj;
    jsonObj["server_name"] = this->mServerName;
    jsonObj["server_status_msg"] =  serverStatusStr.at(this->mDeployStatus)  + ": " + this->mStatusMessage;
    jsonObj["instances_status"] = nlohmann::json::array();

    for (const auto &iter : mInferInstance) {
        uint32_t ind = iter.first;
        InferInstance instance = iter.second;
        nlohmann::json instanceStatusJsonObj;
        instanceStatusJsonObj["instance_id"] = ind;
        bool isOk = false;
        auto ret = GetServerStatus("/health/timed-5", isOk);
        if (ret == 0 && isOk) {
            instanceStatusJsonObj["readiness"] = true;
        } else {
            instanceStatusJsonObj["readiness"] = false;
        }
        ret = GetServerStatus("/v2/health/live", isOk);
        if (ret == 0 && isOk) {
            instanceStatusJsonObj["liveness"] = true;
        } else {
            instanceStatusJsonObj["liveness"] = false;
        }
        instanceStatusJsonObj["restore_state"] = restartStateMap[instance.restoreState];
        jsonObj["instances_status"].push_back(instanceStatusJsonObj);
    }
    nlohmann::json modelInfo;
    GetModelInfo(modelInfo);
    jsonObj["model_info"] = modelInfo;
    LOG_M("[Get] Success to get deploy status of the server %s, detail: %s",
        this->mServerName.c_str(), jsonObj.dump().c_str());
    return std::make_pair(0, jsonObj.dump());
}
void SetContainerIPEnv(nlohmann::json &containers)
{
    const std::vector<std::pair<std::string, std::string>> envVars = {
        {"MIES_CONTAINER_IP", "status.podIP"},
        {"MIES_CONTAINER_MANAGEMENT_IP", "status.podIP"},
        {"POD_IP", "status.podIP"},
        {"HOST_IP", "status.hostIP"}
    };

    for (const auto& [name, fieldPath] : envVars) {
        containers["env"].emplace_back(
            nlohmann::json({
                {"name", name},
                {"valueFrom", {
                        {"fieldRef", {
                            {"fieldPath", fieldPath}
                        }
                        }
                    }
                }
            })
        );
    }

    return;
}

void SetContainerEnv(nlohmann::json &containers, const LoadServiceParams &serverParams)
{
    containers["env"] = {
        {
            {"name", "MINDIE_SERVER_DISTRIBUTE"},
            {"value", "1"},
        },
        {
            {"name", "MINDIE_SERVER_PROBE_ONLY"},
            {"value", "1"},
        },
        {
            {"name", "RANK_TABLE_FILE"},
            {"value", "/mnt/mindie-service/ms/writable-data/hccl.json"}
        },
        {
            {"name", "MIES_CONFIG_JSON_PATH"},
            {"value", "/mnt/mindie-service/ms/config/config.json"}
        },
        {
            {"name", "MIES_INSTALL_PATH"},
            {"value", serverParams.miesInstallPath}
        },
        {
            {"name", "MINDIE_USE_HTTPS"},
            {"value", serverParams.mindieUseHttps ? "true" : "false"}
        }
    };
    SetContainerIPEnv(containers);
}

void SetContainerCommand(nlohmann::json &containers, const std::string &startupCmd)
{
    containers["command"] = {"/bin/bash", "-c",
        R"(while true; do
            RANKTABLEFILE_TMP=/user/serverid/devindex/config/..data/hccl.json;
            json_string=$(cat $RANKTABLEFILE_TMP);
            echo $json_string;
            status=$(echo $json_string | grep -o '\"status\":\"[^\"]*' | sed 's/\"status\":\"//');
            echo $status;
            if [[ $status = "completed" ]]; then
                echo "status is completed";
                cp /user/serverid/devindex/config/..data/hccl.json /mnt/mindie-service/ms/writable-data/hccl.json
                chmod 640 /mnt/mindie-service/ms/writable-data/hccl.json
                break;
            fi;
            sleep 1;
            done;                              )"
            + startupCmd
    };
}

void SetAntiPod(nlohmann::json &deploymentJson, std::string deploymentName)
{
    // pod反亲和部署
    deploymentJson["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"] = {
        {"requiredDuringSchedulingIgnoredDuringExecution", {
            {
                {"labelSelector", {
                    {"matchExpressions", {
                        {
                            {"key", "deploy-name"},
                            {"operator", "In"},
                            {"values", {deploymentName}}
                        }
                    }}
                }},
                {"topologyKey", "kubernetes.io/hostname"}
            }
        }}
    };
}

void SetVolumns(nlohmann::json &podSpec, nlohmann::json &containers,
    const LoadServiceParams &serverParams, std::string hcclConfigMapName)
{
    podSpec["volumes"] = nlohmann::json::array();
    SetHostPathVolumns(podSpec, containers, serverParams);
    nlohmann::json mount = nlohmann::json::object();
    nlohmann::json mountShm = nlohmann::json::object(); // 共享内存设置
    nlohmann::json volume = nlohmann::json::object();
    nlohmann::json volumeShm = nlohmann::json::object();  // 共享内存设置
    volume["name"] = "ascend-910-config";
    volume["configMap"] = nlohmann::json::object();
    volume["configMap"]["name"] = hcclConfigMapName;
    volume["configMap"]["defaultMode"] = 0640; // 0640 配置文件权限
    podSpec["volumes"].emplace_back(volume);
    volumeShm["name"] = "dshm";
    volumeShm["emptyDir"] = nlohmann::json::object();
    volumeShm["emptyDir"]["medium"] = "Memory";
    volumeShm["emptyDir"]["sizeLimit"] = "16Gi";
    podSpec["volumes"].emplace_back(volumeShm);
    mount["name"] = "ascend-910-config";
    mount["mountPath"] = "/user/serverid/devindex/config";
    containers["volumeMounts"].emplace_back(mount);
    mountShm["name"] = "dshm";
    mountShm["mountPath"] = "/dev/shm";
    containers["volumeMounts"].emplace_back(mountShm);
    return;
}

nlohmann::json SetPodSpec(const LoadServiceParams &serverParams, std::string deploymentName,
    const std::string &hcclConfigMapName)
{
    nlohmann::json containers;
    nlohmann::json securityContext;
    containers["image"] = serverParams.serverImage;
    containers["imagePullPolicy"] = "IfNotPresent";
    containers["name"] = deploymentName;
    securityContext["allowPrivilegeEscalation"] = false;
    securityContext["capabilities"]["drop"] = {"ALL"};
    securityContext["seccompProfile"]["type"] = "RuntimeDefault";
    containers["securityContext"] = securityContext;
    containers["readinessProbe"]["periodSeconds"] = 5; // 5 探针周期
    containers["readinessProbe"]["exec"]["command"] = {"/bin/bash", "-c",
        "$MIES_INSTALL_PATH/scripts/http_client_ctl/probe.sh readiness"};
    containers["readinessProbe"]["timeoutSeconds"] = serverParams.readinessTimeout; // 执行探测的时间间隔
    containers["readinessProbe"]["failureThreshold"] = serverParams.readinessFailureThreshold; // ready可出现问题次数阈值

    containers["volumeMounts"] = nlohmann::json::array();
    nlohmann::json podSpec;
    SetVolumns(podSpec, containers, serverParams, hcclConfigMapName);

    SetContainerCommand(containers, serverParams.startupCmd);
    containers["ports"] = {{{"containerPort", serverParams.mindieServerPort}}};
    SetContainerEnv(containers, serverParams);
    containers["resources"] = {{"requests", {{"huawei.com/" + serverParams.npuType,
        serverParams.npuNum}, {"memory", std::to_string(serverParams.memRequest) + "Mi"},
        {"cpu", std::to_string(serverParams.cpuRequest) + "m"}}},
        {"limits", {{"huawei.com/" + serverParams.npuType, serverParams.npuNum},
        {"memory", std::to_string(serverParams.memRequest * 2) + "Mi"},
        {"cpu", std::to_string(serverParams.cpuRequest * 2) + "m"}}}};
    podSpec["nodeSelector"] = {{"accelerator", "huawei-Ascend910"}};
    podSpec["containers"] = { containers };
    podSpec["automountServiceAccountToken"] = false;
    podSpec["terminationGracePeriodSeconds"] = 0;
    nlohmann::json podSecurityContext;
    podSecurityContext["fsGroup"] = 1001; // 1001 群组id, 挂载到容器内
    podSpec["securityContext"] = podSecurityContext;
    return podSpec;
}

std::string CreateDeployJson(const LoadServiceParams &serverParams, std::string deploymentName,
    const std::string &hcclConfigMapName)
{
    auto podSpec = SetPodSpec(serverParams, deploymentName, hcclConfigMapName);

    nlohmann::json podTemplate;
    podTemplate["metadata"] = {{"labels", {{"app", deploymentName}, {"deploy-name", deploymentName},
        {"ring-controller.atlas", "ascend-910b"}}}};
    podTemplate["spec"] = podSpec;
    nlohmann::json spec;
    spec["replicas"] = serverParams.crossNodeNum;
    spec["selector"] = {{"matchLabels", {{"app", deploymentName}}}};
    spec["template"] = podTemplate;
    nlohmann::json deploymentJson;
    deploymentJson["apiVersion"] = "apps/v1";
    deploymentJson["kind"] = "Deployment";
    deploymentJson["metadata"] = {{"name", deploymentName}, {"namespace", serverParams.nameSpace},
        {"labels", {{"app", deploymentName}, {"ring-controller.atlas", "ascend-910b"},
        {"deploy-name", deploymentName}}}};
    deploymentJson["metadata"]["annotations"] = {{"mindie_port", std::to_string(serverParams.mindieServerMngPort)},
        {"init_delay", std::to_string(serverParams.initDealy)},
        {"liveness_timeout", std::to_string(serverParams.livenessTimeout)},
        {"liveness_failure_threshold", std::to_string(serverParams.livenessFailureThreshold)},
        {"readiness_timeout", std::to_string(serverParams.readinessTimeout)},
        {"readiness_failure_threshold", std::to_string(serverParams.readinessFailureThreshold)}};
    deploymentJson["spec"] = spec;
    SetAntiPod(deploymentJson, deploymentName);
    std::string jsonString = deploymentJson.dump();
    return jsonString;
}

std::string CreateServiceJson(const LoadServiceParams &serverParams, std::string labelKey, std::string labelValue,
    std::string serviceName)
{
    nlohmann::json j;
    j["kind"] = "Service";
    j["apiVersion"] = "v1";
    j["metadata"] = {{"labels", {{"k8s-app", serverParams.name}}}, {"name", serviceName},
        {"namespace", serverParams.nameSpace}};
    j["spec"] = {{"type", serverParams.serverType}, {"ports", {{{"port", serverParams.mindieServerPort},
        {"targetPort", serverParams.mindieServerPort}, {"nodePort", serverParams.servicePort}}}},
        {"selector", {{labelKey, labelValue}}}};
    std::string jsonString = j.dump();
    return jsonString;
}

int32_t ParseDeviceRequestParams(const nlohmann::json &mainObj, LoadServiceParams &serverParams)
{
    if (!mainObj.contains("resource_requests") || !mainObj["resource_requests"].contains("memory") ||
        !mainObj["resource_requests"].contains("cpu_core") || !mainObj["resource_requests"].contains("npu_type") ||
        !mainObj["resource_requests"].contains("npu_chip_num")) {
        LOG_E("[%s] [Deployer] Missing required keys: resource_requests/memory/cpu_core/npu_type/npu_chip_num.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }

    if (!mainObj["resource_requests"]["memory"].is_number_integer() ||
        !mainObj["resource_requests"]["npu_chip_num"].is_number_integer() ||
        !mainObj["resource_requests"]["npu_type"].is_string() ||
        !mainObj["resource_requests"]["cpu_core"].is_number_integer()) {
        LOG_E("[%s] [Deployer] Value of field 'resource_requests' is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    serverParams.npuType = mainObj["resource_requests"]["npu_type"];
    if (serverParams.npuType != "Ascend910") {
        LOG_E("[%s] [Deployer] Cross node server only support 'Ascend910' as the value of 'npu_type' field.",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }

    serverParams.cpuRequest = mainObj["resource_requests"]["cpu_core"];
    if (serverParams.cpuRequest < 1000 || serverParams.cpuRequest > 256000) { // 1000, 256000 cpu range
        LOG_E("[%s] [Deployer] The value of cpu request field 'cpu_core' must in the range of [1000, 256000].",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    serverParams.npuNum = mainObj["resource_requests"]["npu_chip_num"];
    if (serverParams.npuNum != 8) { // 8 当前约束单机最多使用8卡
        LOG_E("[%s] [Deployer] The value of field 'npu_chip_num' must be 8.",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    serverParams.memRequest = mainObj["resource_requests"]["memory"];
    if (serverParams.memRequest < 1000 || serverParams.memRequest > 256000) { // 1000, 256000 memory range
        LOG_E("[%s] [Deployer] The value of memory request field 'memory' must in the range of [1000MB, 256000MB].",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    return 0;
}

static int32_t ParseResourceParams(const nlohmann::json &mainObj, LoadServiceParams &serverParams)
{
    if (ParseDeviceRequestParams(mainObj, serverParams) != 0) {
        return -1;
    }

    if (!mainObj.contains("replicas") || !mainObj["replicas"].is_number_integer()) {
        LOG_E("[%s] [Deployer] Missing required key 'replicas' or the value is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    serverParams.replicas = mainObj["replicas"];

    if (serverParams.replicas > g_maxInstanceNum || serverParams.replicas <= 0) {
        LOG_E("[%s] [Deployer] The 'replicas' value for cross node deployment should be an integer in the "
            "range of [1, %u], but got %u.", GetErrorCode(ErrorType::RESOURCE_LIMIT,
            DeployerFeature::CROSSNODE_SERVER).c_str(), g_maxInstanceNum, serverParams.replicas);
        return -1;
    }

    if (!mainObj.contains("cross_node_num") || !mainObj["cross_node_num"].is_number_integer()) {
        LOG_E("[%s] [Deployer] Missing required key 'cross_node_num' or the value is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    serverParams.crossNodeNum = mainObj["cross_node_num"];
    if ((serverParams.crossNodeNum != static_cast<int64_t>(g_minCrossNodeNum)) &&
        (serverParams.crossNodeNum != static_cast<int64_t>(g_maxCrossNodeNum))) { // 当前仅支持已验证过的节点数量：2和4
        LOG_E("[%s] [Deployer] 'cross_node_num' only support %lu or %lu.",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::CROSSNODE_SERVER).c_str(),
            g_minCrossNodeNum, g_maxCrossNodeNum);
        return -1;
    }
    if (ParseProbeSetting(mainObj, serverParams) != 0) {
        return -1;
    }

    serverParams.initDealy = mainObj["init_delay"];
    if (serverParams.initDealy < 10 || serverParams.initDealy > 1800) { // 10, 1800 最长允许30分钟pod启动时间, 超过且服务未就绪自动重启
        LOG_E("[%s] [Deployer] The value of 'init_delay' must in the range of [10, 1800] seconds, but got %d.",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::CROSSNODE_SERVER).c_str(), serverParams.initDealy);
        return -1;
    }

    return 0;
}

int32_t ParseMindIEParams(nlohmann::json &mainObj, bool mindieInitTlsSwitch, LoadServiceParams &serverParams)
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
        LOG_E("[%s] [Deployer] Dynamic loaded MindIE Server TLS switch must be consistent with the one that "
            "initializes the process.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::CROSSNODE_SERVER).c_str());
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

int32_t ParseKubeConfig(const nlohmann::json &mainObj, LoadServiceParams &serverParams)
{
    if (!IsJsonStringValid(mainObj, "server_name", 1, 48) || // 48 最大长度，超过k8s不支持
        !IsJsonStringValid(mainObj, "scheduler") || !IsJsonStringValid(mainObj, "service_type")) {
        return -1;
    }
    if (mainObj["scheduler"] != "default") {
        LOG_E("[%s] [Deployer] Value of key'cheduler' must be \"default\".",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    serverParams.scheduler = mainObj["scheduler"];

    serverParams.nameSpace = "mindie";

    serverParams.serverImage = "mindie:server";
    serverParams.startupCmd = "/mnt/mindie-service/ms/run/run.sh";
    serverParams.serverType = mainObj["service_type"];
    if (serverParams.serverType == "NodePort") {
        if (!IsJsonIntValid(mainObj, "service_port", 30000, 32767)) { // 30000, 32767, k8s Service port范围
            return -1;
        }
        serverParams.servicePort = mainObj["service_port"];
    }
    if (serverParams.serverType != "NodePort") {
        LOG_E("[%s] [Deployer] Only support 'NodePort' for 'service_port' field.",
            GetErrorCode(ErrorType::UNAVAILABLE, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    if (ParseResourceParams(mainObj, serverParams) != 0) {
        return -1;
    }
    if (SetMountPath(serverParams) != 0) {
        return -1;
    }
    return 0;
}

static int32_t ParseLoadParams(const std::string &jsonStr, bool mindieInitTlsSwitch, LoadServiceParams &serverParams)
{
    if (!CheckJsonStringSize(jsonStr) || !nlohmann::json::accept(jsonStr)) {
        LOG_E("[%s] [Deployer] Invalid JSON format of deploy config.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }

    auto mainObj = nlohmann::json::parse(jsonStr, CheckJsonDepthCallBack);
    if (!mainObj.contains("server_name") || !mainObj["server_name"].is_string()) {
        LOG_E("[%s] [Deployer] Lack of key 'server_name', or the value type is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    serverParams.name = mainObj["server_name"];

    if (ParseKubeConfig(mainObj, serverParams) != 0) {
        return -1;
    }

    if (ParseMindIEParams(mainObj, mindieInitTlsSwitch, serverParams) != 0) {
        return -1;
    }

    return 0;
}

int32_t CrossNodeServer::SendKubeHttpRequest(const std::string& target, Http::verb method,
    const std::string &header, const std::string &reqBody, std::string& responseBody)
{
    std::map<boost::beast::http::field, std::string> headMap;
    headMap[boost::beast::http::field::content_type] = header;
    Request req = { target, method, headMap, reqBody};
    int32_t code = 0;
    int32_t result = mKubeHttpClient.SendRequest(req, mTimeoutSeconds,
        mRetryTimes, responseBody, code);
    LOG_D("Send message to Kubernetes API-Server, ret is %d, status code is %d.", result, code);
    if (result == -1) {
        LOG_E("[%s] [Deployer] Failed to send request to Kubernetes API Server, status code is %d.",
            GetErrorCode(ErrorType::UNREACHABLE, DeployerFeature::CROSSNODE_SERVER).c_str(),
            code);
        return -1;
    }
    static std::set<int32_t> statusOk = {200, 201, 202, 203}; // 200 201 202 203: status ok
    if (statusOk.count(code) == 0) {
        LOG_E("[%s] [Deployer] Received invalid response from Kubernetes API Server, status code is %d.",
            GetErrorCode(ErrorType::UNREACHABLE, DeployerFeature::CROSSNODE_SERVER).c_str(),
            code);
        return -1;
    }
    return 0;
}

int32_t CrossNodeServer::GetFromMindIEServer(HttpClient &httpClient, const std::string& target,
    std::string& responseBody, uint32_t timeoutSeconds, uint32_t retryTimes) const
{
    std::map<boost::beast::http::field, std::string> headMap;
    headMap[boost::beast::http::field::content_type] = "";
    Request req = { target, boost::beast::http::verb::get, headMap, ""};
    int32_t code = 0;
    int32_t result = httpClient.SendRequest(req, timeoutSeconds,
        retryTimes, responseBody, code);
    LOG_D("send message to mindie server, ret %d, status code: %d", result, code);
    if (result == -1) {
        LOG_E("[%s] [Deployer] Failed to send request, code: %d",
            GetErrorCode(ErrorType::UNREACHABLE, DeployerFeature::CROSSNODE_SERVER).c_str(),
            code);
        return -1;
    }

    static std::set<int32_t> statusOk = {200, 201, 202, 203}; // 200 201 202 203: status ok
    if (statusOk.count(code) == 0) {
        LOG_E("[%s] [Deployer] Response message status is not ok, code: %d",
            GetErrorCode(ErrorType::UNREACHABLE, DeployerFeature::CROSSNODE_SERVER).c_str(),
            code);
        return -1;
    }
    return 0;
}

int CrossNodeServer::GetServerStatus(const std::string &url, bool &isOk)
{
    LOG_I("Attempting to get status from mindie server.");
    if (mInferInstance.size() == 0 || mInferInstance[0].masterPodIp == "") {
        LOG_I("Infernce instance is not ready. No master pod IP found.");
        return -1;
    }
    this->mMindieGetStatusHttpClient.SetHostAndPort(mInferInstance[0].masterPodIp,
        std::to_string(this->mServiceParams.mindieServerMngPort));
    std::string response;
    if (GetFromMindIEServer(this->mMindieGetStatusHttpClient, url, response,
        mTimeoutSeconds, mRetryTimes) != 0) {
        LOG_E("[%s] [Deployer] Failed to get information from master pod.",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    isOk = true;
    return 0;
}

int CrossNodeServer::GetModelInfo(nlohmann::json &modelInfo)
{
    if (mInferInstance.size() == 0 || mInferInstance[0].masterPodIp == "") {
        LOG_I("Infernce instance is not ready. No master pod IP found.");
        return -1;
    }
    this->mMindieGetStatusHttpClient.SetHostAndPort(mInferInstance[0].masterPodIp,
        std::to_string(this->mServiceParams.mindieServerMngPort));
    std::string url = "/info";
    std::string response;
    LOG_I("Attempting to get mindie information.");
    if (GetFromMindIEServer(this->mMindieGetStatusHttpClient, url, response, mTimeoutSeconds, mRetryTimes) != 0) {
        LOG_E("[%s] [Deployer] Failed to get information from master pod.",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    if (!CheckJsonStringSize(response) || !nlohmann::json::accept(response)) {
        LOG_E("[%s] [Deployer] Invalid JSON format response received when getting master pod information.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    modelInfo = nlohmann::json::parse(response, CheckJsonDepthCallBack);
    return 0;
}

int32_t CrossNodeServer::LabelMasterPod(const std::string &masterContainerIp, uint32_t ind, bool &isLabeled)
{
    std::string url = "/api/v1/pods?fieldSelector=status.podIP=" + masterContainerIp;
    std::string response;
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::get, "", "", response) != 0) {
        LOG_E("[%s] [Deployer] Pod IP not found, maybe the pod is pending.",
            GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::CROSSNODE_SERVER).c_str());
        this->mDeployStatus = DeployStatus::FAILED;
        this->mStatusMessage = "POD with IP " + masterContainerIp  +
            " is not found, please check that if the POD has been deleted!";
        return -1;
    }

    mInferInstance[ind].masterPodIp = masterContainerIp;
    mInferInstance[ind].masterPodCreateTime = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch());
    if (!CheckJsonStringSize(response) || !nlohmann::json::accept(response)) {
        LOG_E("[%s] [Deployer] Invalid JSON format response when getting master pod result.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    auto respJsonbObj = nlohmann::json::parse(response, CheckJsonDepthCallBack);
    if (!respJsonbObj.contains("items") || !respJsonbObj["items"].is_array() || !(respJsonbObj["items"].size() > 0) ||
        !respJsonbObj["items"][0].contains("metadata")) {
        LOG_E("[%s] [Deployer] Missing or invalid 'metadata' in pod information response.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    if (!IsJsonStringValid(respJsonbObj["items"][0]["metadata"], "name") ||
        !IsJsonStringValid(respJsonbObj["items"][0]["metadata"], "namespace")) {
        LOG_E("[%s] [Deployer] Pod information respons metadata does not contain valid 'name' or 'namespace'.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    std::string podName = respJsonbObj["items"][0]["metadata"]["name"];
    std::string podNameSpace = respJsonbObj["items"][0]["metadata"]["namespace"];
    url = "/api/v1/namespaces/" + podNameSpace + "/pods/" + podName;
    nlohmann::json jsonObj;
    jsonObj["metadata"]["labels"]["cross-node-app"] = this->mServerName + "-master-node";
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::patch,
        "application/strategic-merge-patch+json", jsonObj.dump(), response) != 0) {
        LOG_E("[%s] [Deployer] Failed to label the master pod!",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
        this->mDeployStatus = DeployStatus::FAILED;
        this->mStatusMessage = "POD " + podName +
            " is not found, please check that if the POD has been deleted!";
        return -1;
    }
    isLabeled = true;
    return 0;
}

int32_t CrossNodeServer::ModifyRanktable(const nlohmann::json &ranktable, uint32_t ind, bool &isLabeled)
{
    if (!IsJsonStringValid(ranktable["server_list"][0], "container_ip")) {
        LOG_E("[%s] [Deployer] Hccl.json does not contain the key 'container_ip'",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    std::string masterContainerIp = ranktable["server_list"][0]["container_ip"];
    if (LabelMasterPod(masterContainerIp, ind, isLabeled) != 0) {
        LOG_E("[%s] [Deployer] Failed to label master container IP.",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    return 0;
}

int32_t CrossNodeServer::CreateService(const LoadServiceParams &serverParams)
{
    this->mUseService = true;
    auto jsonString = CreateServiceJson(serverParams, "cross-node-app", serverParams.name + "-master-node",
        serverParams.name + mServiceNameSuffix);
    std::string url = "/api/v1/namespaces/" + serverParams.nameSpace + "/services";
    std::string response;
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::post, "", jsonString, response) != 0) {
        this->mDeployStatus = DeployStatus::FAILED;
        this->mStatusMessage = "Failed to create service!";
        auto ret = this->ClearResources();
        if (ret.first != 0) {
            LOG_W("[%s] [Deployer] Failed to clear resources!",
                GetWarnCode(ErrorType::WARNING, DeployerFeature::CROSSNODE_SERVER).c_str());
        }
        LOG_E("[%s] [Deployer] Failed to create the service!",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    mKubeResources.serviceName = serverParams.name + mServiceNameSuffix;
    LOG_M("[Create] Success to create NodePort Service!");
    this->mDeployStatus = DeployStatus::CREATED;
    ServerSaveStatus status = {mServiceParams.replicas, mServiceParams.nameSpace, mServiceParams.name,
        "mindie_cross_node", mUseService};
    if (StatusHandler::GetInstance()->SaveStatusToFile(status) != 0) {
        this->mStatusMessage = "succeesd to create the server, but failed to save server status to file";
        LOG_M("[Create] Server created and save server status to file successfully.");
    } else {
        LOG_M("[Create] Server created successfully, and server status saved to file.");
        this->mStatusMessage = "succeesd to create the server, and succeed to save server status to file";
    }
    return 0;
}

int32_t CrossNodeServer::FindAndLabelMasterPod(uint32_t ind, bool &isLabeled)
{
    std::string deploymentName = this->mServerName + mDeploymentNameMiddle + std::to_string(ind);
    std::string url = "/api/v1/namespaces/" + this->mKubeResources.nameSpace + "/configmaps/" +
        mRanktableNamePrefix + deploymentName;
    std::string response;
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::get, "", "", response) != 0) {
        LOG_E("[%s] [Deployer] Get HCCL ConfigMap from Kubernetes server failed!",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
        this->mDeployStatus = DeployStatus::FAILED;
        this->mStatusMessage = "Failed to get ConfigMap " + mRanktableNamePrefix + deploymentName +
            ", please check that if the ConfigMap has been deleted!";
        return -1;
    }
    std::string logCode = GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER);
    if (!CheckJsonStringSize(response) || !nlohmann::json::accept(response)) {
        LOG_E("[%s] [Deployer] Invalid JSON format of response when getting ranktable result.", logCode.c_str());
        return -1;
    }
    nlohmann::json respJsonbObj = nlohmann::json::parse(response, CheckJsonDepthCallBack);
    if (!respJsonbObj.contains("data")) {
        LOG_E("[%s] [Deployer] Ranktable ConfigMap does not contain key data.", logCode.c_str());
        return -1;
    }
    if (!IsJsonStringValid(respJsonbObj["data"], "hccl.json")) {
        LOG_E("[%s] [Deployer] Ranktable ConfigMap does not contain key hccl.json", logCode.c_str());
        return -1;
    }
    std::string rankTableJsonStr = respJsonbObj["data"]["hccl.json"];
    if (!CheckJsonStringSize(rankTableJsonStr) || !nlohmann::json::accept(rankTableJsonStr)) {
        LOG_E("[%s] [Deployer] Format of hccl.json is invalid.", logCode.c_str());
        return -1;
    }
    auto ranktable = nlohmann::json::parse(rankTableJsonStr, CheckJsonDepthCallBack);
    if (!IsJsonStringValid(ranktable, "status")) {
        LOG_E("[%s] [Deployer] Ranktable ConfigMap does not contain key hccl.json.", logCode.c_str());
        return -1;
    }

    if (ranktable["status"] != "completed") {
        LOG_W("[%s] [Deployer] Ranktable is not finished generating, the status is not completed.",
            GetWarnCode(ErrorType::WARNING, DeployerFeature::CROSSNODE_SERVER).c_str());
        return 0;
    }
    if (!IsJsonArrayValid(ranktable, "server_list", g_minCrossNodeNum, g_maxCrossNodeNum)) {
        return -1;
    }

    if (ModifyRanktable(ranktable, ind, isLabeled) != 0) {
        return -1;
    }
    return 0;
}

void CrossNodeServer::AssociateMasterPod(const LoadServiceParams &serverParams)
{
    std::vector<bool> masterPodLabels(serverParams.replicas, false);
    while (this->mDeployStatus != DeployStatus::STOPPING) {
        sleep(1);
        bool isAllLabeled = true;
        for (uint32_t i = 0; i < serverParams.replicas; i++) {
            if (!masterPodLabels[i]) {
                isAllLabeled = false;
            }
        }
        if (isAllLabeled) {
            break;
        }
        for (uint32_t ind = 0; ind < serverParams.replicas; ind++) {
            if (masterPodLabels[ind]) {
                continue;
            }
            bool isLabeled = false;
            if (FindAndLabelMasterPod(ind, isLabeled) != 0) {
                masterPodLabels[ind] = isLabeled;
                return;
            }
            masterPodLabels[ind] = isLabeled;
        }
    }

    if (this->mDeployStatus == DeployStatus::STOPPING) {
        return;
    }

    auto thread = std::unique_ptr<std::thread>(
        new (std::nothrow)std::thread(&CrossNodeServer::MinitoringInstance, this));
    if (thread == nullptr) {
        LOG_E("[%s] [Deployer] Failed to create minitoring instance thread of cross node server!",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, DeployerFeature::CROSSNODE_SERVER).c_str());
        return;
    }

    this->mMonitoringThread = std::move(thread);
    return;
}

std::pair<int32_t, std::string> CrossNodeServer::CreateServiceThread(std::string &logCode)
{
    if (CreateService(mServiceParams) != 0) {
        return {-1, "Failed to create service!"};
    }

    std::unique_ptr<std::thread> thread;
    try {
        thread = std::make_unique<std::thread>(
            &CrossNodeServer::AssociateMasterPod, this, mServiceParams);
    } catch (const std::bad_alloc&) {
        LOG_E("[%s] [Deployer] Failed to create the thread!", logCode.c_str());
        return std::make_pair(-1, std::string("failed to create the thread!"));
    }

    this->mCreateServiceThread = std::move(thread);
    return std::make_pair(0, std::string("Creating the server!"));
}

std::pair<int32_t, std::string> CrossNodeServer::Deploy(const std::string &config)
{
    if (ParseLoadParams(config, mClientParams.mindieClientTlsItems.tlsEnable, mServiceParams) != 0) {
        return std::make_pair(-1, std::string("failed to parse load params!"));
    }
    mServerName = mServiceParams.name;
    mKubeResources.nameSpace = mServiceParams.nameSpace;
    std::string logCode = GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, DeployerFeature::CROSSNODE_SERVER);
    for (uint32_t i = 0; i < mServiceParams.replicas; i++) {
        std::string deploymentName = mServiceParams.name + mDeploymentNameMiddle + std::to_string(i);
        std::string rankTableName = mRanktableNamePrefix + deploymentName;
        ResourceLabel hcclLabel{"ring-controller.atlas", "ascend-910b"};
        auto hcclConfigMap = CreateConfigMapJson(mServiceParams.nameSpace, rankTableName,
            "hccl.json", R"({"status":"initializing"})", hcclLabel);

        std::string url = "/api/v1/namespaces/" + mServiceParams.nameSpace + "/configmaps";
        std::string response;
        if (this->SendKubeHttpRequest(url, boost::beast::http::verb::post, "", hcclConfigMap, response) != 0) {
            LOG_E("[%s] [Deployer] Failed to create ranktable ConfigMap!", logCode.c_str());
            auto ret = this->ClearResources();
            if (ret.first != 0) {
                LOG_W("[Deployer] Failed to clear resources!");
            }
            return std::make_pair(-1, std::string("failed to create ranktable ConfigMap!"));
        }

        mKubeResources.configMapNames.emplace_back(rankTableName);

        std::string jsonString = CreateDeployJson(mServiceParams, deploymentName, rankTableName);
        url = "/apis/apps/v1/namespaces/" + mServiceParams.nameSpace + "/deployments";

        if (this->SendKubeHttpRequest(url, boost::beast::http::verb::post, "", jsonString, response) != 0) {
            auto ret = this->ClearResources();
            if (ret.first != 0) {
                LOG_W("[Deployer] Failed to clear resources!");
            }
            LOG_E("[%s] [Deployer] Failed to create kube Deployment resource!", logCode.c_str());
            return std::make_pair(-1, std::string("failed to create deployment!"));
        }

        mKubeResources.deploymentNames.emplace_back(deploymentName);
        InferInstance instance = { RestoreState::NONE, mServiceParams.nameSpace, std::chrono::seconds(),
            "", deploymentName, rankTableName, InstanceStatus::UNREADY };
        this->mInferInstance[i] = instance;
    }
    LOG_M("[Create] Create the deployment for %s Success, now it's going to create the service!", mServerName.c_str());

    return CreateServiceThread(logCode);
}

std::pair<int32_t, std::string> CrossNodeServer::Unload()
{
    this->mDeployStatus = DeployStatus::STOPPING;
    if (this->mCreateServiceThread != nullptr) {
        this->mCreateServiceThread->join();
        this->mCreateServiceThread = nullptr;
    }
    if (this->mMonitoringThread != nullptr) {
        this->mMonitoringThread->join();
        this->mMonitoringThread = nullptr;
    }

    if (StatusHandler::GetInstance()->RemoveServerStatus(this->mServerName) == 0) {
        this->mStatusMessage = "succeed to clear server status in file";
        LOG_M("[Clear] Succeed to clear server status in file.");
    } else {
        LOG_M("[Clear] Failed to clear server status in file.");
        this->mStatusMessage = "failed to clear server status in file";
    }
    return this->ClearResources();
}

std::pair<int32_t, std::string> CrossNodeServer::ClearResources()
{
    LOG_D("Ready to clear kube resources.");
    std::string response;
    if (mKubeResources.serviceName.size() != 0) {
        std::string url = "/api/v1/namespaces/" + mKubeResources.nameSpace +
            "/services/" + mKubeResources.serviceName;
        auto result = this->SendKubeHttpRequest(url, boost::beast::http::verb::delete_, "", "", response);
        if (result != 0) {
            return std::make_pair(-1, "failed to send delete k8s Service request.");
        }
    }
    mKubeResources.serviceName.resize(0);

    for (uint32_t i = 0; i < mKubeResources.configMapNames.size(); i++) {
        std::string url = "/api/v1/namespaces/" + mKubeResources.nameSpace + "/configmaps/" +
            mKubeResources.configMapNames[i];
        auto result = this->SendKubeHttpRequest(url, boost::beast::http::verb::delete_, "", "", response);
        if (result != 0) {
            LOG_E("[%s] [Deployer] Failed to send delete k8s ConfigMap request.",
                GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
            return std::make_pair(-1, "send delete k8s ConfigMap request failed.");
        }
    }
    mKubeResources.configMapNames.resize(0);

    for (uint32_t i = 0; i < mKubeResources.deploymentNames.size(); i++) {
        auto url = "/apis/apps/v1/namespaces/" + mKubeResources.nameSpace + "/deployments/" +
            mKubeResources.deploymentNames[i];
        auto result = this->SendKubeHttpRequest(url, boost::beast::http::verb::delete_, "",
            "", response);
        if (result != 0) {
            LOG_E("[%s] [Deployer] Failed to send delete k8s Deployment request.",
                GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
            return std::make_pair(-1, "failed to send delete k8s Deployment request failed.");
        }
    }
    mKubeResources.deploymentNames.resize(0);
    LOG_M("[Clear] Success to clear the resources of the server %s.", this->mServerName.c_str());
    return std::make_pair(0, "succeed to clear resources");
}

std::pair<int32_t, std::string> CrossNodeServer::Update(__attribute__((unused)) const std::string &config)
{
    return std::make_pair(-1, std::string("cross node mode does not support update yet"));
}

int32_t CrossNodeServer::GetMasterPodIPByLabel(const std::string &serverName, const std::string &deploymentName,
    std::string &podIP)
{
    std::string url = "/api/v1/pods?labelSelector=app%3D" + deploymentName + "%2Ccross-node-app%3D" +
        serverName + "-master-node";
    std::string response;
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::get, "", "", response) != 0) {
        LOG_E("[%s] [Deployer] Master pod not found.",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    if (!CheckJsonStringSize(response) || !nlohmann::json::accept(response)) {
        LOG_E("[%s] [Deployer] Format of pod information is not valid JSON",
            GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    auto respJsonbObj = nlohmann::json::parse(response, CheckJsonDepthCallBack);
    if (!respJsonbObj.contains("items") || !respJsonbObj["items"].is_array() || !(respJsonbObj["items"].size() > 0)) {
        LOG_E("[%s] [Deployer] Pod information does not contain any valid items in the response.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    auto item =  respJsonbObj["items"][0];
    if (!item.contains("status")) {
        LOG_E("[%s] [Deployer] Pod information does not contain status.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    if (!IsJsonStringValid(item["status"], "podIP")) {
        return -1;
    }
    podIP = item["status"]["podIP"];
    return 0;
}

int32_t CrossNodeServer::FromDeployment(const std::string &deploymentName)
{
    std::string url = "/apis/apps/v1/namespaces/" + mKubeResources.nameSpace + "/deployments/" + deploymentName;
    std::string responseBody;
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::get, "", "", responseBody) != 0) {
        LOG_E("[%s] [Deployer] Restore from JSON. Failed to get deployment %s.",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str(), deploymentName.c_str());
        return -1;
    }
    if (!CheckJsonStringSize(responseBody) || !nlohmann::json::accept(responseBody)) {
        LOG_E("[%s] [Deployer] Recieve invalid JSON format while get deployment from Kubernetes.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }

    auto deployJson = nlohmann::json::parse(responseBody, CheckJsonDepthCallBack);
    if (!deployJson.contains("metadata") || !deployJson["metadata"].contains("annotations")) {
        LOG_E("[%s] [Deployer] Deployment description does not contain key 'metadata' or the value missing "
            "'annotations'.", GetErrorCode(ErrorType::INVALID_PARAMETER, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }

    const std::vector<std::string> metadataFields = {"uid", "resourceVersion", "selfLink",
        "creationTimestamp", "generation"};
    for (const auto& field : metadataFields) { deployJson["metadata"].erase(field); }

    mKubeResources.deploymentJsons.push_back(deployJson.dump());
    mKubeResources.deploymentNames.push_back(deploymentName);

    if (!IsJsonStringValid(deployJson["metadata"]["annotations"], "mindie_port") ||
        !IsJsonStringValid(deployJson["metadata"]["annotations"], "init_delay") ||
        !IsJsonStringValid(deployJson["metadata"]["annotations"], "liveness_timeout") ||
        !IsJsonStringValid(deployJson["metadata"]["annotations"], "liveness_failure_threshold") ||
        !IsJsonStringValid(deployJson["metadata"]["annotations"], "readiness_timeout") ||
        !IsJsonStringValid(deployJson["metadata"]["annotations"], "readiness_failure_threshold")) {
        return -1;
    }
    if (StrToInt(std::string(deployJson["metadata"]["annotations"]["mindie_port"]),
        this->mServiceParams.mindieServerMngPort) != 0 ||
        StrToInt(std::string(deployJson["metadata"]["annotations"]["init_delay"]),
        this->mServiceParams.initDealy) != 0 ||
        StrToInt(std::string(deployJson["metadata"]["annotations"]["liveness_timeout"]),
        this->mServiceParams.livenessTimeout) != 0 ||
        StrToInt(std::string(deployJson["metadata"]["annotations"]["liveness_failure_threshold"]),
        this->mServiceParams.livenessFailureThreshold) != 0 ||
        StrToInt(std::string(deployJson["metadata"]["annotations"]["readiness_timeout"]),
        this->mServiceParams.readinessTimeout) != 0 ||
        StrToInt(std::string(deployJson["metadata"]["annotations"]["readiness_failure_threshold"]),
        this->mServiceParams.readinessFailureThreshold) != 0) {
        return -1;
    }
    return 0;
}

int32_t CrossNodeServer::FromReplicaResource(uint32_t ind)
{
    std::string deploymentName = mServerName + mDeploymentNameMiddle + std::to_string(ind);
    if (FromDeployment(deploymentName) != 0) {
        return -1;
    }
    std::string ranktableName =  mRanktableNamePrefix + deploymentName;
    std::string url = "/api/v1/namespaces/" + mKubeResources.nameSpace + "/configmaps/" + ranktableName;
    std::string responseBody;
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::get, "", "", responseBody) != 0) {
        LOG_E("[%s] [Deployer] Restore from json. Failed to get ConfigMap %s.",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str(),
            ranktableName.c_str());
        return -1;
    }
    mKubeResources.configMapNames.push_back(ranktableName);
    InferInstance instance = { RestoreState::NONE, mKubeResources.nameSpace, std::chrono::seconds(),
        "", deploymentName, ranktableName, InstanceStatus::UNREADY };
    this->mInferInstance[ind] = instance;
    return 0;
}

int32_t CrossNodeServer::FromJson(const nlohmann::json &serverStatus)
{
    this->mServerName = serverStatus["server_name"];
    std::string serverNamespace = serverStatus["namespace"];
    int64_t relicas = serverStatus["replicas"];
    if (relicas > g_maxInstanceNum || relicas <= 0) {
        LOG_E("[%s] [Deployer] Restore from JSON. Key 'replicas' is not in the range of [1, %u].",
            GetErrorCode(ErrorType::RESOURCE_LIMIT, DeployerFeature::CROSSNODE_SERVER).c_str(),
            g_maxInstanceNum);
        return -1;
    }
    mServiceParams.replicas = relicas;
    mServiceParams.detectInnerError = true;
    mKubeResources.nameSpace = serverNamespace;
    std::string responseBody;
    std::string url;
    if (serverStatus["use_service"]) {
        url = "/api/v1/namespaces/" + mKubeResources.nameSpace +
            "/services/" + mServerName + mServiceNameSuffix;
        if (this->SendKubeHttpRequest(url, boost::beast::http::verb::get, "", "", responseBody) != 0) {
            LOG_E("[%s] [Deployer] Restore from json. Failed to get service, maybe the service is deleted!",
                GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
            return -1;
        }
        mKubeResources.serviceName = mServerName + mServiceNameSuffix;
    }

    for (uint32_t i = 0; i < relicas; i++) {
        if (FromReplicaResource(i) != 0) {
            return -1;
        }
    }

    auto thread = std::make_unique<std::thread>(
        &CrossNodeServer::AssociateMasterPod, this, mServiceParams);

    if (thread == nullptr) {
        LOG_E("[%s] [Deployer] Failed to create the thread!",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    this->mCreateServiceThread = std::move(thread);
    this->mStatusMessage = "the server is restored successfully";
    return 0;
}

int32_t CrossNodeServer::CreateInstanceResource(uint32_t ind, const std::string &nameSpace,
    const std::string &ranktableName, const std::string &deplymentName)
{
    ResourceLabel hcclLabel{"ring-controller.atlas", "ascend-910b"};
    auto hcclConfigMap = CreateConfigMapJson(nameSpace, ranktableName,
        "hccl.json", R"({"status":"initializing" })", hcclLabel);

    std::string url = "/api/v1/namespaces/" + nameSpace + "/configmaps";
    std::string responseBody;
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::post, "", hcclConfigMap, responseBody) != 0) {
        LOG_E("[%s] [Deployer] Restoring instance. Create ranktable ConfigMap failed!",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    std::string deployJson;
    if (mKubeResources.deploymentJsons.size() > 0) {
        if (ind >= mKubeResources.deploymentJsons.size()) {
            LOG_E("[%s] [Deployer] The index %u exceeds the maximum deploymentJsons"
                " arrayarray size limit of %zu.",
                GetErrorCode(ErrorType::OUT_OF_RANGE, DeployerFeature::CROSSNODE_SERVER).c_str(), ind,
                mKubeResources.deploymentJsons.size());
            return -1;
        }
        deployJson = mKubeResources.deploymentJsons[ind];
    } else {
        deployJson = CreateDeployJson(mServiceParams, deplymentName, ranktableName);
    }
    url = "/apis/apps/v1/namespaces/" + nameSpace + "/deployments";

    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::post, "", deployJson, responseBody) != 0) {
        LOG_E("[%s] [Deployer] Restoring instance.  Create kube Deployment resource failed!",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str());
        return -1;
    }
    return 0;
}

int32_t CrossNodeServer::RetryLabelMasterPod(uint32_t ind, RestoreState &restoreState)
{
    for (uint32_t i = 0; i < 10; i++) {  // 10 find master pod retry times
        bool isLabeled = false;
        if (FindAndLabelMasterPod(ind, isLabeled) != 0) {
            sleep(g_findMaterPodInterval);
            continue;
        }
        if (!isLabeled) {
            LOG_W("[%s] [Deployer] Restoring instance. The master pod is not labeled.",
                GetWarnCode(ErrorType::WARNING, DeployerFeature::CROSSNODE_SERVER).c_str());
            sleep(g_findMaterPodInterval);
            continue;
        }
        LOG_I("Restoring instance. Succeed to label the master pod, finish the whole restore process.");
        restoreState = RestoreState::NONE;
        return 0;
    }
    if (restoreState == RestoreState::PENDING) {
        return -1;
    }
    return 0;
}

int32_t CrossNodeServer::RecoverInstance(uint32_t ind, InferInstance &instacne)
{
    if (instacne.restoreState == RestoreState::NONE) {
        instacne.restoreState = RestoreState::RECREATING;
    } else if (instacne.restoreState == RestoreState::PENDING) {
        if (RetryLabelMasterPod(ind, instacne.restoreState) != 0) {
            return -1;
        }
        return 0;
    }
    std::string url = "/api/v1/namespaces/" + instacne.nameSpace + "/configmaps/" + instacne.ranktableName;
    std::string responseBody;
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::delete_, "", "", responseBody) != 0) {
        LOG_E("[%s] [Deployer] Restoring instance. Failed to delete the old ranktable ConfigMap %s.",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str(),
            instacne.ranktableName.c_str());
        return -1;
    }

    url = "/apis/apps/v1/namespaces/" + instacne.nameSpace + "/deployments/" + instacne.deploymentName;
    if (this->SendKubeHttpRequest(url, boost::beast::http::verb::delete_, "", "", responseBody) != 0) {
        LOG_E("[%s] [Deployer] Restoring instance. Failed to the delete old deployment %s.",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str(),
            instacne.deploymentName.c_str());
        return -1;
    }
    sleep(3); // 3 wait delete finish
    if (CreateInstanceResource(ind, instacne.nameSpace, instacne.ranktableName, instacne.deploymentName) != 0) {
        return -1;
    }

    instacne.restoreState = RestoreState::PENDING;
    if (RetryLabelMasterPod(ind, instacne.restoreState) != 0) {
        return -1;
    }
    return 0;
}

void CrossNodeServer::CheckInstanceStatus(uint32_t ind, InferInstance &instance)
{
    if (instance.status == InstanceStatus::ABNORMAL) {
        LOG_I("Minitoring instance. Try to restore instance %u.", ind);
        if (this->RecoverInstance(ind, instance) != 0) {
            LOG_E("[%s] [Deployer] Restoring the instance: failed to restore %s.",
                GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str(),
                instance.deploymentName.c_str());
        } else {
            LOG_I("Minitoring instance. Succeed to restart instance_%u, please wait for its readiness.", ind);
            instance.status = InstanceStatus::UNREADY;
        }
    }
}

void CrossNodeServer::MinitoringInstance()
{
    while (mDeployStatus != DeployStatus::STOPPING) {
        try {
            UpdateInstanceStatus();
        } catch (const std::exception& e) {
            std::string msg = "[CrossNodeServer] UpdateInstanceStatus failed: " + std::string(e.what());
            LOG_E(msg);
            throw;
        }catch (...) {
            std::string msg = "[CrossNodeServer] UpdateInstanceStatus failed: Possible due to network call "
                "blocking/failures or race conditions in multi-threaded access. For details, "
                "check the UpdateInstanceStatus function.";
            LOG_E(msg);
            throw;
        }
        for (auto &iter : mInferInstance) {
            uint32_t ind = iter.first;
            InferInstance &instance = iter.second;
            this->CheckInstanceStatus(ind, instance);
        }
        sleep(g_monitorInstanceInterval);
    }
    return;
}

void CrossNodeServer::UpdateInstanceStatus()
{
    for (auto &iter : mInferInstance) {
        uint32_t ind = iter.first;
        InferInstance &instance = iter.second;
        this->mMointerMindieHttpClient.SetHostAndPort(instance.masterPodIp,
            std::to_string(this->mServiceParams.mindieServerMngPort));
        std::string url = this->mServiceParams.detectInnerError ? std::string("/health/timed-") +
            std::to_string(mServiceParams.livenessTimeout) : std::string("/v2/health/ready");
        std::string response;
        LOG_D("Updating instance status. Ready to get readiness of mindie server master pod, server: %s, instance_%u.",
            this->mServerName.c_str(), ind);
        if (GetFromMindIEServer(mMointerMindieHttpClient, url, response,
            static_cast<uint32_t>(mServiceParams.livenessTimeout),
            mServiceParams.livenessFailureThreshold - 1) == 0) {
            LOG_I("Updating instance status. Instance_%u is ready.", ind);
            instance.status = InstanceStatus::READY;
        } else {
            if (instance.status == InstanceStatus::READY) {
                instance.status = InstanceStatus::ABNORMAL;
                LOG_E("[%s] [Deployer] Inference instance_%u enter abnormal state!",
                    GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::CROSSNODE_SERVER).c_str(),
                    ind);
                instance.masterPodIp = "";
            } else if (instance.status == InstanceStatus::UNREADY &&
                    ((std::chrono::duration_cast<std::chrono::seconds>(
                    std::chrono::system_clock::now().time_since_epoch()) -
                    instance.masterPodCreateTime).count() > this->mServiceParams.initDealy)) {
                instance.status = InstanceStatus::ABNORMAL;
                LOG_E("[%s] [Deployer] Inference instance_%u has been in an unready state for over %d seconds. "
                    "It has now entered an abnormal state.", GetErrorCode(ErrorType::CALL_ERROR,
                    DeployerFeature::CROSSNODE_SERVER).c_str(), ind, this->mServiceParams.initDealy);
            }
        }
    }
    return;
}

REGISTER_INFER_SERVER(CrossNodeServer, mindie_cross_node);

}