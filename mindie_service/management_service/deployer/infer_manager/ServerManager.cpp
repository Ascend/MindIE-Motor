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
#include "ServerManager.h"
#include <cstdlib>
#include <sys/types.h>
#include <iomanip>
#include <set>
#include <fstream>
#include <boost/beast/http.hpp>
#include "Logger.h"
#include "StatusHandler.h"
#include "ConfigParams.h"
#include "HttpServer.h"
#include "Util.h"

namespace Http = Beast::http;
namespace MINDIE::MS {

namespace {
uint32_t g_maxServerNum = 1; // 1 当前限制最大支持管理1个服务
};

std::pair<ErrorCode, Response> ServerManager::PostServersHandler(const Http::request<Http::string_body> &req)
{
    std::string ip = "unknown";
    auto ip_header = req.find("X-Real-IP");
    if (ip_header != req.end()) {
        ip = std::string(ip_header->value());
    }
    LOG_M("[Start] IP:%s Handle server request", ip.c_str());
    if (req.target() == "/v1/servers") {
        auto result = this->LoadServer(req.body());
        LOG_M("[End] IP:%s Load server result: %d", ip.c_str(), static_cast<int>(result.first));
        return result;
    } else if (req.target().starts_with("/v1/servers/")) {
        auto result = this->UpdateServer(req.body());
        LOG_M("[End] IP:%s Update server result: %d", ip.c_str(), static_cast<int>(result.first));
        return result;
    }
    LOG_M("[Exit] IP:%s Invalid target, code %d", ip.c_str(), static_cast<int>(ErrorCode::NOT_FOUND));
    Response resp;
    return std::make_pair(ErrorCode::NOT_FOUND, resp);
}

std::pair<ErrorCode, Response> ServerManager::InferServerDeploy(const std::string &loadConfigJson,
                                                                const nlohmann::json &jsonObj, std::string &logCode)
{
    Response response;
    auto inferServer = InferServerFactory::GetInstance()->CreateInferServer(jsonObj["server_type"]);
    if (inferServer == nullptr || inferServer->Init(mClientParams, mStatusFile).first != 0) {
        LOG_E("[%s] [Deployer] Failed to create inference server!", logCode.c_str());
        response.message = "Failed to create infer server!";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, response);
    }
    auto ret = inferServer->Deploy(loadConfigJson);
    response.message = ret.second;
    if (ret.first == 0) {
        this->mServerHandlers[jsonObj["server_name"]] = std::move(inferServer);
        return std::make_pair(ErrorCode::OK, response);
    }
    return std::make_pair(ErrorCode::INTERNAL_ERROR, response);
}

std::pair<ErrorCode, Response> ServerManager::LoadServer(const std::string &loadConfigJson)
{
    Response response;
    std::lock_guard<std::mutex> guard(this->mMutex);
    std::string logCode = GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER_MANAGER);
    if (this->mServerHandlers.size() == g_maxServerNum) {
        LOG_E("[%s] [Deployer] Server number must be in range of [1, %d], but got %d.",
            logCode.c_str(), g_maxServerNum, this->mServerHandlers.size());
        response.message = "server number cannot over 1.";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, response);
    }
    if (!CheckJsonStringSize(loadConfigJson) || !nlohmann::json::accept(loadConfigJson)) {
        LOG_E("[%s] [Deployer] Load config is not a valid JSON!", logCode.c_str());
        response.message = "load config is not a valid json!";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, response);
    }

    auto jsonObj = nlohmann::json::parse(loadConfigJson, CheckJsonDepthCallBack);
    if (!IsJsonStringValid(jsonObj, "server_name")) {
        response.message = "load config not contain server_name!";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, response);
    }
    if (!IsValidString(jsonObj["server_name"])) {
        response.message = "server_name must contain only alnum, '-' or  '_'";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, response);
    }
    if (mServerHandlers.find(jsonObj["server_name"]) != mServerHandlers.end()) {
        LOG_E("[%s] [Deployer] Server with same name is already loaded.", logCode.c_str());
        response.message = "server with same name is already loaded";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, response);
    }

    if (!IsJsonStringValid(jsonObj, "server_type")) {
        response.message = "load config not contain server_type!";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, response);
    }

    if (jsonObj["server_type"] != "mindie_cross_node" && jsonObj["server_type"] != "mindie_single_node") {
        LOG_E("[%s] Load config 'deploy_type' is not 'mindie_cross_node' or 'mindie_single_node'!", logCode.c_str());
        response.message = "load config deploy_type is not mindie_cross_node or mindie_single_node!";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, response);
    }

    return InferServerDeploy(loadConfigJson, jsonObj, logCode);
}

