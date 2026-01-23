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
#include <string>
#include <vector>
#include "gtest/gtest.h"
#include "SecurityUtils.h"
#include "Util.h"
#include "nlohmann/json.hpp"

using namespace MINDIE::MS;
using Json = nlohmann::json;

class TestSecurityUtils : public testing::Test {
protected:
    void SetUp() override {
        // Setup code if needed
    }

    void TearDown() override {
        // Cleanup code if needed
    }
};

/*
测试描述: 测试IsStringSafeForJson函数
测试步骤:
    1. 测试正常字符串（字母、数字、空格）
    2. 测试允许的控制字符（制表符、换行符、回车符）
    3. 测试不允许的控制字符
    4. 测试空字符串
    5. 测试边界值
预期结果:
    1. 正常字符串返回true
    2. 允许的控制字符返回true
    3. 不允许的控制字符返回false
    4. 空字符串返回true
    5. 边界值正确处理
*/
TEST_F(TestSecurityUtils, TestIsStringSafeForJson)
{
    // 测试正常字符串
    EXPECT_TRUE(IsStringSafeForJson("Hello World 123"));
    EXPECT_TRUE(IsStringSafeForJson("abcdefghijklmnopqrstuvwxyz"));
    EXPECT_TRUE(IsStringSafeForJson("ABCDEFGHIJKLMNOPQRSTUVWXYZ"));
    EXPECT_TRUE(IsStringSafeForJson("0123456789"));
    EXPECT_TRUE(IsStringSafeForJson("!@#$%^&*()_+-=[]{}|;':\",./<>?"));

    // 测试允许的控制字符
    EXPECT_TRUE(IsStringSafeForJson("Hello\tWorld"));  // 制表符 0x09
    EXPECT_TRUE(IsStringSafeForJson("Hello\nWorld"));  // 换行符 0x0A
    EXPECT_TRUE(IsStringSafeForJson("Hello\rWorld"));  // 回车符 0x0D
    EXPECT_TRUE(IsStringSafeForJson("Hello\t\n\rWorld"));

    // 测试不允许的控制字符
    EXPECT_FALSE(IsStringSafeForJson(std::string("Hello\x00World", 11)));  // NULL 0x00
    EXPECT_FALSE(IsStringSafeForJson(std::string("Hello\x01World", 11)));  // SOH 0x01
    EXPECT_FALSE(IsStringSafeForJson(std::string("Hello\x08World", 11)));  // BS 0x08
    EXPECT_FALSE(IsStringSafeForJson(std::string("Hello\x0BWorld", 11)));  // VT 0x0B
    EXPECT_FALSE(IsStringSafeForJson(std::string("Hello\x0CWorld", 11)));  // FF 0x0C
    EXPECT_FALSE(IsStringSafeForJson(std::string("Hello\x0EWorld", 11)));  // SO 0x0E
    EXPECT_FALSE(IsStringSafeForJson(std::string("Hello\x1FWorld", 11)));  // US 0x1F

    // 测试空字符串
    EXPECT_TRUE(IsStringSafeForJson(""));

    // 测试边界值
    EXPECT_TRUE(IsStringSafeForJson(std::string(1, 0x20)));   // 空格字符
    EXPECT_FALSE(IsStringSafeForJson(std::string(1, 0x1F)));  // 边界控制字符
}

