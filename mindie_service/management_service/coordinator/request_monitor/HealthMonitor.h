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
#ifndef MINDIE_MS_COORDINATOR_HEALTH_MONITOR_H
#define MINDIE_MS_COORDINATOR_HEALTH_MONITOR_H

#include <cstdint>
#include <atomic>
#include <mutex>
#include <string>
#include "RequestMgr.h"

namespace MINDIE::MS {
/**
 * @brief Health monitor configuration constants
 */
struct HealthMonitorConstants {
    ///< Threshold for request interception
    static constexpr double memoryThreshold = 0.8;
    ///< Threshold for resetting consecutive intercept counter
    static constexpr double resetThreshold = 0.6;
    ///< Maximum consecutive intercepts before triggering graceful shutdown
    static constexpr int maxConsecutiveIntercepts = 100;
};

/**
* @brief Health statistics structure
*/
struct HealthStats {
    bool valid = false;                    ///< Whether the monitor is valid
    double currentMemoryUsage = 0.0;       ///< Memory usage ratio (0.0-1.0)
    int64_t currentMemoryBytes = 0;        ///< Current memory usage in bytes
    int64_t memoryLimitBytes = 0;          ///< Memory limit in bytes
    int consecutiveIntercepts = 0;         ///< Consecutive intercept count
    int totalIntercepts = 0;               ///< Total intercept count
    int activeRequests = 0;                ///< Active request count
    bool isMemoryPressureHigh = false;     ///< Whether memory pressure is high
};

/**
* @brief Health monitor configuration
*/
struct HealthMonitorConfig {
    // 修改为使用独立的结构体
    double memoryThreshold = HealthMonitorConstants::memoryThreshold;
    double resetThreshold = HealthMonitorConstants::resetThreshold;
    int maxConsecutiveIntercepts = HealthMonitorConstants::maxConsecutiveIntercepts;
};

/**
* @brief Health Monitor class
*
* Responsible for monitoring system health and making decisions about:
* - Request interception based on memory pressure
* - Graceful shutdown when system is under persistent pressure
*
* This class aggregates data from multiple sources (MemoryUtil, ReqManage, Configure)
* and makes system-level decisions.
*/
class HealthMonitor {
public:
    /**
    * @brief Get singleton instance
    */
    static HealthMonitor& GetInstance();

    /**
    * @brief Initialize the health monitor
    * @param reqManage Pointer to request manager (for checking active requests)
    * @return true if initialization successful
    */
    bool Initialize(ReqManage* reqManage);

    /**
    * @brief Check if health monitor is valid and operational
    */
    bool IsValid() const { return isValid_.load(); }

    /**
    * @brief Check if current memory usage exceeds threshold
    * @return true if memory usage is above threshold and request should be intercepted
    */
    bool ShouldInterceptRequest();

    /**
    * @brief Record a memory-based interception
    * This updates counters and may trigger graceful shutdown if conditions are met
    */
    void RecordIntercept();

    /**
    * @brief Reset consecutive intercept counter
    * Called when memory usage drops below reset threshold
    */
    void ResetInterceptCount();

    /**
    * @brief Get current health statistics
    */
    HealthStats GetStats() const;

    /**
    * @brief Get current memory usage ratio
    * @return Memory usage ratio (0.0-1.0), or -1.0 if unavailable
    */
    double GetCurrentMemoryUsage() const;

    /**
    * @brief Update configuration (thread-safe)
    */
    void SetConfig(const HealthMonitorConfig& config);

    /**
    * @brief Get current configuration (thread-safe)
    */
    HealthMonitorConfig GetConfig() const;

    // Delete copy constructor and assignment operator
    HealthMonitor(const HealthMonitor&) = delete;
    HealthMonitor& operator=(const HealthMonitor&) = delete;

private:
    HealthMonitor();
    ~HealthMonitor() = default;

    /**
    * @brief Check if graceful shutdown should be triggered
    * Conditions:
    * 1. Consecutive intercepts >= maxConsecutiveIntercepts
    * 2. No active requests
    * 3. Memory usage still high
    */
    bool ShouldTriggerGracefulShutdown() const;

    /**
    * @brief Trigger graceful shutdown by sending SIGTERM
    */
    void TriggerGracefulShutdown();

    /**
    * @brief Calculate current memory usage ratio
    */
    double CalculateMemoryUsage() const;

private:
    // Initialization state
    std::atomic<bool> initialized_{false};
    std::atomic<bool> isValid_{false};

    // Cached memory limit (obtained during initialization)
    int64_t memoryLimitBytes_{0};

    // Reference to request manager (for checking active requests)
    ReqManage* reqManage_{nullptr};

    // Intercept statistics
    std::atomic<int> consecutiveIntercepts_{0};
    std::atomic<int> totalIntercepts_{0};
    std::atomic<uint64_t> lastHighMemoryTime_{0};

    // Configuration with mutex protection for thread safety
    mutable std::mutex configMutex_;
    HealthMonitorConfig config_;
};

} // namespace MINDIE::MS

#endif // MINDIE_MS_COORDINATOR_HEALTH_MONITOR_H