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
#include <memory>
#include <iostream>
#include <cstdlib>
#include <thread>
#include <nlohmann/json.hpp>
#include "gtest/gtest.h"
#include "stub.h"
#include "AlarmManager.h"
#include "AlarmConfig.h"
#include "AlarmRequestHandler.h"
#include "ControllerConfig.h"
#include "JsonFileManager.h"
#include "HttpClient.h"
#include "Helper.h"
#include "SharedMemoryUtils.h"
#include "IPCConfig.h"

using namespace MINDIE::MS;
const int CODE_OK = 200;

class TestAlarmManager : public testing::Test {
public:
    void SetUp() override
    {
        port = GetUnBindPort();
        std::string controllerJson = GetMSControllerConfigJsonPath();
        auto testJson = GetAlarmManagerTestJsonPath();
        CopyFile(controllerJson, testJson);
        JsonFileManager manager(testJson);
        manager.Load();
        manager.Set({"controller_alarm_port"}, port);
        manager.SetList({"tls_config", "alarm_tls_enable"}, false);
        manager.Save();
        ModifyJsonItem(testJson, "controller_alarm_port", "", port);
        setenv("MINDIE_MS_CONTROLLER_CONFIG_FILE_PATH", testJson.c_str(), 1);
        std::cout << "MINDIE_MS_CONTROLLER_CONFIG_FILE_PATH=" << testJson << std::endl;

        ControllerConfig::GetInstance()->Init();

        clientTlsItems.tlsEnable = false;
        waitSeconds = 5; // wait seconds
        retryTimes = 2; // retry times
    }

    nlohmann::json ReadAndVerifyAlarmJson(
        std::string jsonStr,
        MINDIE::MS::AlarmCategory expectedCategory,
        const std::string& expectedAlarmId,
        bool expectedCleared,
        const std::string& testName)
    {
        EXPECT_FALSE(jsonStr.empty()) << "Shared memory should not be empty for " << testName;

        nlohmann::json alarmJson = nlohmann::json::parse(jsonStr);
        EXPECT_TRUE(alarmJson.is_array() && !alarmJson.empty()) <<
        "Alarm JSON should be a non-empty array for " << testName;

        nlohmann::json record = alarmJson[0];
        EXPECT_EQ(record["category"], static_cast<int32_t>(expectedCategory)) <<
        "Category mismatch for " << testName;
        EXPECT_EQ(record["alarmId"], expectedAlarmId) << "Alarm ID mismatch for " << testName;
        EXPECT_EQ(record["cleared"], static_cast<int32_t>(expectedCleared)) <<
        "Cleared status mismatch for " << testName;
        return record;
    }
    std::unique_ptr<std::thread> mMainThread;
    TlsItems clientTlsItems;
    int waitSeconds;
    int retryTimes;
    int port;
};

class MockAlarmManager : public AlarmManager {
public:
    std::vector<std::string> alarmMessages; // 存储告警消息

    // 重写虚函数，改为向 vector 添加消息
    bool WriteAlarmToSHM(const std::string& alarmMsg) override
    {
        alarmMessages.push_back(alarmMsg);
        return true; // 模拟始终成功
    }
};

struct Params {
    TlsItems clientTlsItems;
    int waitSeconds;
    int retryTimes;
};

std::string FillAlarmJson(AlarmRecord& alarm)
{
    nlohmann::json alarmRecordsJ = nlohmann::json::array();
    nlohmann::json alarmRecordJ = nlohmann::json::object();

    const char* modelIDEnv = std::getenv("MODEL_ID");
    std::string modelID = (modelIDEnv != nullptr) ? modelIDEnv : "";
    alarm.nativeMeDn = modelID;

    alarm.originSystemType = "MindIE";
    alarm.originSystem = "MindIE";
    alarm.originSystemName = "MindIE";
    alarm.additionalInformation = alarm.additionalInformation + ", pod id=" + modelID;

    alarmRecordJ["category"] = alarm.category;
    alarmRecordJ["cleared"] = alarm.cleared;
    alarmRecordJ["clearCategory"] = alarm.clearCategory;
    alarmRecordJ["occurUtc"] = alarm.occurUtc;
    alarmRecordJ["occurTime"] = alarm.occurTime;
    alarmRecordJ["nativeMeDn"] = alarm.nativeMeDn;
    alarmRecordJ["originSystem"] = alarm.originSystem;
    alarmRecordJ["originSystemName"] = alarm.originSystemName;
    alarmRecordJ["originSystemType"] = alarm.originSystemType;
    alarmRecordJ["location"] = alarm.location;
    alarmRecordJ["moi"] = alarm.moi;
    alarmRecordJ["eventType"] = alarm.eventType;
    alarmRecordJ["alarmId"] = alarm.alarmId;
    alarmRecordJ["alarmName"] = alarm.alarmName;
    alarmRecordJ["severity"] = alarm.severity;
    alarmRecordJ["probableCause"] = alarm.probableCause;
    alarmRecordJ["reasonId"] = alarm.reasonId;
    alarmRecordJ["serviceAffectedType"] = alarm.serviceAffectedType;
    alarmRecordJ["additionalInformation"] = alarm.additionalInformation;

    alarmRecordsJ.emplace_back(alarmRecordJ);

    std::string jsonString = alarmRecordsJ.dump();
    return jsonString;
}
std::string FillCoordinatorExceptionAlarmInfoMock(AlarmCategory category,
    CoordinatorExceptionReason reasonID)
{
    AlarmRecord alarm;

    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.category = static_cast<int32_t>(category);
    if (category == AlarmCategory::ALARM_CATEGORY_ALARM) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    } else {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    }
    
    const char* podIpEnv = std::getenv("POD_IP");
    std::string podIp = (podIpEnv != nullptr) ? podIpEnv : "";
    std::string serviceLocation = "service name=Coordinator, service ip=" + podIp;
    
    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.additionalInformation = serviceLocation;

    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_QUALITY_OF_SERVICE);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_CRITICAL);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::REQ_CONGESTION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::REQ_CONGESTION);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::REQ_CONGESTION);

    return FillAlarmJson(alarm);
}