/*
测试描述: 测试SanitizeStringForJson函数
测试步骤:
    1. 测试需要转义的特殊字符
    2. 测试控制字符转换为\u00XX格式
    3. 测试正常字符保持不变
    4. 测试空字符串
    5. 测试混合字符串
预期结果:
    1. 特殊字符正确转义
    2. 控制字符转换为Unicode转义序列
    3. 正常字符不变
    4. 空字符串返回空字符串
    5. 混合字符串正确处理
*/
TEST_F(TestSecurityUtils, TestSanitizeStringForJson)
{
    // 测试需要转义的特殊字符
    EXPECT_EQ(SanitizeStringForJson("\""), "\\\"");
    EXPECT_EQ(SanitizeStringForJson("\\"), "\\\\");
    EXPECT_EQ(SanitizeStringForJson("\b"), "\\b");
    EXPECT_EQ(SanitizeStringForJson("\f"), "\\f");
    EXPECT_EQ(SanitizeStringForJson("\n"), "\\n");
    EXPECT_EQ(SanitizeStringForJson("\r"), "\\r");
    EXPECT_EQ(SanitizeStringForJson("\t"), "\\t");

    // 测试其他控制字符转换为\u00XX格式
    EXPECT_EQ(SanitizeStringForJson(std::string(1, 0x00)), "\\u0000");
    EXPECT_EQ(SanitizeStringForJson(std::string(1, 0x01)), "\\u0001");

    EXPECT_EQ(SanitizeStringForJson(std::string(1, 0x0B)), "\\u000b");
    EXPECT_EQ(SanitizeStringForJson(std::string(1, 0x0E)), "\\u000e");
    EXPECT_EQ(SanitizeStringForJson(std::string(1, 0x1F)), "\\u001f");

    // 测试正常字符保持不变
    EXPECT_EQ(SanitizeStringForJson("Hello World"), "Hello World");
    EXPECT_EQ(SanitizeStringForJson("123456789"), "123456789");
    EXPECT_EQ(SanitizeStringForJson("abcXYZ"), "abcXYZ");

    // 测试空字符串
    EXPECT_EQ(SanitizeStringForJson(""), "");

    // 测试混合字符串
    EXPECT_EQ(SanitizeStringForJson("Hello\"World\n"), "Hello\\\"World\\n");
    EXPECT_EQ(SanitizeStringForJson("Test\t\r\b"), "Test\\t\\r\\b");

    const char mixed_chars[] = {'A', '\x00', 'B', '\x01', 'C'};
    std::string mixed_str(mixed_chars, sizeof(mixed_chars));
    EXPECT_EQ(SanitizeStringForJson(mixed_str), "A\\u0000B\\u0001C");
}

/*
测试描述: 测试ValidateAndSanitizeIP函数
测试步骤:
    1. 测试有效IPv4地址
    2. 测试无效IPv4地址
    3. 测试IPv6地址
    4. 测试边界值
    5. 测试空字符串和格式错误
预期结果:
    1. 有效IPv4地址返回清理后的地址
    2. 无效IPv4地址返回空字符串
    3. IPv6地址正确处理
    4. 边界值正确处理
    5. 错误输入返回空字符串
*/
TEST_F(TestSecurityUtils, TestValidateAndSanitizeIP)
{
    // 测试有效IPv4地址
    EXPECT_EQ(ValidateAndSanitizeIP("127.0.0.1"), "127.0.0.1");
    EXPECT_EQ(ValidateAndSanitizeIP("192.168.1.1"), "192.168.1.1");
    EXPECT_EQ(ValidateAndSanitizeIP("10.0.0.1"), "10.0.0.1");
    EXPECT_EQ(ValidateAndSanitizeIP("172.16.0.1"), "172.16.0.1");
    EXPECT_EQ(ValidateAndSanitizeIP("255.255.255.255"), "255.255.255.255");
    EXPECT_EQ(ValidateAndSanitizeIP("1.1.1.1"), "1.1.1.1");

    // 测试无效IPv4地址
    EXPECT_EQ(ValidateAndSanitizeIP("256.256.256.256"), "");
    EXPECT_EQ(ValidateAndSanitizeIP("192.168.1"), "");
    EXPECT_EQ(ValidateAndSanitizeIP("192.168.1.1.1"), "");
    EXPECT_EQ(ValidateAndSanitizeIP("192.168.-1.1"), "");
    EXPECT_EQ(ValidateAndSanitizeIP("192.168.abc.1"), "");
    EXPECT_EQ(ValidateAndSanitizeIP("192.168..1"), "");
    EXPECT_EQ(ValidateAndSanitizeIP(".192.168.1.1"), "");
    EXPECT_EQ(ValidateAndSanitizeIP("192.168.1.1."), "");

    // 测试IPv6地址（基本格式）
    EXPECT_NE(ValidateAndSanitizeIP("2001:0db8:85a3:0000:0000:8a2e:0370:7334"), "");
    EXPECT_NE(ValidateAndSanitizeIP("2001:db8:85a3::8a2e:370:7334"), "");
    EXPECT_EQ(ValidateAndSanitizeIP("invalid:ipv6:address"), "");

    // 测试边界值
    EXPECT_EQ(ValidateAndSanitizeIP("0.0.0.1"), "0.0.0.1");
    EXPECT_EQ(ValidateAndSanitizeIP("255.0.0.0"), "255.0.0.0");

    // 测试空字符串和格式错误
    EXPECT_EQ(ValidateAndSanitizeIP(""), "");
    EXPECT_EQ(ValidateAndSanitizeIP("not.an.ip.address"), "");
    EXPECT_EQ(ValidateAndSanitizeIP("123456"), "");
    EXPECT_EQ(ValidateAndSanitizeIP("..."), "");
}

