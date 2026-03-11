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
#ifndef MINDIE_MS_COORDINATOR_METRICS_H
#define MINDIE_MS_COORDINATOR_METRICS_H

#include <string>
#include <vector>
#include <regex>
#include "nlohmann/json.hpp"
#include "ClusterNodes.h"

namespace MINDIE::MS {

enum class RegexPattern : uint32_t {
    POUND = 0,    /* 匹配前置# */
    LINE_BREAK,   /* 匹配\n */
    WHITE_SPACE,  /* 匹配行首空格 */
    LINE,         /* 匹配\n前的所有字符，不含\n */
    HELP,         /* 匹配行首# HELP */
    TYPE,         /* 匹配行首# TYPE */
    METRIC,       /* 匹配指标名称，不涉及 { } " " = / . 等符号 */
    METRIC_LABEL, /* 匹配指标标签 */
    METRIC_VALUE, /* 匹配指标数值，取下一个子串或直到行尾前的单个token */
    METRIC_TYPE,  /* 匹配指标类别 */
    REGEX_PATTERN_LEN
};

class Metrics {
public:
    Metrics() {};
    ~Metrics() = default;

    void InitMetricsPattern();
    std::string GetAndAggregateMetrics(const std::map<uint64_t, InstanceInfo> &podInfos);

private:
    void ComputeSeqLenTableWidths(const std::vector<int>& bounds, int& colWidth, int& maxLabelWidth, int& totalWidth) const;
    void PrintTokenDistribution() const;
    // 根据Pod节点信息，获取Server的Metric文本
    nlohmann::json GetServerMetircs(const std::map<uint64_t, InstanceInfo> &podInfos) const;
    // 解析单个pod的Metric字段，并存到对应的json中
    int32_t ParseMetrics(nlohmann::json &podMetric);
    // 聚合所有pod节点的Metric
    nlohmann::json AggregateMetrics(nlohmann::json &podMetric, const std::map<std::string, uint64_t>& stats) const;
    // 序列化Metric
    uint64_t ProcessSingleMetric(nlohmann::json &podMetric, nlohmann::json &singleMetric, const std::map<std::string,
        uint64_t>& statsMetrics, nlohmann::json &aggregate) const;
    std::string SerializeMetrics(const nlohmann::json &metrics) const;
    void InitPatternsRegex(RegexPattern pattern, std::string str);
    int32_t ParseMetricHelp(nlohmann::json &singleMetirc, std::string &line);
    int32_t ParseMetricType(nlohmann::json &singleMetirc, std::string &line);
    int32_t ParseMetricBodyBlock(nlohmann::json &singleMetirc, std::string &body);
    int32_t ParseMetricBody(nlohmann::json &metricArray, nlohmann::json &singleMetirc, std::string &line,
        uint32_t &count, bool &isParsing);
    nlohmann::json ParseMetricText(std::string &metrics);

    std::vector<std::regex> patterns {};
    uint32_t metricCount = 0;
};
extern std::map<int, std::map<int, int>> token_distribution;
extern const std::vector<int> token_ranges;
void TryUpdateTokenDistributionFromUsage(const std::string& body);
}
#endif