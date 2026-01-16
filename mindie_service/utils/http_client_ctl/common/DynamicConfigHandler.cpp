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
#include <functional>
#include <thread>
#include <vector>
#include <iostream>
#include <mutex>
#include <fstream>
#include <map>
#include <nlohmann/json.hpp>
#include "Logger.h"
#include "Util.h"
#include "DynamicConfigHandler.h"

namespace MINDIE {
namespace MS {

DynamicConfigHandler::~DynamicConfigHandler()
{
    Stop();
}

DynamicConfigHandler& DynamicConfigHandler::GetInstance()
{
    static DynamicConfigHandler instance;
    return instance;
}

void DynamicConfigHandler::Stop()
{
    GetInstance().isRunning.store(false);
}

void DynamicConfigHandler::Start(std::string inputComponent)
{
    GetInstance().component = inputComponent;
    std::thread t([]() {
        int intervalMs = 5000;
        while (GetInstance().isRunning.load()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(intervalMs));
            std::lock_guard<std::mutex> locker(GetInstance().vectorMutex);
            for (auto &configTuple: GetInstance().callBackFunctions) {
                if (GetInstance().IsTriggered(configTuple.first)) {
                    configTuple.second();
                }
            }
        }
    });
    t.detach();
}

std::vector<std::string> DynamicConfigHandler::SplitString(const std::string& s, const char delimiter)
{
    std::vector<std::string> ans;
    if (s.empty()) {
        return ans;
    }

    for (long unsigned int i = 0; i < s.length();) {
        long unsigned int pos = s.find(delimiter, i);
        if (pos != std::string::npos) {
            if (pos == i) { // 跳过多个连续的分隔符
                i = pos + 1;
                continue;
            } else {
                std::string strTemp = s.substr(i, pos - i);
                ans.push_back(strTemp);
                i = pos + 1;
            }
        } else {
            std::string strTemp = s.substr(i, s.length() - i);
            ans.push_back(strTemp);
            break;
        }
    }
    return ans;
}

std::string DynamicConfigHandler::GetConfigPath() const
{
    std::map<std::string, std::string> componentPathMap = {
        {"Controller", "/conf/ms_controller.json"},
        {"Coordinator", "/conf/ms_coordinator.json"}
    };
    try {
        return componentPathMap.at(component);
    } catch (const std::out_of_range& e) {
        std::string defaultConfigPath = "/conf/config.json";
        LOG_W("Component %s not supported, use default config path %s", component.c_str(),
            defaultConfigPath.c_str());
        return defaultConfigPath;
    }
}

bool DynamicConfigHandler::IsTriggered(const std::string pathExpression) const
{
    try {
        const char *miesInstallPath = std::getenv("MIES_INSTALL_PATH");
        if (miesInstallPath == nullptr) {
            return false;
        }
        std::string configPath = miesInstallPath + GetConfigPath();
        std::string jsonString;
        uint32_t mode = 0640;
        auto ret = FileToBuffer(configPath, jsonString, mode, true);
        if (ret != 0) {
            LOG_E("Failed to open config path.");
            return false;
        }
        std::vector<std::string> pathVec = SplitString(pathExpression);
        nlohmann::json configJson = nlohmann::json::parse(jsonString, CheckJsonDepthCallBack);
        for (std::string& path : pathVec) {
            configJson = configJson.at(path);
        }
        return static_cast<bool>(configJson);
    } catch (nlohmann::json::parse_error& e) {
        LOG_E("Failed to parse config json.");
        return false;
    } catch (std::exception& e) {
        return false;
    }
    return true;
}

}
}