std::pair<ErrorCode, Response> ServerManager::UnloadServer(const Http::request<Http::string_body> &req)
{
    std::string ip = "unknown";
    auto ip_header = req.find("X-Real-IP");
    if (ip_header != req.end()) {
        ip = std::string(ip_header->value());
    }
    std::string name = std::string(req.target()).substr(std::string("/v1/servers/").size());
    LOG_M("[Start] IP:%s Unload server, name %s", ip.c_str(), name.c_str());
    Response resp;
    std::lock_guard<std::mutex> guard(this->mMutex);
    if (mServerHandlers.find(name) == mServerHandlers.end()) {
        LOG_M("[Exit] IP:%s Unload server failed, name %s, reason not_exist", ip.c_str(), name.c_str());
        LOG_E("[%s] [Deployer] The server with the specified name does not exist and cannot be unloaded. "
            "Provided server name: %s.", GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::SERVER_MANAGER).c_str(),
            name.c_str());
        resp.message = "server to be unload is not existing";
        return std::make_pair(ErrorCode::NOT_FOUND, resp);
    }
    auto ret = this->mServerHandlers[name]->Unload();
    resp.message = ret.second;
    if (ret.first != 0) {
        LOG_M("[Exit] IP:%s Unload server failed, name %s, reason unload_error", ip.c_str(), name.c_str());
        LOG_E("[%s] [Deployer] Failed to unload server.",
              GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::SERVER_MANAGER).c_str());
        return std::make_pair(ErrorCode::INTERNAL_ERROR, resp);
    }

    this->mServerHandlers.erase(name);
    LOG_M("[Exit] IP:%s Unload server success, name %s", ip.c_str(), name.c_str());
    return std::make_pair(ErrorCode::OK, resp);
}

std::pair<ErrorCode, Response> ServerManager::GetDeployStatus(const Http::request<Http::string_body>
        &req)
{
    Response resp;
    std::string name = std::string(req.target()).substr(std::string("/v1/servers/").size());
    std::lock_guard<std::mutex> guard(this->mMutex);
    if (mServerHandlers.find(name) == mServerHandlers.end()) {
        LOG_E("[%s] [Deployer] Server with name %s is not found.",
            GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::SERVER_MANAGER).c_str(),
            name.c_str());
        resp.message = "GetDeployStatus: resource not found";
        return std::make_pair(ErrorCode::NOT_FOUND, resp);
    }
    auto ret = this->mServerHandlers[name]->GetDeployStatus();
    if (ret.first != 0) {
        resp.message = ret.second;
        return std::make_pair(ErrorCode::INTERNAL_ERROR, resp);
    }
    resp.message = "success";
    resp.data = ret.second;
    return std::make_pair(ErrorCode::OK, resp);
}

static bool ServerInfoCheck(const nlohmann::json &status, std::string &warnCode)
{
    if (!status.is_object()) {
        LOG_W("[%s] [Deployer] Restoring from file. Server status is not a object.", warnCode.c_str());
        return false;
    }
    if (!IsJsonStringValid(status, "server_name") || !IsValidString(status["server_name"])) {
        LOG_W("[%s] [Deployer] Restore file server does not contain a valid 'server_name' or the 'server_name' "
              "contains invalid characters. It must only contain alphanumeric characters, '-', or '_'.",
              warnCode.c_str());
        return false;
    }
    if (!IsJsonStringValid(status, "namespace") || !IsValidString(status["namespace"])) {
        LOG_W("[%s] [Deployer] Restore file server does not contain a valid 'namespace' or the 'namespace' contains"
              " invalid characters. It must only contain alphanumeric characters, '-', or '_'.",
              warnCode.c_str());
        return false;
    }

    if (!IsJsonStringValid(status, "server_type")) {
        LOG_W("[%s] [Deployer] The restore file server is missing a valid 'server_type' field.", warnCode.c_str());
        return false;
    }
    if (!status.contains("replicas") || !status["replicas"].is_number_integer()) {
        LOG_W("[%s] [Deployer] The restore file server is missing a valid 'replicas' field.", warnCode.c_str());
        return false;
    }
    if (!status.contains("use_service") || !status["use_service"].is_boolean()) {
        LOG_W("[%s] [Deployer] The restore file server is missing a valid 'use_service' field.", warnCode.c_str());
        return false;
    }

    return true;
}
int32_t ServerManager::RestoreOneInferServer(const nlohmann::json &status)
{
    std::string warnCode = GetWarnCode(ErrorType::WARNING, DeployerFeature::SERVER_MANAGER);
    if (!ServerInfoCheck(status, warnCode)) {
        return -1;
    }

    std::string serverName = status["server_name"];
    if (mServerHandlers.find(status["server_name"]) != mServerHandlers.end()) {
        LOG_W("[%s] [Deployer] Server with same name %s is already loaded.", warnCode.c_str(), serverName.c_str());
        return -1;
    }

    auto inferServer = InferServerFactory::GetInstance()->CreateInferServer(status["server_type"]);
    if (inferServer == nullptr) {
        LOG_W("[%s] [Deployer] Failed to create infer server!", warnCode.c_str());
        return -1;
    }

    if (inferServer->Init(mClientParams, this->mStatusFile).first != 0) {
        LOG_W("[%s] [Deployer] Failed to initialize infer server %s.", warnCode.c_str(),
              status["server_name"].dump().c_str());
        return -1;
    }
    if (inferServer->FromJson(status) != 0) {
        LOG_W("[%s] [Deployer] Failed to restore infer server %s!", warnCode.c_str(),
              status["server_name"].dump().c_str());
        return -1;
    }
    this->mServerHandlers[status["server_name"]] = std::move(inferServer);
    return 0;
}

