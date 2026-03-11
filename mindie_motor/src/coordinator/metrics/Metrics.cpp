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
#include "Metrics.h"
#include <algorithm>
#include <cstdint>
#include <cmath>
#include <limits>
#include <regex>
#include <iomanip>
#include "HttpClient.h"
#include "Configure.h"
#include "RequestMgr.h"
#include <iomanip>
#include <sstream>

namespace MINDIE::MS {


void Metrics::InitPatternsRegex(RegexPattern pattern, std::string str)
{
    patterns[static_cast<uint32_t>(pattern)] = std::regex(str);
}

// 所有正则项已通过正则redos工具检测
void Metrics::InitMetricsPattern()
{
    patterns.resize(static_cast<uint32_t>(RegexPattern::REGEX_PATTERN_LEN));
    InitPatternsRegex(RegexPattern::POUND, "^#");
    InitPatternsRegex(RegexPattern::LINE_BREAK, "^\n");
    InitPatternsRegex(RegexPattern::WHITE_SPACE, "^[ \t]+");
    InitPatternsRegex(RegexPattern::LINE, "[^\n]+");
    InitPatternsRegex(RegexPattern::HELP, "^(#[\t ]+HELP[\t ]+)");
    InitPatternsRegex(RegexPattern::TYPE, "^(#[\t ]+TYPE[\t ]+)");
    InitPatternsRegex(RegexPattern::METRIC, "^[a-zA-Z_:]([a-zA-Z_:]|[0-9])*");
    InitPatternsRegex(RegexPattern::METRIC_LABEL, "[^ \t\n]+");
    InitPatternsRegex(RegexPattern::METRIC_VALUE, "[^{ \t\n]+");
    InitPatternsRegex(RegexPattern::METRIC_TYPE, "[a-zA-Z]+");
}

int32_t Metrics::ParseMetricHelp(nlohmann::json &singleMetirc, std::string &line)
{
    std::smatch lineRes;
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::HELP)])) { // 匹配# HELP失败
        LOG_E("[%s] [Metrics] Match # HELP line failed",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    // 匹配成功，继续匹配metric name
    line = lineRes.suffix();
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::METRIC)])) { // 匹配失败
        LOG_E("[%s] [Metrics] Match metric name in help line failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    singleMetirc["NAME"] = lineRes.str();
    // 去除空格
    line = lineRes.suffix();
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::WHITE_SPACE)])) {
        LOG_E("[%s] [Metrics] Remove help space failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    line = lineRes.suffix();
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::LINE)])) { // 匹配失败
        LOG_E("[%s] [Metrics] Match help text failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    singleMetirc["HELP"] = lineRes.str();
    // 去除行尾的\n，应在字符串首位
    line = lineRes.suffix();
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::LINE_BREAK)])) {  // 若行尾不是\n
        LOG_E("[%s] [Metrics] Match help line break \\n failed.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, CoordinatorFeature::METRICS).c_str());
            // 表明在取完所需字段后仍有多余字段，需返回异常
        return -1;
    }
    line.erase(0, 1);  // 删除行首的\n
    return 0;
}