/*
测试描述: 测试ValidateAndSanitizeDeviceId函数
测试步骤:
    1. 测试有效设备ID
    2. 测试无效字符
    3. 测试空字符串
    4. 测试边界情况
预期结果:
    1. 有效设备ID返回清理后的ID
    2. 包含无效字符返回空字符串
    3. 空字符串返回空字符串
    4. 边界情况正确处理
*/
TEST_F(TestSecurityUtils, TestValidateAndSanitizeDeviceId)
{
    // 测试有效设备ID
    EXPECT_EQ(ValidateAndSanitizeDeviceId("device123"), "device123");
    EXPECT_EQ(ValidateAndSanitizeDeviceId("DEVICE_123"), "DEVICE_123");
    EXPECT_EQ(ValidateAndSanitizeDeviceId("device-123"), "device-123");
    EXPECT_EQ(ValidateAndSanitizeDeviceId("Device_ID-123"), "Device_ID-123");
    EXPECT_EQ(ValidateAndSanitizeDeviceId("a1b2c3"), "a1b2c3");
    EXPECT_EQ(ValidateAndSanitizeDeviceId("ABC123XYZ"), "ABC123XYZ");

    // 测试无效字符
    EXPECT_EQ(ValidateAndSanitizeDeviceId("device 123"), "");  // 空格
    EXPECT_EQ(ValidateAndSanitizeDeviceId("device@123"), "");  // @符号
    EXPECT_EQ(ValidateAndSanitizeDeviceId("device.123"), "");  // 点号
    EXPECT_EQ(ValidateAndSanitizeDeviceId("device#123"), "");  // #符号
    EXPECT_EQ(ValidateAndSanitizeDeviceId("device$123"), "");  // $符号
    EXPECT_EQ(ValidateAndSanitizeDeviceId("device%123"), "");  // %符号
    EXPECT_EQ(ValidateAndSanitizeDeviceId("device/123"), "");  // 斜杠

    // 测试空字符串
    EXPECT_EQ(ValidateAndSanitizeDeviceId(""), "");

    // 测试边界情况
    EXPECT_EQ(ValidateAndSanitizeDeviceId("a"), "a");
    EXPECT_EQ(ValidateAndSanitizeDeviceId("1"), "1");
    EXPECT_EQ(ValidateAndSanitizeDeviceId("-"), "-");
    EXPECT_EQ(ValidateAndSanitizeDeviceId("_"), "_");
}