int32_t InitStatusFile(const std::string &statusFile)
{
    LOG_I("Restoring from file. File not exist, now initialize it!");
    nlohmann::json obj;
    obj["server_list"] = nlohmann::json::array();
    return DumpStringToFile(statusFile, obj.dump(4)); // 4 json格式行缩进为4
}

static int32_t CheckStatusFileHasList(const std::string &statusFile, nlohmann::json &statusJson)
{
    std::string jsonString;
    if (FileToBuffer(statusFile, jsonString, 0640) != 0) {    // status文件的权限要求是0640
        LOG_E("[%s] [Deployer] Restoring from file. Cannot convert file %s to buffer.",
            GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::SERVER_MANAGER).c_str(),
            statusFile.c_str());
        return -1;
    }
    if (!CheckJsonStringSize(jsonString) || !nlohmann::json::accept(jsonString)) {
        LOG_E("[%s] [Deployer] Restoring from file. File %s is not valid JSON format.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER_MANAGER).c_str(),
            statusFile.c_str());
        return -1;
    }
    statusJson = nlohmann::json::parse(jsonString, CheckJsonDepthCallBack);
    if (!statusJson.contains("server_list") || !statusJson["server_list"].is_array()) {
        LOG_E("[%s] [Deployer] Restoring from file. Status JSON does not contain 'server_list'.",
              GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER_MANAGER).c_str());
        return -1;
    }
    if (statusJson["server_list"].size() > g_maxServerNum) {
        LOG_E("[%s] [Deployer] Restoring from file. 'server_list' number is out of range [0, %d].",
            GetErrorCode(ErrorType::RESOURCE_LIMIT, DeployerFeature::SERVER_MANAGER).c_str(),
            g_maxServerNum);
        return -1;
    }
    return 0;
}

int32_t ServerManager::FromStatusFile(const std::string &statusPath)
{
    std::string canonicalPath = statusPath;
    bool isFileExist = false;
    if (!PathCheck(canonicalPath, isFileExist, 0640)) {   // status文件的权限要求是0640
        return -1;
    }
    if (!isFileExist && (InitStatusFile(canonicalPath) != 0)) {
        return -1;
    }
    this->mStatusFile = canonicalPath;
    StatusHandler::GetInstance()->SetStatusFile(canonicalPath);
    nlohmann::json statusJson;
    if (CheckStatusFileHasList(this->mStatusFile, statusJson) != 0) {
        return -1;
    }
    std::vector<bool> isRestoreSuccessful(statusJson["server_list"].size(), false);
    for (uint32_t i = 0; i < statusJson["server_list"].size(); i++) {
        if (RestoreOneInferServer(statusJson["server_list"][i]) == 0) {
            isRestoreSuccessful[i] = true;
        }
    }
    nlohmann::json newStatusJson;
    newStatusJson["server_list"] = nlohmann::json::array();
    for (uint32_t i = 0; i < statusJson["server_list"].size(); i++) {
        if (isRestoreSuccessful[i]) {
            newStatusJson["server_list"].push_back(statusJson["server_list"][i]);
        }
    }
    return DumpStringToFile(this->mStatusFile, newStatusJson.dump(4)); // 4 json格式行缩进为4
}

std::pair<ErrorCode, Response> ServerManager::UpdateServer(const std::string &updateConfigJson)
{
    Response resp;
    if (!CheckJsonStringSize(updateConfigJson) || !nlohmann::json::accept(updateConfigJson)) {
        LOG_E("[%s] [Deployer] Failed to load config. The provided configuration is not valid JSON.",
              GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER_MANAGER).c_str());
        resp.message = "load config is not a valid json!";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, resp);
    }

    auto jsonObj = nlohmann::json::parse(updateConfigJson, CheckJsonDepthCallBack);
    if (!jsonObj.contains("server_name") || !jsonObj["server_name"].is_string()) {
        LOG_E("[%s] [Deployer] The update configuration does not include a valid 'server_name' field.",
              GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::SERVER_MANAGER).c_str());
        resp.message = "update json does not include server name.";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, resp);
    }
    std::string name = jsonObj["server_name"];
    if (mServerHandlers.find(name) == mServerHandlers.end()) {
        LOG_E("[%s] [Deployer] The server to be updated does not exist in the current configuration.",
              GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::SERVER_MANAGER).c_str());
        resp.message = "server to be updated is not existing";
        return std::make_pair(ErrorCode::INVALID_PARAMETER, resp);
    }
    auto ret = this->mServerHandlers[name]->Update(updateConfigJson);
    if (ret.first != 0) {
        LOG_E("[%s] [Deployer] Failed to update server.",
              GetErrorCode(ErrorType::CALL_ERROR, DeployerFeature::SERVER_MANAGER).c_str());
        resp.message = ret.second;
        return std::make_pair(ErrorCode::INTERNAL_ERROR, resp);
    }
    resp.message = ret.second;
    return std::make_pair(ErrorCode::OK, resp);
}
}