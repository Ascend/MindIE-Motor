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
#include <ctime>
#include <memory>
#include <string>
#include <cstring>
#include <fstream>
#include <algorithm>
#include <sys/stat.h>
#include <sys/ipc.h>
#include <arpa/inet.h> // for inet_pton
#include "securec.h"
#include "Logger.h"
#include "ConfigParams.h"

namespace MINDIE {
namespace MS {

static constexpr int MIN_VALID_KEY = 0x20161111;
static constexpr int MAX_VALID_KEY = 0x20169999;
// URL白名单常量
static constexpr const char* ALLOWED_URLS[] = {
    "/v1/startup",
    "/v1/health",
    "/v1/readiness",
    "/v2/health/ready",
    "/stopService"
};
static constexpr size_t ALLOWED_URLS_COUNT = sizeof(ALLOWED_URLS) / sizeof(ALLOWED_URLS[0]);

int32_t DumpStringToFile(const std::string &filePath, const std::string &data)
{
    std::ofstream outfile(filePath, std::ios::out);
    if (outfile.is_open()) {
        outfile << data << std::endl;  // 4 换行缩进为4个空格
        outfile.close();
        uint32_t mode = 0640;
        if (chmod(filePath.c_str(), mode) != 0) {
            LOG_E("[%s] [ConfigParams] Failed to set permissions for file %s.",
                GetErrorCode(ErrorType::EXCEPTION, CommonFeature::HTTPCLIENT).c_str(),
                filePath.c_str());
            return -1;
        }
    } else {
        LOG_E("[%s] [ConfigParams] Failed to dump data to file %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            filePath.c_str());
        return -1;
    }
    return 0;
}

bool IsValidIp(const std::string &ip, bool allowAllZeroIp)
{
    std::string logCode = GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT);

    if (ip.empty()) {
        LOG_E("[%s] [ConfigParams] IP address is empty.", logCode.c_str());
        return false;
    }

    if (ip.find(':') != std::string::npos) {
        return IsValidIpv6(ip, allowAllZeroIp, logCode);
    } else {
        return IsValidIpv4(ip, allowAllZeroIp, logCode);
    }
}

bool IsValidIpv4(const std::string &ip, bool allowAllZeroIp, const std::string &logCode)
{
    if (!allowAllZeroIp && ip == "0.0.0.0") {
        LOG_E("[%s] [ConfigParams] Ip address can not be 0.0.0.0.", logCode.c_str());
        return false;
    }

    struct in_addr addr;
    if (inet_pton(AF_INET, ip.c_str(), &addr) != 1) {
        LOG_E("[%s] [ConfigParams] Invalid IPv4 address format: '%s'.", logCode.c_str(), ip.c_str());
        return false;
    }
    return true;
}

bool IsValidIpv6(const std::string &ip, bool allowAllZeroIp, const std::string &logCode)
{
    if (!allowAllZeroIp && ip == "::") {
        return false;
    }

    struct in6_addr addr;
    if (inet_pton(AF_INET6, ip.c_str(), &addr) != 1) {
        LOG_E("[%s] [ConfigParams] Invalid IPv6 address format: '%s'.", logCode.c_str(), ip.c_str());
        return false;
    }
    return true;
}

std::string FormatHostPort(const std::string &host, int port)
{
    if (host.empty()) {
        return "";
    }
    struct in6_addr addr;
    if (inet_pton(AF_INET6, host.c_str(), &addr) == 1) {
        return "[" + host + "]:" + std::to_string(port);
    }
    return host + ":" + std::to_string(port);
}

bool IsValidPort(int64_t port)
{
    return (port >= 1024) && (port <= 65535); // 1024, 65535 最小最大端口号
}

bool IsValidPortString(const std::string &s)
{
    if (!std::all_of(s.begin(), s.end(), [](char c) {
        return std::isdigit(c);
    })) {
        LOG_E("[%s] [ConfigParams] Invalid port numbebr. The string '%s' contains non-digit characters. "
            "Please provide a valid numeric string.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPCLIENT).c_str(),
            s.c_str());
        return false;
    }

    try {
        int port = std::stoi(s);
        return (port >= 1024) && (port <= 65535); // 1024, 65535 最小最大端口号
    } catch (...) {
        LOG_E("[%s] [ConfigParams] Invalid port number: '%s'.",
            GetErrorCode(ErrorType::EXCEPTION, CommonFeature::HTTPCLIENT).c_str(),
            s.c_str());
        return false;
    }
    return false;
}

bool IsJsonStringValid(const nlohmann::json &jsonObj, const std::string &key, uint32_t minLen, uint32_t maxLen)
{
    if (!jsonObj.contains(key)) {
        LOG_E("[%s] [ConfigParams] JSON object is missing the expected key '%s'.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str());
        return false;
    }
    if (!jsonObj[key].is_string()) {
        LOG_E("[%s] [ConfigParams] Invalid JSON value for key '%s'. Expected a string, but found: %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str(), typeid(jsonObj[key]).name());
        return false;
    }
    std::string val = jsonObj[key];
    if (val.size() < minLen || val.size() > maxLen) {
        LOG_E("[%s] [ConfigParams] Invalid length for string value of key '%s'. "
            "Length must be between [%u, %u], but found: %zu.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str(), minLen, maxLen, val.size());
        return false;
    }
    return true;
}

bool IsJsonArrayValid(const nlohmann::json &jsonObj, const std::string &key, uint64_t min, uint64_t max)
{
    if (!jsonObj.contains(key)) {
        LOG_E("[%s] [ConfigParams]The JSON object is missing the expected key '%s'. Please ensure the key exists.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str());
        return false;
    }
    if (!jsonObj[key].is_array()) {
        LOG_E("[%s] [ConfigParams] Invalid JSON value type for key '%s'. Expected an array, but found: %s.",
              GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), key.c_str(),
              typeid(jsonObj[key]).name());
        return false;
    }
    auto array = jsonObj[key];
    if (array.size() < min || array.size() > max) {
        LOG_E("[%s] [ConfigParams] The size of the JSON array for key '%s' is out of range. Expected between "
            "[%lu, %lu], but got %zu.", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str(), min, max, array.size());;
        return false;
    }
    return true;
}

bool IsJsonIntValid(const nlohmann::json &jsonObj, const std::string &key, int64_t min, int64_t max)
{
    if (!jsonObj.contains(key)) {
        LOG_E("[%s] [ConfigParams]The JSON object is missing the expected key '%s'. Please ensure the key exists.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str());
        return false;
    }
    if (!jsonObj[key].is_number_integer()) {
        LOG_E("[%s] [ConfigParams] Invalid JSON value type for key '%s'. Expected an integer, but found: %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str(), typeid(jsonObj[key]).name());
        return false;
    }
    int64_t val = jsonObj[key];

    if (val < min || val > max) {
        LOG_E("[%s] [ConfigParams] Invalid integer value for key '%s'. The value must be in the range [%ld, %ld], "
            "but found: %ld.", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str(), min, max, val);
        return false;
    }
    return true;
}

bool IsJsonDoubleValid(const nlohmann::json &jsonObj, const std::string &key, double min, double max)
{
    if (!jsonObj.contains(key)) {
        LOG_E("[%s] [ConfigParams]The JSON object is missing the expected key '%s'. Please ensure the key exists.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str());
        return false;
    }
    if (!jsonObj[key].is_number()) {
        LOG_E("[%s] [ConfigParams] Invalid JSON value type for key '%s'. Expected a number, but found: %s.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str(), typeid(jsonObj[key]).name());
        return false;
    }
    double val = jsonObj[key];

    if (val < min || val > max) {
        LOG_E("[%s] [ConfigParams] Invalid double value for key '%s'. The value must be in the range [%f, %f], "
            "but found: %f.", GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str(), min, max, val);
        return false;
    }
    return true;
}

bool IsJsonObjValid(const nlohmann::json &jsonObj, const std::string &key)
{
    if (!jsonObj.contains(key)) {
        LOG_E("[%s] [ConfigParams] Missing key '%s' in the JSON object. Please ensure the key exists.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str());
        return false;
    }

    if (!jsonObj[key].is_object()) {
        LOG_E("[%s] [ConfigParams] Invalid type for key '%s'. Expected an object, but found: %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str(), typeid(jsonObj[key]).name());
        return false;
    }
    return true;
}

bool IsJsonBoolValid(const nlohmann::json &jsonObj, const std::string &key)
{
    if (!jsonObj.contains(key)) {
        LOG_E("[%s] [ConfigParams] Missing key '%s' in the JSON object. Please ensure the key exists.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str());
        return false;
    }
    if (!jsonObj[key].is_boolean()) {
        LOG_E("[%s] [ConfigParams] Invalid type for key '%s'. Expected a boolean, but found: %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            key.c_str(), typeid(jsonObj[key]).name());
        return false;
    }
    return true;
}

static bool IsValidChar(char c)
{
    return std::isalnum(c) || c == '-' || c == '_';
}

bool IsValidString(const std::string &str)
{
    bool ret = std::all_of(str.begin(), str.end(), IsValidChar);
    if (!ret) {
        LOG_E("[%s] [ConfigParams] Invalid string '%s'. The string must contain only alphanumeric characters,"
            " '-', or '_'.", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), str.c_str());
    }
    return ret;
}
static bool IsValidUrlChar(char c)
{
    return std::isalnum(c) || c == '-' || c == '_' || c == '/';
}

bool IsUrlInWhitelist(const std::string &url)
{
    for (size_t i = 0; i < ALLOWED_URLS_COUNT; ++i) {
        if (url == ALLOWED_URLS[i]) {
            return true;
        }
    }
    return false;
}

bool IsValidUrlString(const std::string &str)
{
    if (str.empty()) {
        LOG_E("[%s] [ConfigParams] The URL string is empty. Please provide a valid URL.",
              GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str());
        return false;
    }

    // 检查URL是否在白名单中
    if (!IsUrlInWhitelist(str)) {
        LOG_E("[%s] [ConfigParams] The URL string '%s' is not in the allowed whitelist.",
              GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), str.c_str());
        return false;
    }
    if (str.at(0) != '/') {
        LOG_E("[%s] [ConfigParams] The URL string '%s' does not start with '/'. URLs must start with '/'.",
              GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), str.c_str());
        return false;
    }
    bool ret = std::all_of(str.begin(), str.end(), IsValidUrlChar);
    if (!ret) {
        LOG_E("[%s] [ConfigParams] The URL string '%s' must contain only alphanumeric characters, '-', '_', or '/'.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), str.c_str());
    }
    return ret;
}

static X509_CRL *LoadCertRevokeListFile(const std::string &crlFile)
{
    // load crl file
    BIO *in = BIO_new(BIO_s_file());
    if (in == nullptr) {
        return nullptr;
    }

    char *realCrlPath = new (std::nothrow) char[crlFile.size() + 1];
    if (realCrlPath == nullptr) {
        (void)BIO_free(in);
        return nullptr;
    }
    auto ret = strcpy_s(realCrlPath, crlFile.size() + 1, crlFile.c_str());
    if (ret != 0) {
        LOG_E("[%s] [ConfigParams] Failed to copy CRL file path",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str());
        (void)BIO_free(in);
        delete []realCrlPath;
        realCrlPath = nullptr;
        return nullptr;
    }
    int result = BIO_ctrl(in, BIO_C_SET_FILENAME, BIO_CLOSE | BIO_FP_READ, realCrlPath);
    if (result <= 0) {
        (void)BIO_free(in);
        delete []realCrlPath;
        realCrlPath = nullptr;
        return nullptr;
    }

    X509_CRL *crl = PEM_read_bio_X509_CRL(in, nullptr, nullptr, nullptr);
    if (crl == nullptr) {
        (void)BIO_free(in);
        delete []realCrlPath;
        realCrlPath = nullptr;
        return nullptr;
    }

    (void)BIO_free(in);
    delete []realCrlPath;
    realCrlPath = nullptr;
    return crl;
}

int32_t IsNameExpected(X509_NAME *name, const std::string &expectCN, const std::string &expectO)
{
    std::string organization;
    std::string commonName;
    for (int32_t i = 0; i < X509_NAME_entry_count(name); ++i) {
        X509_NAME_ENTRY *ne = X509_NAME_get_entry(name, i);
        X509_NAME_ENTRY *ne2 = X509_NAME_ENTRY_dup(ne);

        ASN1_OBJECT *field = X509_NAME_ENTRY_get_object(ne2);
        const char *fieldStr = OBJ_nid2sn(OBJ_obj2nid(field));
        if (fieldStr == nullptr) {
            LOG_E("[%s] [ConfigParams] Failed to convert numerical identifier to certificate field name.",
                  GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPCLIENT).c_str());
            return -1;
        }
        if (strcmp(fieldStr, "O") == 0) {
            organization = reinterpret_cast<const char *>(ASN1_STRING_get0_data(X509_NAME_ENTRY_get_data(ne)));
        } else if (strcmp(fieldStr, "CN") == 0) {
            commonName = reinterpret_cast<const char *>(ASN1_STRING_get0_data(X509_NAME_ENTRY_get_data(ne)));
        }
        X509_NAME_ENTRY_free(ne2);
    }
    if (expectO != organization) {
        LOG_E("[%s] [ConfigParams] Organization %s not match the expected %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            organization.c_str(), expectO.c_str());
        return -1;
    } else if (expectCN != commonName) {
        LOG_E("[%s] [ConfigParams] Common name %s not match the expected %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            commonName.c_str(), expectCN.c_str());
        return -1;
    }
    LOG_I("Organization and common name match the expect one: %s : %s.", expectO.c_str(), expectCN.c_str());
    return 0;
}

static bool CheckBasicConstraints(X509 *cert)
{
    const STACK_OF(X509_EXTENSION) *extlist = X509_get0_extensions(cert);
    if (extlist == nullptr) {
        return false;
    }

    int extcount = sk_X509_EXTENSION_num(extlist);
    for (int i = 0; i < extcount; i++) {
        X509_EXTENSION *ext = sk_X509_EXTENSION_value(extlist, i);
        if (OBJ_obj2nid(X509_EXTENSION_get_object(ext)) == NID_basic_constraints) {
            BASIC_CONSTRAINTS *bs = nullptr;
            bs = (BASIC_CONSTRAINTS *)X509V3_EXT_d2i(ext);
            if (bs == nullptr) {
                return false;
            }
            bool ca = (bs->ca != 0);
            BASIC_CONSTRAINTS_free(bs);
            return ca;
        }
    }
    return false;
}

static bool CheckKeyUsage(X509 *cert)
{
    const STACK_OF(X509_EXTENSION) *extlist = X509_get0_extensions(cert);
    if (extlist == nullptr) {
        return false;
    }

    int extcount = sk_X509_EXTENSION_num(extlist);
    for (int i = 0; i < extcount; i++) {
        X509_EXTENSION *ext = sk_X509_EXTENSION_value(extlist, i);
        if (OBJ_obj2nid(X509_EXTENSION_get_object(ext)) == NID_key_usage) {
            ASN1_BIT_STRING *keyUsage = nullptr;
            keyUsage = (ASN1_BIT_STRING *)X509V3_EXT_d2i(ext);
            if (keyUsage == nullptr) {
                return false;
            }
            bool keyCertSign = ASN1_BIT_STRING_get_bit(keyUsage, 5); // 证书签名标志的位位置为5
            ASN1_BIT_STRING_free(keyUsage);
            return keyCertSign;
        }
    }
    return false;
}

static int32_t CheckSubjectName(X509_STORE_CTX *x509ctx, const VerifyItems &verifyItems)
{
    auto chain = X509_STORE_CTX_get_chain(x509ctx);
    if (chain == nullptr) {
        LOG_E("[%s] [ConfigParams] Failed to retrieve certificate chain.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    if (sk_X509_num(chain) != 3) { // 3 证书链长度必须为3
        LOG_E("[%s] [ConfigParams] The certificate chain length is not 3. It is %d..",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(), sk_X509_num(chain));
        return -1;
    }
    X509 *cert = X509_STORE_CTX_get_current_cert(x509ctx);
    if (cert == nullptr) {
        LOG_E("[%s] [ConfigParams] Failed to retrieve current certificate in the chain.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    if (X509_cmp(sk_X509_value(chain, 0), cert) != 0) {
        if (!CheckBasicConstraints(cert)) {
            LOG_E("[%s] [ConfigParams] Certificate's Basic Constraints extension does not contain CA flag.",
                GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str());
            return -1;
        }
    }
    if (!CheckKeyUsage(cert)) {
        LOG_E("[%s] [ConfigParams] Certificate's Key Usage extension does not contain Key Cert Sign flag.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    X509_NAME *name = X509_get_subject_name(cert);
    if (name == nullptr) {
        LOG_E("[%s] [ConfigParams] Get subject name failed.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    if (IsNameExpected(name, verifyItems.commonName, verifyItems.organization) != 0) {
        return -1;
    }
    name = X509_get_issuer_name(cert);
    if (name == nullptr) {
        LOG_E("[%s] [ConfigParams] Get issuer subject name of client certificate failed.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    if (IsNameExpected(name, verifyItems.interCACommonName, verifyItems.organization) != 0) {
        return -1;
    }
    return 0;
}

int CertVerify(X509_STORE_CTX *x509ctx, std::string logCode)
{
    auto verifyResult = X509_verify_cert(x509ctx);
    if (verifyResult != 1U) {
        LOG_E("[%s] [ConfigParams] Verify failed in callback error: %s", logCode.c_str(),
              X509_verify_cert_error_string(X509_STORE_CTX_get_error(x509ctx)));
        return -1;
    }
    X509 *peerCert = X509_STORE_CTX_get0_cert(x509ctx); // 获取对端证书
    if (!peerCert) {
        LOG_E("[%s] [ConfigParams] Certificate not found in SSL context.", logCode.c_str());
        return -1;
    }
    EVP_PKEY *pkey = X509_get_pubkey(peerCert);
    if (!pkey) {
        LOG_E("[%s] [ConfigParams] Certificate has no public key.", logCode.c_str());
        return -1;
    }
    int keyType = EVP_PKEY_id(pkey);
    int keyBits = EVP_PKEY_get_bits(pkey);
    EVP_PKEY_free(pkey);
    if (keyType == EVP_PKEY_RSA && keyBits < 2048) { // 2048: Minimun key content bit length
        LOG_E("[%s] [ConfigParams] RSA key length is too short, key length is less than 2048.",
            logCode.c_str());
        return -1;
    } else if (keyType == EVP_PKEY_EC && keyBits < 256) { // 256: Minimun key content bit length
        LOG_E("[%s] [ConfigParams] ECDSA key length is too short, key length is less than 256.",
            logCode.c_str());
        return -1;
    }
    return 0;
}

int VerifyCallback(X509_STORE_CTX *x509ctx, void *arg)
{
    if (x509ctx == nullptr || arg == nullptr) {
        return 0;
    }
    VerifyItems *verifyPtr = static_cast<VerifyItems *>(arg);
    std::string crlFile = verifyPtr->crlFile;
    std::string logCode = GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPCLIENT);
    if (strlen(crlFile.c_str()) != 0) {
        bool isFileExist = false;
        uint32_t mode = verifyPtr->checkFiles ? 0400 : 0777;    // crl文件的权限要求是0400, 不校验是0777
        if (!PathCheck(crlFile, isFileExist, mode, verifyPtr->checkFiles)) {   // crl文件的权限要求是0400
            return -1;
        }
        if (!isFileExist) {
            LOG_E("[%s] [ConfigParams] File %s not exists.", logCode.c_str(), crlFile.c_str());
            return -1;
        }
        X509_CRL *crl = LoadCertRevokeListFile(crlFile);
        if (crl == nullptr) {
            LOG_E("[%s] [ConfigParams] Failed to load Certificate Revocation List (CRL).", logCode.c_str());
            return -1;
        }
        if (X509_cmp_current_time(X509_CRL_get0_nextUpdate(crl)) <= 0) {
            LOG_W("Certificate Revocation List (CRL) file '%s' has expired. "
                "Current time is after the next update time.", crlFile.c_str());
        }
        X509_STORE *x509Store = X509_STORE_CTX_get0_store(x509ctx);
        X509_STORE_CTX_set_flags(x509ctx, static_cast<unsigned long>(X509_V_FLAG_CRL_CHECK));
        auto result = X509_STORE_add_crl(x509Store, crl);
        if (result != 1U) {
            LOG_E("[%s] [ConfigParams] Failed to add CRL to the store, result is %d.", logCode.c_str(), result);
            X509_CRL_free(crl);
            return -1;
        }
        X509_CRL_free(crl);
    }
    if (CertVerify(x509ctx, logCode) != 0) {
        return -1;
    }
    if (verifyPtr->checkSubject) {
        if (CheckSubjectName(x509ctx, *verifyPtr) != 0) {
            LOG_E("[%s] [ConfigParams] Subject name of certificate is wrong.", logCode.c_str());
            return -1;
        }
        LOG_M("[Verify] Success to verify subject name.");
    }
    return 1;
}

void EraseDecryptedData(std::pair<char *, int32_t> &result)
{
    if (result.first != nullptr) {
        for (int32_t i = 0; i < result.second; i++) {
            result.first[i] = '\0';
        }
        delete[] result.first;
        result.first = nullptr;
    }
    result.second = 0;
}

bool DecryptPassword(int domainId, std::pair<char *, int32_t> &password, const TlsItems &tlsConfig)
{
    std::string pwdPath = tlsConfig.tlsPasswd;
    bool isFileExist = false;
    uint32_t mode = tlsConfig.checkFiles ? 0400 : 0640; // 口令文件的权限要求是0400, 不校验是0640
    std::string logCode = GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT);
    if (!PathCheck(pwdPath, isFileExist, mode, tlsConfig.checkFiles)) {   // 口令文件的权限要求是0400
        LOG_E("[%s] [ConfigParams] Password file path check failed.", logCode.c_str());
        return false;
    } else if (!isFileExist) {
        LOG_E("[%s] [ConfigParams] Password file is not exist.", logCode.c_str());
        return false;
    }
    std::ifstream in(pwdPath);
    if (!in.is_open()) {
        LOG_E("[%s] [ConfigParams] Failed to open the file.", logCode.c_str());
        return false;
    }
    in.seekg(0, std::ios::end);
    std::streamsize fileSize = in.tellg();
    in.seekg(0, std::ios::beg);
    int32_t dataLength = static_cast<int32_t>(fileSize) + 1;
    auto buffer = new (std::nothrow) char[dataLength]();
    if (buffer == nullptr) {
        LOG_E("[%s] [ConfigParams] Allocate memory for buffer failed.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, CommonFeature::HTTPCLIENT).c_str());
        return false;
    }

    in.read(buffer, dataLength);
    if (buffer[strlen(buffer) - 1] == '\n') {
        buffer[strlen(buffer) - 1] = '\0';
        dataLength--;
    } else {
        buffer[dataLength - 1] = '\0';
    }
    LOG_I("Read password in file %s success.", pwdPath.c_str());
    password = std::make_pair(buffer, dataLength);
    return true;
}

int32_t StrToInt(const std::string &numberStr, int64_t &number)
{
    try {
        // 转换为整数并检查范围
        number = std::stoi(numberStr);
    } catch (...) {
        LOG_E("[%s] [ConfigParams] Number in string %s is not valid.",
            GetErrorCode(ErrorType::EXCEPTION, CommonFeature::HTTPCLIENT).c_str(),
            numberStr.c_str());
        return -1;
    }
    return 0;
}

std::string GetCurrentDateTimeString()
{
    auto now = std::chrono::system_clock::now();

    std::time_t nowC = std::chrono::system_clock::to_time_t(now);

    struct std::tm nowTm;
    localtime_r(&nowC, &nowTm);

    std::stringstream ss;
    ss << std::put_time(&nowTm, "%Y-%m-%d %H:%M:%S");

    // 获取微秒部分
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()) % 1000;
    ss << "." << std::setfill('0') << std::setw(3) << ms.count(); // 3 微秒精度
    return ss.str();
}
}
}