/*
测试描述: 测试IsValidModelID函数
测试步骤:
    1. 测试有效的模型ID格式
    2. 测试各种后缀格式
    3. 测试无效格式
    4. 测试边界情况
预期结果:
    1. 有效格式返回true
    2. 支持的后缀格式返回true
    3. 无效格式返回false
    4. 边界情况正确处理
*/
TEST_F(TestSecurityUtils, TestIsValidModelID)
{
    // 测试有效的模型ID格式 - 日期格式
    EXPECT_TRUE(IsValidModelID("model_20240101"));           // YYYYMMDD
    EXPECT_TRUE(IsValidModelID("GPT_2024-01-01"));           // YYYY-MM-DD
    EXPECT_TRUE(IsValidModelID("BERT_20241231"));            // YYYYMMDD
    EXPECT_TRUE(IsValidModelID("LLaMA_2024-12-31"));         // YYYY-MM-DD

    // 测试有效的模型ID格式 - 时间格式
    EXPECT_TRUE(IsValidModelID("model_20240101123456"));     // YYYYMMDDHHMMSS
    EXPECT_TRUE(IsValidModelID("GPT_2024-01-01T12:34:56")); // YYYY-MM-DDTHH:MM:SS

    // 测试有效的模型ID格式 - UUID格式
    EXPECT_TRUE(IsValidModelID("model_12345678-1234-1234-1234-123456789012"));
    EXPECT_TRUE(IsValidModelID("GPT_abcdef12-3456-7890-abcd-ef1234567890"));
    EXPECT_TRUE(IsValidModelID("BERT_12345678123412341234123456789012"));  // 无连字符UUID

    // 测试有效的模型ID格式 - 字母数字格式
    EXPECT_TRUE(IsValidModelID("model_v1-0-0"));
    EXPECT_TRUE(IsValidModelID("GPT_version123"));
    EXPECT_TRUE(IsValidModelID("BERT_abc123def"));

    // 测试边界情况
    EXPECT_FALSE(IsValidModelID(""));  // 空字符串
    EXPECT_TRUE(IsValidModelID("a_b"));
    EXPECT_TRUE(IsValidModelID("ab_cd"));

    // 测试超长字符串
    std::string longModelID = std::string(100, 'a') + "_" + std::string(100, 'b');
    EXPECT_FALSE(IsValidModelID(longModelID));  // 超过128字符限制
}

/*
测试描述: 测试IsValidMetricsInfo函数
测试步骤:
    1. 测试正常大小的字符串
    2. 测试超过10MB限制的字符串
    3. 测试空字符串
    4. 测试边界值
预期结果:
    1. 正常大小返回true
    2. 超过限制返回false
    3. 空字符串返回true
    4. 边界值正确处理
*/
TEST_F(TestSecurityUtils, TestIsValidMetricsInfo)
{
    // 测试正常大小的字符串
    EXPECT_TRUE(IsValidMetricsInfo("normal metrics info"));
    EXPECT_TRUE(IsValidMetricsInfo("{}"));
    EXPECT_TRUE(IsValidMetricsInfo("{\"cpu\": 50, \"memory\": 80}"));

    // 测试空字符串
    EXPECT_TRUE(IsValidMetricsInfo(""));

    // 测试较大但在限制内的字符串
    std::string largeButValid(1024 * 1024, 'a');  // 1MB
    EXPECT_TRUE(IsValidMetricsInfo(largeButValid));

    // 测试接近限制的字符串
    std::string nearLimit(10 * 1024 * 1024 - 1, 'a');  // 接近10MB
    EXPECT_TRUE(IsValidMetricsInfo(nearLimit));

    // 测试超过10MB限制的字符串
    std::string tooLarge(10 * 1024 * 1024 + 1, 'a');  // 超过10MB
    EXPECT_FALSE(IsValidMetricsInfo(tooLarge));

    // 测试远超限制的字符串
    std::string wayTooLarge(50 * 1024 * 1024, 'a');  // 50MB
    EXPECT_FALSE(IsValidMetricsInfo(wayTooLarge));
}

