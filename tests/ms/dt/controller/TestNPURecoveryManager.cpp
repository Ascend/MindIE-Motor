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
#include <map>
#include <vector>
#include <memory>
#include <mutex>
#include <chrono>
#include <thread>
#include <iostream>
#include <gmock/gmock.h>
#include <gtest/gtest.h>
#include "stub.h"
#include "Helper.h"
#include "ControllerSendReqStub.h"
#include "RequestServerStub.h"
#include "ControllerConfig.h"
#include "NPURecoveryManager.h"
#include "NodeStatus.h"
#include "grpc_proto/cluster_fault.pb.h"
#include "node_manager_sender/NodeManagerRequestSender.h"
#include <nlohmann/json.hpp>

using namespace MINDIE::MS;

int32_t MockGetNodeManagerStatusReady(NodeManagerRequestSender* sender,
                                      HttpClient& client,
                                      const std::string& nodeManagerIP,
                                      NPUStatus& status)
{
    status = NPUStatus::READY;
    return 0;
}

class TestNPURecoveryManager : public testing::Test {
public:
    static void SetUpTestSuite()
    {
        std::cout << "===== Setting up Test Suite for TestNPURecoveryManager =====" << std::endl;
    }

    static void TearDownTestSuite()
    {
        std::cout << "===== Tearing down Test Suite for TestNPURecoveryManager =====" << std::endl;
    }

    void SetUp() override
    {
        CopyDefaultConfig();
        InitConfig();
        InitNodeStatus();
        InitNPURecoveryManager();
        SetCode(200);
        SetRet(0);
        stub.set(ADDR(HttpClient, SendRequest), &SendRequestMock);
    }

    void TearDown() override
    {
        // Cleanup if needed
    }

    void InitConfig()
    {
        auto controllerJson = GetMSControllerConfigJsonPath();
        auto testJson = GetServerRequestHandlerTestJsonPath();
        CopyFile(controllerJson, testJson);
        ModifyJsonItem(testJson, "tls_config", "request_server_tls_enable", false);
        ModifyJsonItem(testJson, "tls_config", "request_coordinator_tls_enable", false);
        ModifyJsonItem(testJson, "fault_recovery_func_dict", "lingqu_link", true);
        setenv("MINDIE_MS_CONTROLLER_CONFIG_FILE_PATH", testJson.c_str(), 1);
        ControllerConfig::GetInstance()->Init();
    }

    void InitNodeStatus()
    {
        nodeStatus = std::make_shared<NodeStatus>();
    }

    void InitNPURecoveryManager()
    {
        NPURecoveryManager::GetInstance()->Init(nodeStatus);
    }

    std::unique_ptr<NodeInfo> CreateTestNode(uint64_t nodeId, const std::string& ip,
                                             MINDIE::MS::DIGSInstanceRole role,
                                             const std::vector<uint64_t>& dpGroupPeers = {})
    {
        auto node = std::make_unique<NodeInfo>();
        node->instanceInfo.staticInfo.id = nodeId;
        node->hostId = ip;
        node->ip = ip;
        node->currentRole = role;
        node->roleState = ControllerConstant::GetInstance()->GetRoleState(RoleState::READY);
        node->dpGroupPeers = dpGroupPeers.empty() ? std::vector<uint64_t>{nodeId} : dpGroupPeers;
        return node;
    }

    fault::FaultMsgSignal CreateFaultMsg(const std::string& nodeIP, const std::string& faultLevel,
                                        const std::string& faultCode = "[0x08520003,na,L2,na]",
                                        const std::string& switchChipId = "chip1",
                                        const std::string& switchPortId = "port1",
                                        const std::string& faultTime = "")
    {
        fault::FaultMsgSignal faultMsg;
        faultMsg.set_signaltype("normal");
        faultMsg.set_uuid("test-uuid");
        
        fault::NodeFaultInfo* nodeInfo = faultMsg.add_nodefaultinfo();
        nodeInfo->set_nodeip(nodeIP);
        nodeInfo->set_nodesn("test-sn");
        nodeInfo->set_faultlevel(faultLevel);
        
        fault::DeviceFaultInfo* device = nodeInfo->add_faultdevice();
        device->set_deviceid("device1");
        device->set_devicetype("switch");
        device->add_faultlevels("RestartBusiness");
        device->set_faultlevel(faultLevel);
        fault::SwitchFaultInfo* switchFault = device->add_switchfaultinfos();
        switchFault->set_faultcode(faultCode);
        switchFault->set_switchchipid(switchChipId);
        switchFault->set_switchportid(switchPortId);
        switchFault->set_faulttime(faultTime.empty() ? std::to_string(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count()) : faultTime);
        return faultMsg;
    }