int32_t Metrics::ParseMetricType(nlohmann::json &singleMetirc, std::string &line)
{
    std::smatch lineRes;
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::TYPE)])) { // 匹配# TYPE失败
        LOG_E("[%s] [Metrics] Match # TYPE line failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    // 匹配成功，继续匹配metric name
    line = lineRes.suffix();
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::METRIC)])) { // 匹配失败
        LOG_E("[%s] [Metrics] Match metric name in type line failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    // 去除空格
    line = lineRes.suffix();
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::WHITE_SPACE)])) {
        LOG_E("[%s] [Metrics] Remove type space failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    line = lineRes.suffix();
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::METRIC_TYPE)])) { // 匹配失败
        LOG_E("[%s] [Metrics] Match metric type failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    singleMetirc["TYPE"] = lineRes.str();
    // 去除行尾的\n，应在字符串首位
    line = lineRes.suffix();
    if (!std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::LINE_BREAK)])) {  // 若行尾不是\n
        LOG_E("[%s] [Metrics] Match type line break \\n failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    line.erase(0, 1);  // 删除行首的\n
    return 0;
}

int32_t Metrics::ParseMetricBodyBlock(nlohmann::json &singleMetirc, std::string &body)
{
    std::smatch bodyRes;
    if (!std::regex_search(body, bodyRes, patterns[static_cast<uint32_t>(RegexPattern::METRIC_LABEL)])) { // 匹配失败
        LOG_E("[%s] [Metrics] Match metric label name failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    // 匹配成功，保存metric label
    singleMetirc["LABEL"].emplace_back(bodyRes.str());
    body = bodyRes.suffix();
    // 去除空格
    if (!std::regex_search(body, bodyRes, patterns[static_cast<uint32_t>(RegexPattern::WHITE_SPACE)])) { // 匹配失败，返回error
        LOG_E("[%s] [Metrics] Remove body space failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    // 取第二个子串，数值
    if (!std::regex_search(body, bodyRes, patterns[static_cast<uint32_t>(RegexPattern::METRIC_VALUE)])) { // 匹配失败
    LOG_E("[%s] [Metrics] Match metric value failed.",
        GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::METRICS).c_str());
    return -1;
    }
    double value;
    try {
        value = std::stod(bodyRes.str());
    } catch (const std::exception &e) {
        LOG_E("[%s] [Metrics] Invalid metric value.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    singleMetirc["VALUE"].emplace_back(value);
    return 0;
}

int32_t Metrics::ParseMetricBody(nlohmann::json &metricArray, nlohmann::json &singleMetirc, std::string &line,
    uint32_t &count, bool &isParsing)
{
    std::smatch lineRes;
    while (std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::LINE)])) {
        std::smatch bodyRes;
        std::string body = lineRes.str();
        // 匹配到#，完成当前指标解析
        if (std::regex_search(body, bodyRes, patterns[static_cast<uint32_t>(RegexPattern::POUND)])) {
            metricArray.emplace_back(singleMetirc);
            count++;
            return 0;
        }
        if (ParseMetricBodyBlock(singleMetirc, body) != 0) {
            LOG_E("[%s] [Metrics] Parse metric body block failed.",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::METRICS).c_str());
            return -1;
        }
        // 去除行尾的\n，应在字符串首位
        line = lineRes.suffix();
        if (std::regex_search(line, lineRes, patterns[static_cast<uint32_t>(RegexPattern::LINE_BREAK)])) {
            line = lineRes.suffix();
            if (line.empty()) {  // 判断字符串是否结束
                metricArray.emplace_back(singleMetirc);
                count++;
                isParsing = false;
                return 0;
            }
        } else {  // 若行尾不是\n，表明在取完所需字段后仍有多余字段，需返回异常
            LOG_E("[%s] [Metrics] Match body's line break \\n failed.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CoordinatorFeature::METRICS).c_str());
            return -1;
        }
    }
    return -1;
}

nlohmann::json Metrics::ParseMetricText(std::string &metrics)
{
    uint32_t count = 0;
    nlohmann::json metricArray = nlohmann::json::array();
    bool isParsing = true;
    while (isParsing) {
        nlohmann::json singleMetirc = {{"NAME", ""}, {"HELP", ""}, {"TYPE", ""}};
        singleMetirc["LABEL"] = nlohmann::json::array();
        singleMetirc["VALUE"] = nlohmann::json::array();
        if (ParseMetricHelp(singleMetirc, metrics) != 0) {
            LOG_E("[%s] [Metrics] Parse metric help failed.",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::METRICS).c_str());
            return {};
        }
        if (ParseMetricType(singleMetirc, metrics) != 0) {
            LOG_E("[%s] [Metrics] Parse metric type failed.",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::METRICS).c_str());
            return {};
        }
        if (ParseMetricBody(metricArray, singleMetirc, metrics, count, isParsing) != 0) {
            LOG_E("[%s] [Metrics] Parse metric body failed.",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::METRICS).c_str());
            return {};
        }
    }
    // 校验指标的个数
    if (metricCount == 0) {
        metricCount = count;
    } else if (metricCount != count) {  // 每次获得相同个数的metric
        LOG_E("[%s] [Metrics] Metric count different.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::METRICS).c_str());
        return {};
    }
    return metricArray;
}

int32_t Metrics::ParseMetrics(nlohmann::json &podMetric)
{
    if (!podMetric.is_array() || podMetric.size() == 0) {
        LOG_E("[%s] [Metrics] Invalid pods metric JSON file.",
            GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::METRICS).c_str());
        return -1;
    }
    // 每次解析把metricCount重置为0
    metricCount = 0;
    try {
        for (uint32_t i = 0; i < podMetric.size(); i++) {
            if (!podMetric[i].contains("metrics_str") || !podMetric[i]["metrics_str"].is_string()) {
                LOG_E("[%s] [Metrics] Invalid 'metrics_str' in pod metrics JSON file.",
                    GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::METRICS).c_str());
                return -1;
            }
            std::string metric = podMetric[i].at("metrics_str");
            nlohmann::json parsedMetric = ParseMetricText(metric);
            if (parsedMetric.empty()) {
                LOG_E("[%s] [Metrics] Parse metric text failed.",
                    GetErrorCode(ErrorType::INVALID_INPUT, CoordinatorFeature::METRICS).c_str());
                return -1;
            }
            // parsedMetric 作为array类型的"metrics"节点插入到"pods"中
            podMetric[i]["metrics"] = parsedMetric;
            podMetric[i].erase("metrics_str");
        }
    } catch (const std::exception &e) {
        LOG_E("[%s] [Metrics] Parse metrics failed, exception is %s\n",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::METRICS).c_str(),
            e.what());
        return -1;
    }
    return 0;
}