/*
测试描述: 测试IsValidRankTable函数
测试步骤:
    1. 测试正常rank table内容
    2. 测试超过50MB限制
    3. 测试包含危险字符
    4. 测试空字符串
预期结果:
    1. 正常内容返回true
    2. 超过限制返回false
    3. 包含危险字符返回false
    4. 空字符串返回false
*/
TEST_F(TestSecurityUtils, TestIsValidRankTable)
{
    // 测试正常rank table内容
    EXPECT_TRUE(IsValidRankTable("rank_table_content"));
    EXPECT_TRUE(IsValidRankTable("{\"rank_table\": \"data\"}"));
    EXPECT_TRUE(IsValidRankTable("normal text content"));

    // 测试空字符串（应该返回false）
    EXPECT_FALSE(IsValidRankTable(""));

    // 测试包含危险字符
    EXPECT_FALSE(IsValidRankTable(std::string("content\x00with\x01danger", 19)));
    EXPECT_FALSE(IsValidRankTable(std::string("content\x08danger", 14)));

    // 测试较大但在限制内的字符串
    std::string largeButValid(10 * 1024 * 1024, 'a');  // 10MB
    EXPECT_TRUE(IsValidRankTable(largeButValid));

    // 测试接近限制的字符串
    std::string nearLimit(50 * 1024 * 1024 - 1, 'a');  // 接近50MB
    EXPECT_TRUE(IsValidRankTable(nearLimit));

    // 测试超过50MB限制的字符串
    std::string tooLarge(50 * 1024 * 1024 + 1, 'a');  // 超过50MB
    EXPECT_FALSE(IsValidRankTable(tooLarge));
}

/*
测试描述: 测试IsValidFaultSignalType函数
测试步骤:
    1. 测试有效信号类型
    2. 测试无效字符
    3. 测试超长字符串
    4. 测试空字符串
预期结果:
    1. 有效信号类型返回true
    2. 无效字符返回false
    3. 超长字符串返回false
    4. 空字符串返回false
*/
TEST_F(TestSecurityUtils, TestIsValidFaultSignalType)
{
    // 测试有效信号类型
    EXPECT_TRUE(IsValidFaultSignalType("SIGNAL_TYPE_1"));
    EXPECT_TRUE(IsValidFaultSignalType("error_signal"));
    EXPECT_TRUE(IsValidFaultSignalType("WARNING123"));
    EXPECT_TRUE(IsValidFaultSignalType("fault_type_abc"));
    EXPECT_TRUE(IsValidFaultSignalType("CRITICAL_ERROR"));
    EXPECT_TRUE(IsValidFaultSignalType("signal123"));

    // 测试无效字符
    EXPECT_FALSE(IsValidFaultSignalType("signal-type"));    // 连字符
    EXPECT_FALSE(IsValidFaultSignalType("signal.type"));    // 点号
    EXPECT_FALSE(IsValidFaultSignalType("signal type"));    // 空格
    EXPECT_FALSE(IsValidFaultSignalType("signal@type"));    // @符号
    EXPECT_FALSE(IsValidFaultSignalType("signal#type"));    // #符号
    EXPECT_FALSE(IsValidFaultSignalType("signal$type"));    // $符号

    // 测试空字符串
    EXPECT_FALSE(IsValidFaultSignalType(""));

    // 测试边界长度
    std::string maxLength(64, 'a');
    EXPECT_TRUE(IsValidFaultSignalType(maxLength));

    // 测试超长字符串
    std::string tooLong(65, 'a');
    EXPECT_FALSE(IsValidFaultSignalType(tooLong));

    std::string wayTooLong(128, 'a');
    EXPECT_FALSE(IsValidFaultSignalType(wayTooLong));
}

