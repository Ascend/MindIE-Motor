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
#include "StatusHandler.h"
#include <fstream>
#include "Logger.h"
#include "ConfigParams.h"
#include "Util.h"

namespace MINDIE::MS {

int32_t StatusHandler::GetStatusFromPath(nlohmann::json &statusJson)
{
    std::string jsonString;
    if (FileToBuffer(this->mStatusFile, jsonString, 0640) != 0) {   // status文件的权限要求是0640
        LOG_E("[%s] [Deployer] Get status from path failed. Cannot convert file %s to buffer.",
            GetErrorCode(ErrorType::NOT_FOUND, DeployerFeature::STATUS_HANDLER).c_str(),
            mStatusFile.c_str());
        return -1;
    }
    if (!CheckJsonStringSize(jsonString) || !nlohmann::json::accept(jsonString)) {
        LOG_E("[%s] [Deployer] Get status from path failed. File %s is not valid JSON format.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::STATUS_HANDLER).c_str(),
            mStatusFile.c_str());
        return -1;
    }
    statusJson = nlohmann::json::parse(jsonString, CheckJsonDepthCallBack);
    if (!statusJson.contains("server_list") || !statusJson["server_list"].is_array()) {
        LOG_E("[%s] [Deployer] Get status from path failed. Status JSON does not contain 'server_list'.",
            GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::STATUS_HANDLER).c_str());
        return -1;
    }
    return 0;
}

int32_t StatusHandler::RemoveServerStatus(std::string serverName)
{
    std::lock_guard<std::mutex> guard(this->mMutex);
    nlohmann::json statusJson;
    int32_t ret = GetStatusFromPath(statusJson);
    if (ret != 0) {
        return -1;
    }
    nlohmann::json newServerList = nlohmann::json::array();
    for (auto &status : statusJson["server_list"]) {
        if (!status.is_object()) {
            LOG_E("[%s] [Deployer] Failed to remove status from file. An element in 'server_list' is not a valid "
                "JSON object.", GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::STATUS_HANDLER).c_str());
            return -1;
        }
        if (!IsJsonStringValid(status, "server_name")) {
            return -1;
        }
        if (status["server_name"] != serverName) {
            newServerList.push_back(status);
        }
    }

    statusJson["server_list"] = newServerList;
    return DumpStringToFile(this->mStatusFile, statusJson.dump(4)); // 4 json格式行缩进为4
}

int32_t StatusHandler::SaveStatusToFile(ServerSaveStatus status)
{
    std::lock_guard<std::mutex> guard(this->mMutex);
    nlohmann::json statusJson;
    int32_t ret = GetStatusFromPath(statusJson);
    if (ret != 0) {
        return -1;
    }
    for (auto &oneStatus : statusJson["server_list"]) {
        if (!oneStatus.is_object()) {
            LOG_E("[%s] [Deployer] Failed to save status to file. Element of 'server_list' is not a JSON object.",
                GetErrorCode(ErrorType::INVALID_INPUT, DeployerFeature::STATUS_HANDLER).c_str());
            return -1;
        }
        if (!IsJsonStringValid(oneStatus, "server_name")) {
            return -1;
        }
    }

    nlohmann::json serverStatus = {
        {"replicas", status.replicas},
        {"namespace", status.nameSpace},
        {"server_name", status.serverName},
        {"server_type", status.serverType},
        {"use_service", status.useService}
    };

    statusJson["server_list"].push_back(serverStatus);
    return DumpStringToFile(this->mStatusFile, statusJson.dump(4)); // 4 json格式行缩进为4
}

}