std::string FillCoordinatorExceptionAlarmInfoFail1Mock(AlarmCategory category,
    CoordinatorExceptionReason reasonID)
{
    AlarmRecord alarm;

    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.category = static_cast<int32_t>(category);
    if (category == AlarmCategory::ALARM_CATEGORY_ALARM) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    } else {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    }
    
    const char* podIpEnv = std::getenv("POD_IP");
    std::string podIp = (podIpEnv != nullptr) ? podIpEnv : "";
    std::string serviceLocation = "service name=Coordinator, service ip=" + podIp;
    
    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.additionalInformation = serviceLocation;

    alarm.eventType = static_cast<int32_t>(EventType::EVENT_TYPE_QUALITY_OF_SERVICE);
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_CRITICAL);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::COORDINATOR_EXCEPTION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::COORDINATOR_EXCEPTION);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::COORDINATOR_EXCEPTION);

    return FillAlarmJson(alarm);
}

std::string FillCoordinatorExceptionAlarmInfoFail2Mock(AlarmCategory category,
    CoordinatorExceptionReason reasonID)
{
    AlarmRecord alarm;

    alarm.reasonId = static_cast<int32_t>(reasonID);
    alarm.category = static_cast<int32_t>(category);
    if (category == AlarmCategory::ALARM_CATEGORY_ALARM) {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_NO);
    } else {
        alarm.cleared = static_cast<int32_t>(AlarmCleared::ALARM_CLEARED_YES);
    }
    
    const char* podIpEnv = std::getenv("POD_IP");
    std::string podIp = (podIpEnv != nullptr) ? podIpEnv : "";
    std::string serviceLocation = "service name=Coordinator, service ip=" + podIp;
    
    alarm.occurUtc = GetTimeStampNowInMillisec();
    alarm.occurTime = GetLocalTimesMillisec();

    alarm.location = serviceLocation;
    alarm.moi = serviceLocation;
    alarm.additionalInformation = serviceLocation;

    alarm.eventType = 0;
    alarm.severity = static_cast<int32_t>(AlarmSeverity::ALARM_SEVERITY_CRITICAL);
    alarm.serviceAffectedType = static_cast<int32_t>(ServiceAffectedType::SERVICE_AFFECTED_YES);
    alarm.clearCategory = static_cast<int32_t>(AlarmClearCategory::ALARM_CLEAR_CATEGORY_AUTO);

    alarm.alarmId = AlarmConfig::GetInstance()->GetAlarmIDString(AlarmType::COORDINATOR_EXCEPTION);
    alarm.alarmName = AlarmConfig::GetInstance()->GetAlarmNameString(AlarmType::COORDINATOR_EXCEPTION);
    alarm.probableCause = AlarmConfig::GetInstance()->GetProbableCauseString(AlarmType::COORDINATOR_EXCEPTION);

    return FillAlarmJson(alarm);
}

int SendHttpRequestMock(const std::string& ip,
                        const std::string& portStr,
                        const std::string& url,
                        const Params& params,
                        const std::string& alarmInfo)
{
    HttpClient httpClient;
    httpClient.Init(ip, portStr, params.clientTlsItems, true);

    std::string response;
    int32_t code;
    std::map<boost::beast::http::field, std::string> headers;
    headers[boost::beast::http::field::accept] = "*/*";
    headers[boost::beast::http::field::content_type] = "application/json";

    Request req = {url, boost::beast::http::verb::post, headers, alarmInfo};
    auto ret = httpClient.SendRequest(req, params.waitSeconds, params.retryTimes, response, code);

    nlohmann::json responseJsonObj = nlohmann::json::parse(response);

    // response 校验，校验是否为管理面健康检查接口返回
    if (ret == 0 && code == CODE_OK) {
        try {
            nlohmann::json responseJsonObj = nlohmann::json::parse(response);
            if (responseJsonObj.contains("message") && responseJsonObj.contains("status")) {
                return 0;
            } else {
                std::cerr << "JSON response validation failed: " << responseJsonObj.dump() << std::endl;
            }
        } catch (const nlohmann::json::parse_error& e) {
            std::cerr << "JSON parsing error: " << e.what() << std::endl;
        } catch (const std::exception& e) {
            std::cerr << "Exception: " << e.what() << std::endl;
        }
    }

    LOG_E("[SendHttpRequest] Get ip %s, port %s, url %s, ret %d", ip.c_str(), portStr.c_str(), url.c_str(), code);
    return -1;
}