/*
测试描述: 测试IsValidFaultLevel函数
测试步骤:
    1. 测试有效故障级别
    2. 测试无效故障级别
    3. 测试空字符串
    4. 测试超长字符串
预期结果:
    1. 有效故障级别返回true
    2. 无效故障级别返回false
    3. 空字符串返回false
    4. 超长字符串返回false
*/
TEST_F(TestSecurityUtils, TestIsValidFaultLevel)
{
    // 测试有效故障级别
    EXPECT_TRUE(IsValidFaultLevel("UnHealthy"));
    EXPECT_TRUE(IsValidFaultLevel("SubHealthy"));
    EXPECT_TRUE(IsValidFaultLevel("Healthy"));

    // 测试无效故障级别
    EXPECT_FALSE(IsValidFaultLevel("unhealthy"));      // 大小写错误
    EXPECT_FALSE(IsValidFaultLevel("UNHEALTHY"));      // 大小写错误
    EXPECT_FALSE(IsValidFaultLevel("Critical"));       // 不支持的级别
    EXPECT_FALSE(IsValidFaultLevel("Warning"));        // 不支持的级别
    EXPECT_FALSE(IsValidFaultLevel("Error"));          // 不支持的级别
    EXPECT_FALSE(IsValidFaultLevel("Normal"));         // 不支持的级别
    EXPECT_FALSE(IsValidFaultLevel("Unknown"));        // 不支持的级别

    // 测试空字符串
    EXPECT_FALSE(IsValidFaultLevel(""));

    // 测试包含额外字符
    EXPECT_FALSE(IsValidFaultLevel("Healthy "));       // 尾部空格
    EXPECT_FALSE(IsValidFaultLevel(" Healthy"));       // 前部空格
    EXPECT_FALSE(IsValidFaultLevel("Un Healthy"));     // 中间空格

    // 测试超长字符串
    std::string tooLong(33, 'a');
    EXPECT_FALSE(IsValidFaultLevel(tooLong));

    std::string wayTooLong(64, 'a');
    EXPECT_FALSE(IsValidFaultLevel(wayTooLong));
}

/*
测试描述: 测试IsValidNodeName函数
测试步骤:
    1. 测试有效节点名
    2. 测试无效字符
    3. 测试空字符串
    4. 测试超长字符串
    5. 测试包含危险字符
预期结果:
    1. 有效节点名返回true
    2. 无效字符返回false
    3. 空字符串返回true（允许）
    4. 超长字符串返回false
    5. 包含危险字符返回false
*/
TEST_F(TestSecurityUtils, TestIsValidNodeName)
{
    // 测试有效节点名
    EXPECT_TRUE(IsValidNodeName("node1"));
    EXPECT_TRUE(IsValidNodeName("worker-node-01"));
    EXPECT_TRUE(IsValidNodeName("master_node"));
    EXPECT_TRUE(IsValidNodeName("node.example.com"));
    EXPECT_TRUE(IsValidNodeName("NODE123"));
    EXPECT_TRUE(IsValidNodeName("cluster-node-1.domain.com"));
    EXPECT_TRUE(IsValidNodeName("node_1-2.test"));

    // 测试空字符串（应该允许）
    EXPECT_TRUE(IsValidNodeName(""));

    // 测试无效字符
    EXPECT_FALSE(IsValidNodeName("node@1"));         // @符号
    EXPECT_FALSE(IsValidNodeName("node#1"));         // #符号
    EXPECT_FALSE(IsValidNodeName("node$1"));         // $符号
    EXPECT_FALSE(IsValidNodeName("node%1"));         // %符号
    EXPECT_FALSE(IsValidNodeName("node 1"));         // 空格
    EXPECT_FALSE(IsValidNodeName("node/1"));         // 斜杠
    EXPECT_FALSE(IsValidNodeName("node\\1"));        // 反斜杠
    EXPECT_FALSE(IsValidNodeName("node*1"));         // 星号

    // 测试包含危险字符
    EXPECT_FALSE(IsValidNodeName(std::string("node\x00test", 9)));
    EXPECT_FALSE(IsValidNodeName(std::string("node\x01test", 9)));
    EXPECT_FALSE(IsValidNodeName(std::string("node\x08test", 9)));

    // 测试边界长度
    std::string maxLength(256, 'a');
    EXPECT_TRUE(IsValidNodeName(maxLength));

    // 测试超长字符串
    std::string tooLong(257, 'a');
    EXPECT_FALSE(IsValidNodeName(tooLong));

    std::string wayTooLong(512, 'a');
    EXPECT_FALSE(IsValidNodeName(wayTooLong));

    // 测试边界情况
    EXPECT_TRUE(IsValidNodeName("a"));
    EXPECT_TRUE(IsValidNodeName("1"));
    EXPECT_TRUE(IsValidNodeName("-"));
    EXPECT_TRUE(IsValidNodeName("_"));
    EXPECT_TRUE(IsValidNodeName("."));
}

