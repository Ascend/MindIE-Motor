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
#include <iostream>
#include <vector>
#include <string>
#include <sstream>
#include <map>
#include <algorithm>
#include <cstdlib>
#include "gtest/gtest.h"
#define main __main_coordinator__
#include "coordinator/main.cpp"
#include "JsonFileManager.h"
#include "Helper.h"
#include "MindIEServer.h"
#include "RequestHelper.h"
#include "stub.h"
#include "MyThreadPool.h"
#include "HttpClientAsync.h"

using namespace MINDIE::MS;

class TestReqKeepAliveTimeout : public testing::Test {
protected:
    void SetUp()
    {
        CopyDefaultConfig();
        auto coordinatorJson = GetMSCoordinatorTestJsonPath();
        setenv("MINDIE_MS_COORDINATOR_CONFIG_FILE_PATH", coordinatorJson.c_str(), 1);
        std::cout << "MINDIE_MS_COORDINATOR_CONFIG_FILE_PATH=" << coordinatorJson << std::endl;

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
        manager.SetList({"http_config", "keep_alive_seconds"}, 5); // 长链接的时间改短到5秒，防止用例执行太久
        manager.Save();
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
    std::string statusPort = "2030";
};

static std::condition_variable g_cv;

static void ServerExceptionCloseHandlerStub(std::shared_ptr<ServerConnection> connection)
{
    g_cv.notify_one();
}

/*
测试描述: 发送流式推理请求之后，客户端不断连，等待coordinator主动触发断连
测试步骤:
    1. 将coordinator的长链接时间设置为5秒
    2. 客户端向coordinator发送流式请求，请求结束后不断连，不重新发请求
    3. 连接超过5秒后，coordinator主动关闭连接

预期结果:
    1. 超时时间内，coordinator能主动关闭连接
    2. 请求的响应结果正确
*/
TEST_F(TestReqKeepAliveTimeout, TestReqKeepAliveTimeoutTC01)
{
    std::vector<std::thread> threads;
    for (size_t i = 0; i < numMindIEServer; i++) {
        predictIPList.emplace_back("127.0.0.1");
        auto pPort = std::to_string(GetUnBindPort());
        auto mPort = std::to_string(GetUnBindPort());
        auto iPort = std::to_string(GetUnBindPort());
        auto predictIP = predictIPList[i];
        auto manageIP = predictIPList[i];
        predictPortList.push_back(pPort);
        managePortList.push_back(mPort);
        interCommonPortLists.push_back(iPort);
        std::cout << "ip = " << predictIPList[i] << ", predict port = " << pPort <<
                        ", manage port = " << mPort << std::endl;

        threads.push_back(std::thread(CreateMindIEServer,
        std::move(predictIP), std::move(pPort),
        std::move(manageIP), std::move(mPort),
        4, i, "")); // 默认4线程启动服务
    }

    char *argsCoordinator[1] = {"ms_coordinator"};
    std::thread threadObj(__main_coordinator__, 1, std::ref(argsCoordinator));

    auto pdInstance = SetPDRole(predictIPList, predictPortList, managePortList,
        interCommonPortLists, 2, 2, manageIP, managePort); // 2表示PD两种类型
    WaitCoordinatorReady(manageIP, managePort);
    auto reqBody = CreateOpenAIRequest({"My name is Olivier and I", "My name is Olivier and I"}, true);
    std::string results = "";
    std::mutex mtx;
    HttpClientAsync httpClient;
    TlsItems tls;
    tls.tlsEnable = false;
    httpClient.Init(tls);
    boost::beast::http::request<boost::beast::http::dynamic_body> req;
    req.version(HTTP_VERSION_11);
    req.method(boost::beast::http::verb::post);
    req.target("/v1/chat/completions");
    req.keep_alive(true); // 保持长链接
    boost::beast::ostream(req.body()) << reqBody;
    req.prepare_payload();
    ClientHandler clientHandler;
    clientHandler.RegisterFun(ClientHandlerType::CHUNK_BODY_RES, [&] (std::shared_ptr<ClientConnection> connection) {
        boost::beast::string_view body = connection->GetResChunkedBody();
        results += body;
    });
    Stub stub;
    stub.set(ADDR(RequestListener, ServerExceptionCloseHandler), &ServerExceptionCloseHandlerStub);
    uint32_t id;
    auto ret = httpClient.AddConnection(predictIP, predictPort, id, clientHandler);
    ASSERT_TRUE(ret);
    auto conn = httpClient.GetConnection(id);
    ASSERT_NE(conn, nullptr);
    conn->SendReq(req);
    std::unique_lock<std::mutex> lock(mtx);
    if (g_cv.wait_for(lock, std::chrono::seconds(10)) == std::cv_status::no_timeout) { // 最多10秒
        EXPECT_EQ(ProcessStreamRespond(results), "My name is Olivier and IMy name is Olivier and I");
        conn = httpClient.GetConnection(id);
        EXPECT_EQ(conn, nullptr);
    } else {
        std::cout << "TestReqKeepAliveTimeoutTC01 timeout" << std::endl;
        EXPECT_TRUE(false);
    }
    stub.reset(ADDR(RequestListener, ServerExceptionCloseHandler));
    EXPECT_TRUE(threadObj.joinable());
    // 将线程放入后台运行
    threadObj.detach();
    for (size_t i = 0; i < threads.size(); ++i) {
        threads[i].detach();
    }
    std::cout << "--------------------- Test end ----------------------" << std::endl;
    sleep(2); // 睡眠2s，供请求退出
    std::_Exit(0);
}

/*
测试描述: 发送非流式推理请求之后，客户端不断连，等待coordinator主动触发断连
测试步骤:
    1. 将coordinator的长链接时间设置为5秒
    2. 客户端向coordinator发送非流式请求，请求结束后不断连，不重新发请求
    3. 连接超过5秒后，coordinator主动关闭连接

预期结果:
    1. 超时时间内，coordinator能主动关闭连接
    2. 请求的响应结果正确
*/
TEST_F(TestReqKeepAliveTimeout, TestReqKeepAliveTimeoutTC02)
{
    std::vector<std::thread> threads;
    for (size_t i = 0; i < numMindIEServer; i++) {
        predictIPList.emplace_back("127.0.0.1");
        auto pPort = std::to_string(GetUnBindPort());
        auto mPort = std::to_string(GetUnBindPort());
        auto iPort = std::to_string(GetUnBindPort());
        auto predictIP = predictIPList[i];
        auto manageIP = predictIPList[i];
        predictPortList.push_back(pPort);
        managePortList.push_back(mPort);
        interCommonPortLists.push_back(iPort);
        std::cout << "ip = " << predictIPList[i] << ", predict port = " << pPort <<
                        ", manage port = " << mPort << std::endl;

        threads.push_back(std::thread(CreateMindIEServer,
        std::move(predictIP), std::move(pPort),
        std::move(manageIP), std::move(mPort),
        4, i, "")); // 默认4线程启动服务
    }

    char *argsCoordinator[1] = {"ms_coordinator"};
    std::thread threadObj(__main_coordinator__, 1, std::ref(argsCoordinator));

    auto pdInstance = SetPDRole(predictIPList, predictPortList, managePortList,
        interCommonPortLists, 2, 2, manageIP, managePort); // 2表示PD两种类型
    WaitCoordinatorReady(manageIP, statusPort);
    auto reqBody = CreateOpenAIRequest({"My name is Olivier and I", "My name is Olivier and I"}, false);
    std::string results = "";
    std::mutex mtx;
    HttpClientAsync httpClient;
    TlsItems tls;
    tls.tlsEnable = false;
    httpClient.Init(tls);
    boost::beast::http::request<boost::beast::http::dynamic_body> req;
    req.version(HTTP_VERSION_11);
    req.method(boost::beast::http::verb::post);
    req.target("/v1/chat/completions");
    req.keep_alive(true); // 保持长链接
    boost::beast::ostream(req.body()) << reqBody;
    req.prepare_payload();
    ClientHandler clientHandler;
    clientHandler.RegisterFun(ClientHandlerType::RES, [&] (std::shared_ptr<ClientConnection> connection) {
        auto &res = connection->GetResMessage();
        auto body = boost::beast::buffers_to_string(res.body().data());
        results = body;
    });
    Stub stub;
    stub.set(ADDR(RequestListener, ServerExceptionCloseHandler), &ServerExceptionCloseHandlerStub);
    uint32_t id;
    auto ret = httpClient.AddConnection(predictIP, predictPort, id, clientHandler);
    ASSERT_TRUE(ret);
    auto conn = httpClient.GetConnection(id);
    ASSERT_NE(conn, nullptr);
    conn->SendReq(req);
    std::unique_lock<std::mutex> lock(mtx);
    if (g_cv.wait_for(lock, std::chrono::seconds(10)) == std::cv_status::no_timeout) { // 最多10秒
        EXPECT_EQ(results, "My name is Olivier and IMy name is Olivier and I");
        conn = httpClient.GetConnection(id);
        EXPECT_EQ(conn, nullptr);
    } else {
        std::cout << "TestReqKeepAliveTimeoutTC01 timeout" << std::endl;
        EXPECT_TRUE(false);
    }
    stub.reset(ADDR(RequestListener, ServerExceptionCloseHandler));
    EXPECT_TRUE(threadObj.joinable());
    // 将线程放入后台运行
    threadObj.detach();
    for (size_t i = 0; i < threads.size(); ++i) {
        threads[i].detach();
    }
    std::cout << "--------------------- Test end ----------------------" << std::endl;
    sleep(2); // 睡眠2s，供请求退出
}

/*
测试描述: 客户端与coordinator建连，不发送请求，等待coordinator主动触发断连
测试步骤:
    1. 将coordinator的长链接时间设置为5秒
    2. 客户端向coordinator建立连接，不发请求
    3. 连接超过5秒后，coordinator主动关闭连接
预期结果:
    1. 超时时间内，coordinator能主动关闭连接
*/
TEST_F(TestReqKeepAliveTimeout, TestReqKeepAliveTimeoutTC03)
{
    std::vector<std::thread> threads;

    for (size_t i = 0; i < numMindIEServer; i++) {
        predictIPList.emplace_back("127.0.0.1");
        auto pPort = std::to_string(GetUnBindPort());
        auto mPort = std::to_string(GetUnBindPort());
        auto iPort = std::to_string(GetUnBindPort());
        auto predictIP = predictIPList[i];
        auto manageIP = predictIPList[i];
        predictPortList.push_back(pPort);
        managePortList.push_back(mPort);
        interCommonPortLists.push_back(iPort);
        std::cout << "ip = " << predictIPList[i] << ", predict port = " << pPort <<
                        ", manage port = " << mPort << std::endl;

        threads.push_back(std::thread(CreateMindIEServer,
        std::move(predictIP), std::move(pPort),
        std::move(manageIP), std::move(mPort),
        4, i, "")); // 默认4线程启动服务
    }

    char *argsCoordinator[1] = {"ms_coordinator"};
    std::thread threadObj(__main_coordinator__, 1, std::ref(argsCoordinator));

    auto pdInstance = SetPDRole(predictIPList, predictPortList, managePortList,
        interCommonPortLists, 2, 2, manageIP, managePort); // 2表示PD两种类型
    WaitCoordinatorReady(manageIP, managePort);
    std::mutex mtx;
    HttpClientAsync httpClient;
    TlsItems tls;
    tls.tlsEnable = false;
    httpClient.Init(tls);
    Stub stub;
    stub.set(ADDR(RequestListener, ServerExceptionCloseHandler), &ServerExceptionCloseHandlerStub);
    uint32_t id;
    auto ret = httpClient.AddConnection(predictIP, predictPort, id);
    ASSERT_TRUE(ret);
    std::unique_lock<std::mutex> lock(mtx);
    if (g_cv.wait_for(lock, std::chrono::seconds(10)) == std::cv_status::no_timeout) { // 最多10秒
        auto conn = httpClient.GetConnection(id);
        EXPECT_EQ(conn, nullptr);
    } else {
        std::cout << "TestReqKeepAliveTimeoutTC03 timeout" << std::endl;
        EXPECT_TRUE(false);
    }
    stub.reset(ADDR(RequestListener, ServerExceptionCloseHandler));
    EXPECT_TRUE(threadObj.joinable());
    // 将线程放入后台运行
    threadObj.detach();
    for (size_t i = 0; i < threads.size(); ++i) {
        threads[i].detach();
    }
    std::cout << "--------------------- Test end ----------------------" << std::endl;
    sleep(2); // 睡眠2s，供请求退出
}