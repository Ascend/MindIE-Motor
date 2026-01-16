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
#include "LogLevelDynamicHandler.h"
#include <thread>
#include <fstream>
#include <regex>
#include <map>
#include "Util.h"

namespace MINDIE {
namespace MS {

std::atomic<LogLevelDynamicHandler *> LogLevelDynamicHandler::instance_{nullptr};
std::mutex LogLevelDynamicHandler::mtx_;

LogLevelDynamicHandler::LogLevelDynamicHandler()
{
}

LogLevelDynamicHandler::~LogLevelDynamicHandler()
{
    Stop();
}

LogLevelDynamicHandler &LogLevelDynamicHandler::GetInstance()
{
    if (instance_ == nullptr) {
        std::lock_guard<std::mutex> lock(mtx_);
        if (instance_ == nullptr) {
            instance_ = new LogLevelDynamicHandler();
        }
    }
    return *instance_;
}

std::string LogLevelDynamicHandler::GetConfigPath(std::string component) const
{
    std::map<std::string, std::string> componentPathMap = {
        {"Controller", "/conf/ms_controller.json"},
        {"Coordinator", "/conf/ms_coordinator.json"}
    };
    try {
        return componentPathMap.at(component);
    } catch (const std::out_of_range& e) {
        std::string defaultConfigPath = "/conf/config.json";
        LOG_W("Component %s not supported, use default config path %s", component.c_str(), defaultConfigPath.c_str());
        return defaultConfigPath;
    }
}

void LogLevelDynamicHandler::Init(size_t intervalMs, std::string component, bool needWriteToFile)
{
    LogLevelDynamicHandler &instance = LogLevelDynamicHandler::GetInstance();
    if (instance.isRunning_) {
        return;
    }
    std::string homePath;
    if (instance.GetHomePath(homePath)) {
        instance.jsonPath_ = homePath + instance.GetConfigPath(component);
    } else {
        LOG_E("Failed to get home path.");
        return;
    }
    const char *logLevelEnv = std::getenv("MINDIE_LOG_LEVEL");
    instance.defaultLogLevel_ = "INFO";
    if (logLevelEnv != nullptr && Logger::GetLogLevel(logLevelEnv) != LogLevel::MINDIE_LOG_UNKNOWN) {
        instance.defaultLogLevel_ = logLevelEnv;
    }
    LOG_M("Dynamic Log level start with log level: %s", instance.defaultLogLevel_.c_str());
    instance.intervalMs_ = intervalMs;
    instance.needWriteToFile_ = needWriteToFile;
    instance.isRunning_ = true;
    std::thread t([&instance]() {
        while (instance.isRunning_) {
            try {
                // 检查动态日志参数是否需要刷新
                instance.GetAndSetLogConfig();
            } catch (const std::exception &e) {
                LOG_E("LogLevelDynamicHandler callback exception: %s", e.what());
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(instance.intervalMs_));
        }
    });
    t.detach();
}

void LogLevelDynamicHandler::Stop()
{
    LogLevelDynamicHandler &instance = LogLevelDynamicHandler::GetInstance();
    if (!instance.isRunning_) {
        return;
    }
    instance.isRunning_ = false;
}

bool LogLevelDynamicHandler::CheckLogConfig(const std::string &jsonPath, nlohmann::json &inputJsonData) const
{
    std::string homePath;
    if (!GetHomePath(homePath)) {
        LOG_E("Error: Get home path failed.");
        return false;
    }
    return ReadLogConfig(jsonPath, inputJsonData);
}

bool LogLevelDynamicHandler::ReadLogConfig(const std::string &jsonPath, nlohmann::json &inputJsonData) const
{
    try {
        std::string checkPath = jsonPath;
        bool isFileExist = true;
        uint32_t mode = 0640;
        if (!PathCheck(checkPath, isFileExist, mode, true, false)) {
            LOG_E("Failed to open dynamic log config path");
            return false;
        }
        std::ifstream file(checkPath);
        if (!file.is_open()) {
            LOG_E("Error: Open json file failed");
            return false;
        }
        nlohmann::json jsonData;
        file >> jsonData;
        file.close();
        try {
            inputJsonData = jsonData.at("LogConfig");
        } catch (const nlohmann::json::exception &e) {
            LOG_W("%s, please check your json config", e.what());
            return false;
        }
    } catch (const std::exception &e) {
        LOG_E("Json file is invalid. Please check json format!");
        return false;
    }

    return true;
}

bool LogLevelDynamicHandler::GetHomePath(std::string &homePath) const
{
    const char *miesInstallPath = std::getenv("MIES_INSTALL_PATH");
    if (miesInstallPath == nullptr) {
        return false;
    }
    homePath = miesInstallPath;
    return true;
}

void LogLevelDynamicHandler::GetAndSetLogConfig()
{
    nlohmann::json logConfigJson;
    if (!CheckLogConfig(jsonPath_, logConfigJson)) {
        InsertLogConfigToFile();
        return;
    }
    if (!logConfigJson.contains("dynamicLogLevel")) {
        return;
    }
    // 无效参数需要修改为上一次有效值
    if (!CheckAndFlushInvalidParam(logConfigJson)) {
        return;
    }
    const auto& dynamicLogLevel = logConfigJson["dynamicLogLevel"];
    if (dynamicLogLevel.is_string() && dynamicLogLevel.get<std::string>().empty()) {
        // 动态日志级别修改为空，需要恢复参数为默认值
        if (!lastLogLevel_.empty()) {
            LOG_M("Dynamic log level set to empty, need to clear config params.");
            ClearLogConfigParam();
        }
        return;
    }
    bool sameDynamicLogLevel = true;
    bool sameValidHours = true;
    if (CheckDynamicLogLevelChanged(dynamicLogLevel)) {
        sameDynamicLogLevel = false;
    }
    const auto& dynamicLogLevelValidHours = logConfigJson["dynamicLogLevelValidHours"];
    if (CheckValidHoursChanged(dynamicLogLevelValidHours)) {
        sameValidHours = false;
    }
    const auto& dynamicLogLevelValidTime = logConfigJson["dynamicLogLevelValidTime"];
    currentValidTime_ = dynamicLogLevelValidTime.get<std::string>();
    UpdateDynamicLogParam(sameDynamicLogLevel, sameValidHours);
    hasSetDynamicLog_ = true;
}

bool LogLevelDynamicHandler::IsGreaterThanNow(const std::string& other)
{
    std::tm targetTm = {};
    targetTm.tm_isdst = -1;
    strptime(other.c_str(), "%Y-%m-%d %H:%M:%S", &targetTm);
    std::time_t targetTime = mktime(&targetTm);
    return (targetTime > std::time(nullptr));
}

bool IsValidTimeFormat(const std::string& timeStr)
{
    if (timeStr.empty()) {
        return true;
    }
    std::tm tm = {};
    std::istringstream iss(timeStr);
    iss >> std::get_time(&tm, "%Y-%m-%d %H:%M:%S");
    return !iss.fail();
}

std::string TransformToUpper(const std::string& str)
{
    std::string s;
    for (char c: str) {
        s += toupper(c);
    }
    return s;
}

bool LogLevelDynamicHandler::CheckAndFlushInvalidParam(const nlohmann::json &logConfigJson)
{
    bool ret = true;
    if (!logConfigJson["dynamicLogLevel"].is_string()) {
        LOG_E("The config of dynamicLogLevel is not supported, flush to: %s", lastLogLevel_.c_str());
        SetConfigFile("dynamicLogLevel", lastLogLevel_, false);
        ret = false;
    } else {
        std::string dynamicLogLevel = logConfigJson["dynamicLogLevel"].get<std::string>();
        if (dynamicLogLevel != "" &&
            Logger::GetLogLevel(TransformToUpper(dynamicLogLevel)) == LogLevel::MINDIE_LOG_UNKNOWN) {
            LOG_E("The config of dynamicLogLevel is not supported, flush to: %s", lastLogLevel_.c_str());
            SetConfigFile("dynamicLogLevel", lastLogLevel_, false);
            ret = false;
        }
    }
    if (!logConfigJson["dynamicLogLevelValidHours"].is_number_integer()) {
        LOG_E("The config of dynamicLogLevelValidHours is not supported, flush to: %s",
            std::to_string(lastValidHours_).c_str());
        SetConfigFile("dynamicLogLevelValidHours", std::to_string(lastValidHours_), true);
        ret = false;
    } else {
        int hours = logConfigJson["dynamicLogLevelValidHours"].get<int>();
        int maxHours = 7 * 24;
        if (hours < 1 || hours > maxHours) {
            LOG_E("The config of dynamicLogLevelValidHours is not supported, flush to: %s",
                std::to_string(lastValidHours_).c_str());
            SetConfigFile("dynamicLogLevelValidHours", std::to_string(lastValidHours_), true);
            ret = false;
        }
    }
    if (!logConfigJson["dynamicLogLevelValidTime"].is_string() ||
        !IsValidTimeFormat(logConfigJson["dynamicLogLevelValidTime"].get<std::string>())) {
        LOG_E("The config of dynamicLogLevelValidTime is not supported, flush to: %s", lastValidTime_.c_str());
        SetConfigFile("dynamicLogLevelValidTime", lastValidTime_, false);
        ret = false;
    } else {
        // 如果dynamicLogLevelValidTime晚于当前时间，刷新为当前时间
        if (IsGreaterThanNow(logConfigJson["dynamicLogLevelValidTime"].get<std::string>())) {
            std::time_t currentTime = std::time(nullptr);
            char currentTimeBuf[20] = {0};
            strftime(currentTimeBuf, sizeof(currentTimeBuf), "%Y-%m-%d %H:%M:%S", std::localtime(&currentTime));
            std::string currentTimeStr(currentTimeBuf);
            LOG_E("The config of dynamicLogLevelValidTime %s is greater than now, flush to: %s",
                logConfigJson["dynamicLogLevelValidTime"].get<std::string>().c_str(), currentTimeStr.c_str());
            SetConfigFile("dynamicLogLevelValidTime", currentTimeStr, false);
            ret = false;
        }
    }
    return ret;
}

bool LogLevelDynamicHandler::CheckDynamicLogLevelChanged(const nlohmann::json& dynamicLogLevel)
{
    currentLevel_ = TransformToUpper(dynamicLogLevel.get<std::string>());
    if (currentLevel_ != lastLogLevel_) {
        LOG_M("Detected log level change to %s", currentLevel_.c_str());
        return true;
    }
    return false;
}

bool LogLevelDynamicHandler::CheckValidHoursChanged(const nlohmann::json& dynamicLogLevelValidHours)
{
    currentValidHours_ = dynamicLogLevelValidHours.get<int>();
    if (currentValidHours_ != lastValidHours_) {
        LOG_M("Detected log level valid hours change from %d to %d", lastValidHours_, currentValidHours_);
        return true;
    }
    return false;
}

void LogLevelDynamicHandler::UpdateDynamicLogParam(const bool sameDynamicLogLevel, const bool sameValidHours)
{
    auto now = std::chrono::system_clock::now();
    std::time_t nowTime = std::chrono::system_clock::to_time_t(now);
    char timeBuffer[20] = {};
    std::strftime(timeBuffer, sizeof(timeBuffer), "%Y-%m-%d %H:%M:%S", std::localtime(&nowTime));
    if (sameDynamicLogLevel) {
        if (!sameValidHours) {
            currentValidTime_ = timeBuffer;
        }
    } else {
        // 如果第一次读取到配置文件中的dynamicLogLevelValidTime就不为空，是服务重启，不更新validTime
        if (currentValidTime_.empty() || hasSetDynamicLog_) {
            currentValidTime_ = timeBuffer;
        }
    }
    UpdateDynamicLogParamToFile();
}

bool ParseTimeString(const std::string& timeStr, std::time_t& outTime)
{
    std::tm tm = {};
    std::istringstream iss(timeStr);
    iss >> std::get_time(&tm, "%Y-%m-%d %H:%M:%S");
    if (iss.fail()) {
        return false;
    }
    outTime = std::mktime(&tm);
    return true;
}

// 判断当前时间是否在有效范围内
bool LogLevelDynamicHandler::IsCurrentTimeWithinValidRange(const std::string& validTimeStr, int validHours) const
{
    std::time_t startTime;
    if (!ParseTimeString(validTimeStr, startTime)) {
        return false;
    }
    const std::time_t endTime = startTime + validHours * 3600;
    const std::time_t currentTime = std::time(nullptr);
    return (currentTime >= startTime && currentTime < endTime);
}

void LogLevelDynamicHandler::UpdateDynamicLogParamToFile()
{
    if (!IsCurrentTimeWithinValidRange(currentValidTime_, currentValidHours_)) {
        LOG_M("dynamicLogLevelValidTime is out, need to clear config params.");
        ClearLogConfigParam();
        return;
    }
    // 若参数都未改变且还在生效区间内，无需继续
    if (lastLogLevel_ == currentLevel_ && lastValidHours_ == currentValidHours_) {
        return;
    }
    lastLogLevel_ = currentLevel_;
    lastValidHours_ = currentValidHours_;
    lastValidTime_ = currentValidTime_;
    SetConfigFile("dynamicLogLevel", currentLevel_, false);
    SetConfigFile("dynamicLogLevelValidTime", currentValidTime_, false);
    Logger::Singleton()->SetLogLevel(Logger::GetLogLevel(currentLevel_));
}

void LogLevelDynamicHandler::ClearLogConfigParam()
{
    // dynamicLogLevel与dynamicLogLevelValidTime清空，dynamicLogLevelValidHours保留
    LOG_M("Clear dynamicLogLevel, dynamicLogLevelValidHours and dynamicLogLevelValidTime.");
    SetConfigFile("dynamicLogLevel", "", false);
    SetConfigFile("dynamicLogLevelValidHours", "2", true);
    SetConfigFile("dynamicLogLevelValidTime", "", false);
    lastLogLevel_ = "";
    lastValidTime_ = "";
    // 恢复环境变量设置值
    Logger::Singleton()->SetLogLevel(Logger::GetLogLevel(defaultLogLevel_));
}

void LogLevelDynamicHandler::InsertLogConfigToFile()
{
    // 保证仅一个进程可以写配置文件
    if (!needWriteToFile_) {
        return;
    }
    std::string jsonString;
    uint32_t mode = 0640;
    auto ret = FileToBuffer(jsonPath_, jsonString, mode, true);
    if (ret != 0) {
        LOG_E("Failed to open config file.");
        return;
    }
    try {
        nlohmann::json config = nlohmann::json::parse(jsonString, CheckJsonDepthCallBack);
        config["LogConfig"] = {
            {"dynamicLogLevel", ""},
            {"dynamicLogLevelValidHours", 2},
            {"dynamicLogLevelValidTime", ""}
        };
        std::ofstream out(jsonPath_);
        int jsonIndentation = 4;
        out << config.dump(jsonIndentation);
        out.close();
        LOG_M("Dynamic Log Level config undetected, insert config into the file.");
    } catch (nlohmann::json::parse_error& e) {
        LOG_E("Failed to parse log level dynamic config file.");
    } catch (std::exception& e) {
        LOG_E("Failed to write back to log level dynamic config file.");
    }
}

void LogLevelDynamicHandler::SetConfigFile(const std::string key, const std::string value, bool isNumber)
{
    // 保证仅一个进程可以写配置文件
    if (!needWriteToFile_) {
        return;
    }
    std::vector<std::string> fileContent;
    bool isModified = false;

    std::ifstream ifs(jsonPath_);
    if (!ifs.is_open()) {
        LOG_E("Failed to open config file");
        return;
    }

    std::string line;
    std::regex pattern("\"" + key + "\"\\s*:\\s*[^,]+");
    while (std::getline(ifs, line)) {
        if (std::regex_search(line, pattern)) {
            std::string modifiedLine;
            if (isNumber) {
                // 整数不带引号
                modifiedLine = regex_replace(line, pattern, "\"" + key + "\" : " + value);
            } else {
                // 字符串保留引号
                modifiedLine = regex_replace(line, pattern, "\"" + key + "\" : \"" + value + "\"");
            }
            fileContent.push_back(modifiedLine);
            isModified = true;
        } else {
            fileContent.push_back(line);
        }
    }
    ifs.close();
    if (!isModified) {
        LOG_E("Key not found in config file");
        return;
    }
    std::ofstream ofs(jsonPath_);
    if (!ofs.is_open()) {
        LOG_E("Failed to write config file");
        return;
    }

    for (const std::string &l : fileContent) {
        ofs << l << std::endl;
    }
    ofs.close();
    return;
}

}
}