static void AggregateCacheUsagePerc(nlohmann::json &pods, nlohmann::json &singleMetirc, uint32_t index)
{
    double sum = 0;
    double weightValue = 0;
    for (uint32_t i = 0; i < pods.size(); i++) {
        size_t memSize = pods[i]["NPU_mem_size"];
        double value = pods[i]["metrics"][index]["VALUE"][0];
        weightValue += memSize * value;
        sum += memSize;
    }
    auto metricValue = weightValue / sum; // 浮点数除法，允许nan和inf
    singleMetirc["VALUE"].emplace_back(metricValue);
}

static void AggregateMetricByMean(nlohmann::json &pods, nlohmann::json &singleMetirc, uint32_t index)
{
    if (singleMetirc["TYPE"] == "counter" || singleMetirc["TYPE"] == "gauge") {
        uint32_t podCount = pods.size();
        double sum = 0;
        for (uint32_t i = 0; i < podCount; i++) {
            double value = pods[i]["metrics"][index]["VALUE"][0];
            sum += value;
        }
        double metricValue = sum / static_cast<double>(podCount); // 浮点数除法，允许nan和inf
        singleMetirc["VALUE"].emplace_back(metricValue);
        return;
    }
}

static void AggregateMetricBySum(nlohmann::json &pods, nlohmann::json &singleMetirc, uint32_t index)
{
    if (singleMetirc["TYPE"] == "counter" || singleMetirc["TYPE"] == "gauge") {
        double sum = 0;
        for (uint32_t i = 0; i < pods.size(); i++) {
            double value = pods[i]["metrics"][index]["VALUE"][0];
            sum += value;
        }
        singleMetirc["VALUE"].emplace_back(sum);
        return;
    }
    uint32_t blockNum = singleMetirc["LABEL"].size();
    std::vector<double> sumArr(blockNum, 0);
    for (uint32_t i = 0; i < blockNum; i++) {
        for (uint32_t j = 0; j < pods.size(); j++) {
            double value = pods[j]["metrics"][index]["VALUE"][i];
            sumArr[i] += value;
        }
    }
    singleMetirc["VALUE"] = sumArr;
}

