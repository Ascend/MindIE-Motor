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
#include <nlohmann/json.hpp>
#include <boost/beast/http.hpp>
#include "ControllerConfig.h"
#include "ControllerConstant.h"
#include "Logger.h"
#include "Util.h"
#include "NodeManagerRequestSender.h"

namespace MINDIE {
namespace MS {

constexpr int32_t CODE_OK = 200;

void NodeManagerRequestSender::Init(std::shared_ptr<NodeStatus> nodeStatus)
{
    mNodeStatus = nodeStatus;
}

int32_t NodeManagerRequestSender::SendCommandToNodeManager(HttpClient& client,
                                                           const std::string& nodeManagerIP, NodeManagerCmd cmd)
{
    std::string url = ControllerConstant::GetInstance()->GetNodeManagerURI(NodeManagerURI::SEND_FAULT_COMMAND);
    nlohmann::json bodyJson;
    bodyJson["cmd"] = NodeManagerCmdToString(cmd);
    std::string body = bodyJson.dump();
    auto port = ControllerConfig::GetInstance()->GetNodeManagerPort();
    std::map<boost::beast::http::field, std::string> headers;
    headers[boost::beast::http::field::accept] = "*/*";
    headers[boost::beast::http::field::content_type] = "application/json";

    client.SetHostAndPort(nodeManagerIP, port);
    Request req = {url, boost::beast::http::verb::post, headers, body};

    // 添加DEBUG日志，记录完整请求信息 todo删掉
    try {
        auto jsonBody = nlohmann::json::parse(body, CheckJsonDepthCallBack);
        std::string prettyBody = jsonBody.dump(4);
        LOG_D("[NodeManagerRequestSender] Sending request to NodeManager:\n"
              "Target: %s:%s\n"
              "URL: %s\n"
              "Method: POST\n"
              "Headers: Content-Type: application/json, Accept: */*\n"
              "Body (JSON):\n%s",
              nodeManagerIP.c_str(), port.c_str(), url.c_str(), prettyBody.c_str());
    } catch (const std::exception& e) {
        LOG_D("[NodeManagerRequestSender] Sending request to NodeManager:\n"
              "Target: %s:%s\n"
              "URL: %s\n"
              "Method: POST\n"
              "Headers: Content-Type: application/json, Accept: */*\n"
              "Body (Raw): %s\n"
              "Note: Failed to parse body as JSON for pretty printing: %s",
              nodeManagerIP.c_str(), port.c_str(), url.c_str(), body.c_str(), e.what());
    }

    std::string response;
    int32_t code = 400;
    int32_t ret = client.SendRequest(req, cmd == NodeManagerCmd::STOP_ENGINE ? 10 : 90,
                                     0, response, code); // stop_engine 需要等待10秒，其他指令需要等待90秒
    if (ret != 0 || code != CODE_OK) {
        LOG_E("[NodeManagerRequestSender] Send fault info to NodeManager %s:%s failed, cmd: %s, ret: %d, code: %d",
              nodeManagerIP.c_str(), port.c_str(), NodeManagerCmdToString(cmd).c_str(), ret, code);
        return -1;
    }

    LOG_I("[NodeManagerRequestSender] Send fault info to NodeManager %s:%s successfully, cmd: %s",
          nodeManagerIP.c_str(), port.c_str(), NodeManagerCmdToString(cmd).c_str());
    return 0;
}

int32_t NodeManagerRequestSender::GetNodeManagerNodeStatus(HttpClient& client,
                                                           const std::string& nodeManagerIP, NPUStatus& status)
{
    if (nodeManagerIP.empty()) {
        LOG_I("[NodeManagerRequestSender] NodeManager IP is empty");
        return -1;
    }
    
    std::string url = ControllerConstant::GetInstance()->GetNodeManagerURI(NodeManagerURI::GET_NODE_STATUS);
    auto port = ControllerConfig::GetInstance()->GetNodeManagerPort();
    
    std::map<boost::beast::http::field, std::string> headers;
    headers[boost::beast::http::field::accept] = "*/*";

    client.SetHostAndPort(nodeManagerIP, port);
    Request req = {url, boost::beast::http::verb::get, headers, ""};

    std::string response;
    int32_t code = 400;
    int32_t ret = client.SendRequest(req, ControllerConfig::GetInstance()->GetHttpTimeoutSeconds(),
                                     ControllerConfig::GetInstance()->GetHttpRetries(), response, code);
    if (ret != 0 || code != CODE_OK) {
        LOG_E("[NodeManagerRequestSender] Get node status from NodeManager %s:%s failed, ret: %d, code: %d",
              nodeManagerIP.c_str(), port.c_str(), ret, code);
        return -1;
    }

    LOG_I("[NodeManagerRequestSender] Successfully get node status from NodeManager %s:%s",
          nodeManagerIP.c_str(), port.c_str());
    return ParseNodeStatusResponse(response, status);
}

std::string NodeManagerRequestSender::NodeManagerCmdToString(NodeManagerCmd cmd)
{
    switch (cmd) {
        case NodeManagerCmd::PAUSE_ENGINE:
            return "PAUSE_ENGINE";
        case NodeManagerCmd::REINIT_NPU:
            return "REINIT_NPU";
        case NodeManagerCmd::START_ENGINE:
            return "START_ENGINE";
        case NodeManagerCmd::STOP_ENGINE:
            return "STOP_ENGINE";
        default:
            return "UNKNOWN";
    }
}

NPUStatus NodeManagerRequestSender::StringToNPUStatus(const std::string& status)
{
    if (status == "ready") {
        return NPUStatus::READY;
    }
    if (status == "init") {
        return NPUStatus::INIT;
    }
    if (status == "normal") {
        return NPUStatus::NORMAL;
    }
    if (status == "pause") {
        return NPUStatus::PAUSE;
    }
    if (status == "abnormal") {
        return NPUStatus::ABNORMAL;
    }
    return NPUStatus::UNKNOWN;
}

int32_t NodeManagerRequestSender::ParseNodeStatusResponse(const std::string& response, NPUStatus& status)
{
    try {
        if (!nlohmann::json::accept(response)) {
            LOG_E("[NodeManagerRequestSender] Invalid JSON response: %s", response.c_str());
            return -1;
        }

        auto json = nlohmann::json::parse(response, CheckJsonDepthCallBack);
        if (!json.contains("status")) {
            LOG_E("[NodeManagerRequestSender] Missing required 'status' field in response: %s", response.c_str());
            return -1;
        }

        std::string statusStr = json["status"].get<std::string>();
        status = StringToNPUStatus(statusStr);

        return 0;
    } catch (const std::exception& e) {
        LOG_E("[NodeManagerRequestSender] Parse response failed, error: %s, response: %s", e.what(), response.c_str());
        return -1;
    }
}
} // namespace MS
} // namespace MINDIE