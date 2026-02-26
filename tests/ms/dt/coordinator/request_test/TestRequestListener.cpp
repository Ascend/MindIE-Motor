/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2024-2026. All rights reserved.
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
#include <vector>
#include <string>
#include "gtest/gtest.h"
#define main __main_coordinator__
#include "coordinator/main.cpp"
#include "JsonFileManager.h"
#include "Helper.h"
#include "MindIEServer.h"
#include "RequestHelper.h"
#include "ThreadSafeVector.h"
#include "stub.h"
#include "MemoryUtil.h"

using namespace MINDIE::MS;

// Stub functions for MemoryUtil to simulate high memory pressure (90% usage)
static int64_t g_memoryLimit = 1000000000LL;    // 1GB
static int64_t g_memoryUsage = 900000000LL;    // 900MB (90%)

int64_t GetMemoryLimitStub()
{
    return g_memoryLimit;
}

int64_t GetMemoryUsageStub()
{
    return g_memoryUsage;
}

class TestRequestListener : public testing::Test {
protected:
    void SetUp() override
    {
        CopyDefaultConfig();
        auto coordinatorJson = GetMSCoordinatorTestJsonPath();
        setenv("MINDIE_MS_COORDINATOR_CONFIG_FILE_PATH", coordinatorJson.c_str(), 1);

        JsonFileManager manager(coordinatorJson);
        manager.Load();
        manager.SetList({"http_config", "predict_ip"}, predictIP);
        manager.SetList({"http_config", "predict_port"}, predictPort);
        manager.SetList({"http_config", "manage_ip"}, manageIP);
        manager.SetList({"http_config", "manage_port"}, managePort);
        manager.SetList({"http_config", "status_port"}, statusPort);
        manager.SetList({"tls_config", "controller_server_tls_enable"}, false);
        manager.SetList({"tls_config", "request_server_tls_enable"}, false);
        manager.SetList({"tls_config", "mindie_client_tls_enable"}, false);
        manager.SetList({"tls_config", "external_tls_enable"}, false);
        manager.SetList({"tls_config", "status_tls_enable"}, false);
        manager.Save();
    }

    std::vector<std::thread> StartServices()
    {
        std::vector<std::thread> threads;
        for (size_t i = 0; i < numMindIEServer; i++) {
            predictIPList.emplace_back("127.0.0.1");
            auto pPort = std::to_string(GetUnBindPort());
            auto mPort = std::to_string(GetUnBindPort());
            auto iPort = std::to_string(GetUnBindPort());
            auto manageIP = predictIPList[i];
            auto predictIP = predictIPList[i];
            predictPortList.push_back(pPort);
            managePortList.push_back(mPort);
            interCommonPortLists.push_back(iPort);
            threads.push_back(std::thread(CreateMindIEServer,
                std::move(manageIP), std::move(pPort),
                std::move(predictIP), std::move(mPort),
                4, i, ""));
        }
        return threads;
    }

    uint8_t numMindIEServer = 4;
    ThreadSafeVector<std::string> predictIPList;
    ThreadSafeVector<std::string> predictPortList;
    ThreadSafeVector<std::string> managePortList;
    ThreadSafeVector<std::string> interCommonPortLists;
    std::string manageIP = "127.0.0.1";
    std::string managePort = "1026";
    std::string predictIP = "127.0.0.1";
    std::string predictPort = "1025";
    std::string statusPort = "2020";
};

/*
 * 测试描述: 覆盖 RequestListener::LogIntercept 函数
 * 通过模拟高内存压力，触发 PreRequestCheck -> ShouldIntercept -> LogIntercept 调用链
 * 测试步骤:
 *    1. 打桩 MemoryUtil::GetMemoryLimit 和 GetMemoryUsage 模拟 90% 内存使用率
 *    2. 启动 coordinator 和 mock 服务
 *    3. 发送推理请求
 * 预期结果:
 *    请求被拦截，返回 "Memory limit exceeded"，LogIntercept 被调用
 */
TEST_F(TestRequestListener, LogInterceptMemoryPressure)
{
    // 打桩 MemoryUtil，在 coordinator 启动前设置
    Stub stubLimit;
    Stub stubUsage;
    stubLimit.set(ADDR(MINDIE::MS::MemoryUtil, GetMemoryLimit), GetMemoryLimitStub);
    stubUsage.set(ADDR(MINDIE::MS::MemoryUtil, GetMemoryUsage), GetMemoryUsageStub);

    std::vector<std::thread> threads = StartServices();

    char *argsCoordinator[1] = {"ms_coordinator"};
    std::thread threadObj(__main_coordinator__, 1, std::ref(argsCoordinator));

    auto nonStreamBody = CreateOpenAIRequest({"My name is Olivier and I", "OK"}, false);
    std::string response;
    TlsItems tlsItems;

    auto pdInstance = SetPDRole(predictIPList, predictPortList, managePortList,
        interCommonPortLists, 2, 2, manageIP, managePort);
    WaitCoordinatorReady(manageIP, statusPort);

    // 发送请求，由于内存压力高(90% > 80% threshold)，应被拦截
    // 预期: PreRequestCheck 返回 false，ShouldIntercept 被调用，LogIntercept 被调用
    auto code = SendInferRequest(predictIP, predictPort, tlsItems, "/v1/chat/completions", nonStreamBody, response);

    EXPECT_TRUE(response.find("Memory limit exceeded") != std::string::npos);
    EXPECT_EQ(code, 0);

    stubLimit.reset(ADDR(MINDIE::MS::MemoryUtil, GetMemoryLimit));
    stubUsage.reset(ADDR(MINDIE::MS::MemoryUtil, GetMemoryUsage));

    if (threadObj.joinable()) {
        threadObj.detach();
    }
    for (size_t i = 0; i < threads.size(); ++i) {
        threads[i].detach();
    }
}
