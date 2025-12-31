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
#include <string>
#include <vector>
#include <algorithm>
#include <mutex>
#include <condition_variable>
#include <iostream>
#include <cstdlib>
#include <nlohmann/json.hpp>
#include "HttpClient.h"
#include "ConfigParams.h"
#include "Logger.h"
#include "Util.h"

using namespace MINDIE::MS;
static int64_t g_maxPort = 65535; // 65535 最大端口号
static int64_t g_minPort = 1024; // 1024 最小端口号
static void AssignParams(nlohmann::json mainObj, MSCtlParams &params)
{
    params.dstIp = mainObj["http"]["dstIP"];
    params.dstPort = mainObj["http"]["dstPort"];
    params.timeOut = mainObj["http"]["timeout"];
    params.clientTlsItems.tlsEnable = true;
    if (params.clientTlsItems.tlsEnable) {
        params.clientTlsItems.caCert = mainObj["http"]["ca_cert"];
        params.clientTlsItems.tlsCert = mainObj["http"]["tls_cert"];
        params.clientTlsItems.tlsKey = mainObj["http"]["tls_key"];
        params.clientTlsItems.tlsCrl = mainObj["http"]["tls_crl"];
    }
}

static int32_t CheckInitConfigJsonValid(const nlohmann::json &mainObj)
{
    if (!IsJsonObjValid(mainObj, "http") ||
        !IsJsonStringValid(mainObj["http"], "dstIP") ||
        !IsJsonIntValid(mainObj["http"], "dstPort", g_minPort, g_maxPort) ||
        !IsJsonIntValid(mainObj["http"], "timeout", 1, 60)) {    // 60 最大超时时间
        return -1;
    }
    if (!IsJsonStringValid(mainObj["http"], "ca_cert") ||
        !IsJsonStringValid(mainObj["http"], "tls_cert") ||
        !IsJsonStringValid(mainObj["http"], "tls_key") ||
        !IsJsonStringValid(mainObj["http"], "tls_crl", 0)) {
        return -1;
    }
    std::string ip = mainObj["http"]["dstIP"];
    if (!IsValidIp(ip)) {
        LOG_E("[%s] [Msctl] Invalid ip adress",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::MSCTL).c_str());
        return -1;
    }

    return 0;
}

static int32_t ParseInitConfig(MSCtlParams &params)
{
    const char* homeDir = std::getenv("HOME");
    if (homeDir == nullptr) {
        LOG_E("[%s] [Msctl] Error: HOME environment variable not set.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::MSCTL).c_str());
        return -1;
    }
    std::string fileName = std::string(homeDir) + std::string("/.mindie_ms/msctl.json");
    std::string jsonString;
    if (FileToBuffer(fileName, jsonString, 0640) != 0) {    // msctl.json的权限要求是0640
        LOG_E("[%s] [Msctl] Filename is not file or file cannot be opened.",
            GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::MSCTL).c_str());
        return -1;
    }
    if (!CheckJsonStringSize(jsonString) || !nlohmann::json::accept(jsonString)) {
        LOG_E("[%s] [Msctl] Invalid json format",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::MSCTL).c_str());
        return -1;
    }
    auto mainObj = nlohmann::json::parse(jsonString, CheckJsonDepthCallBack);
    if (CheckInitConfigJsonValid(mainObj) != 0) {
        return -1;
    }

    AssignParams(mainObj, params);

    MINDIE::MS::DefaultLogConfig defaultLogConfig;
    std::string logLevel = "";
    if (mainObj.contains("log_level") && !mainObj["log_level"].empty()) {
        logLevel = mainObj["log_level"];
    }
    mainObj.erase("log_level");
    mainObj["log_info"] = {{"log_level", logLevel}};
    return Logger::Singleton()->Init(defaultLogConfig, mainObj, fileName);
}

static uint32_t g_resourceInd = 2;  // 2 资源
static uint32_t g_optionType = 3; // 3 选项类型
static uint32_t g_optionValue = 4;    // 4 选项
static int32_t g_totalArgs = 5; // 5 共5个参数

static void PrintResponse(const std::string &response)
{
    if (CheckJsonStringSize(response) && nlohmann::json::accept(response)) {
        nlohmann::json responseJsonObj = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        std::cout << responseJsonObj.dump(4) << std::endl; // 4   换行缩进为4个空格
    } else if (response.size() != 0) {
        std::cout << response.substr(0, 256) << std::endl; // 256 最大打印长度
    }
}

