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

namespace MINDIE {
namespace MS {
static int64_t g_codeOK = 200; // 200 接口返回200
constexpr const char* MINDIE_HTTP_CLIENT_CTL_JSON = "../conf/http_client_ctl.json";
constexpr auto CONFIG_CHECK_FILES_ENV = "MINDIE_CHECK_INPUTFILES_PERMISSION";
static void AssignParams(nlohmann::json mainObj, ProbeCtlParams &params)
{
    params.clientTlsItems.tlsEnable = mainObj["tls_enable"].get<bool>();
    auto env = std::getenv("MINDIE_USE_HTTPS");
    if (env != nullptr && std::string(env) == "true") {
        params.clientTlsItems.tlsEnable = true;
    }
    if (env != nullptr && std::string(env) == "false") {
        params.clientTlsItems.tlsEnable = false;
    }
    if (!params.clientTlsItems.tlsEnable) {
        return;
    }
    params.clientTlsItems.caCert = mainObj["cert"]["ca_cert"];
    params.clientTlsItems.tlsCert = mainObj["cert"]["tls_cert"];
    params.clientTlsItems.tlsKey = mainObj["cert"]["tls_key"];
    params.clientTlsItems.tlsPasswd = mainObj["cert"]["tls_passwd"];
    if (mainObj["cert"].count("tls_crl") > 0) {
        params.clientTlsItems.tlsCrl = mainObj["cert"]["tls_crl"];
    }
}

static int32_t CheckConfigJsonValid(const nlohmann::json &mainObj)
{
    if (!IsJsonBoolValid(mainObj, "tls_enable")) {
        return -1;
    }
    if (!IsJsonObjValid(mainObj, "cert") ||
        !IsJsonStringValid(mainObj["cert"], "ca_cert") ||
        !IsJsonStringValid(mainObj["cert"], "tls_cert") ||
        !IsJsonStringValid(mainObj["cert"], "tls_key") ||
        !IsJsonStringValid(mainObj["cert"], "tls_passwd") ||
        !IsJsonStringValid(mainObj["cert"], "tls_crl", 0)) {
        return -1;
    }

    return 0;
}

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

static int32_t ParseInitConfig(ProbeCtlParams &params)
{
    auto env = std::getenv("MINDIE_UTILS_HTTP_CLIENT_CTL_CONFIG_FILE_PATH");
    std::string configFile = (env == nullptr) ? MINDIE_HTTP_CLIENT_CTL_JSON : env;
    params.checkMountedFiles = GetCheckFiles();
    uint32_t mode = (env == nullptr || params.checkMountedFiles) ? 0640 : 0777;  // 校验权限是0640, 不校验是0777
    std::string jsonString;
    if (FileToBuffer(configFile, jsonString, mode,
        (env == nullptr || params.checkMountedFiles)) != 0) {    // probe.json的权限要求是0640
        LOG_E("[%s] [ParseInitConfig] Failed to load the configuration file '%s'. "
            "Please check the file path and permissions.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPCLIENT).c_str(), configFile.c_str());
        return -1;
    }
    if (!nlohmann::json::accept(jsonString)) {
        LOG_E("[%s] [ParseInitConfig] The configuration file '%s' has an invalid JSON format. "
            "Please ensure the file is properly formatted.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), configFile.c_str());
        return -1;
    }
    auto mainObj = nlohmann::json::parse(jsonString, CheckJsonDepthCallBack);
    if (CheckConfigJsonValid(mainObj) != 0) {
        return -1;
    }

    AssignParams(mainObj, params);
    MINDIE::MS::DefaultLogConfig defaultLogConfig;
    defaultLogConfig.option.subModule = SubModule::MS_DEPLOYER;
    return Logger::Singleton()->Init(defaultLogConfig, mainObj, configFile);
}

static void PrintResponse(const std::string &response)
{
    if (nlohmann::json::accept(response)) {
        nlohmann::json responseJsonObj = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        std::cout << responseJsonObj.dump(4) << std::endl; // 4   换行缩进为4个空格
    } else if (response.size() != 0) {
        std::cout << response.substr(0, 256) << std::endl; // 256 最大打印长度
    }
}

static void PrintHelp()
{
    std::cout << "http_client_ctl: A Command Line Tool\n\n";
    std::cout << "Usage: http_client_ctl [IP] [Port] [Url]\n\n";

    std::cout << "IP:\n";
    std::cout << "  The destiny IP for https get request\n\n";

    std::cout << "Port:\n";
    std::cout << "  The destiny Port for https get request\n\n";

    std::cout << "Url:\n";
    std::cout << "  The destiny Url for https get request\n\n";

    std::cout << "TimeoutSeconds:\n";
    std::cout << "  The timeout seconds for https get request\n\n";

    std::cout << "RetryTimes:\n";
    std::cout << "  The retry times for https get request\n\n";

    std::cout << "Examples:\n";
    std::cout << "  Performs https get request:\n";
    std::cout << "    http_client_ctl 127.0.0.1 1026 /v1/liveness 10 3\n";
    std::cout << "\n";
}


int ParseTimeoutAndRetries(int64_t &timeoutSeconds, int64_t &retryTimes, const std::vector<std::string> &argv)
{
    std::string timeoutSecondsStr = argv[4]; // 4 timeoutSecond
    try {
        timeoutSeconds = std::stoi(timeoutSecondsStr);
    } catch (const std::exception& e) {
        LOG_E("[%s] [Client] Parse timeout seconds %s failed.",
            GetErrorCode(ErrorType::EXCEPTION, CommonFeature::HTTPCLIENT).c_str(),
            timeoutSecondsStr.c_str());
        return -1;
    }
    if (timeoutSeconds < 1 || timeoutSeconds > 600) { // 600 最大超时时间
        LOG_E("[%s] [Client] The timeout seconds value must be in range [1, 600]. Provided value %lld.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), timeoutSeconds);
        return -1;
    }
    std::string retryTimesStr = argv[5]; // 5 retry times
    try {
        retryTimes = std::stoi(retryTimesStr);
    } catch (const std::exception& e) {
        LOG_E("[%s] [Client] Parse retry times %s failed.",
            GetErrorCode(ErrorType::EXCEPTION, CommonFeature::HTTPCLIENT).c_str(),
            retryTimesStr.c_str());
        return -1;
    }
    if (retryTimes < 0 || retryTimes > 30) { // 30 最大重试次数
        LOG_E("[%s] [Client] Retry times must be in range [0, 30], but got %lld.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), retryTimes);
        return -1;
    }
    return 0;
}


int32_t ParseIPPortUrl(std::string &ip, std::string &portStr, std::string &url, const std::vector<std::string> &argv)
{
    ip = argv[1];
    if (!IsValidIp(ip)) {
        LOG_E("[%s] [Client] The provided IP address '%s' is invalid. Please ensure it is a valid IP address.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), ip.c_str());
        return -1;
    }
    portStr = argv[2]; // 2 port
    int64_t port = 0;
    try {
        port = std::stoi(portStr);
    } catch (const std::exception& e) {
        LOG_E("[%s] [Client] Failed to parse the port value '%s'. Please ensure it is a valid integer.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), portStr.c_str());
        return -1;
    }
    if (!IsValidPort(port)) {
        LOG_E("[%s] [Client] The provided port '%lld' is invalid. Please ensure the port is within the valid range.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), port);
        return -1;
    }
    url = argv[3]; // 3 url
    if (!IsValidUrlString(url)) {
        LOG_E("[%s] [Client] The provided URL '%s' is invalid. Please ensure the URL follows the correct format.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), url.c_str());
        return -1;
    }
    return 0;
}

}
}

using namespace MINDIE::MS;
int main(int argc, char* argv[])
{
    if (argc == 2 && (strcmp(argv[1], "--help") == 0 || strcmp(argv[1], "-h") == 0)) { // 2 命令参数数
        PrintHelp();
        return 0;
    }
    if (argc < 6) { // 6 有效参数的个数为6
        LOG_E("[%s] [Client] Insufficient input parameters. Expected parameters: [IP] [Port] [Url] [TimeoutSeconds]"
            " [RetryTimes]", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str());
        exit(1);
    }
    ProbeCtlParams params;
    std::string logCode = GetErrorCode(ErrorType::CALL_ERROR, CommonFeature::HTTPCLIENT);
    if (ParseInitConfig(params) != 0) {
        LOG_E("[%s] [Client] Failed to parse configuration. Please check config file for errors.", logCode.c_str());
        return -1;
    }
    std::string ip;
    std::string port;
    std::string url;
    int64_t timeoutSeconds = 0;
    int64_t retryTimes = 0;
    std::vector<std::string> argVec(6); // 6 有效参数的个数为6
    for (int i = 0; i < 6; ++i) {       // 6 有效参数的个数为6
        argVec[i] = argv[i];
    }
    if (ParseIPPortUrl(ip, port, url, argVec) != 0 || ParseTimeoutAndRetries(timeoutSeconds, retryTimes, argVec) != 0) {
        LOG_E("[%s] [Client] Failed to parse parameters. Please check the provided arguments.", logCode.c_str());
        exit(-1);
    }

    params.clientTlsItems.checkFiles = params.checkMountedFiles;
    HttpClient httpClient;
    if (httpClient.Init(ip, port, params.clientTlsItems, true) != 0) {
        LOG_E("[%s] [Client] HTTP client initial failed. Please verify the IP and port settings.", logCode.c_str());
        exit(1);
    };
    std::string response;
    int32_t code;
    std::map<boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    Request req = {url, boost::beast::http::verb::get, map, ""};
    auto ret = httpClient.SendRequest(req, timeoutSeconds, retryTimes, response, code);
    PrintResponse(response);
    if (ret == 0 && code == g_codeOK) {
        LOG_I("[Client] HTTP request success. IP is %s, Port is %s, URL is %s.",
            ip.c_str(), port.c_str(), url.c_str());
        exit(0);
    }
    LOG_E("[%s] [Client] HTTP request failed. IP is %s, Port is %s, URL is %s, Response Code is %d. ", GetErrorCode(
        ErrorType::UNREACHABLE, CommonFeature::HTTPCLIENT).c_str(), ip.c_str(), port.c_str(), url.c_str(), code);
    exit(1);
}