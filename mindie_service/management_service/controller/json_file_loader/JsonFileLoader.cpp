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
#include "JsonFileLoader.h"
#include "Logger.h"
#include "Util.h"

namespace MINDIE::MS {
nlohmann::json FileToJsonObj(const std::string& filePath, uint32_t mode, bool checkOwner)
{
    std::string confStr;
    auto ret = FileToBuffer(filePath, confStr, mode, checkOwner);
    if (ret != 0) {
        LOG_E("[%s] [JsonFileLoader] Read %s failed in JSON file loader.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::JSON_FILE_LOADER).c_str(),
            filePath.c_str());
        return {};
    }
    if (!nlohmann::json::accept(confStr)) {
        LOG_E("[%s] [JsonFileLoader] Invalid JSON input in JSON file loader.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::JSON_FILE_LOADER).c_str());
        return {};
    }
    return nlohmann::json::parse(confStr, CheckJsonDepthCallBack);
}
}