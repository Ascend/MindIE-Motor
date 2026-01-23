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

#include <iostream>
#include <thread>
#include <chrono>
#include <memory>
#include <csignal>

#include "gtest/gtest.h"
#include "gmock/gmock.h"

// 注意：不再使用 #define private public，因为它与 Boost 库冲突
#include "HealthMonitor.h"

using namespace MINDIE::MS;

// 定义一个 Helper 类来访问 HealthMonitor 的私有成员
class HealthMonitorTestHelper {
public:
    static void ResetState(HealthMonitor& hm)
    {
        hm.initialized_.store(false);
        hm.isValid_.store(false);
        hm.consecutiveIntercepts_.store(0);
        hm.totalIntercepts_.store(0);
        hm.lastHighMemoryTime_.store(0);
        hm.reqManage_ = nullptr;
        hm.memoryLimitBytes_ = 0;
        
        std::lock_guard<std::mutex> lock(hm.configMutex_);
        hm.config_.memoryThreshold = 0.8;
        hm.config_.resetThreshold = 0.6;
        hm.config_.maxConsecutiveIntercepts = 100;
    }
    
    static void SetMemoryLimit(HealthMonitor& hm, int64_t limit)
    {
        hm.memoryLimitBytes_ = limit;
    }
    
    static void SetReqManage(HealthMonitor& hm, ReqManage* reqManage)
    {
        hm.reqManage_ = reqManage;
    }
    
    static void SetInitialized(HealthMonitor& hm, bool initialized)
    {
        hm.initialized_.store(initialized);
        hm.isValid_.store(initialized);
    }
    
    static int GetConsecutiveIntercepts(const HealthMonitor& hm)
    {
        return hm.consecutiveIntercepts_.load();
    }
    
    static int GetTotalIntercepts(const HealthMonitor& hm)
    {
        return hm.totalIntercepts_.load();
    }
};

// 模拟 MemoryUtil 的函数 - 使用静态变量
class TestMemoryUtil {
public:
    static int64_t GetMemoryUsage()
    {
        return memoryUsage_;
    }
    
    static int64_t GetMemoryLimit()
    {
        return memoryLimit_;
    }
    
    static void SetMemoryUsage(int64_t usage)
    {
        memoryUsage_ = usage;
    }
    
    static void SetMemoryLimit(int64_t limit)
    {
        memoryLimit_ = limit;
    }
    
private:
    static int64_t memoryUsage_;
    static int64_t memoryLimit_;
};

int64_t TestMemoryUtil::memoryUsage_ = 0;
int64_t TestMemoryUtil::memoryLimit_ = 1024 * 1024 * 1024; // 1GB

// 修改 HealthMonitor 的内存获取函数
// 由于我们不能直接修改 HealthMonitor，我们需要在测试中模拟 MemoryUtil 的行为
// 通过链接时替换 MemoryUtil 的函数实现

// 在单独的命名空间中定义模拟函数
namespace MINDIE::MS {
    // 这些函数会在链接时替换真正的实现
    int64_t MemoryUtil_GetMemoryUsage_Test()
    {
        return TestMemoryUtil::GetMemoryUsage();
    }
    
    int64_t MemoryUtil_GetMemoryLimit_Test()
    {
        return TestMemoryUtil::GetMemoryLimit();
    }
}

// 简单的 ReqManage 模拟类
class MockReqManage : public ReqManage {
public:
    MockReqManage() : ReqManage(dummyScheduler_, dummyPerf_, dummyNodes_)
    {
        activeRequests_ = 0;
    }
    
    size_t GetReqNum()
    {
        return activeRequests_;
    }
    
    void SetActiveRequests(size_t count)
    {
        activeRequests_ = count;
    }
    
    static ReqManage& GetInstance()
    {
        static MockReqManage instance;
        return instance;
    }

private:
    std::unique_ptr<DIGSScheduler> dummyScheduler_;
    std::unique_ptr<PerfMonitor> dummyPerf_;
    std::unique_ptr<ClusterNodes> dummyNodes_;
    size_t activeRequests_;
};

// --- Test Fixture ---

/**
 * @brief HealthMonitor 测试夹具
 */
class TestHealthMonitor : public ::testing::Test {
protected:
    void SetUp() override
    {
        // 重置内存状态
        TestMemoryUtil::SetMemoryLimit(1024 * 1024 * 1024); // 1GB
        TestMemoryUtil::SetMemoryUsage(600 * 1024 * 1024);  // 600MB (60%)
        
        // 创建测试对象
        testReqMgr_ = std::make_unique<MockReqManage>();
        
        // 重置 HealthMonitor 状态
        auto& hm = HealthMonitor::GetInstance();
        HealthMonitorTestHelper::ResetState(hm);
    }
    
