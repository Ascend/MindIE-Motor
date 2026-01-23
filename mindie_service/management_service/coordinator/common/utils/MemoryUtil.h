/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#ifndef MINDIE_MS_COORDINATOR_MEMORY_UTIL_H
#define MINDIE_MS_COORDINATOR_MEMORY_UTIL_H

#include <cstdint>
#include <string>

namespace MINDIE::MS {

/**
 * @brief Memory information structure
 */
struct MemoryInfo {
    int64_t limitBytes = -1;      ///< Memory limit in bytes, -1 indicates unlimited or unavailable
    int64_t usageBytes = -1;      ///< Current usage in bytes, -1 indicates unavailable
    int64_t availableBytes = -1;  ///< Available memory in bytes, -1 indicates unavailable
    std::string source;           ///< Data source (e.g. "cgroup_v2", "cgroup_v1", "proc_meminfo")
};

/**
* @brief Memory utility class (Pure query class)
*
* This class provides static methods to retrieve memory information from the system.
* It supports cgroup v1, cgroup v2, and /proc/meminfo as data sources.
*
* Design principle: This class only provides memory data queries without any business logic.
* Business logic (interception decisions, graceful shutdown, etc.) should be handled by
* the HealthMonitor class.
*/
class MemoryUtil {
public:
    // Delete constructors to enforce static-only usage
    MemoryUtil() = delete;
    ~MemoryUtil() = delete;
    MemoryUtil(const MemoryUtil&) = delete;
    MemoryUtil& operator=(const MemoryUtil&) = delete;

    /**
    * @brief Get complete memory information
    * @return MemoryInfo struct with limit, usage, and available memory
    */
    static MemoryInfo GetMemoryInfo();

    /**
    * @brief Get memory limit in bytes
    * Tries cgroup v2, then cgroup v1, then /proc/meminfo
    * @return Memory limit in bytes, or -1 if unavailable
    */
    static int64_t GetMemoryLimit();

    /**
    * @brief Get current memory usage in bytes
    * Tries cgroup v2, then cgroup v1, then /proc/meminfo
    * @return Current memory usage in bytes, or -1 if unavailable
    */
    static int64_t GetMemoryUsage();

    /**
    * @brief Format bytes to human-readable string
    * @param bytes Number of bytes
    * @return Formatted string (e.g., "1.50 GB", "256.00 MB")
    */
    static std::string FormatBytes(int64_t bytes);

    /**
    * @brief Check if request config may exceed Pod memory limit
    *
    * Calculates maxReqs * bodyLimit and compares with Pod memory limit,
    * prints warning log if theoretical max memory demand exceeds Pod limit.
    * This function is marked noexcept to ensure it never throws exceptions.

    */
    static void CheckRequestConfigMemoryRisk() noexcept;

private:
    /// Get memory limit from cgroup v2
    static int64_t GetMemoryLimitFromCgroupV2();

    /// Get memory limit from cgroup v1
    static int64_t GetMemoryLimitFromCgroupV1();

    /// Get total system memory from /proc/meminfo
    static int64_t GetMemoryLimitFromProc();

    /// Get memory usage from cgroup v2
    static int64_t GetMemoryUsageFromCgroupV2();

    /// Get memory usage from cgroup v1
    static int64_t GetMemoryUsageFromCgroupV1();

    /// Get memory usage from /proc/meminfo
    static int64_t GetMemoryUsageFromProc();
};

} // namespace MINDIE::MS

#endif // MINDIE_MS_COORDINATOR_MEMORY_UTIL_H