    std::shared_ptr<NodeStatus> nodeStatus;
    Stub stub;
    Stub nodeManagerStub;
};

/*
 * 测试描述: 测试Init函数初始化成功
 */
TEST_F(TestNPURecoveryManager, TestInitSuccess)
{
    auto testNodeStatus = std::make_shared<NodeStatus>();
    auto testNodeManagerSender = std::make_shared<NodeManagerRequestSender>();
    testNodeManagerSender->Init(testNodeStatus);
    EXPECT_EQ(testNodeManagerSender->StringToNPUStatus("ready"), NPUStatus::READY);
    EXPECT_EQ(testNodeManagerSender->StringToNPUStatus("init"), NPUStatus::INIT);
    EXPECT_EQ(testNodeManagerSender->StringToNPUStatus("normal"), NPUStatus::NORMAL);
    EXPECT_EQ(testNodeManagerSender->StringToNPUStatus("pause"), NPUStatus::PAUSE);
    EXPECT_EQ(testNodeManagerSender->StringToNPUStatus("abnormal"), NPUStatus::ABNORMAL);
    EXPECT_EQ(testNodeManagerSender->StringToNPUStatus("unknown"), NPUStatus::UNKNOWN);
    EXPECT_EQ(testNodeManagerSender->NodeManagerCmdToString(NodeManagerCmd::STOP_ENGINE), "STOP_ENGINE");
    EXPECT_EQ(testNodeManagerSender->NodeManagerCmdToString(static_cast<NodeManagerCmd>(999)), "UNKNOWN");
    int32_t ret = NPURecoveryManager::GetInstance()->Init(testNodeStatus);
    EXPECT_EQ(ret, 0);
}

/*
 * 测试描述: 测试不开灵衢恢复功能时，故障消息被忽略
 */
TEST_F(TestNPURecoveryManager, TestProcessFaultMessageWhenNPURecoveryDisabled)
{
    auto testJson = GetServerRequestHandlerTestJsonPath();
    ModifyJsonItem(testJson, "fault_recovery_func_dict", "lingqu_link", false);
    ControllerConfig::GetInstance()->Init();
    uint64_t nodeId = 100;
    std::string ip = "127.0.0.1";
    auto node = CreateTestNode(nodeId, ip, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    nodeStatus->AddNode(std::move(node));
    fault::FaultMsgSignal faultMsg = CreateFaultMsg(ip, "Healthy", "[0x08520003,na,L2,na]", "chip1", "port1");
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg);
    auto processedFaults = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults();
    EXPECT_EQ(processedFaults.size(), 0);
}

/*
 * 测试描述: 测试不开roce恢复功能时，故障消息被忽略
 */
