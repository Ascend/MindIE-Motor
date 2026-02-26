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
#include "CoordinatorRequestHandler.h"
#include <string>
#include "CCAERequestHandler.h"
namespace MINDIE::MS {
constexpr int32_t CODE_OK = 200;

const size_t HELP_PREFIX_LENGTH = 7;  // Length of "# HELP "
const size_t TYPE_PREFIX_LENGTH = 7;  // Length of "# TYPE "
const size_t SEND_NODE_STATUS_INFO_LOG_FREQUENCY = 60;
const uint32_t SHIFT_OFFSET_32 = 32;

struct MetricData {
    std::string name;
    std::string help;
    std::string type;
    std::unordered_map<std::string, double> values;
};

bool StartsWith(const std::string& str, const std::string& prefix)
{
    if (str.length() < prefix.length()) {
        return false;
    }
    return str.compare(0, prefix.length(), prefix) == 0;
}

void ParseMetricLine(const std::string& line, std::unordered_map<std::string, double>& values)
{
    size_t lastSpacePos = line.find_last_of(' ');
    if (lastSpacePos == std::string::npos) {
        LOG_E("[CoordinatorRequestHandler] The metric format of line %s is invalid.", line.c_str());
        return;
    }
    std::string key = line.substr(0, lastSpacePos);
    std::string valueStr = line.substr(lastSpacePos + 1); // get value
    double value = std::stod(valueStr);

    size_t curlyBracePos = key.find('{');
    if (curlyBracePos != std::string::npos) {
        size_t bracketLen = 2;
        if (key.back() != '}' || key.size() - curlyBracePos - bracketLen <= 0) {
            LOG_E("[CoordinatorRequestHandler] The metric format of line %s is invalid.", line.c_str());
            return;
        }
        std::string metricName = key.substr(0, curlyBracePos);
        std::string labels = key.substr(curlyBracePos + 1, key.size() - curlyBracePos - 2); // Remove the final '}'
        values[metricName + labels] = value;
    } else {
        values[key] = value;
    }
}

bool IsValidMetricFormat(MetricData& currentMetric, const std::string& line, std::vector<MetricData>& metrics)
{
    if (StartsWith(line, "# HELP")) {
        size_t spacePos = line.find(' ', HELP_PREFIX_LENGTH); // skip "# HELP"
        if (spacePos != std::string::npos) {
            if (!currentMetric.name.empty() && !currentMetric.type.empty()) {
                currentMetric = {};
            }
            currentMetric.name = line.substr(HELP_PREFIX_LENGTH, spacePos - HELP_PREFIX_LENGTH);
            currentMetric.help = line.substr(spacePos + 1); // get help information
        } else {
            LOG_E("[CoordinatorRequestHandler] The format of # HELP line %s is invalid.", line.c_str());
            return false;
        }
    } else if (StartsWith(line, "# TYPE")) {
        size_t spacePos = line.find(' ', TYPE_PREFIX_LENGTH); // skip "# TYPE"
        if (spacePos != std::string::npos) {
            currentMetric.type = line.substr(spacePos + 1); // get type information
            if (currentMetric.type != "counter" && currentMetric.type != "gauge" && currentMetric.type != "histogram") {
                LOG_E("[CoordinatorRequestHandler] The metric type of line %s is invalid.", line.c_str());
                return false;
            }
        } else {
            LOG_E("[CoordinatorRequestHandler] The format of # TYPE line %s is invalid.", line.c_str());
            return false;
        }
    } else if (!StartsWith(line, "#")) {
        if (!currentMetric.name.empty() && !currentMetric.type.empty()) {
            ParseMetricLine(line, currentMetric.values);
            metrics.push_back(currentMetric);
        } else {
            LOG_E("[CoordinatorRequestHandler] Line %s contains metrics data "
                "before # HELP or # TYPE line.", line.c_str());
            return false;
        }
    }
    return true;
}

std::vector<MetricData> ParsePrometheusData(const std::string& data)
{
    std::vector<MetricData> metrics;
    std::istringstream iss(data);
    std::string line;
    MetricData currentMetric;

    while (std::getline(iss, line)) {
        line.erase(0, line.find_first_not_of(" \t"));
        line.erase(line.find_last_not_of(" \t") + 1); // Remove trailing spaces

        if (line.empty()) {
            continue;
        }
        
        if (!IsValidMetricFormat(currentMetric, line, metrics)) {
            LOG_E("[CoordinatorRequestHandler] Parse prometheus data failed.");
        }
    }

    return metrics;
}

static bool IsValidateCounterMetric(const MetricData& metric)
{
    for (const auto& [key, value] : metric.values) {
        // 计数器类型的值不能为负数
        if (value < 0) {
            LOG_E("[CoordinatorRequestHandler] The value of counter metric %s which is negative for key %s is %f.",
                metric.name.c_str(), key.c_str(), value);
            return false;
        }
    }
    return true;
}

static bool IsValidateHistogramMetric(const MetricData& metric)
{
    double sum = 0.0;
    double count = 0.0;
    for (const auto& [key, value] : metric.values) {
        if (key == metric.name + "_sum") {
            sum = value;
        } else if (key == metric.name + "_count") {
            count = value;
        } else if (key.find(metric.name + "_bucket") != std::string::npos) {
            if (value < 0) {
                LOG_E("[CoordinatorRequestHandler] The value of histogram bucket %s which is negative is %f.",
                    key.c_str(), value);
                return false;
            }
        }
    }
    if (sum < 0 || count < 0) {
        LOG_E("[CoordinatorRequestHandler] The sum or count of histogram %s is invalid. Sum is %f, count is %f.",
            metric.name.c_str(), sum, count);
        return false;
    }
    return true;
}

static bool IsValidateGaugeMetric(const MetricData& metric)
{
    for (const auto& [key, value] : metric.values) {
        if (value < 0) {
            LOG_E("[CoordinatorRequestHandler] The value of gauge metric %s which is negative for key %s is %f.",
                metric.name.c_str(), key.c_str(), value);
            return false;
        }
    }
    return true;
}

static bool IsValidMetrics(const std::string& metricInfo)
{
    std::vector<MetricData> metrics = ParsePrometheusData(metricInfo);

    for (const auto& metric : metrics) {
        if (!metric.name.empty() && !metric.type.empty()) {
            if (metric.type == "counter" && !IsValidateCounterMetric(metric)) {
                LOG_E("[CoordinatorRequestHandler] Counter metric is invalid.");
                return false;
            } else if (metric.type == "histogram" && !IsValidateHistogramMetric(metric)) {
                LOG_E("[CoordinatorRequestHandler] Histogram metric is invalid.");
                return false;
            } else if (metric.type == "gauge" && !IsValidateGaugeMetric(metric)) {
                LOG_E("[CoordinatorRequestHandler] Gauge metric is invalid.");
                return false;
            }
        }
    }

    return true;
}

void CoordinatorRequestHandler::SetRunStatus(bool run)
{
    return mRun.store(run);
}

bool CoordinatorRequestHandler::ShouldPrintInfoLogWhenSendNodeStatus() const
{
    uint64_t sendStatusRounds = mSendStatusRoundCounter.fetch_add(1, std::memory_order_relaxed);
    return (sendStatusRounds % SEND_NODE_STATUS_INFO_LOG_FREQUENCY) == 0;
}

bool CoordinatorRequestHandler::SendNodeStatus2SingleCoordinatorNode(std::shared_ptr<HttpClient> client,
    std::shared_ptr<NodeStatus> nodeStatus, std::shared_ptr<CoordinatorStore> coordinatorStore,
    const std::unique_ptr<Coordinator>& node, bool printInfoLog) const
{
    auto ret = -1;
    auto port = ControllerConfig::GetInstance()->GetCoordinatorPort();
    auto deployMode = ControllerConfig::GetInstance()->GetDeployMode();

    if (printInfoLog) {
        LOG_I("[CoordinatorRequestHandler] Sending node status, start target coordinator, IP %s.", node->ip.c_str());
    }

    std::string response;
    int32_t code = 400;
    std::string jsonString;
    try {
        jsonString = GenerateNodeStatusStr(nodeStatus, deployMode, printInfoLog);
    }  catch (const std::exception& e) {
        LOG_E("[%s] [CoordinatorRequestHandler] Sending node status, failed to generate string, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str(), e.what());
        return false;
    }
    std::map <boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    client->SetHostAndPort(node->ip, port);
    Request req = {ControllerConstant::GetInstance()->GetCoordinatorURI(CoordinatorURI::POST_REFRESH),
        boost::beast::http::verb::post, map, jsonString};
    LOG_D("[CoordinatorRequestHandler] Sending node status, IP %s, port %s, body %s.", node->ip.c_str(),
        port.c_str(), jsonString.c_str());
    ret = client->SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
        ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
    if (ret != 0 || code != CODE_OK) {
        LOG_E("[%s] [CoordinatorRequestHandler] Sending node status, send request failed, IP %s, port %s, "
            "ret code %d, request ret %d",
            GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str(),
            node->ip.c_str(), port.c_str(), code, ret);
        coordinatorStore->UpdateCoordinatorStatus(node->ip, false);
        CCAERequestHandler::SetcoordinatorHealthy(false);
        return false;
    }
    CCAERequestHandler::SetcoordinatorHealthy(true);
    coordinatorStore->UpdateCoordinatorStatus(node->ip, true);
    
    return true;
}

void CoordinatorRequestHandler::SendNodeStatus(std::shared_ptr<HttpClient> client,
    std::shared_ptr<NodeStatus> nodeStatus, std::shared_ptr<CoordinatorStore> coordinatorStore) const
{
    if (!mRun.load()) {
        LOG_I("[CoordinatorRequestHandler] Synchronization is off, so node status sending is skipped.");
        return;
    }

    bool printInfoLog = ShouldPrintInfoLogWhenSendNodeStatus();

    std::vector<std::unique_ptr<Coordinator>> coordinatorNodes;
    try {
        coordinatorNodes = coordinatorStore->GetCoordinators();
    } catch (const std::exception& e) {
        LOG_E("[%s] [CoordinatorRequestHandler] Failed to send node status, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str(), e.what());
        return;
    }

    for (auto &node : std::as_const(coordinatorNodes)) {
        if (!SendNodeStatus2SingleCoordinatorNode(client, nodeStatus, coordinatorStore, node, printInfoLog)) {
            LOG_E("[%s] [CoordinatorRequestHandler] Failed to send node status, ip is %s.",
                  GetErrorCode(ErrorType::EXCEPTION, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str(),
                  node->ip.c_str()
                  );
        }
    }
}

void CoordinatorRequestHandler::SendMetricsRequest(
    std::shared_ptr<HttpClient> client,
    std::shared_ptr<CoordinatorStore> coordinatorStore,
    std::vector<std::unique_ptr<Coordinator>> &coordinatorNodes,
    std::string &response) const
{
    auto ret = -1;
    auto port = ControllerConfig::GetInstance()->GetCoordinatorExternalPort();

    for (auto &node : std::as_const(coordinatorNodes)) {
        LOG_I("[CoordinatorRequestHandler] Getting metrics information, start target coordinator, IP %s.",
            node->ip.c_str());
        int32_t code = 400;
        std::map <boost::beast::http::field, std::string> map;
        map[boost::beast::http::field::accept] = "*/*";
        map[boost::beast::http::field::content_type] = "application/json";
        client->SetHostAndPort(node->ip, port);
        Request req = {ControllerConstant::GetInstance()->GetCoordinatorURI(CoordinatorURI::GET_METRICS),
                       boost::beast::http::verb::get, map, ""};
        LOG_D("[CoordinatorRequestHandler] Getting metrics information, IP %s, port %s.", node->ip.c_str(),
              port.c_str());
        ret = client->SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
            ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
        if (ret != 0 || code != CODE_OK) {
            LOG_E("[%s] [CoordinatorRequestHandler] Getting metrics information, send request failed, IP %s, port %s, "
                  "ret code %d, request ret %d",
                  GetErrorCode(ErrorType::UNREACHABLE, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str(),
                  node->ip.c_str(), port.c_str(), code, ret);
            coordinatorStore->UpdateCoordinatorStatus(node->ip, false);
            return;
        }
        if (response.empty()) {
            LOG_E("[CoordinatorRequestHandler] Response is empty, generate Prometheus metrics failed.");
            return;
        }
        if (!IsValidMetrics(response)) {
            LOG_E("[CoordinatorRequestHandler] Metric information is invalid.");
            return;
        }
        coordinatorStore->UpdateCoordinatorStatus(node->ip, true);
    }
}

static void SetStaticInfo(nlohmann::json &staticInfo, const NodeInfo &nodeInfo)
{
    staticInfo["group_id"] = nodeInfo.instanceInfo.staticInfo.groupId;
    staticInfo["max_seq_len"] = nodeInfo.instanceInfo.staticInfo.maxSeqLen;
    staticInfo["virtual_id"] = nodeInfo.instanceInfo.staticInfo.virtualId;
    staticInfo["max_output_len"] = nodeInfo.instanceInfo.staticInfo.maxOutputLen;
    staticInfo["total_slots_num"] = nodeInfo.instanceInfo.staticInfo.totalSlotsNum;
    staticInfo["total_block_num"] = nodeInfo.instanceInfo.staticInfo.totalBlockNum;
    staticInfo["block_size"] = nodeInfo.instanceInfo.staticInfo.blockSize;
    staticInfo["label"] = nodeInfo.instanceInfo.staticInfo.label;
    staticInfo["role"] = nodeInfo.instanceInfo.staticInfo.role;
    staticInfo["p_percentage"] = nodeInfo.instanceInfo.staticInfo.flexPRatio;
}

static void SetPeers(nlohmann::json &dynamicInfo, const NodeInfo &nodeInfo, std::shared_ptr<NodeStatus> nodeStatus)
{
    dynamicInfo["peers"] = nlohmann::json::array();
    for (auto &peer : nodeInfo.activePeers) {
        auto it = std::find_if(nodeInfo.peers.begin(), nodeInfo.peers.end(),
            [&peer](const uint64_t &id) { return id == peer; });
        if (it == nodeInfo.peers.end()) {
            continue;
        }
        if (!nodeStatus->IsNodeLinkedByPeer(peer, nodeInfo.instanceInfo.staticInfo.id)) {
            continue;
        }
        if (nodeStatus->IsIgnoredInPDSeparate(peer)) {
            LOG_D("[CoordinatorRequestHandler] Node %lu ignore peers %lu.", nodeInfo.instanceInfo.staticInfo.id,
                  peer);
            continue;
        }
        dynamicInfo["peers"].emplace_back(peer);
    }
}

static void FillNode(nlohmann::json &node, nlohmann::json &dynamicInfo, const NodeInfo &nodeInfo)
{
    auto port = nodeInfo.port;
    auto metricPort = nodeInfo.metricPort;
    dynamicInfo["avail_slots_num"] = nodeInfo.instanceInfo.dynamicInfo.availSlotsNum;
    dynamicInfo["avail_block_num"] = nodeInfo.instanceInfo.dynamicInfo.availBlockNum;
    dynamicInfo["waiting_request_num"] = nodeInfo.instanceInfo.dynamicInfo.waitingRequestNum;
    dynamicInfo["running_request_num"] = nodeInfo.instanceInfo.dynamicInfo.runningRequestNum;
    dynamicInfo["swapped_request_num"] = nodeInfo.instanceInfo.dynamicInfo.swappedRequestNum;
    dynamicInfo["free_npu_block_nums"] = nodeInfo.instanceInfo.dynamicInfo.freeNpuBlockNums;
    dynamicInfo["free_cpu_block_nums"] = nodeInfo.instanceInfo.dynamicInfo.freeCpuBlockNums;
    dynamicInfo["total_npu_block_nums"] = nodeInfo.instanceInfo.dynamicInfo.totalNpuBlockNums;
    dynamicInfo["total_cpu_block_nums"] = nodeInfo.instanceInfo.dynamicInfo.totalCpuBlockNums;
    node["dynamic_info"] = dynamicInfo;
    node["id"] = nodeInfo.instanceInfo.staticInfo.id;
    node["ip"] = nodeInfo.ip;
    node["model_name"] = nodeInfo.modelName;
    node["port"] = port;
    node["metric_port"] = metricPort;
    node["inter_comm_port"] = nodeInfo.interCommPort;
    nlohmann::json staticInfo;
    SetStaticInfo(staticInfo, nodeInfo);
    node["static_info"] = staticInfo;
}

template <typename T>
bool IsIgnoreNode(DeployMode deployMode, std::shared_ptr<NodeStatus> nodeStatus, const T &it)
{
    if (deployMode == DeployMode::PD_SEPARATE && nodeStatus->IsIgnoredInPDSeparate(it.first)) {
        LOG_D("[CoordinatorRequestHandler] Generating server status string, ignore server %lu, IP %s, healthy %d, "
              "initialized %d, role state %s in PD separate mode.",
              it.first, it.second->ip.c_str(), it.second->isHealthy, it.second->isInitialized,
              it.second->roleState.c_str());
        return false;
    }
    if (deployMode == DeployMode::SINGLE_NODE && nodeStatus->IsIgnoredInSingleNode(it.first)) {
        LOG_D("[CoordinatorRequestHandler] Generating server status string, ignore server %lu, IP %s, healthy %d, "
              "initialized %d, role state %s in single node mode.",
              it.first, it.second->ip.c_str(), it.second->isHealthy, it.second->isInitialized,
              it.second->roleState.c_str());
        return false;
    }
    return true;
}

void GenerateSingleNodeStatusStr(nlohmann::json& nodes, nlohmann::json& node, nlohmann::json& dynamicInfo,
    const std::pair<const uint64_t, std::unique_ptr<NodeInfo>>& it)
{
    FillNode(node, dynamicInfo, *it.second);
    nodes["instances"].emplace_back(node);
    nodes["ids"].emplace_back(it.first);

    LOG_D("[CoordinatorRequestHandler] Generating server status string, ID %lu, role %c.", it.first,
        it.second->instanceInfo.staticInfo.role);
}

std::string CoordinatorRequestHandler::GenerateNodeStatusStr(std::shared_ptr<NodeStatus> nodeStatus,
    DeployMode deployMode, bool printInfoLog) const
{
    nlohmann::json nodes;
    nodes["instances"] = nlohmann::json::array();
    nodes["ids"] = nlohmann::json::array();
    auto allNodes = nodeStatus->GetAllNodes();
    std::pair<size_t, size_t> pdNodeNum = {0, 0};
    for (auto &it : std::as_const(allNodes)) {
        if (!IsIgnoreNode(deployMode, nodeStatus, it)) {
            continue;
        }
        if (it.second->inferenceType == InferenceType::UNAVAILABLE) {
            LOG_D("[CoordinatorRequestHandler] Skip UNAVAILABLE node %lu, IP %s when sending to coordinator.",
                it.first, it.second->ip.c_str());
            continue;
        }
        nlohmann::json node;
        nlohmann::json dynamicInfo;
        if (it.second->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::DECODE_INSTANCE ||
            it.second->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE ||
            it.second->instanceInfo.staticInfo.role == MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
            SetPeers(dynamicInfo, *it.second, nodeStatus);
            if (it.second->instanceInfo.staticInfo.role != MINDIE::MS::DIGSInstanceRole::FLEX_INSTANCE) {
                std::string pdRole = it.second->instanceInfo.staticInfo.role ==
                                    MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE ? "prefill" : "decode";
                if (dynamicInfo["peers"].empty()) {
                    LOG_D("[%s] [CoordinatorRequestHandler] Generating server status string, ignore %s server %lu, "
                        "IP %s, which has empty available peers.",
                        GetWarnCode(ErrorType::WARNING, ControllerFeature::COORDINATOR_REQUEST_HANDLER).c_str(),
                        pdRole.c_str(), it.first, it.second->ip.c_str());
                    continue;
                }
                if (pdRole == "decode") {
                    pdNodeNum.first++;
                } else {
                    pdNodeNum.second++;
                }
            }
        }
        GenerateSingleNodeStatusStr(nodes, node, dynamicInfo, it);
    }
    uint64_t concatPDNodeNum = (static_cast<uint64_t>(pdNodeNum.first) << SHIFT_OFFSET_32) |
                                static_cast<uint64_t>(pdNodeNum.second);
    uint64_t lastConcatPDNodeNum = mLastPDNodeCounter.exchange(concatPDNodeNum, std::memory_order_relaxed);
    if (lastConcatPDNodeNum != concatPDNodeNum || printInfoLog) {
        LOG_I("[CoordinatorRequestHandler] Generating server status string, number of servers in body is %zu."
            "(%zu P + %zu D)", nodes["instances"].size(), pdNodeNum.second, pdNodeNum.first);
    }
    return nodes.dump();
};
}