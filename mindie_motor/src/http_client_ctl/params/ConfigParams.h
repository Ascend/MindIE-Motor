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
#ifndef MINDIE_MS_CONFIG
#define MINDIE_MS_CONFIG
#include <nlohmann/json.hpp>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/ssl.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>
#include <string>
#include <map>
#include <cstdint>
#include <vector>
#include "Util.h"
namespace MINDIE {
namespace MS {

bool IsJsonStringValid(const nlohmann::json &jsonObj, const std::string &key, uint32_t minLen = 1,
    uint32_t maxLen = 4096);
bool IsJsonObjValid(const nlohmann::json &jsonObj, const std::string &key);
bool IsJsonBoolValid(const nlohmann::json &jsonObj, const std::string &key);
bool IsJsonIntValid(const nlohmann::json &jsonObj, const std::string &key, int64_t min, int64_t max);
bool IsJsonArrayValid(const nlohmann::json &jsonObj, const std::string &key, uint64_t min, uint64_t max);
bool IsJsonDoubleValid(const nlohmann::json &jsonObj, const std::string &key, double min, double max);
int VerifyCallback(X509_STORE_CTX *x509ctx, void *arg);
bool IsValidString(const std::string &str);
bool IsValidUrlString(const std::string &str);
bool IsUrlInWhitelist(const std::string &url);

struct TlsItems {
    bool tlsEnable = true;
    bool checkFiles = true;
    std::string caCert;
    std::string tlsCert;
    std::string tlsKey;
    std::string tlsPasswd;
    std::string tlsCrl;
};

struct MSCtlParams {
    int64_t dstPort;
    std::string dstIp;
    TlsItems clientTlsItems;
    int64_t timeOut;
    std::string logLevel;
};

struct LoadServiceParams {
    std::string name;
    std::string nameSpace;
    int64_t replicas;
    int64_t crossNodeNum;
    std::string serverImage;
    int64_t servicePort;
    int64_t mindieServerPort;
    int64_t mindieServerMngPort;
    int64_t initDealy;
    bool detectInnerError;
    bool mindieUseHttps;
    int64_t livenessFailureThreshold;
    int64_t livenessTimeout;
    int64_t readinessFailureThreshold;
    int64_t readinessTimeout;
    std::string serverType; // 只接受[NodePort, None]
    std::string startupCmd;
    int64_t memRequest;
    int64_t cpuRequest;
    std::string miesInstallPath;
    std::string npuType;  // 只接受["Ascend910", "Ascend310P"]
    int64_t npuNum;
    std::map<std::string, std::tuple<std::string, std::string, bool>> mountMap;
    bool npuFaultReschedule;
    std::string scheduler;
    std::string modelName;
    int64_t worldSize;
    int64_t cpuMemSize;
    std::string createTime;
    std::string updateTime;
    std::string maxUnavailable;
    std::string maxSurge;
    int64_t terminationGracePeriodSeconds;
};

struct ResourcesInfo {
    std::string nameSpace;
    std::vector<std::string> deploymentNames;
    std::vector<std::string> deploymentJsons;
    std::string serviceName;
    std::vector<std::string> configMapNames;
};

struct HttpServerParams {
    std::string ip;
    int64_t port;
    TlsItems serverTlsItems;
    bool checkSubject = true;
};

struct ResourceLabel {
    std::string key;
    std::string value;
};

struct HttpClientParams {
    std::string k8sIP;
    int64_t k8sPort;
    int64_t prometheusPort;
    TlsItems k8sClientTlsItems;
    TlsItems mindieClientTlsItems;
    TlsItems promClientTlsItems;
};

struct ServerSaveStatus {
    int64_t replicas;
    std::string nameSpace;
    std::string serverName;
    std::string serverType;
    bool useService;
};

struct ProbeCtlParams {
    TlsItems clientTlsItems;
    bool checkMountedFiles = true;
};

struct VerifyItems {
    bool checkSubject;
    std::string crlFile;
    std::string organization;
    std::string commonName;
    std::string interCACommonName;
    bool checkFiles = true;
};

enum class ErrorCode {
    OK = 0,
    INTERNAL_ERROR = 1,
    INVALID_PARAMETER = 2,
    NOT_FOUND = 3
};

bool CreateFile(std::string &path);

bool IsValidIp(const std::string &ip, bool allowAllZeroIp = false);

bool IsValidIpv4(const std::string &ip, bool allowAllZeroIp, const std::string &logCode);

bool IsValidIpv6(const std::string &ip, bool allowAllZeroIp, const std::string &logCode);

std::string FormatHostPort(const std::string &host, int port);

bool IsValidPort(int64_t port);

bool IsValidPortString(const std::string &s);

int32_t DumpStringToFile(const std::string &filePath, const std::string &data);

void EraseDecryptedData(std::pair<char *, int32_t> &result);
bool DecryptPassword(int domainId, std::pair<char *, int32_t> &password, const TlsItems &tlsConfig);

int32_t StrToInt(const std::string &numberStr, int64_t &number);

std::string GetCurrentDateTimeString();
}
}
#endif