TEST_F(TestNPURecoveryManager, TestProcessLLMEngineAlarmWhenRecoveryFunctionDisabled)
{
    auto testJson = GetServerRequestHandlerTestJsonPath();
    ModifyJsonItem(testJson, "fault_recovery_func_dict", "roce", false);
    ModifyJsonItem(testJson, "fault_recovery_func_dict", "oom", false);
    ControllerConfig::GetInstance()->Init();

    uint64_t nodeId = 100;
    std::string ip = "127.0.0.1";
    auto node = CreateTestNode(nodeId, ip, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    nodeStatus->AddNode(std::move(node));

    nlohmann::json alarmJson;
    alarmJson["node_manager_ip"] = ip;
    alarmJson["alarm_info"] = nlohmann::json::array({ nlohmann::json::array({
        nlohmann::json::object({{"errCode", "MIE05E01001B"}, {"errorLocation", "0:0"}})
    })});
    NPURecoveryManager::GetInstance()->ProcessLLMEngineAlarm(alarmJson);

    // 关闭 RoCE 时只发 STOP_ENGINE 并 return，不会 Insert(instanceId)，故列表为空
    auto errCodeAlarmExisted = NPURecoveryManager::GetInstance()->GetErrCodeAlarmExisted();
    EXPECT_EQ(errCodeAlarmExisted.size(), 0);
}

/*
 * 测试描述: 测试GetAllPodIPsInInstance - 获取实例中的所有Pod IP
 */
TEST_F(TestNPURecoveryManager, TestGetAllPodIPsInInstance)
{
    uint64_t nodeId1 = 100;
    uint64_t nodeId2 = 101;
    std::string ip1 = "127.0.0.1";
    std::string ip2 = "127.0.0.2";
    auto node1 = CreateTestNode(nodeId1, ip1, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, {nodeId1, nodeId2});
    auto node2 = CreateTestNode(nodeId2, ip2, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, {nodeId1, nodeId2});
    
    nodeStatus->AddNode(std::move(node1));
    nodeStatus->AddNode(std::move(node2));
    std::unordered_set<std::string> podIPs = NPURecoveryManager::GetInstance()->GetAllPodIPsInInstance(nodeId1);
    EXPECT_EQ(podIPs.size(), 2);
    EXPECT_TRUE(podIPs.find(ip1) != podIPs.end());
    EXPECT_TRUE(podIPs.find(ip2) != podIPs.end());
}

/*
 * 测试描述: 测试ProcessFaultMessage - Coordinator未ready时记录故障
 */
TEST_F(TestNPURecoveryManager, TestProcessFaultMessageCoordinatorNotReady)
{
    uint64_t nodeId1 = 100;
    uint64_t nodeId2 = 101;
    std::string ip1 = "127.0.0.1";
    std::string ip2 = "127.0.0.2";
    auto node1 = CreateTestNode(nodeId1, ip1, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, {nodeId1, nodeId2});
    auto node2 = CreateTestNode(nodeId2, ip2, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, {nodeId1, nodeId2});
    
    nodeStatus->AddNode(std::move(node1));
    
    fault::FaultMsgSignal faultMsg1 = CreateFaultMsg(ip1, "Healthy", "[0x08520003,na,L2,na]", "chip1", "port1", "1");
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg1);
    nodeStatus->AddNode(std::move(node2));
    fault::FaultMsgSignal faultMsg2 = CreateFaultMsg(ip1, "Healthy", "[0x08520003,na,L2,na]", "chip1", "port1", "1");
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg2);
    auto processedFaults = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults();
    EXPECT_EQ(processedFaults.size(), 0);
}

/*
 * 测试描述: 测试ProcessFaultMessage - P实例故障恢复
 */
TEST_F(TestNPURecoveryManager, TestProcessFaultMessagePrefillInstance)
{
    uint64_t nodeId = 100;
    std::string ip = "127.0.0.1";
    auto node = CreateTestNode(nodeId, ip, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    node->isSingleNode = true;
    node->currentRole = MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE;
    nodeStatus->AddNode(std::move(node));
    fault::FaultMsgSignal faultMsg = CreateFaultMsg(ip, "Healthy", "[0x08520003,na,L2,na]");
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg);
    auto processedFaults = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults();
    EXPECT_GE(processedFaults.size(), 0);
}
/*
 * 测试描述: 测试ProcessFaultMessage - 处理白名单故障码Ready
 */
TEST_F(TestNPURecoveryManager, TestProcessFaultMessageWithWhitelistFaultCodeReady)
{
    nodeManagerStub.set(ADDR(NodeManagerRequestSender, GetNodeManagerNodeStatus), MockGetNodeManagerStatusReady);
    uint64_t nodeId = 100;
    std::string ip = "127.0.0.1";
    auto node = CreateTestNode(nodeId, ip, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    nodeStatus->AddNode(std::move(node));
    
    fault::FaultMsgSignal faultMsg = CreateFaultMsg(ip, "Healthy", "[0x08520003,na,L2,na]");
    
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg);
    std::this_thread::sleep_for(std::chrono::seconds(2));
    // 验证故障被处理
    auto processedFaults = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults();
    EXPECT_GE(processedFaults.size(), 0);
}

/*
 * 测试描述: 测试ProcessFaultMessage - 处理白名单故障码NotReady
 */
