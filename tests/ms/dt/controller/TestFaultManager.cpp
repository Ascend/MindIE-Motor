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
#include <nlohmann/json.hpp>
#include <gmock/gmock.h>
#include <gtest/gtest.h>
#include "stub.h"
#include "Helper.h"
#include "ControllerSendReqStub.h"
#include "RequestServerStub.h"
#include "ControllerConfig.h"
#include "NodeScheduler.h"
#include "StatusUpdater.h"
#include "ProcessManager.h"
#include "FaultManager.h"
#include "AlarmRequestHandler.h"
#include "SharedMemoryUtils.h"
#include "IPCConfig.h"
#include "RankTableLoader.h"
#include "ServerRequestHandler.h"
#include "FaultManager.cpp"

class TestFaultManager : public testing::Test {
public:
    static void SetUpTestSuite()
    {
        // 在所有测试开始前只运行一次
        std::cout << "===== Setting up Test Suite for TestFaultManager =====" << std::endl;
    }

    static void TearDownTestSuite()
    {
        // 在所有测试结束后只运行一次
        std::cout << "===== sTearing down Test Suite for TestFaultManager =====" << std::endl;
    }

    void SetUp() override
    {
        InitNodeStatus();
        InitFaultManager();
        stub.set(ADDR(HttpClient, SendRequest), &SendRequestMock);
    }

    void InitFaultManager()
    {
        faultManager = std::make_shared<FaultManager>();
        auto deployMode = ControllerConfig::GetInstance()->GetDeployMode();
        faultManager->Init(nodeStatus, deployMode);
        faultManager->isNeedWaitNpuProcessExit = false;
    }

    void InitNodeStatus()
    {
        nodeStatus = std::make_shared<NodeStatus>();
    }

    void SetupTestGroup(uint64_t groupId, const std::vector<uint64_t>& prefillNodes, const std::vector<uint64_t>& decodeNodes)
    {
        std::pair<std::vector<uint64_t>, std::vector<uint64_t>> group = std::make_pair(prefillNodes, decodeNodes);
        nodeStatus->AddGroup(groupId, group);
    }

    std::unique_ptr<NodeInfo> CreateTestNode(uint64_t nodeId, MINDIE::MS::DIGSInstanceRole role, uint64_t groupId)
    {
        auto node = std::make_unique<NodeInfo>();
        node->instanceInfo.staticInfo.id = nodeId;
        node->instanceInfo.staticInfo.role = role;
        node->instanceInfo.staticInfo.groupId = groupId;
        node->deleteTime = std::chrono::seconds(0);
        node->activePeers = {};
        node->peers = {};
        node->dpGroupPeers = {nodeId};
        return node;
    }

    std::shared_ptr<FaultManager> faultManager;
    std::shared_ptr<NodeStatus> nodeStatus;
    Stub stub;
};

/*
 * 测试描述: 测试SetRankTableLoader函数
 */
TEST_F(TestFaultManager, TestSetRankTableLoader)
{
    faultManager->SetRankTableLoader(NULL);
}

/*
 * 测试描述: 测试GetRankTableLoader函数。
 */
TEST_F(TestFaultManager, TestGetRankTableLoader)
{
    (void)faultManager->GetRankTableLoader();
}

/*
 * 测试描述: 测试GetGRTPdCnt函数当ranktableloader为空
 */
TEST_F(TestFaultManager, TestGetGRTPdCntWhenRankTableIsNull)
{
    int32_t prefillCnt = std::numeric_limits<int32_t>::min();
    int32_t decodeCnt = std::numeric_limits<int32_t>::min();

    faultManager->SetRankTableLoader(nullptr);
    GRT_PD_CNT pd_cnt = faultManager->GetGRTPdCnt();
    EXPECT_EQ(pd_cnt, std::make_pair(prefillCnt, decodeCnt));
}

/*
 * 测试描述: 测试GetGRTPdCnt函数ranktable非空
 */
