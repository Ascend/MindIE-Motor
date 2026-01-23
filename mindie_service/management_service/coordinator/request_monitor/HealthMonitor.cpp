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
#include "HealthMonitor.h"
#include "MemoryUtil.h"
#include "RequestMgr.h"
#include "Configure.h"
#include "Logger.h"

#include <csignal>
#include <chrono>

namespace MINDIE::MS {

HealthMonitor& HealthMonitor::GetInstance()
{
    static HealthMonitor instance;
    return instance;
}

HealthMonitor::HealthMonitor()
{
    // Default configuration
    config_.memoryThreshold = HealthMonitorConstants::memoryThreshold;
    config_.resetThreshold = HealthMonitorConstants::resetThreshold;
    config_.maxConsecutiveIntercepts = HealthMonitorConstants::maxConsecutiveIntercepts;
}

bool HealthMonitor::Initialize(ReqManage* reqManage)
{
    if (initialized_.exchange(true)) {
        LOG_W("[HealthMonitor] Already initialized");
        return isValid_.load();
    }

    // Get memory limit from MemoryUtil
    memoryLimitBytes_ = MemoryUtil::GetMemoryLimit();
    if (memoryLimitBytes_ <= 0) {
        LOG_E("[HealthMonitor] Failed to get memory limit, health monitor will be disabled");
        isValid_.store(false);
        return false;
    }

    reqManage_ = reqManage;

    // Try to get maxConsecutiveIntercepts from config
    auto configure = Configure::Singleton();
    if (configure != nullptr) {
        std::lock_guard<std::mutex> lock(configMutex_);
        config_.maxConsecutiveIntercepts = static_cast<int>(configure->reqLimit.maxReqs);
    }

    isValid_.store(true);
    
    HealthMonitorConfig currentConfig;
    {
        std::lock_guard<std::mutex> lock(configMutex_);
        currentConfig = config_;
    }
    
    LOG_I("[HealthMonitor] Initialized successfully with memory limit: %lld bytes, "
        "threshold: %.2f, maxConsecutiveIntercepts: %d",
        memoryLimitBytes_, currentConfig.memoryThreshold, currentConfig.maxConsecutiveIntercepts);

    return true;
}

bool HealthMonitor::ShouldInterceptRequest()
{
    if (!isValid_.load()) {
        return false;
    }

    double currentUsage = CalculateMemoryUsage();
    if (currentUsage < 0) {
        LOG_W("[HealthMonitor] Failed to get current memory usage");
        return false;
    }

    HealthMonitorConfig currentConfig;
    {
        std::lock_guard<std::mutex> lock(configMutex_);
        currentConfig = config_;
    }

    // Reset consecutive intercepts if memory usage drops below reset threshold
    if (currentUsage < currentConfig.resetThreshold) {
        if (consecutiveIntercepts_.load() > 0) {
            LOG_D("[HealthMonitor] Memory usage (%.2f) below reset threshold (%.2f), "
                "resetting intercept count",
                currentUsage, currentConfig.resetThreshold);
            consecutiveIntercepts_.store(0);
        }
        return false;
    }

    // Check if memory usage exceeds threshold
    if (currentUsage >= currentConfig.memoryThreshold) {
        // Update last high memory time
        uint64_t currentTime = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
        lastHighMemoryTime_.store(currentTime);
        return true;
    }

    return false;
}

void HealthMonitor::RecordIntercept()
{
    if (!isValid_.load()) {
        return;
    }

    totalIntercepts_++;
    int consecutive = consecutiveIntercepts_.fetch_add(1) + 1;

    LOG_D("[HealthMonitor] Recorded intercept. Total: %d, Consecutive: %d",
        totalIntercepts_.load(), consecutive);

    // Check if graceful shutdown should be triggered
    if (ShouldTriggerGracefulShutdown()) {
        TriggerGracefulShutdown();
    }
}

void HealthMonitor::ResetInterceptCount()
{
    consecutiveIntercepts_.store(0);
    LOG_D("[HealthMonitor] Intercept count reset");
}

bool HealthMonitor::ShouldTriggerGracefulShutdown() const
{
    if (!isValid_.load()) {
        return false;
    }

    HealthMonitorConfig currentConfig;
    {
        std::lock_guard<std::mutex> lock(configMutex_);
        currentConfig = config_;
    }

    int consecutive = consecutiveIntercepts_.load();
    if (consecutive < currentConfig.maxConsecutiveIntercepts) {
        return false;
    }

    // Check if there are active requests
    int activeRequests = 0;
    if (reqManage_ != nullptr) {
        activeRequests = static_cast<int>(reqManage_->GetReqNum());
    }

    if (activeRequests != 0) {
        LOG_D("[HealthMonitor] Cannot trigger shutdown: %d active requests remaining",
            activeRequests);
        return false;
    }

    // Check current memory usage one more time
    double currentUsage = CalculateMemoryUsage();
    if (currentUsage < currentConfig.memoryThreshold) {
        LOG_D("[HealthMonitor] Memory usage dropped below threshold, canceling shutdown");
        return false;
    }

    return true;
}

void HealthMonitor::TriggerGracefulShutdown()
{
    double currentUsage = CalculateMemoryUsage();
    int activeRequests = reqManage_ ? static_cast<int>(reqManage_->GetReqNum()) : 0;

    HealthMonitorConfig currentConfig;
    {
        std::lock_guard<std::mutex> lock(configMutex_);
        currentConfig = config_;
    }

    LOG_C("[HealthMonitor] Triggering graceful shutdown. "
        "Consecutive intercepts: %d (threshold: %d), "
        "Memory usage: %.2f (limit: %s), "
        "Active requests: %d",
        consecutiveIntercepts_.load(),
        currentConfig.maxConsecutiveIntercepts,
        currentUsage,
        MemoryUtil::FormatBytes(memoryLimitBytes_).c_str(),
        activeRequests);

    // Send SIGTERM for graceful shutdown
    std::raise(SIGTERM);
}

double HealthMonitor::CalculateMemoryUsage() const
{
    int64_t currentUsage = MemoryUtil::GetMemoryUsage();
    if (currentUsage < 0 || memoryLimitBytes_ <= 0) {
        return -1.0;
    }
    return static_cast<double>(currentUsage) / static_cast<double>(memoryLimitBytes_);
}

double HealthMonitor::GetCurrentMemoryUsage() const
{
    return CalculateMemoryUsage();
}

HealthStats HealthMonitor::GetStats() const
{
    HealthStats stats;
    stats.valid = isValid_.load();
    stats.memoryLimitBytes = memoryLimitBytes_;
    stats.consecutiveIntercepts = consecutiveIntercepts_.load();
    stats.totalIntercepts = totalIntercepts_.load();

    // Get current memory usage
    int64_t currentMemory = MemoryUtil::GetMemoryUsage();
    stats.currentMemoryBytes = currentMemory;

    HealthMonitorConfig currentConfig;
    {
        std::lock_guard<std::mutex> lock(configMutex_);
        currentConfig = config_;
    }

    if (memoryLimitBytes_ > 0 && currentMemory >= 0) {
        stats.currentMemoryUsage = static_cast<double>(currentMemory) /
                                static_cast<double>(memoryLimitBytes_);
        stats.isMemoryPressureHigh = stats.currentMemoryUsage >= currentConfig.memoryThreshold;
    }

    // Get active requests
    if (reqManage_ != nullptr) {
        stats.activeRequests = static_cast<int>(reqManage_->GetReqNum());
    }

    return stats;
}

void HealthMonitor::SetConfig(const HealthMonitorConfig& config)
{
    {
        std::lock_guard<std::mutex> lock(configMutex_);
        config_ = config;
    }
    LOG_I("[HealthMonitor] Configuration updated: memoryThreshold=%.2f, "
        "resetThreshold=%.2f, maxConsecutiveIntercepts=%d",
        config.memoryThreshold, config.resetThreshold, config.maxConsecutiveIntercepts);
}

HealthMonitorConfig HealthMonitor::GetConfig() const
{
    std::lock_guard<std::mutex> lock(configMutex_);
    return config_;
}

} // namespace MINDIE::MS