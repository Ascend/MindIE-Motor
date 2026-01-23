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
#include "SecurityUtils.h"
#include <regex>
#include <algorithm>
#include <cctype>

namespace MINDIE {
namespace MS {

bool IsStringSafeForJson(const std::string& input)
{
    // 检查控制字符，但排除允许的字符（制表符、换行符、回车符）
    for (char c : input) {
        if (c < 0x20 && c != 0x09 && c != 0x0A && c != 0x0D) {
            return false;
        }
    }
    return true;
}

static inline char HexDigit(unsigned v)
{
    return v < 10 ? char('0' + v) : char('a' + (v - 10)); // v 在 10..15），映射到 'a'..'f'
}

std::string SanitizeStringForJson(const std::string& input)
{
    if (input.empty()) {
        return input;
    }

    std::string result;
    result.reserve(input.size() * 2); // 2：预留为输入长度的2倍容量，减少重分配

    for (unsigned char uc : input) {
        switch (uc) {
            case '\"': result += "\\\""; break;
            case '\\': result += "\\\\"; break;
            case '\b': result += "\\b"; break;
            case '\f': result += "\\f"; break;
            case '\n': result += "\\n"; break;
            case '\r': result += "\\r"; break;
            case '\t': result += "\\t"; break;
            default:
                if (uc <= 0x1F) { // 0x1F：ASCII 31，控制字符上限（0x00–0x1F 需转义为 \u00XX
                    // 写入 \u00XX
                    result += '\\';
                    result += 'u';
                    result += '0';
                    result += '0';
                    result += HexDigit((uc >> 4) & 0xF); // 4：高四位；0xF：掩码取低4位
                    result += HexDigit(uc & 0xF); // 0xF：掩码取低4位
                } else {
                    result += static_cast<char>(uc);
                }
                break;
        }
    }

    return result;
}

std::string ValidateAndSanitizeIP(const std::string& ip)
{
    if (ip.empty()) {
        return ip;
    }

    // 基本IPv4校验
    std::regex ipPattern(R"(^(\d{1,3}\.){3}\d{1,3}$)");
    if (!std::regex_match(ip, ipPattern)) {
        // 允许IPv6
        std::regex ipv6Pattern(R"(^([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$)");
        std::regex ipv6CompressedPattern(R"(^([0-9a-fA-F]{1,4}::?){1,7}[0-9a-fA-F]{1,4}$)");
        if (!std::regex_match(ip, ipv6Pattern) && !std::regex_match(ip, ipv6CompressedPattern)) {
            return "";
        }
    }

    // IPv4检查项
    if (ip.find('.') != std::string::npos) {
        std::string sanitizedIp = SanitizeStringForJson(ip);
        std::vector<std::string> parts;
        size_t start = 0;
        size_t end = sanitizedIp.find('.');
        
        while (end != std::string::npos) {
            parts.push_back(sanitizedIp.substr(start, end - start));
            start = end + 1;
            end = sanitizedIp.find('.', start);
        }
        parts.push_back(sanitizedIp.substr(start));
        
        if (parts.size() != 4) { // 检查是否符合ip地址分为4段的形式
            return "";
        }
        
        for (const auto& part : parts) {
            if (part.empty() || part.length() > 3) { // 检查是否地址大于3位
                return "";
            }
            
            if (!std::all_of(part.begin(), part.end(), ::isdigit)) {
                return "";
            }
            
            int num = std::stoi(part);
            if (num < 0 || num > 255) { // 检查地址字段是否大于255
                return "";
            }
        }
        
        return sanitizedIp;
    }
    
    // 对于IPv6，仅进行清理处理
    return SanitizeStringForJson(ip);
}

std::string ValidateAndSanitizeDeviceId(const std::string& deviceId)
{
    if (deviceId.empty()) {
        return deviceId;
    }

    // 设备ID应该只包含字母数字字符、连字符和下划线
    std::regex deviceIdPattern(R"(^[a-zA-Z0-9\-_]+$)");
    if (!std::regex_match(deviceId, deviceIdPattern)) {
        return "";
    }

    return SanitizeStringForJson(deviceId);
}

bool IsValidModelID(const std::string& modelID)
{
    // 基本检查
    if (modelID.empty() || modelID.length() > 128) { // modelID字符长度不超过128
        return false;
    }
    return true;
}

bool IsValidMetricsInfo(const std::string& metricsInfo)
{
    // 限制metricsInfo的最大长度，防止过大的数据
    constexpr size_t maxMetricsInfoSize = 10 * 1024 * 1024; // 10MB
    
    if (metricsInfo.length() > maxMetricsInfoSize) {
        return false;
    }
    
    return true;
}

bool IsValidRankTable(const std::string& rankTable)
{
    // 限制rank table的最大长度，防止过大的数据
    constexpr size_t maxRankTableSize = 50 * 1024 * 1024; // 50MB
    
    if (rankTable.empty()) {
        return false;
    }
    
    if (rankTable.length() > maxRankTableSize) {
        return false;
    }
    
    // 检查是否包含危险字符
    if (!IsStringSafeForJson(rankTable)) {
        return false;
    }
    
    return true;
}

bool IsValidFaultSignalType(const std::string& signalType)
{
    if (signalType.empty()) {
        return false;
    }
    
    // 限制信号类型长度
    constexpr size_t maxSignalTypeLen = 64; // 信号类型长度不超过64
    if (signalType.length() > maxSignalTypeLen) {
        return false;
    }
    
    // 只允许字母、数字和下划线
    std::regex signalTypePattern(R"(^[a-zA-Z0-9_]+$)");
    if (!std::regex_match(signalType, signalTypePattern)) {
        return false;
    }
    
    return true;
}

bool IsValidFaultLevel(const std::string& faultLevel)
{
    if (faultLevel.empty()) {
        return false;
    }
    
    // 限制故障级别长度
    constexpr size_t maxFaultLevelLen = 32; // 故障等级长度不超过32
    if (faultLevel.length() > maxFaultLevelLen) {
        return false;
    }
    
    // 只允许预定义的故障级别
    if (faultLevel != "UnHealthy" && faultLevel != "SubHealthy" && faultLevel != "Healthy") {
        return false;
    }
    
    return true;
}

bool IsValidNodeName(const std::string& nodeName)
{
    if (nodeName.empty()) {
        return true; // 允许空节点名
    }
    
    // 限制节点名长度
    constexpr size_t maxNodelNameLen = 256; // 节点名称不超过256
    if (nodeName.length() > maxNodelNameLen) {
        return false;
    }
    
    // 检查是否包含危险字符
    if (!IsStringSafeForJson(nodeName)) {
        return false;
    }
    
    // 节点名应该只包含字母、数字、连字符、下划线和点
    std::regex nodeNamePattern(R"(^[a-zA-Z0-9\-_.]+$)");
    if (!std::regex_match(nodeName, nodeNamePattern)) {
        return false;
    }
    
    return true;
}

} // namespace MS
} // namespace MINDIE