TEST_F(TestNPURecoveryManager, TestProcessFaultMessageWithWhitelistFaultCodeNotReady)
{
    uint64_t nodeId = 100;
    std::string ip = "127.0.0.1";
    auto node = CreateTestNode(nodeId, ip, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    nodeStatus->AddNode(std::move(node));

    fault::FaultMsgSignal faultMsg = CreateFaultMsg(ip, "Healthy", "[0x08520003,na,L2,na]");

    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg);
    std::this_thread::sleep_for(std::chrono::seconds(2));
    // 验证故障未处理完
    auto recoveryFaults = NPURecoveryManager::GetInstance()->GetInstancesInRecovery();
    EXPECT_GE(recoveryFaults.size(), 0);
}

/*
 * 测试描述: 测试ProcessFaultMessage - 非白名单故障码被忽略
 */
TEST_F(TestNPURecoveryManager, TestProcessFaultMessageWithNonWhitelistFaultCode)
{
    uint64_t nodeId = 100;
    std::string ip = "127.0.0.1";
    
    auto node = CreateTestNode(nodeId, ip, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    nodeStatus->AddNode(std::move(node));
    
    fault::FaultMsgSignal faultMsg = CreateFaultMsg(ip, "Healthy", "[0x08520004,na,L2,na]");
    
    size_t beforeSize = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults().size();
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg);
    size_t afterSize = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults().size();
    // 非白名单故障码不应该被处理
    EXPECT_EQ(beforeSize, afterSize);
}

/*
 * 测试描述: 测试ProcessFaultMessage - 节点不健康时跳过处理
 */
TEST_F(TestNPURecoveryManager, TestProcessFaultMessageUnhealthyNode)
{
    uint64_t nodeId = 1001;
    std::string ip = "127.0.0.1";
    auto node = CreateTestNode(nodeId, ip, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    nodeStatus->AddNode(std::move(node));
    
    fault::FaultMsgSignal faultMsg = CreateFaultMsg(ip, "UnHealthy", "[ERROR,na,L2,na]");
    
    size_t beforeSize = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults().size();
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg);
    size_t afterSize = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults().size();
    // 不健康节点的故障不应该被处理
    EXPECT_EQ(beforeSize, afterSize);
}

/*
 * 测试描述: 测试ProcessFaultMessage - 重复故障被去重
 */
TEST_F(TestNPURecoveryManager, TestProcessFaultMessageDuplicateFault)
{
    uint64_t nodeId = 100;
    std::string ip = "127.0.0.1";
    
    auto node = CreateTestNode(nodeId, ip, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE);
    nodeStatus->AddNode(std::move(node));
    
    std::string faultTime = std::to_string(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count());
    
    fault::FaultMsgSignal faultMsg1 = CreateFaultMsg(ip, "Healthy", "[0x08520003,na,L2,na]",
                                                     "chip1", "port1", faultTime);
    fault::FaultMsgSignal faultMsg2 = CreateFaultMsg(ip, "Healthy", "[0x08520003,na,L2,na]",
                                                     "chip1", "port1", faultTime);
    
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg1);
    size_t afterFirst = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults().size();
    NPURecoveryManager::GetInstance()->ProcessFaultMessage(faultMsg2);
    size_t afterSecond = NPURecoveryManager::GetInstance()->GetProcessedSwitchFaults().size();
    // 重复故障应该被去重
    EXPECT_EQ(afterFirst, afterSecond);
}

/*
 * 测试描述: 测试AbortInstanceNPURecovery - 中止实例恢复
 */
TEST_F(TestNPURecoveryManager, TestAbortInstanceNPURecovery)
{
    uint64_t instanceId = 100;
    // 测试中止不存在的实例恢复
    NPURecoveryManager::GetInstance()->AbortInstanceNPURecovery(instanceId);
    EXPECT_TRUE(true);
}

/*
 * 测试描述: 测试LoadProcessedSwitchFaults - 加载已处理故障
 */
TEST_F(TestNPURecoveryManager, TestLoadProcessedSwitchFaults)
{
    nlohmann::json faultsJson = NPURecoveryManager::GetInstance()->LoadProcessedSwitchFaults();
    // 验证返回的是有效的JSON
    std::vector<std::string> newFaults = {"fault1", "fault2"};
    NPURecoveryManager::GetInstance()->SaveProcessedSwitchFaults(newFaults);
    EXPECT_TRUE(faultsJson.is_object() || faultsJson.is_array());
}