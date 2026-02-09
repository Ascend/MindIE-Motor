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
#include <string>
#include <chrono>
#include <sstream>
#include "AlarmConfig.h"

namespace MINDIE::MS {

AlarmConfig::AlarmConfig()
{
    InitAlarmID();
    InitAlarmName();
    InitProbableCause();
}

void AlarmConfig::InitAlarmID()
{
    mAlarmIDToString[AlarmType::CONTROLLER_TO_SLAVE] = "0xFC001000";
    mAlarmIDToString[AlarmType::SERVICE_LEVEL_DEGRADATION] = "0xFC001001";
    mAlarmIDToString[AlarmType::INSTANCE_EXCEPTION] = "0xFC001002";
    mAlarmIDToString[AlarmType::SERVER_EXCEPTION] =  "0xFC001003";
    mAlarmIDToString[AlarmType::COORDINATOR_EXCEPTION] =  "0xFC001004";
    mAlarmIDToString[AlarmType::REQ_CONGESTION] =  "0xFC001005";
    mAlarmIDToString[AlarmType::CLUSTER_CONNECTION] = "0xFC001006";
    mAlarmIDToString[AlarmType::COORDINATOR_CONN_PD_EXCEPTION] = "0xFC001007";
}

void AlarmConfig::InitAlarmName()
{
    mAlarmNameToString[AlarmType::CONTROLLER_TO_SLAVE] = "Controller Master To Slave Alarm";
    mAlarmNameToString[AlarmType::SERVICE_LEVEL_DEGRADATION] = "Service Level Degradation Alarm";
    mAlarmNameToString[AlarmType::INSTANCE_EXCEPTION] = "Model Instance Exception Alarm";
    mAlarmNameToString[AlarmType::SERVER_EXCEPTION] = "MindIE Server Exception Alarm";
    mAlarmNameToString[AlarmType::COORDINATOR_EXCEPTION] = "Coordinator Service Exception Alarm";
    mAlarmNameToString[AlarmType::REQ_CONGESTION] = "Coordinator Request Congestion Alarm";
    mAlarmNameToString[AlarmType::CLUSTER_CONNECTION] = "Cluster Connection Exception Alarm";
    mAlarmNameToString[AlarmType::COORDINATOR_CONN_PD_EXCEPTION] =
 	        "Coordinator Connection To PD instance Exception Alarm";
}

void AlarmConfig::InitProbableCause()
{
    mProbableCauseToString[AlarmType::CONTROLLER_TO_SLAVE] = "1:软硬件故障导致原来的主Controller异常";
    mProbableCauseToString[AlarmType::SERVICE_LEVEL_DEGRADATION] = "1:软硬件故障,导致P或者D实例减少";
    mProbableCauseToString[AlarmType::INSTANCE_EXCEPTION] = "1:软硬件故障实例异常";
    mProbableCauseToString[AlarmType::SERVER_EXCEPTION] = "1:MindIE Server无响应;2:MindIE Server响应异常状态;"
                                                        "3:P实例或者D实例故障恢复重启主动:触发MindIE Server重启";
    mProbableCauseToString[AlarmType::COORDINATOR_EXCEPTION] = "1:无可用P或者D实例组;2:coordinator自身状态异常";
    mProbableCauseToString[AlarmType::REQ_CONGESTION] = "1:Coordinator正在处理的请求拥塞";
    mProbableCauseToString[AlarmType::CLUSTER_CONNECTION] = "1:集群服务连接失败;2:订阅RankTable失败;3:订阅故障消息失败;4:连接中断";
    mProbableCauseToString[AlarmType::COORDINATOR_CONN_PD_EXCEPTION] = "1:Coordinator与PD实例之间通信异常";
}

std::string AlarmConfig::GetAlarmIDString(AlarmType alarmType)
{
    auto iter = mAlarmIDToString.find(alarmType);
    if (iter == mAlarmIDToString.end()) {
        LOG_E("[%s] [AlarmConfig] The ID of alarm %d is not found.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::ALARM_REQUEST_HANDLER).c_str(),
            alarmType);
        return {};
    }
    return iter->second;
}

std::string AlarmConfig::GetAlarmNameString(AlarmType alarmType)
{
    auto iter = mAlarmNameToString.find(alarmType);
    if (iter == mAlarmNameToString.end()) {
        LOG_E("[%s] [AlarmConfig] The name of alarm %d is not found.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::ALARM_REQUEST_HANDLER).c_str(),
            alarmType);
        return {};
    }
    return iter->second;
}

std::string AlarmConfig::GetProbableCauseString(AlarmType alarmType)
{
    auto iter = mProbableCauseToString.find(alarmType);
    if (iter == mProbableCauseToString.end()) {
        LOG_E("[%s] [AlarmConfig] The probable cause of alarm %d is not found.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::ALARM_REQUEST_HANDLER).c_str(),
            alarmType);
        return {};
    }
    return iter->second;
}
}