/*
测试描述: AlarmManagerRunSuccess测试用例，默认不开启全零监听
测试步骤:
    1. 启动AlarmManager:
        - 期待AlarmManager.Init()返回0表示初始化成功
        - 启动服务器的运行线程，并detach()
        - 等待一段时间以确保AlarmManager完全启动
    2. 通过执行SendHttpRequestMock进行检查:
        - SendHttpRequestMock参数127.0.0.1 1026 /v1/startup并期待返回值为0
        - SendHttpRequestMock参数127.0.0.1 1026 /v1/health并期待返回值为0

预期结果:
    1. AlarmManager成功启动:
        - AlarmManager.Init()返回0
        - AlarmManager.Run()在新线程中运行
    2. SendHttpRequestMock的运行结果为0表示成功:
        - 运行SendHttpRequestMock 127.0.0.1 port /v1/startup成功
        - 运行SendHttpRequestMock 127.0.0.1 port /v1/health成功
*/
TEST_F(TestAlarmManager, AlarmManagerRunSuccess)
{
    ControllerConfig::GetInstance()->Init();
    MockAlarmManager AlarmManager;

    EXPECT_EQ(AlarmManager.Init(nullptr), 0);

    std::this_thread::sleep_for(std::chrono::seconds(3)); // wait time

    Params params;
    params.clientTlsItems = clientTlsItems;
    params.waitSeconds = waitSeconds;
    params.retryTimes = retryTimes;

    std::string controllerAlarmInfo = AlarmRequestHandler::GetInstance()->FillControllerToSlaveEventInfo(
        ControllerToSlaveReason::MASTER_CONTROLLER_EXCEPTION);
    AlarmManager.AlarmAdded(controllerAlarmInfo); // 原始告警直接入队
    std::this_thread::sleep_for(std::chrono::seconds(1));
    std::string jsonStr = AlarmManager.alarmMessages[0];
    ReadAndVerifyAlarmJson(
        jsonStr,
        AlarmCategory::ALARM_CATEGORY_EVENT,
        AlarmConfig::GetInstance()->GetAlarmIDString(
            AlarmType::CONTROLLER_TO_SLAVE),
        true,
        "ALARM_CATEGORY_EVENT");
    std::string coorAlarmStr = FillCoordinatorExceptionAlarmInfoMock(
        AlarmCategory::ALARM_CATEGORY_ALARM, CoordinatorExceptionReason::INSTANCE_MISSING);
    bool success1 =
        (SendHttpRequestMock("127.0.0.1", std::to_string(port), "/v1/alarm/coordinator", params, coorAlarmStr) == 0);
    // LLMEngine alarm 需要合法JSON格式: node_manager_ip + alarm_info (可为空数组)
    std::string llmEngineAlarmStr = R"({"node_manager_ip": "127.0.0.1", "alarm_info": []})";
    bool success2 = (SendHttpRequestMock("127.0.0.1", std::to_string(port), "/v1/alarm/llm_engine", params,
        llmEngineAlarmStr) == 0);
    
    EXPECT_TRUE(success1);
    EXPECT_TRUE(success2);
}

TEST_F(TestAlarmManager, CoordinatorAlarmSendFail)
{
    ControllerConfig::GetInstance()->Init();
    MockAlarmManager AlarmManager;

    EXPECT_EQ(AlarmManager.Init(nullptr), 0);

    std::this_thread::sleep_for(std::chrono::seconds(3)); // wait time

    Params params;
    params.clientTlsItems = clientTlsItems;
    params.waitSeconds = waitSeconds;
    params.retryTimes = retryTimes;

    std::string coorAlarmStr1 = FillCoordinatorExceptionAlarmInfoFail1Mock(
        AlarmCategory::ALARM_CATEGORY_ALARM, CoordinatorExceptionReason::INSTANCE_MISSING);
    std::string coorAlarmStr2 = FillCoordinatorExceptionAlarmInfoFail2Mock(
        AlarmCategory::ALARM_CATEGORY_ALARM, CoordinatorExceptionReason::INSTANCE_MISSING);
    bool fail1 =
        (SendHttpRequestMock("127.0.0.1", std::to_string(port), "/v1/alarm/coordinator", params, coorAlarmStr1) == 0);
    bool fail2 =
        (SendHttpRequestMock("127.0.0.1", std::to_string(port), "/v1/alarm/coordinator", params, coorAlarmStr2) == 0);
    
    EXPECT_FALSE(fail1);
    EXPECT_FALSE(fail2);
}