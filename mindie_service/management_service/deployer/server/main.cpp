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
#include <iostream>
#include <condition_variable>
#include "HttpServer.h"
#include "ServerManager.h"
#include "ConfigParams.h"
#include "Util.h"

using namespace MINDIE::MS;
static int64_t g_maxPort = 65535; // 65535 最大端口号
static int64_t g_minPort = 1024; // 1024 最小端口号
static bool IsTlsItemsValid(const nlohmann::json &mainObj, const std::string &tlsEnableKey,
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
            !IsJsonStringValid(mainObj[tlsItemName], "tls_passwd", 0) ||
            !IsJsonStringValid(mainObj[tlsItemName], "tls_crl", 0)) {
            return false;
        }
    }
    return true;
}
static bool IsJsonValid(const nlohmann::json &mainObj)
{
    if (!IsJsonStringValid(mainObj, "ip") || !IsJsonIntValid(mainObj, "port", g_minPort, g_maxPort) ||
        !IsJsonStringValid(mainObj, "k8s_apiserver_ip") ||
        !IsJsonIntValid(mainObj, "k8s_apiserver_port", g_minPort, g_maxPort) ||
        !IsJsonStringValid(mainObj, "ms_status_file")) {
        return false;
    }
    std::string tlsItemName = "server_tls_items";
    if (!IsJsonObjValid(mainObj, tlsItemName) ||
        !IsJsonStringValid(mainObj[tlsItemName], "ca_cert") ||
        !IsJsonStringValid(mainObj[tlsItemName], "tls_cert") ||
        !IsJsonStringValid(mainObj[tlsItemName], "tls_key") ||
        !IsJsonStringValid(mainObj[tlsItemName], "tls_passwd", 0) ||
        !IsJsonStringValid(mainObj[tlsItemName], "tls_crl", 0)) {
        return false;
    }
    if (!IsTlsItemsValid(mainObj, "client_k8s_tls_enable", "client_k8s_tls_items")) {
        return false;
    }
    if (!IsTlsItemsValid(mainObj, "client_mindie_server_tls_enable", "client_mindie_tls_items")) {
        return false;
    }
    return true;
}