TEST_F(TestFaultManager, TestGetGRTPdCntWhenRankTableNotNull)
{
    int32_t prefillCnt = 0;
    int32_t decodeCnt = 1;
    std::shared_ptr<RankTableLoader> loader = std::make_shared<RankTableLoader>();
    faultManager->SetRankTableLoader(loader);

    auto crossNodeRankTable = GetA3CrossNodeMSRankTableLoaderJsonPath();
    ChangeFileMode600(crossNodeRankTable);
    setenv("GLOBAL_RANK_TABLE_FILE_PATH", crossNodeRankTable.c_str(), 1);
    GRT_PD_CNT pd_cnt = faultManager->GetGRTPdCnt();
    EXPECT_EQ(pd_cnt, std::make_pair(prefillCnt, decodeCnt));
}

/*
 * 测试描述: 测试GetStaticElasticPdCnt函数。
 */
TEST_F(TestFaultManager, TestGetStaticElasticPdCnt)
{
    // 期望的PD实例个数，从静态扩缩容模板中读取
    int32_t prefillCnt = std::numeric_limits<int32_t>::min();
    int32_t decodeCnt = std::numeric_limits<int32_t>::min();

    STATIC_ELASTIC_PD_CNT expectedPDCnt = faultManager->GetStaticElasticPdCnt();
    EXPECT_EQ(expectedPDCnt, std::make_pair(prefillCnt, decodeCnt));
}

TEST_F(TestFaultManager, TestGetStaticElasticPdCntFromTestJson)
{
    // 期望的PD实例个数，从静态扩缩容模板中读取
    int32_t prefillCnt = 2;
    int32_t decodeCnt = 1;
    auto elasticScalingJson = GetElasticScalingJsonPath();
    ChangeFileMode(elasticScalingJson);
    ControllerConfig::GetInstance()->SetStaticElasticTemplatePath(elasticScalingJson);
    STATIC_ELASTIC_PD_CNT expectedPDCnt = faultManager->GetStaticElasticPdCnt();

    EXPECT_EQ(expectedPDCnt, std::make_pair(prefillCnt, decodeCnt));
}

TEST_F(TestFaultManager, TestInstanceLevelNonRedundantScaleIn)
{
    int32_t prefillCnt = std::numeric_limits<int32_t>::min();
    int32_t decodeCnt = std::numeric_limits<int32_t>::min();

    // 设置elasticjson路径
    auto elasticScalingJson = GetElasticScalingJsonPath();
    ChangeFileMode(elasticScalingJson);
    ControllerConfig::GetInstance()->SetStaticElasticTemplatePath(elasticScalingJson);

    // 设置ranktablejson路径
    auto crossNodeRankTable = GetA3CrossNodeMSRankTableLoaderJsonPath();
    ChangeFileMode600(crossNodeRankTable);
    setenv("GLOBAL_RANK_TABLE_FILE_PATH", crossNodeRankTable.c_str(), 1);

    faultManager->InstanceLevelNonRedundantScaleIn();
}

TEST_F(TestFaultManager, TestReleaseDpGroupPeersForNode)
{
    uint64_t groupId = 0;
    std::unique_ptr<NodeInfo> node = std::make_unique<NodeInfo>();
    node->dpGroupPeers.push_back(0);
    std::vector<uint64_t> para_a;
    para_a.push_back(0);
    std::vector<uint64_t> para_b;
    para_b.push_back(0);
    std::pair<std::vector<uint64_t>, std::vector<uint64_t>> group = std::make_pair(para_a, para_b);
    bool result = faultManager->ReleaseDpGroupPeersForNode(0, std::move(node), group);
    EXPECT_EQ(result, false);
}

TEST_F(TestFaultManager, TestHandleSoftwareFault)
{
    faultManager->HandleSoftwareFault(0, SoftwareFaultType::HTTP_COMM_FAILED); // 在没有注册过HTTP_COMM_FAILED事件handler
    int32_t ret = faultManager->RegisterSoftwareFaultHandler(SoftwareFaultType::HTTP_COMM_FAILED, nullptr);
    EXPECT_EQ(ret, -1);
    auto handler = [](std::shared_ptr<NodeStatus>, uint64_t) { return -1; };
    ret = faultManager->RegisterSoftwareFaultHandler(SoftwareFaultType::HTTP_COMM_FAILED, handler);
    EXPECT_EQ(ret, 0);
    ret = faultManager->RegisterSoftwareFaultHandler(SoftwareFaultType::HTTP_COMM_FAILED, handler);
    EXPECT_EQ(ret, -1);
    faultManager->HandleSoftwareFault(0, SoftwareFaultType::HTTP_COMM_FAILED); // 在注册过HTTP_COMM_FAILED事件handler时
}