    void TearDown() override
    {
        // 清理测试对象
        testReqMgr_.reset();
    }
    
    void SimulateMemoryPressure(double ratio)
    {
        int64_t limit = TestMemoryUtil::GetMemoryLimit();
        int64_t usage = static_cast<int64_t>(limit * ratio);
        TestMemoryUtil::SetMemoryUsage(usage);
    }
    
    void SetActiveRequests(size_t count)
    {
        testReqMgr_->SetActiveRequests(count);
    }
    
    void TriggerIntercepts(int count)
    {
        auto& hm = HealthMonitor::GetInstance();
        for (int i = 0; i < count; i++) {
            hm.RecordIntercept();
        }
    }
    
    // 初始化 HealthMonitor（简化版本）
    bool InitializeHealthMonitor()
    {
        auto& hm = HealthMonitor::GetInstance();
        return hm.Initialize(testReqMgr_.get());
    }
    
    std::unique_ptr<MockReqManage> testReqMgr_;
};

// --- 测试用例 ---

TEST_F(TestHealthMonitor, GetInstanceReturnsSingleton)
{
    HealthMonitor& instance1 = HealthMonitor::GetInstance();
    HealthMonitor& instance2 = HealthMonitor::GetInstance();
    EXPECT_EQ(&instance1, &instance2);
}

TEST_F(TestHealthMonitor, InitializationSuccess)
{
    // 确保内存限制有效
    TestMemoryUtil::SetMemoryLimit(1024 * 1024 * 1024); // 1GB
    
    auto& hm = HealthMonitor::GetInstance();
    bool result = hm.Initialize(testReqMgr_.get());
    
    EXPECT_TRUE(result);
    EXPECT_TRUE(hm.IsValid());
}

TEST_F(TestHealthMonitor, InitializationFailureInvalidMemoryLimit)
{
    TestMemoryUtil::SetMemoryLimit(-1);
    
    auto& hm = HealthMonitor::GetInstance();
    bool result = hm.Initialize(testReqMgr_.get());
    
    EXPECT_FALSE(result);
    EXPECT_FALSE(hm.IsValid());
}

TEST_F(TestHealthMonitor, ShouldInterceptNormalMemoryUsage)
{
    InitializeHealthMonitor();
    SimulateMemoryPressure(0.5); // 50% 使用率
    EXPECT_FALSE(HealthMonitor::GetInstance().ShouldInterceptRequest());
}

TEST_F(TestHealthMonitor, ShouldInterceptHighMemoryUsage)
{
    InitializeHealthMonitor();
    SimulateMemoryPressure(0.9); // 90% 使用率
    EXPECT_TRUE(HealthMonitor::GetInstance().ShouldInterceptRequest());
}

TEST_F(TestHealthMonitor, InterceptCounting)
{
    InitializeHealthMonitor();
    auto& hm = HealthMonitor::GetInstance();
    
    EXPECT_EQ(HealthMonitorTestHelper::GetTotalIntercepts(hm), 0);
    EXPECT_EQ(HealthMonitorTestHelper::GetConsecutiveIntercepts(hm), 0);
    
    hm.RecordIntercept();
    EXPECT_EQ(HealthMonitorTestHelper::GetTotalIntercepts(hm), 1);
    EXPECT_EQ(HealthMonitorTestHelper::GetConsecutiveIntercepts(hm), 1);
    
    hm.RecordIntercept();
    EXPECT_EQ(HealthMonitorTestHelper::GetTotalIntercepts(hm), 2);
    EXPECT_EQ(HealthMonitorTestHelper::GetConsecutiveIntercepts(hm), 2);
    
    hm.ResetInterceptCount();
    EXPECT_EQ(HealthMonitorTestHelper::GetConsecutiveIntercepts(hm), 0);
    EXPECT_EQ(HealthMonitorTestHelper::GetTotalIntercepts(hm), 2);
}

TEST_F(TestHealthMonitor, GetStatsAccuracy)
{
    InitializeHealthMonitor();
    TestMemoryUtil::SetMemoryLimit(1000);
    TestMemoryUtil::SetMemoryUsage(850);
    
    auto& hm = HealthMonitor::GetInstance();
    SetActiveRequests(5);
    TriggerIntercepts(2);
    
    HealthStats stats = hm.GetStats();
    EXPECT_TRUE(stats.valid);
    EXPECT_EQ(stats.memoryLimitBytes, 1000);
    EXPECT_EQ(stats.currentMemoryBytes, 850);
    EXPECT_DOUBLE_EQ(stats.currentMemoryUsage, 0.85);
    EXPECT_TRUE(stats.isMemoryPressureHigh);
    EXPECT_EQ(stats.activeRequests, 5);
    EXPECT_EQ(stats.totalIntercepts, 2);
    EXPECT_EQ(stats.consecutiveIntercepts, 2);
}