nlohmann::json Metrics::AggregateMetrics(nlohmann::json &podMetric, const std::map<std::string, uint64_t>& stats) const
{
    uint64_t numAllRequests = stats.at("numAllRequests");
    uint64_t numFailRequests = stats.at("numFailRequests");
    uint64_t numSuccessRequests = stats.at("numSuccessRequests");
    nlohmann::json aggregate = nlohmann::json::array();
    uint32_t failReqIndex = 0;
    for (uint32_t index = 0; index < metricCount; index++) {
        auto pod = podMetric.at(0);
        nlohmann::json singleMetirc = {{"NAME", ""}, {"HELP", ""}, {"TYPE", ""}};
        singleMetirc["LABEL"] = nlohmann::json::array();
        singleMetirc["VALUE"] = nlohmann::json::array();
        singleMetirc["NAME"] = pod["metrics"][index]["NAME"];
        singleMetirc["HELP"] = pod["metrics"][index]["HELP"];
        singleMetirc["TYPE"] = pod["metrics"][index]["TYPE"];
        singleMetirc["LABEL"] = pod["metrics"][index]["LABEL"];
        std::map<std::string, uint64_t> statsMetrics;
        statsMetrics["numAllRequests"] = numAllRequests;
        statsMetrics["numFailRequests"] = numFailRequests;
        statsMetrics["numFailRequests"] = numFailRequests;
        statsMetrics["numSuccessRequests"] = numSuccessRequests;
        statsMetrics["index"] = index;
        statsMetrics["failReqIndex"] = failReqIndex;
        failReqIndex = ProcessSingleMetric(podMetric, singleMetirc, statsMetrics, aggregate);
    }
    if (aggregate.size() > failReqIndex &&
        aggregate[failReqIndex]["NAME"] == "failed_request_perc") {
        double failedRate = 0.0;
        if (numAllRequests > 0) {
            failedRate = static_cast<double>(numFailRequests) / numAllRequests;
        }
        if (aggregate[failReqIndex]["VALUE"].empty()) {
            aggregate[failReqIndex]["VALUE"].emplace_back(failedRate);
        } else {
            aggregate[failReqIndex]["VALUE"][0] = failedRate;
        }
        double percentageNumber = 100.0;
        double percentageScore = static_cast<int>(failedRate * percentageNumber);
        LOG_M("[Metrics] Updated failed request percentage: %.1f%%", percentageScore);
    }
    return aggregate;
}

uint64_t Metrics::ProcessSingleMetric(nlohmann::json &podMetric, nlohmann::json &singleMetric,
    const std::map<std::string, uint64_t>& statsMetrics, nlohmann::json &aggregate) const
{
    uint64_t numAllRequests = statsMetrics.at("numAllRequests");
    uint64_t numFailRequests = statsMetrics.at("numFailRequests");
    uint64_t numSuccessRequests = statsMetrics.at("numSuccessRequests");
    uint64_t index = (statsMetrics.at("index"));
    uint64_t failReqIndex = statsMetrics.at("failReqIndex");
    if (singleMetric["NAME"] == "npu_cache_usage_perc" || singleMetric["NAME"] == "cpu_cache_usage_perc") {
        AggregateCacheUsagePerc(podMetric, singleMetric, index);
        aggregate.emplace_back(singleMetric);
        return failReqIndex;
    }
    if (singleMetric["NAME"] == "npu_prefix_cache_hit_rate") {
        AggregateMetricByMean(podMetric, singleMetric, index);
        aggregate.emplace_back(singleMetric);
        return failReqIndex;
    }
    if (singleMetric["NAME"] == "failed_request_perc") {
        uint64_t newfailReqIndex = aggregate.size();
        aggregate.emplace_back(singleMetric);
        return newfailReqIndex;
    }
    if (singleMetric["NAME"] == "request_received_total") {
        singleMetric["VALUE"].emplace_back(static_cast<double>(numAllRequests));
        aggregate.emplace_back(singleMetric);
        LOG_M("[Metrics] Updated request received total: %lu", numAllRequests);
        return failReqIndex;
    }
    if (singleMetric["NAME"] == "request_failed_total") {
        singleMetric["VALUE"].emplace_back(static_cast<double>(numFailRequests));
        aggregate.emplace_back(singleMetric);
        LOG_M("[Metrics] Updated request failed total: %lu", numFailRequests);
        return failReqIndex;
    }
    if (singleMetric["NAME"] == "request_success_total") {
        singleMetric["VALUE"].emplace_back(static_cast<double>(numSuccessRequests));
        aggregate.emplace_back(singleMetric);
        LOG_M("[Metrics] Updated request success total: %lu", numSuccessRequests);
        return failReqIndex;
    }
    AggregateMetricBySum(podMetric, singleMetric, index);
    aggregate.emplace_back(singleMetric);
    return failReqIndex;
}