TEST_F(TestFaultManager, TestHandleHardwareFault)
{
    faultManager->HandleHardwareFault(0, HardwareFaultType::SUBHEALTHY);
    int32_t ret = faultManager->RegisterHardwareFaultHandler(HardwareFaultType::SUBHEALTHY, nullptr);
    EXPECT_EQ(ret, -1);
    auto handler = [](std::shared_ptr<NodeStatus>, uint64_t) { return -1; };
    ret = faultManager->RegisterHardwareFaultHandler(HardwareFaultType::SUBHEALTHY, handler);
    EXPECT_EQ(ret, -1);
    faultManager->HandleHardwareFault(0, HardwareFaultType::SUBHEALTHY);
}

TEST_F(TestFaultManager, TestRecordSoftwareFaultyNode)
{
    faultManager->RecordSoftwareFaultyNode(0, SoftwareFaultType::HTTP_COMM_FAILED);
}

TEST_F(TestFaultManager, TestGetGroupActivePrefillInstanceCntWithEmptyGroup)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> emptyPrefillNodes = {};
    std::vector<uint64_t> emptyDecodeNodes = {};
    SetupTestGroup(groupId, emptyPrefillNodes, emptyDecodeNodes);
    int32_t result = faultManager->GetGroupActivePrefillInstanceCnt(groupId);
    EXPECT_EQ(result, 0);
}

