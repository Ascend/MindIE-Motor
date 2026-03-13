/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#ifndef MINDIE_MS_ALARM_CONFIG_H
#define MINDIE_MS_ALARM_CONFIG_H
#include <cstdint>
#include <string>
#include <memory>
#include <iostream>
#include <chrono>
#include <nlohmann/json.hpp>
#include "SharedMemoryUtils.h"
#include "Logger.h"
#include "IPCConfig.h"

namespace MINDIE::MS {
constexpr double DEFAULT_REQ_CONGESTION_TRIGGER_RATIO = 0.85;
constexpr double DEFAULT_REQ_CONGESTION_CLEAR_RATIO = 0.75;

enum class AlarmCategory : int32_t {
    ALARM_CATEGORY_ALARM = 1,   // 告警
    ALARM_CATEGORY_CLEAR,   // 清除
    ALARM_CATEGORY_EVENT,   // 事件
    ALARM_CATEGORY_LEVEL_CHANGE,   // 告警级别变更
    ALARM_CATEGORY_CONFIRM,   // 确认
    ALARM_CATEGORY_UNCONFIRM,   // 反确认
    ALARM_CATEGORY_OTHER_CHANGE   // 其他变更
};

enum class AlarmCleared : int32_t {
    ALARM_CLEARED_NO = 0,
    ALARM_CLEARED_YES = 1,
};

enum class AlarmClearCategory : int32_t {
    ALARM_CLEAR_CATEGORY_AUTO = 1,
    ALARM_CLEAR_CATEGORY_MANUAL = 2,
};

// 事件类型定义
enum class EventType : int32_t {
    EVENT_TYPE_COMMUNICATION = 1,    // 通信告警
    EVENT_TYPE_EQUIPMENT,    // 设备告警
    EVENT_TYPE_PROCESSING_ERROR,    // 处理错误告警
    EVENT_TYPE_QUALITY_OF_SERVICE,    // 业务质量告警
    EVENT_TYPE_ENVIRONMENTAL,    // 环境告警
    EVENT_TYPE_INTEGRITY_VIOLATION,    // 完整性告警
    EVENT_TYPE_OPERATIONAL,    // 操作告警
    EVENT_TYPE_PHYSICAL_VIOLATION,    // 物理资源告警
    EVENT_TYPE_SECURITY_VIOLATION,    // 安全告警
    EVENT_TYPE_TIME_DOMAIN,   // 时间域告警
    EVENT_TYPE_PROPERTY_CHANGE,   // 属性值改变
    EVENT_TYPE_OBJECT_CREATION,   // 对象创建
    EVENT_TYPE_OBJECT_DELETION,   // 对象删除
    EVENT_TYPE_RELATION_CHANGE,   // 关系改变
    EVENT_TYPE_STATE_CHANGE,   // 状态改变
    EVENT_TYPE_ROUTE_CHANGE,   // 路由改变
    EVENT_TYPE_PROTECTION_SWITCH,   // 保护倒换
    EVENT_TYPE_EXCEED_LIMIT,   // 越限
    EVENT_TYPE_FILE_TRANSFER_STATUS,   // 文件传输状态
    EVENT_TYPE_BACKUP_STATUS,   // 备份状态
    EVENT_TYPE_HEARTBEAT   // 心跳
};

enum class AlarmType : int32_t {
    CONTROLLER_TO_SLAVE,
    SERVICE_LEVEL_DEGRADATION,
    INSTANCE_EXCEPTION,
    SERVER_EXCEPTION,
    COORDINATOR_EXCEPTION,
    REQ_CONGESTION,
    CLUSTER_CONNECTION,
    COORDINATOR_CONN_PD_EXCEPTION,
    LLM_ENGINE_FAULT
};

enum class LLMEngineFaultReason : int32_t {
    TEXT_GENERATOR_OUT_OF_MEMORY = 1,
    HBM_MULTI_BIT_ERROR = 2,
    SIMULATE_FAILED = 3,
    NON_RECOVERABLE = 4
};

enum class ControllerToSlaveReason : int32_t {
    MASTER_CONTROLLER_EXCEPTION = 1,
};

enum class ScaleInReason : int32_t {
    INSTANCE_REDUCTION = 1,
};

enum class InstanceExceptionReason : int32_t {
    INSTANCE_EXCEPTION = 1,
};

enum class ServerExceptionReason : int32_t {
    SERVER_NO_REPLY = 1,
    SERVER_RESPONSE_ERROR,
    SERVER_REBOOT
};

enum class CoordinatorExceptionReason : int32_t {
    INSTANCE_MISSING = 1,
    COORDINATOR_EXCEPTION
};

enum class RequestCongestionReason : int32_t {
    DEALING_WITH_CONGESTION = 1,
};

enum class CoordinatorConnPDReason : int32_t {
    HTTP_EXCEPTION = 1,
};

enum class ClusterConnectionReason : int32_t {
    REGISTER_FAILED = 1,            // 注册失败
    RANKTABLE_SUBSCRIBE_FAILED = 2, // 订阅RankTable失败
    FAULT_SUBSCRIBE_FAILED = 3,     // 订阅故障消息失败
    CONNECTION_INTERRUPTED = 4      // 连接中断
};

// 告警级别定义
enum class AlarmSeverity : int32_t {
    ALARM_SEVERITY_CRITICAL = 1,   // 紧急
    ALARM_SEVERITY_MAJOR,   // 重要
    ALARM_SEVERITY_MINOR,   // 次要
    ALARM_SEVERITY_WARNING  // 提示
};

enum class ServiceAffectedType : int32_t {
    SERVICE_AFFECTED_NO = 0,
    SERVICE_AFFECTED_YES = 1,
};

struct AlarmRecord {
    int32_t category {};
    int32_t cleared = 0;
    int32_t clearCategory = 1;
    int64_t occurUtc {};
    int64_t occurTime {};
    std::string nativeMeDn {};
    std::string originSystem {};
    std::string originSystemName {};
    std::string originSystemType {};
    std::string location {};
    std::string moi {};
    int32_t eventType = 1;
    std::string alarmId {};
    std::string alarmName {};
    int32_t severity = 4;
    std::string probableCause {};
    int32_t reasonId = 0;
    int32_t serviceAffectedType = 0;
    std::string additionalInformation {};
    std::string matchKey {};
};

class AlarmConfig {
public:
    static AlarmConfig *GetInstance()
    {
        static AlarmConfig instance;
        return &instance;
    }
    std::string GetAlarmIDString(AlarmType alarmType);
    std::string GetAlarmNameString(AlarmType alarmType);
    std::string GetProbableCauseString(AlarmType alarmType);
   
    AlarmConfig(const AlarmConfig &obj) = delete;
    AlarmConfig &operator=(const AlarmConfig &obj) = delete;
    AlarmConfig(AlarmConfig &&obj) = delete;
    AlarmConfig &operator=(AlarmConfig &&obj) = delete;
private:
    AlarmConfig();
    ~AlarmConfig() = default;

    void InitAlarmID();
    void InitAlarmName();
    void InitProbableCause();

    std::unordered_map<AlarmType, std::string> mAlarmIDToString {};
    std::unordered_map<AlarmType, std::string> mAlarmNameToString = {};
    std::unordered_map<AlarmType, std::string> mProbableCauseToString = {};
};
} // MindIE::MS
#endif