static void PrintHelp()
{
    std::cout << "msctl: A Command Line Tool\n\n";
    std::cout << "Usage: msctl [command] [resource_type] [options]\n\n";

    std::cout << "Commands:\n";
    std::cout << "  create  Create a new resource\n";
    std::cout << "  delete  Delete an existing resource\n";
    std::cout << "  get     Retrieve information about a resource\n";
    std::cout << "  update  update a resource\n\n";

    std::cout << "Resource Types:\n";
    std::cout << "  infer_server  Manage inference servers\n\n";

    std::cout << "Options:\n";
    std::cout << "  -f  Specify the configuration file (used with 'create')\n";
    std::cout << "  -n  Specify the name of the resource (used with 'delete' and 'get')\n\n";

    std::cout << "Examples:\n";
    std::cout << "  Create a new inference server using a configuration file:\n";
    std::cout << "    msctl create infer_server -f ./infer_server.json\n";
    std::cout << "\n";
    std::cout << "  Delete an existing inference server by name which is defined in infer_server.json:\n";
    std::cout << "    msctl delete infer_server -n mindie-server\n";
    std::cout << "\n";
    std::cout << "  Get information about an existing inference server by name which is defined in "
        "infer_server.json\n";
    std::cout << "    msctl get infer_server -n mindie-server\n";
    std::cout << "  update a current server using an update file:\n";
    std::cout << "    msctl update infer_server -f update_server.json\n";
}

static int32_t UpdateOp(std::string &response, int32_t code, const MSCtlParams &params,
    HttpClient &httpClient, std::string fileName)
{
    std::string jsonStr;
    if (FileToBuffer(fileName, jsonStr, 0640) != 0) {  // 0640 update_server.json的权限要求是0640
        LOG_E("[%s] [Msctl] File cannot be opened.",
            GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::MSCTL).c_str());
        return -1;
    }
    if (!CheckJsonStringSize(jsonStr) || !nlohmann::json::accept(jsonStr)) {
        LOG_E("[%s] [Msctl] File content is not json.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::MSCTL).c_str());
        return -1;
    }
    nlohmann::json json = nlohmann::json::parse(jsonStr, CheckJsonDepthCallBack);
    if (!IsJsonStringValid(json, "server_name")) {
        LOG_E("[%s] [Msctl] Update file should include server_name.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::MSCTL).c_str());
        return -1;
    }
    std::map<boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    std::string name = json["server_name"];
    Request req = {"/v1/servers/" + name, boost::beast::http::verb::post, map, jsonStr};
    int32_t ret = httpClient.SendRequest(req, params.timeOut, 0, response, code);
    return ret;
}

static bool HttpClientInit(MSCtlParams &params, HttpClient &httpClient)
{
    if (ParseInitConfig(params) != 0) {
        LOG_E("[%s] [Msctl] Failed to parse config from path ~/.mindie_ms/msctl.json!",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::MSCTL).c_str());
        return false;
    }

    if (httpClient.Init(params.dstIp, std::to_string(params.dstPort), params.clientTlsItems, false) != 0) {
        LOG_E("[%s] [Msctl] Init HTTP client Failed!",
            GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::MSCTL).c_str());
        return false;
    }

    return true;
}

int main(int argc, char* argv[])
{
    if (argc == 2 && (strcmp(argv[1], "--help") == 0 || strcmp(argv[1], "-h") == 0)) { // 2 命令参数数
        PrintHelp();
        return 0;
    }
    MSCtlParams params;
    HttpClient httpClient;

    if (!HttpClientInit(params, httpClient)) {
        return -1;
    }

    std::string response;
    int32_t code = 0;
    std::map<boost::beast::http::field, std::string> map;
    if (argc == g_totalArgs && strcmp(argv[1], "create") == 0 && strcmp(argv[g_resourceInd], "infer_server") == 0 &&
        strcmp(argv[g_optionType], "-f") == 0) {
        std::string jsonString;
        if (FileToBuffer(argv[g_optionValue], jsonString, 0640) != 0) { // infer_server.json的权限要求是0640
            LOG_E("[%s] [Msctl] The 'infer_server.json' config file cannot be opened.",
                GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::MSCTL).c_str());
            return -1;
        }
        map[boost::beast::http::field::accept] = "*/*";
        map[boost::beast::http::field::content_type] = "application/json";
        Request req = {"/v1/servers", boost::beast::http::verb::post, map, jsonString};
        httpClient.SendRequest(req, params.timeOut, 0, response, code);
    } else if (argc == g_totalArgs && strcmp(argv[1], "delete") == 0 &&
        strcmp(argv[g_resourceInd], "infer_server") == 0 && strcmp(argv[g_optionType], "-n") == 0) {
        Request req = {"/v1/servers/" + std::string(argv[g_optionValue]), boost::beast::http::verb::delete_, map, ""};
        httpClient.SendRequest(req, params.timeOut, 0, response, code);
    } else if (argc == g_totalArgs && strcmp(argv[1], "get") == 0 && strcmp(argv[g_resourceInd], "infer_server") == 0 &&
        strcmp(argv[g_optionType], "-n") == 0) {
        Request req = {"/v1/servers/" + std::string(argv[g_optionValue]), boost::beast::http::verb::get, map, ""};
        httpClient.SendRequest(req, params.timeOut, 0, response, code);
    } else if (argc == g_totalArgs && strcmp(argv[1], "update") == 0 &&
        strcmp(argv[g_resourceInd], "infer_server") == 0 && strcmp(argv[g_optionType], "-f") == 0) {
        if (UpdateOp(response, code, params, httpClient, argv[g_optionValue]) != 0) {
            return -1;
        }
    } else {
        PrintHelp();
        return -1;
    }
    PrintResponse(response);
    return 0;
}