TEST_F(TestFaultManager, TestGetGroupActivePrefillInstanceCntWithActiveInstances)
{
    uint64_t groupId = 1;
    uint64_t prefillNode1 = 100;
    uint64_t prefillNode2 = 101;
    std::vector<uint64_t> prefillNodes = {prefillNode1, prefillNode2};
    std::vector<uint64_t> decodeNodes = {200, 201};
    SetupTestGroup(groupId, prefillNodes, decodeNodes);
    
    auto node1 = CreateTestNode(prefillNode1, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    node1->activePeers = {200};
    node1->peers = {200, 201};
    nodeStatus->AddNode(std::move(node1));
    
    auto node2 = CreateTestNode(prefillNode2, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    node2->activePeers = {};
    node2->peers = {200, 201};
    nodeStatus->AddNode(std::move(node2));
    
    int32_t result = faultManager->GetGroupActivePrefillInstanceCnt(groupId);
    EXPECT_EQ(result, 1);
}

TEST_F(TestFaultManager, TestGetGroupActivePrefillInstanceCntWithNonExistentNodes)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> prefillNodes = {999};
    std::vector<uint64_t> decodeNodes = {};
    
    SetupTestGroup(groupId, prefillNodes, decodeNodes);
    
    int32_t result = faultManager->GetGroupActivePrefillInstanceCnt(groupId);
    EXPECT_EQ(result, 0);
}

TEST_F(TestFaultManager, TestReleasePrefillInstancesWithOnlyOneInstance)
{
    uint64_t groupId = 1;
    uint64_t prefillNode = 100;
    std::vector<uint64_t> prefillNodes = {prefillNode};
    std::vector<uint64_t> decodeNodes = {200};
    
    SetupTestGroup(groupId, prefillNodes, decodeNodes);
    
    auto node = CreateTestNode(prefillNode, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    node->activePeers = {200};
    nodeStatus->AddNode(std::move(node));
    
    int32_t result = faultManager->ReleasePrefillInstances(groupId);
    EXPECT_EQ(result, -1);
}

TEST_F(TestFaultManager, TestReleasePrefillInstancesWithMultipleInstances)
{
    uint64_t groupId = 1;
    uint64_t prefillNode1 = 100;
    uint64_t prefillNode2 = 101;
    std::vector<uint64_t> prefillNodes = {prefillNode1, prefillNode2};
    std::vector<uint64_t> decodeNodes = {200};
    
    SetupTestGroup(groupId, prefillNodes, decodeNodes);

    auto node1 = CreateTestNode(prefillNode1, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    node1->activePeers = {200};
    nodeStatus->AddNode(std::move(node1));
    
    auto node2 = CreateTestNode(prefillNode2, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    node2->activePeers = {200};
    nodeStatus->AddNode(std::move(node2));

    SetCode(500);
    SetRet(-1);
    
    int32_t result = faultManager->ReleasePrefillInstances(groupId);
    EXPECT_EQ(result, -1);

    SetCode(200);
    SetRet(0);

    result = faultManager->ReleasePrefillInstances(groupId);
    EXPECT_EQ(result, 0);
}

TEST_F(TestFaultManager, TestReleasePrefillInstancesWithNonExistentGroup)
{
    uint64_t nonExistentGroupId = 999;
    
    int32_t result = faultManager->ReleasePrefillInstances(nonExistentGroupId);
    EXPECT_EQ(result, -1);
}

TEST_F(TestFaultManager, TestProcessScaleOutInSingleModeWithEmptyChanges)
{
    std::vector<std::unique_ptr<NodeInfo>> serverNodes;
    NodeChanges nodeChanges;
    
    faultManager->ProcessScaleOutInSingleMode(serverNodes, nodeChanges);
    
    auto allNodes = nodeStatus->GetAllNodes();
    EXPECT_TRUE(allNodes.empty());
}

TEST_F(TestFaultManager, TestProcessScaleOutInSingleModeWithNewNodes)
{
    std::vector<std::unique_ptr<NodeInfo>> serverNodes;
    NodeChanges nodeChanges;
    
    uint64_t newNodeId = 100;
    nodeChanges.newIDs = {newNodeId};

    auto newNode = CreateTestNode(newNodeId, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, 1);
    newNode->ip = "192.168.1.100";
    newNode->port = "8080";
    serverNodes.push_back(std::move(newNode));
    
    faultManager->ProcessScaleOutInSingleMode(serverNodes, nodeChanges);

    auto allNodes = nodeStatus->GetAllNodes();
    EXPECT_TRUE(allNodes.empty());

    SetCode(200);
    SetRet(0);

    faultManager->ProcessScaleOutInSingleMode(serverNodes, nodeChanges);

    allNodes = nodeStatus->GetAllNodes();
}

/*
 * 测试描述: 测试AddInstance2Group函数，当可用实例列表为空时扩容P实例
 * 期望: 返回扩容目标组的groupId, 待扩容P实例Node不需要纠错
 */
TEST_F(TestFaultManager, TestAddInstance2GroupWithEmptyGroup)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> allGroupIds = {groupId};
    
    SetupTestGroup(groupId, {}, {});
    
    // 构造待扩容的P实例
    auto scaleOutPNode = CreateTestNode(711, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    scaleOutPNode->serverInfoList = std::vector<ServerInfo>(1);
    scaleOutPNode->serverInfoList[0].superPodId = std::nullopt; // 硬件类型为A2

    // 填充groupIfAvailServersAdded
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    availableServers.push_back(CreateTestNode(711, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId));
    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(allGroupIds, availableServers, groupIfAvailServersAdded);
    
    uint64_t result = faultManager->AddInstance2Group(*scaleOutPNode, allGroupIds, groupIfAvailServersAdded);
    EXPECT_EQ(result, groupId);
}

/*
 * 测试描述: 测试AddInstance2Group函数，当可用实例列表非空(0D1P)时扩容P实例
 * 期望: 返回NOT_SCALE_OUT, 待扩容P实例Node不存在对端, 需要纠错
 */
TEST_F(TestFaultManager, TestAddInstance2GroupWithNoPeer)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> allGroupIds = {groupId};
    
    SetupTestGroup(groupId, {1044}, {});
    
    // 构造待扩容的P实例
    auto scaleOutPNode = CreateTestNode(711, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    scaleOutPNode->serverInfoList = std::vector<ServerInfo>(1);
    scaleOutPNode->serverInfoList[0].superPodId = std::nullopt; // 硬件类型为A2

    // 填充groupIfAvailServersAdded
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    availableServers.push_back(CreateTestNode(711, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId));
    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(allGroupIds, availableServers, groupIfAvailServersAdded);
    
    uint64_t result = faultManager->AddInstance2Group(*scaleOutPNode, allGroupIds, groupIfAvailServersAdded);
    EXPECT_EQ(result, NOT_SCALE_OUT);
}

/*
 * 测试描述: 测试AddInstance2Group函数，当可用实例列表非空(0D1P)时扩容未定义实例
 * 期望: 返回NOT_SCALE_OUT, 未定义的实例Node被视为P实例, 没有对端, 需要纠错
 */
TEST_F(TestFaultManager, TestAddInstance2GroupWithUndefInstance)
{
    uint64_t groupId = 0; // Undefined的实例groupId默认为0
    std::vector<uint64_t> allGroupIds = {groupId};
    
    SetupTestGroup(groupId, {1044}, {});
    
    // 构造待扩容的未定义实例
    auto scaleOutNoDefinedNode = CreateTestNode(933, MINDIE::MS::DIGSInstanceRole::UN_DEF_INSTANCE, groupId);
    scaleOutNoDefinedNode->serverInfoList = std::vector<ServerInfo>(1);
    scaleOutNoDefinedNode->serverInfoList[0].superPodId = std::nullopt; // 硬件类型为A2

    // 填充groupIfAvailServersAdded
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    availableServers.push_back(CreateTestNode(933, MINDIE::MS::DIGSInstanceRole::UN_DEF_INSTANCE, groupId));
    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(allGroupIds, availableServers, groupIfAvailServersAdded);
    
    uint64_t result = faultManager->AddInstance2Group(*scaleOutNoDefinedNode, allGroupIds, groupIfAvailServersAdded);
    EXPECT_EQ(result, NOT_SCALE_OUT);
}

/*
 * 测试描述: 测试AddInstance2Group函数，当可用实例列表非空(1D1P)时扩容P实例
 * 期望: 返回扩容目标组的groupId, 待扩容P实例Node存在对端, 不需要纠错
 */
TEST_F(TestFaultManager, TestAddInstance2GroupWithPeerExists)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> allGroupIds = {groupId};
    
    SetupTestGroup(groupId, {1044}, {1155});
    
    // 构造待扩容的P实例
    auto scaleOutPNode = CreateTestNode(711, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    scaleOutPNode->serverInfoList = std::vector<ServerInfo>(1);
    scaleOutPNode->serverInfoList[0].superPodId = std::nullopt; // 硬件类型为A2
    
    // 填充groupIfAvailServersAdded
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    availableServers.push_back(CreateTestNode(711, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId));
    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(allGroupIds, availableServers, groupIfAvailServersAdded);
    
    uint64_t result = faultManager->AddInstance2Group(*scaleOutPNode, allGroupIds, groupIfAvailServersAdded);
    EXPECT_EQ(result, groupId);
}

/*
 * 测试描述: 测试AddInstance2Group函数，当可用实例列表非空(0D1P)时同时扩容1D1P实例
 * 期望: 返回扩容目标组的groupId, 已就绪的1D1P实例Node均存在对端, 均不需要纠错
 */
TEST_F(TestFaultManager, TestAddInstance2GroupWithBothPDInstances)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> allGroupIds = {groupId};
    
    SetupTestGroup(groupId, {1044}, {});
    
    // 构造待扩容的P实例和D实例
    auto scaleOutPNode = CreateTestNode(711, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    scaleOutPNode->serverInfoList = std::vector<ServerInfo>(1);
    scaleOutPNode->serverInfoList[0].superPodId = std::make_optional<std::string>("1000"); // 硬件类型为A3
    auto scaleOutDNode = CreateTestNode(822, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, groupId);
    scaleOutDNode->serverInfoList = std::vector<ServerInfo>(1);
    scaleOutDNode->serverInfoList[0].superPodId = std::make_optional<std::string>("1000"); // 硬件类型为A3
    
    // 填充groupIfAvailServersAdded
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    availableServers.push_back(CreateTestNode(711, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId));
    availableServers.push_back(CreateTestNode(822, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, groupId));
    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(allGroupIds, availableServers, groupIfAvailServersAdded);
    
    uint64_t resultP = faultManager->AddInstance2Group(*scaleOutPNode, allGroupIds, groupIfAvailServersAdded);
    uint64_t resultD = faultManager->AddInstance2Group(*scaleOutDNode, allGroupIds, groupIfAvailServersAdded);
    
    EXPECT_EQ(resultP, groupId);
    EXPECT_EQ(resultD, groupId);
}

TEST_F(TestFaultManager, TestSelectGroup2ReleaseInstance)
{
    int32_t ret = faultManager->SelectGroup2ReleaseInstance(1000000);
    EXPECT_EQ(ret, -1);

    uint64_t groupId = 0;
    SetupTestGroup(groupId, {}, {});

    ret = faultManager->SelectGroup2ReleaseInstance(0);
    EXPECT_EQ(ret, -1);

    groupId = 1;
    uint64_t prefillNode1 = 100;
    uint64_t prefillNode2 = 101;
    std::vector<uint64_t> prefillNodes = {prefillNode1, prefillNode2};
    std::vector<uint64_t> decodeNodes = {200};

    SetupTestGroup(groupId, prefillNodes, decodeNodes);

    auto node1 = CreateTestNode(prefillNode1, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    node1->activePeers = {200};
    nodeStatus->AddNode(std::move(node1));
    
    auto node2 = CreateTestNode(prefillNode2, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    node2->activePeers = {200};
    nodeStatus->AddNode(std::move(node2));

    SetCode(200);
    SetRet(0);

    ret = faultManager->SelectGroup2ReleaseInstance(0);
    EXPECT_EQ(ret, 0);
}

/*
 * 测试描述: 测试PreAddAvailableServers2Group函数，当availableServers为空时
 */
TEST_F(TestFaultManager, TestPreAddAvailableServers2GroupWithEmptyServers)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> prefillNodes = {100};
    std::vector<uint64_t> decodeNodes = {200};
    SetupTestGroup(groupId, prefillNodes, decodeNodes);

    std::vector<uint64_t> groupIds = {groupId};
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;

    faultManager->PreAddAvailableServers2Group(groupIds, availableServers, groupIfAvailServersAdded);

    // 验证：空的availableServers不会改变组内节点数量
    EXPECT_EQ(groupIfAvailServersAdded[groupId].first, 1);  // PNodeNum = 1
    EXPECT_EQ(groupIfAvailServersAdded[groupId].second, 1); // DNodeNum = 1
}

/*
 * 测试描述: 测试PreAddAvailableServers2Group函数，当添加PREFILL实例时
 */
TEST_F(TestFaultManager, TestPreAddAvailableServers2GroupWithPrefillInstance)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> prefillNodes = {100};
    std::vector<uint64_t> decodeNodes = {200};
    SetupTestGroup(groupId, prefillNodes, decodeNodes);

    std::vector<uint64_t> groupIds = {groupId};
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    
    // 添加一个PREFILL实例
    auto prefillServer = CreateTestNode(101, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    availableServers.push_back(std::move(prefillServer));

    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(groupIds, availableServers, groupIfAvailServersAdded);

    // 验证：添加一个PREFILL实例后，PNodeNum应该增加1
    EXPECT_EQ(groupIfAvailServersAdded[groupId].first, 2);  // PNodeNum = 1 + 1 = 2
    EXPECT_EQ(groupIfAvailServersAdded[groupId].second, 1); // DNodeNum = 1
}

/*
 * 测试描述: 测试PreAddAvailableServers2Group函数，当添加DECODE实例时
 */
TEST_F(TestFaultManager, TestPreAddAvailableServers2GroupWithDecodeInstance)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> prefillNodes = {100};
    std::vector<uint64_t> decodeNodes = {200};
    SetupTestGroup(groupId, prefillNodes, decodeNodes);

    std::vector<uint64_t> groupIds = {groupId};
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    
    // 添加一个DECODE实例
    auto decodeServer = CreateTestNode(201, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, groupId);
    availableServers.push_back(std::move(decodeServer));

    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(groupIds, availableServers, groupIfAvailServersAdded);

    // 验证：添加一个DECODE实例后，DNodeNum应该增加1
    EXPECT_EQ(groupIfAvailServersAdded[groupId].first, 1);  // PNodeNum = 1
    EXPECT_EQ(groupIfAvailServersAdded[groupId].second, 2); // DNodeNum = 1 + 1 = 2
}

/*
 * 测试描述: 测试PreAddAvailableServers2Group函数，当添加UN_DEF_INSTANCE实例时
 */
TEST_F(TestFaultManager, TestPreAddAvailableServers2GroupWithUndefInstance)
{
    uint64_t groupId = 0; // Undefined的实例groupId默认为0
    std::vector<uint64_t> prefillNodes = {100};
    std::vector<uint64_t> decodeNodes = {200};
    SetupTestGroup(groupId, prefillNodes, decodeNodes);

    std::vector<uint64_t> groupIds = {groupId};
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    
    // 添加一个UN_DEF_INSTANCE实例（未定义角色的实例会被当作PREFILL处理）
    auto undefServer = CreateTestNode(102, MINDIE::MS::DIGSInstanceRole::UN_DEF_INSTANCE, groupId);
    availableServers.push_back(std::move(undefServer));

    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(groupIds, availableServers, groupIfAvailServersAdded);

    // 验证：UN_DEF_INSTANCE被当作PREFILL处理，PNodeNum应该增加1
    EXPECT_EQ(groupIfAvailServersAdded[groupId].first, 2);  // PNodeNum = 1 + 1 = 2
    EXPECT_EQ(groupIfAvailServersAdded[groupId].second, 1); // DNodeNum = 1
}

/*
 * 测试描述: 测试PreAddAvailableServers2Group函数，当添加混合实例（PREFILL和DECODE）时
 */
TEST_F(TestFaultManager, TestPreAddAvailableServers2GroupWithMixedInstances)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> prefillNodes = {100};
    std::vector<uint64_t> decodeNodes = {200};
    SetupTestGroup(groupId, prefillNodes, decodeNodes);

    std::vector<uint64_t> groupIds = {groupId};
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    
    // 添加1个PREFILL实例和2个DECODE实例
    auto prefillServer = CreateTestNode(101, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    auto decodeServer1 = CreateTestNode(201, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, groupId);
    auto decodeServer2 = CreateTestNode(202, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, groupId);
    availableServers.push_back(std::move(prefillServer));
    availableServers.push_back(std::move(decodeServer1));
    availableServers.push_back(std::move(decodeServer2));

    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(groupIds, availableServers, groupIfAvailServersAdded);

    // 验证：添加1个PREFILL和2个DECODE后
    EXPECT_EQ(groupIfAvailServersAdded[groupId].first, 2);  // PNodeNum = 1 + 1 = 2
    EXPECT_EQ(groupIfAvailServersAdded[groupId].second, 3); // DNodeNum = 1 + 2 = 3
}

/*
 * 测试描述: 测试PreAddAvailableServers2Group函数，当存在空指针server时
 */
TEST_F(TestFaultManager, TestPreAddAvailableServers2GroupWithNullServer)
{
    uint64_t groupId = 1;
    std::vector<uint64_t> prefillNodes = {100};
    std::vector<uint64_t> decodeNodes = {200};
    SetupTestGroup(groupId, prefillNodes, decodeNodes);

    std::vector<uint64_t> groupIds = {groupId};
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    
    // 添加一个空指针和一个有效的PREFILL实例
    availableServers.push_back(nullptr);
    auto prefillServer = CreateTestNode(101, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    availableServers.push_back(std::move(prefillServer));

    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(groupIds, availableServers, groupIfAvailServersAdded);

    // 验证：空指针server会被跳过，只有有效的PREFILL实例被计入
    EXPECT_EQ(groupIfAvailServersAdded[groupId].first, 2);  // PNodeNum = 1 + 1 = 2
    EXPECT_EQ(groupIfAvailServersAdded[groupId].second, 1); // DNodeNum = 1
}

/*
 * 测试描述: 测试PreAddAvailableServers2Group函数，当group初始为空时
 */
TEST_F(TestFaultManager, TestPreAddAvailableServers2GroupWithEmptyGroup)
{
    uint64_t groupId = 1;
    SetupTestGroup(groupId, {}, {});  // 空的group

    std::vector<uint64_t> groupIds = {groupId};
    std::vector<std::unique_ptr<NodeInfo>> availableServers;
    
    // 添加1个PREFILL实例和1个DECODE实例
    auto prefillServer = CreateTestNode(100, MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, groupId);
    auto decodeServer = CreateTestNode(200, MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE, groupId);
    availableServers.push_back(std::move(prefillServer));
    availableServers.push_back(std::move(decodeServer));

    std::map<uint64_t, std::pair<uint32_t, uint32_t>> groupIfAvailServersAdded;
    faultManager->PreAddAvailableServers2Group(groupIds, availableServers, groupIfAvailServersAdded);

    // 验证：从空group开始，添加1个P和1个D
    EXPECT_EQ(groupIfAvailServersAdded[groupId].first, 1);  // PNodeNum = 0 + 1 = 1
    EXPECT_EQ(groupIfAvailServersAdded[groupId].second, 1); // DNodeNum = 0 + 1 = 1
}