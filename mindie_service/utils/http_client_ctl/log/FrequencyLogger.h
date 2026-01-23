/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#ifndef MINDIE_MS_LOG_UTILS_H
#define MINDIE_MS_LOG_UTILS_H


#include <chrono>
#include <thread>
#include <unordered_map>
#include <string>
#include "Logger.h"
namespace MINDIE {
namespace MS {
template<typename Func>
void LogWithFrequency(uint64_t intervalMs, Func&& logFunc)
{
    static thread_local std::chrono::steady_clock::time_point lastTime;
    static thread_local uint64_t lastInterval = 0;
    
    // 如果间隔改变，重置时间
    if (lastInterval != intervalMs) {
        lastTime = std::chrono::steady_clock::now();
        lastInterval = intervalMs;
    }
    
    auto now = std::chrono::steady_clock::now();
    auto elapsedDuration = now - lastTime;
    if (elapsedDuration >= std::chrono::milliseconds(intervalMs)) {
        lastTime = now;
        logFunc();
    }
}

class FrequencyLogger {
public:
    /**
     * @brief 构造函数
     * @param intervalMs 默认的日志打印间隔（毫秒）
     */
    explicit FrequencyLogger(uint64_t intervalMs = 1000)
        : intervalMs_(intervalMs), lastTime_(std::chrono::steady_clock::now()) {}
    
    /**
     * @brief 设置新的频率间隔
     * @param intervalMs 新的间隔时间（毫秒）
     */
    void SetInterval(uint64_t intervalMs)
    {
        intervalMs_ = intervalMs;
        lastTime_ = std::chrono::steady_clock::now();
    }
    
    /**
     * @brief 获取当前设置的频率间隔
     * @return 当前的间隔时间（毫秒）
     */
    uint64_t GetInterval() const
    {
        return intervalMs_;
    }
    
    /**
     * @brief 通用频率日志方法
     * @param type 日志类型（RUN或OPERATION）
     * @param level 日志级别
     * @param msg 格式化字符串
     * @param args 格式化参数
     */
    template<typename... Args>
    void Log(LogType type, LogLevel level, const char* msg, Args&&... args)
    {
        auto now = std::chrono::steady_clock::now();
        auto elapsedDuration = now - lastTime_;
        if (elapsedDuration >= std::chrono::milliseconds(intervalMs_)) {
            lastTime_ = now;
            LOG(type, level, msg, std::forward<Args>(args)...);
        }
    }

    template<typename... Args>
    void Critical(const char* msg, Args&&... args)
    {
        Log(LogType::RUN, LogLevel::MINDIE_LOG_CRITICAL, msg,
            std::forward<Args>(args)...);
    }

    template<typename... Args>
    void Error(const char* msg, Args&&... args)
    {
        Log(LogType::RUN, LogLevel::MINDIE_LOG_ERROR, msg,
            std::forward<Args>(args)...);
    }

    template<typename... Args>
    void Warn(const char* msg, Args&&... args)
    {
        Log(LogType::RUN, LogLevel::MINDIE_LOG_WARN, msg,
            std::forward<Args>(args)...);
    }

    template<typename... Args>
    void Info(const char* msg, Args&&... args)
    {
        Log(LogType::RUN, LogLevel::MINDIE_LOG_INFO, msg,
            std::forward<Args>(args)...);
    }

    template<typename... Args>
    void Debug(const char* msg, Args&&... args)
    {
        Log(LogType::RUN, LogLevel::MINDIE_LOG_DEBUG, msg,
            std::forward<Args>(args)...);
    }
    
    template<typename... Args>
    void Perf(const char* msg, Args&&... args)
    {
        Log(LogType::RUN, LogLevel::MINDIE_LOG_PERF, msg,
            std::forward<Args>(args)...);
    }

    template<typename... Args>
    void Operation(const char* msg, Args&&... args)
    {
        Log(LogType::OPERATION, LogLevel::MINDIE_LOG_INFO, msg,
            std::forward<Args>(args)...);
    }
    
private:
    uint64_t intervalMs_;
    std::chrono::steady_clock::time_point lastTime_;
};


} // namespace MS
}
#endif