static int32_t ObtainJsonValue(nlohmann::json mainObj, HttpClientParams &clientParams,
    HttpServerParams &httpServerParams)
{
    const char *ip = std::getenv("MINDIE_MS_SERVER_IP");
    httpServerParams.ip = ip != nullptr ? ip : mainObj["ip"];
    httpServerParams.port = mainObj["port"];
    if (!IsValidIp(httpServerParams.ip)) {
        LOG_E("[%s] [Deployer] Invalid server ip address format.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER).c_str());
        return -1;
    }
    httpServerParams.serverTlsItems.tlsEnable = true;
    if (httpServerParams.serverTlsItems.tlsEnable) {
        httpServerParams.serverTlsItems.caCert = mainObj["server_tls_items"]["ca_cert"];
        httpServerParams.serverTlsItems.tlsCert = mainObj["server_tls_items"]["tls_cert"];
        httpServerParams.serverTlsItems.tlsKey = mainObj["server_tls_items"]["tls_key"];
        httpServerParams.serverTlsItems.tlsPasswd = mainObj["server_tls_items"]["tls_passwd"];
        httpServerParams.serverTlsItems.tlsCrl = mainObj["server_tls_items"]["tls_crl"];
    }
    clientParams.k8sIP = mainObj["k8s_apiserver_ip"];
    clientParams.k8sPort = mainObj["k8s_apiserver_port"];
    if (!IsValidIp(clientParams.k8sIP)) {
        LOG_E("[%s] [Deployer] The provided Kubernetes IP address (%s) format is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER).c_str(), clientParams.k8sIP.c_str());
        return -1;
    }
    clientParams.k8sClientTlsItems.tlsEnable = mainObj["client_k8s_tls_enable"];
    if (mainObj["client_k8s_tls_enable"]) {
        clientParams.k8sClientTlsItems.caCert = mainObj["client_k8s_tls_items"]["ca_cert"];
        clientParams.k8sClientTlsItems.tlsCert = mainObj["client_k8s_tls_items"]["tls_cert"];
        clientParams.k8sClientTlsItems.tlsKey = mainObj["client_k8s_tls_items"]["tls_key"];
        clientParams.k8sClientTlsItems.tlsCrl = mainObj["client_k8s_tls_items"]["tls_crl"];
        clientParams.k8sClientTlsItems.tlsPasswd = mainObj["client_k8s_tls_items"]["tls_passwd"];
    }
    clientParams.mindieClientTlsItems.tlsEnable = mainObj["client_mindie_server_tls_enable"];
    if (mainObj["client_mindie_server_tls_enable"]) {
        clientParams.mindieClientTlsItems.caCert = mainObj["client_mindie_tls_items"]["ca_cert"];
        clientParams.mindieClientTlsItems.tlsCert = mainObj["client_mindie_tls_items"]["tls_cert"];
        clientParams.mindieClientTlsItems.tlsKey = mainObj["client_mindie_tls_items"]["tls_key"];
        clientParams.mindieClientTlsItems.tlsCrl = mainObj["client_mindie_tls_items"]["tls_crl"];
        clientParams.mindieClientTlsItems.tlsPasswd = mainObj["client_mindie_tls_items"]["tls_passwd"];
    }
    return 0;
}

static int32_t ParseCommandArgs(const char* fileName, HttpClientParams &clientParams,
    HttpServerParams &httpServerParams, std::string &statusPath)
{
    std::string jsonString;
    auto ret = FileToBuffer(fileName, jsonString, 0640);    // ms_server.json的权限要求是0640
    if (ret != 0) {
        LOG_E("[%s] [Deployer] Failed to open the server startup JSON file.",
            GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::SERVER).c_str());
        return ret;
    }
    if (!CheckJsonStringSize(jsonString) || !nlohmann::json::accept(jsonString)) {
        LOG_E("[%s] [Deployer] Failed to parse the server startup JSON file.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER).c_str());
        return -1;
    }
    auto mainObj = nlohmann::json::parse(jsonString, CheckJsonDepthCallBack);
    if (!IsJsonValid(mainObj)) {
        LOG_E("[%s] [Deployer] Server startup JSON config is invalid.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER).c_str());
        return -1;
    }
    std::string localIp = mainObj["ip"];
    if (!IsValidIp(localIp)) {
        LOG_E("[%s] [Deployer] Invalid IP address (%s) in server startup configuration.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER).c_str(), localIp.c_str());
        return -1;
    }

    if (ObtainJsonValue(mainObj, clientParams, httpServerParams) != 0) {
        return -1;
    }
    statusPath = mainObj["ms_status_file"];

    MINDIE::MS::DefaultLogConfig defaultLogConfig;
    defaultLogConfig.option.subModule = SubModule::MS_DEPLOYER;
    return Logger::Singleton()->Init(defaultLogConfig, mainObj, fileName);
}

static void PrintHelp()
{
    std::cout << "Usage: ./ms_server [config_file]\n\n";

    std::cout << "Description:\n";
    std::cout << "  ms_server is a management server used to manage mindie inference server.\n\n";

    std::cout << "Arguments:\n";
    std::cout << "  config_file    Path to the configuration file (in JSON format) that specifies the server settings,"
        " for more detail, see the product documentation.\n\n";

    std::cout << "Examples:\n";
    std::cout << "  Run the management server with the specified configuration file:\n";
    std::cout << "    ./ms_server ms_server.json\n";
    std::cout << "\n";
}

int main(int argc, char* argv[])
{
    if (argc != 2 || (argc == 2 && (strcmp(argv[1], "--help") == 0 || strcmp(argv[1], "-h") == 0))) { // 2 命令参数数
        PrintHelp();
        return 0;
    }

    MINDIE::MS::HttpClientParams clientParams;
    MINDIE::MS::HttpServerParams serverParams;
    std::string statusPath;
    int32_t ret = ParseCommandArgs(argv[1], clientParams, serverParams, statusPath);
    if (ret != 0) {
        LOG_E("[%s] [Deployer] Failed to parse input arguments!",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER).c_str());
        return ret;
    }

    MINDIE::MS::ServerManager serverManager(clientParams);

    if (serverManager.FromStatusFile(statusPath) != 0) {
        LOG_W("[%s] [Deployer] Failed to restore from server status!",
            GetWarnCode(ErrorType::WARNING, DeployerFeature::SERVER).c_str());
        return -1;
    }

    HttpServer server(1);
    std::string serversUrl = "/v1/servers";
    if ((server.RegisterPostUrlHandler(serversUrl, std::bind(&MINDIE::MS::ServerManager::PostServersHandler,
        &serverManager, std::placeholders::_1)) != 0) ||
        (server.RegisterGetUrlHandler(serversUrl, std::bind(&MINDIE::MS::ServerManager::GetDeployStatus,
        &serverManager, std::placeholders::_1)) != 0) ||
        (server.RegisterDeleteUrlHandler(serversUrl, std::bind(&MINDIE::MS::ServerManager::UnloadServer,
        &serverManager, std::placeholders::_1)) != 0)) {
        LOG_E("[%s] [Deployer] Failed to register the URL handler!",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::SERVER).c_str());
        return -1;
    }

    if (server.Run(serverParams) != 0) {
        LOG_M("[Start] Failed to run the HTTP server!");
        return -1;
    }
    LOG_M("[Exit] Exit the process gracefully.");
    return 0;
}