/*
测试描述: 测试CheckJsonDepthCallBack函数
测试步骤:
    1. 测试object_start事件的不同深度
    2. 测试array_start事件的不同深度
    3. 测试其他事件类型（应返回true）
    4. 测试边界值（50层）
    5. 测试超过限制的深度
    6. 测试恶意payload深度校验
预期结果:
    1. object_start在限制内返回true，超过返回false
    2. array_start在限制内返回true，超过返回false
    3. 其他事件类型返回true
    4. 边界值（50层）返回true
    5. 超过限制返回false
    6. 恶意payload应被正确检测和拒绝
*/

constexpr int JSON_STRING_DEPTH_MAX = 10;

static std::string GenerateNestedObjectJson(int depth)
{
    std::string json = "{";
    for (int i = 0; i < depth; ++i) {
        json.append("\"level" + std::to_string(i) + "\":{");
    }
    json += "\"value\":\"test\"";
    json.append(depth, '}');
    json += "}";
    return json;
}

static std::string GenerateNestedArrayJson(int depth)
{
    std::string json = "{\"level0\":";
    for (int i = 0; i < depth; ++i) {
        json += "[";
    }
    json += "[]";
    for (int i = 0; i < depth; ++i) {
        json += "]";
    }
    json += "}";
    return json;
}

static std::string GenerateMaliciousPayload()
{
    std::string payload;
    payload += "{\"mask\": \"";
    payload.append(JSON_STRING_DEPTH_MAX + 1, ']');
    payload += "\", \"bomb\": ";
    payload.append(JSON_STRING_DEPTH_MAX + 1, '[');
    payload.append(JSON_STRING_DEPTH_MAX + 1, ']');
    payload += "}";
    return payload;
}

TEST_F(TestSecurityUtils, CheckJsonDepthCallBack_PrimitiveEvents)
{
    Json dummyJson;
    EXPECT_TRUE(CheckJsonDepthCallBack(0, Json::parse_event_t::object_start, dummyJson));
    EXPECT_TRUE(
        CheckJsonDepthCallBack(JSON_STRING_DEPTH_MAX, Json::parse_event_t::object_start, dummyJson));
    EXPECT_FALSE(CheckJsonDepthCallBack(JSON_STRING_DEPTH_MAX + 1, Json::parse_event_t::object_start,
                                              dummyJson));
    EXPECT_TRUE(CheckJsonDepthCallBack(0, Json::parse_event_t::array_start, dummyJson));
    EXPECT_TRUE(
        CheckJsonDepthCallBack(JSON_STRING_DEPTH_MAX, Json::parse_event_t::array_start, dummyJson));
    EXPECT_FALSE(
        CheckJsonDepthCallBack(JSON_STRING_DEPTH_MAX + 1, Json::parse_event_t::array_start, dummyJson));
    EXPECT_TRUE(CheckJsonDepthCallBack(0, Json::parse_event_t::object_end, dummyJson));
    EXPECT_TRUE(
        CheckJsonDepthCallBack(JSON_STRING_DEPTH_MAX, Json::parse_event_t::object_end, dummyJson));
    EXPECT_TRUE(CheckJsonDepthCallBack(0, Json::parse_event_t::key, dummyJson));
    EXPECT_TRUE(CheckJsonDepthCallBack(-1, Json::parse_event_t::object_start, dummyJson));
}