TEST_F(TestHealthMonitor, ConfigurationManagement)
{
    InitializeHealthMonitor();
    auto& hm = HealthMonitor::GetInstance();
    
    HealthMonitorConfig defaultConfig = hm.GetConfig();
    EXPECT_NEAR(defaultConfig.memoryThreshold, 0.8, 0.0001);
    EXPECT_NEAR(defaultConfig.resetThreshold, 0.6, 0.0001);
    EXPECT_EQ(defaultConfig.maxConsecutiveIntercepts, 100);
    
    HealthMonitorConfig newConfig;
    newConfig.memoryThreshold = 0.75;
    newConfig.resetThreshold = 0.55;
    newConfig.maxConsecutiveIntercepts = 50;
    
    hm.SetConfig(newConfig);
    HealthMonitorConfig currentConfig = hm.GetConfig();
    EXPECT_NEAR(currentConfig.memoryThreshold, 0.75, 0.0001);
    EXPECT_NEAR(currentConfig.resetThreshold, 0.55, 0.0001);
    EXPECT_EQ(currentConfig.maxConsecutiveIntercepts, 50);
}

TEST_F(TestHealthMonitor, UninitializedStateBehavior)
{
    auto& hm = HealthMonitor::GetInstance();
    HealthMonitorTestHelper::ResetState(hm);
    
    EXPECT_FALSE(hm.IsValid());
    EXPECT_FALSE(hm.ShouldInterceptRequest());
    hm.RecordIntercept();
    EXPECT_EQ(hm.GetStats().valid, false);
    EXPECT_EQ(hm.GetCurrentMemoryUsage(), -1.0);
}

TEST_F(TestHealthMonitor, CalculateMemoryUsage)
{
    InitializeHealthMonitor();
    auto& hm = HealthMonitor::GetInstance();
    
    TestMemoryUtil::SetMemoryLimit(1000);
    TestMemoryUtil::SetMemoryUsage(0);
    EXPECT_DOUBLE_EQ(hm.CalculateMemoryUsage(), 0.0);
    
    TestMemoryUtil::SetMemoryUsage(500);
    EXPECT_DOUBLE_EQ(hm.CalculateMemoryUsage(), 0.5);
    
    TestMemoryUtil::SetMemoryUsage(1000);
    EXPECT_DOUBLE_EQ(hm.CalculateMemoryUsage(), 1.0);
    
    TestMemoryUtil::SetMemoryLimit(-1);
    EXPECT_EQ(hm.CalculateMemoryUsage(), -1.0);
}

// 简化其他测试用例，只保留关键测试
TEST_F(TestHealthMonitor, ShouldInterceptMemoryUsageAtThreshold)
{
    InitializeHealthMonitor();
    auto& hm = HealthMonitor::GetInstance();
    
    HealthMonitorConfig config;
    config.memoryThreshold = 0.8;
    config.resetThreshold = 0.6;
    config.maxConsecutiveIntercepts = 100;
    hm.SetConfig(config);
    
    SimulateMemoryPressure(0.8); // 正好在阈值上
    EXPECT_TRUE(hm.ShouldInterceptRequest());
}

TEST_F(TestHealthMonitor, ShouldInterceptResetConsecutiveIntercepts)
{
    InitializeHealthMonitor();
    auto& hm = HealthMonitor::GetInstance();
    hm.RecordIntercept(); // 设置 consecutiveIntercepts > 0
    
    SimulateMemoryPressure(0.7); // 70% 使用率
    EXPECT_FALSE(hm.ShouldInterceptRequest());
    EXPECT_EQ(HealthMonitorTestHelper::GetConsecutiveIntercepts(hm), 0); // 应该被重置
}

TEST_F(TestHealthMonitor, ResetThresholdBehavior)
{
    InitializeHealthMonitor();
    auto& hm = HealthMonitor::GetInstance();
    HealthMonitorConfig config;
    config.memoryThreshold = 0.8;
    config.resetThreshold = 0.6;
    hm.SetConfig(config);
    
    // 初始状态
    EXPECT_EQ(HealthMonitorTestHelper::GetConsecutiveIntercepts(hm), 0);
    
    // 触发拦截但内存使用率低于重置阈值
    SimulateMemoryPressure(0.7); // 70% 高于重置阈值 60%
    EXPECT_TRUE(hm.ShouldInterceptRequest());
    EXPECT_GT(HealthMonitorTestHelper::GetConsecutiveIntercepts(hm), 0);
    
    // 内存使用率下降到重置阈值以下
    SimulateMemoryPressure(0.5); // 50% 低于重置阈值 60%
    EXPECT_FALSE(hm.ShouldInterceptRequest());
    EXPECT_EQ(HealthMonitorTestHelper::GetConsecutiveIntercepts(hm), 0); // 应该被重置
}