nlohmann::json Metrics::GetServerMetircs(const std::map<uint64_t, InstanceInfo> &podInfos) const
{
    nlohmann::json collects;
    int32_t code = 400; // 400 bad request
    std::map<boost::beast::http::field, std::string> headMap;
    headMap[boost::beast::http::field::content_type] = "";
    Request req = {"/metrics", boost::beast::http::verb::get, headMap, ""}; // use GET to query mindie-server's metrics
    for (const auto &pair: podInfos) {
        nlohmann::json podCollect;
        auto id = pair.first;
        auto ip = pair.second.ip;
        auto metricPort = pair.second.metricPort;
        podCollect["ip"] = ip;
        podCollect["port"] = metricPort;
        podCollect["identity"] = std::to_string(static_cast<char>(pair.second.role));
        podCollect["NPU_mem_size"] = pair.second.totalBlockNum;

        LOG_M("[Get] Get mindie-server metric: ID %lu, IP %s, port %s.",
            id, ip.c_str(), metricPort.c_str());
        std::string response = "";
        HttpClient client;
        client.Init(ip, metricPort, Configure::Singleton()->mindieMgmtTlsItems);
        auto httpRet = client.SendRequest(req, Configure::Singleton()->httpConfig.httpTimeoutS,
            Configure::Singleton()->exceptionConfig.maxRetry, response, code);
        if (httpRet != 0 || code != 200 || response.empty()) { // 200 ok
            LOG_E("[%s] [Metrics] Get mindie-server{%lu} metrics failed, IP %s, port %s, "
                "ret code %d, request ret %d.",
                GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::METRICS).c_str(),
                id, ip.c_str(), metricPort.c_str(), code, httpRet);
            return {};
        }
        LOG_D("[Metrics] Get metric response from server, %s", response.c_str());
        podCollect["metrics_str"] = response;
        collects.push_back(podCollect);
    }
    return collects;
}

// Write a double as a string, with proper formatting for infinity and NaN
static void WriteValue(std::ostream& out, double value)
{
    if (std::isnan(value)) {
        out << "Nan";
    } else if (std::isinf(value)) {
        out << (value < 0 ? "-Inf" : "+Inf");
    } else {
        out << value;
    }
}

// 函数中json的内容是由前置流程保证写入的类型，理论上不会get失败，GetAndAggregateMetrics的try只是为了保险
std::string Metrics::SerializeMetrics(const nlohmann::json &metrics) const
{
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out.precision(std::numeric_limits<double>::max_digits10 - 1);
    for (size_t i = 0; i < metrics.size(); i++) {
        const auto &metricJson = metrics.at(i);
        std::string name = metricJson.at("NAME").template get<std::string>();
        out << "# HELP " << name << " " << metricJson.at("HELP").template get<std::string>() << "\n";
        out << "# TYPE " << name << " " << metricJson.at("TYPE").template get<std::string>() << "\n";
        for (size_t j = 0; j < metricJson.at("VALUE").size(); j++) {
            out << metricJson.at("LABEL").at(j).template get<std::string>() << " ";
            WriteValue(out, metricJson.at("VALUE").at(j).template get<double>());
            out << "\n";
        }
    }
    return out.str();
}

void Metrics::ComputeSeqLenTableWidths(const std::vector<int>& bounds, int& colWidth, int& maxLabelWidth,
    int& totalWidth) const
{
    constexpr int TABLE_MIN_COL_WIDTH = 6;
    constexpr int TABLE_MIN_LABEL_WIDTH = 11;
    constexpr int TABLE_COL_SEP_WIDTH = 1;

    int maxNumWidth = 0;
    for (int b : bounds) {
        int w = static_cast<int>(std::to_string(b).length());
        if (w > maxNumWidth) {
            maxNumWidth = w;
        }
    }
    int maxCellValue = 0;
    for (const auto& inPair : token_distribution) {
        for (const auto& outPair : inPair.second) {
            if (outPair.second > maxCellValue) {
                maxCellValue = outPair.second;
            }
        }
    }
    int maxValueDigits = static_cast<int>(std::to_string(maxCellValue).length());
    colWidth = std::max({maxNumWidth, maxValueDigits, TABLE_MIN_COL_WIDTH});
    maxLabelWidth = TABLE_MIN_LABEL_WIDTH;
    for (int b : bounds) {
        int labelWidth = 5 + static_cast<int>(std::to_string(b).length());
        if (labelWidth > maxLabelWidth) {
            maxLabelWidth = labelWidth;
        }
    }
    totalWidth = maxLabelWidth + static_cast<int>(bounds.size()) * (colWidth + TABLE_COL_SEP_WIDTH);
}