// 合法的深度边界和异常边界的对象和数组
TEST_F(TestSecurityUtils, CheckJsonDepthCallBack_NestedJsonObjectArray)
{
    EXPECT_NO_THROW({
        auto result = Json::parse(GenerateNestedObjectJson(10), CheckJsonDepthCallBack);
        EXPECT_TRUE(!result.is_null());
    });
    EXPECT_NO_THROW({
        auto result = Json::parse(GenerateNestedObjectJson(JSON_STRING_DEPTH_MAX), CheckJsonDepthCallBack);
        EXPECT_TRUE(!result.is_null());
    });
    // 测试超过限制：nlohmann::json在回调返回false时会停止解析更深层，不会抛出异常
    // 验证深度限制是否生效：检查是否能访问到第51层（应该不能）
    EXPECT_NO_THROW({
        auto result = Json::parse(GenerateNestedObjectJson(JSON_STRING_DEPTH_MAX + 1), CheckJsonDepthCallBack);
        EXPECT_TRUE(!result.is_null());
        // 检查最大访问深度
        auto current = result;
        int maxLevel = 0;
        for (int i = 0; i <= JSON_STRING_DEPTH_MAX + 1; ++i) {
            std::string key = "level" + std::to_string(i);
            if (current.contains(key)) {
                current = current[key];
                maxLevel = i + 1;
            } else {
                break;
            }
        }
        EXPECT_LE(maxLevel, JSON_STRING_DEPTH_MAX) << "深度限制应该生效，最大访问深度应该<=50层";
    });

    EXPECT_NO_THROW({
        auto result = Json::parse(GenerateNestedArrayJson(10), CheckJsonDepthCallBack);
        EXPECT_TRUE(!result.is_null());
    });
    EXPECT_NO_THROW({
        auto result = Json::parse(GenerateNestedArrayJson(JSON_STRING_DEPTH_MAX), CheckJsonDepthCallBack);
        EXPECT_TRUE(!result.is_null());
    });
    // 测试超过限制：检查嵌套数组的实际深度
    EXPECT_NO_THROW({
        auto result = Json::parse(GenerateNestedArrayJson(JSON_STRING_DEPTH_MAX + 1), CheckJsonDepthCallBack);
        EXPECT_TRUE(!result.is_null());
        // 检查嵌套数组的深度
        if (result.contains("level0")) {
            auto current = result["level0"];
            int depth = 0;
            while (current.is_array() && !current.empty()) {
                current = current[0];
                depth++;
            }
            EXPECT_LE(depth, JSON_STRING_DEPTH_MAX) << "深度限制应该生效，数组深度应该<=50层";
        }
    });
}

// 普通、小混合、恶意payload场景
TEST_F(TestSecurityUtils, CheckJsonDepthCallBack_SimpleMixedMalicious)
{
    std::string mixedJson = R"({"level1":{"level2":[{"level3":{"level4":{}}}]}})";
    std::string simpleJson = R"({"key":"value"})";
    EXPECT_NO_THROW({
        auto result = Json::parse(mixedJson, CheckJsonDepthCallBack);
        EXPECT_TRUE(!result.is_null());
    });
    EXPECT_NO_THROW({
        auto result = Json::parse(simpleJson, CheckJsonDepthCallBack);
        EXPECT_TRUE(!result.is_null());
    });

    // 测试恶意payload：验证深度限制是否生效
    std::string maliciousPayload = GenerateMaliciousPayload();
    EXPECT_NO_THROW({
        auto result = Json::parse(maliciousPayload, CheckJsonDepthCallBack);
        EXPECT_TRUE(!result.is_null());
        // 检查"bomb"字段的嵌套深度
        if (result.contains("bomb")) {
            auto bomb = result["bomb"];
            int depth = 0;
            auto current = bomb;
            while (current.is_array() && !current.empty()) {
                current = current[0];
                depth++;
            }
            EXPECT_LE(depth, JSON_STRING_DEPTH_MAX) << "恶意payload应该被拒绝，bomb字段深度应该<=50层";
        } else {
            // 如果bomb字段无法解析，说明深度限制生效
            EXPECT_TRUE(true) << "恶意payload被拒绝（无法解析bomb字段）";
        }
    });
}