void Metrics::PrintTokenDistribution() const
{
    const std::vector<int>& bounds = token_ranges;
    int colWidth = 0;
    int maxLabelWidth = 0;
    int totalWidth = 0;
    ComputeSeqLenTableWidths(bounds, colWidth, maxLabelWidth, totalWidth);
    std::string startLine(totalWidth, '=');
    std::string sepLine(totalWidth, '-');
    LOG_M("[Metrics][Seq_Len_Table] %s", startLine.c_str());
    std::stringstream header;
    header << std::setfill('_') << std::left << std::setw(maxLabelWidth) << "OUTPUT";
    for (size_t col = 0; col < bounds.size(); col++) {
        header << "|" << std::setw(colWidth) << bounds[col];
    }
    LOG_M("[Metrics][Seq_Len_Table] %s", header.str().c_str());
    LOG_M("[Metrics][Seq_Len_Table] %s", sepLine.c_str());
    for (size_t in = 0; in < bounds.size(); in++) {
        std::stringstream row;
        std::string label = "INPUT" + std::to_string(bounds[in]);
        row << std::setfill('_') << std::left << std::setw(maxLabelWidth) << label;
        for (size_t out = 0; out < bounds.size(); out++) {
            int value = 0;
            auto it = token_distribution.find(bounds[in]);
            if (it != token_distribution.end()) {
                auto it2 = it->second.find(bounds[out]);
                if (it2 != it->second.end()) {
                    value = it2->second;
                }
            }
            row << "|" << std::setw(colWidth) << value;
        }
        LOG_M("[Metrics][Seq_Len_Table] %s", row.str().c_str());
    }
    LOG_M("[Metrics][Seq_Len_Table] %s", startLine.c_str());
}

std::string Metrics::GetAndAggregateMetrics(const std::map<uint64_t, InstanceInfo> &podInfos)
{
    LOG_D("[Metrics]GetAndAggregateMetrics");
    auto collects = GetServerMetircs(podInfos);
    if (collects.empty()) {
        LOG_W("[%s] [Metrics] Get mindie-server metrics failed!",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::METRICS).c_str());
        return {};
    }
    auto errCode = ParseMetrics(collects);
    if (errCode != 0) {
        LOG_E("[%s] [Metrics] Parse mindie-server metrics failed!",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::METRICS).c_str());
        return {};
    }
    auto stats = ReqManage::GetInstance().StatRequest();
    auto aggregatedMetrics = AggregateMetrics(collects, stats);
    if (aggregatedMetrics.empty()) {
        LOG_E("[%s] [Metrics] Aggregate mindie-server metrics failed!",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::METRICS).c_str());
        return {};
    }
    std::string ret = "";
    try {
        ret = SerializeMetrics(aggregatedMetrics);
    } catch (const std::exception &e) {
        LOG_E("[%s] [Metrics] Serialize metrics failed with exception %s\n",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::METRICS).c_str(),
            e.what());
    }
    PrintTokenDistribution();
    return ret;
}

void TryUpdateTokenDistributionFromUsage(const std::string& body)
{
    if (body.empty() || body.find("usage") == std::string::npos) {
        return;
    }
    std::string toParse = body;
    const std::string dataPrefix = "data: ";
    if (body.size() >= dataPrefix.size() && body.substr(0, dataPrefix.size()) == dataPrefix) {
        size_t start = dataPrefix.size();
        size_t end = body.find('\n', start);
        toParse = (end != std::string::npos) ? body.substr(start, end - start) : body.substr(start);
        if (toParse == "[DONE]") {
            return;
        }
    }
    try {
        nlohmann::json jsonData = nlohmann::json::parse(toParse);
        if (!jsonData.contains("usage") || !jsonData["usage"].contains("prompt_tokens") ||
            !jsonData["usage"].contains("completion_tokens")) {
            return;
        }
        int inputTokens = jsonData["usage"]["prompt_tokens"].get<int>();
        int outputTokens = jsonData["usage"]["completion_tokens"].get<int>();
        int inputRange = token_ranges.back();
        for (int range : token_ranges) {
            if (inputTokens <= range) {
                inputRange = range;
                break;
            }
        }
        int outputRange = token_ranges.back();
        for (int range : token_ranges) {
            if (outputTokens <= range) {
                outputRange = range;
                break;
            }
        }
        token_distribution[inputRange][outputRange]++;
    } catch (const nlohmann::json::exception &) {
    }
}
}