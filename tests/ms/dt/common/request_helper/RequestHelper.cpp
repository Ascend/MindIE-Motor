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
#include "RequestHelper.h"

#include <boost/beast/ssl.hpp>
#include <boost/beast/http.hpp>
#include <boost/beast/version.hpp>
#include <boost/beast/core.hpp>
#include <boost/asio/ssl/stream.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/config.hpp>
#include <boost/asio.hpp>
#include "Helper.h"

int32_t SendInferRequest(std::string dstIp, std::string dstPort, MINDIE::MS::TlsItems tlsItems,
    std::string url, std::string reqBody, std::string &response)
{
    std::cout << "reqBody: " << reqBody << std::endl;
    HttpClient httpClient;
    tlsItems.tlsEnable = false;
    httpClient.Init(dstIp, dstPort, tlsItems, false);

    int32_t code;

    std::map<boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    Request req = {url, boost::beast::http::verb::post, map, reqBody};
    auto ret = httpClient.SendRequest(req, 60, 3, response, code);
    std::cout << "response: " << response << std::endl;
    std::cout << "code: " << code << std::endl;
    return ret;
}

int32_t SendSSLInferRequest(std::string dstIp, std::string dstPort, MINDIE::MS::TlsItems tlsItems,
    std::string url, std::string reqBody, std::string &response)
{
    std::cout << "reqBody: " << reqBody << std::endl;
    HttpClient httpClient;
    tlsItems.tlsEnable = true;
    httpClient.Init(dstIp, dstPort, tlsItems, true);

    int32_t code;

    std::map<boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    Request req = {url, boost::beast::http::verb::post, map, reqBody};
    auto ret = httpClient.SendRequest(req, 60, 3, response, code);
    std::cout << "response: " << response << std::endl;
    std::cout << "code: " << code << std::endl;
    return ret;
}

int32_t GetInferRequest(std::string dstIp, std::string dstPort, MINDIE::MS::TlsItems tlsItems,
    std::string url, std::string &response, bool tls)
{
    HttpClient httpClient;
    tlsItems.tlsEnable = tls;
    httpClient.Init(dstIp, dstPort, tlsItems, tls);

    int32_t code;

    std::map<boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    Request req = {url, boost::beast::http::verb::get, map, ""};
    auto ret = httpClient.SendRequest(req, 60, 3, response, code);
    return code;
}

int32_t GetMetricsRequest(std::string dstIp, std::string dstPort, MINDIE::MS::TlsItems tlsItems,
    std::string url, std::string &response, bool tls)
{
    HttpClient httpClient;
    tlsItems.tlsEnable = tls;
    httpClient.Init(dstIp, dstPort, tlsItems, tls);

    int32_t code;

    std::map<boost::beast::http::field, std::string> map;
    map[boost::beast::http::field::accept] = "*/*";
    map[boost::beast::http::field::content_type] = "application/json";
    Request req = {url, boost::beast::http::verb::get, map, ""};
    auto ret = httpClient.SendRequest(req, 60, 3, response, code);
    return code;
}

bool SetSingleRole(ThreadSafeVector<std::string> &predictIPs, ThreadSafeVector<std::string> &predictPorts,
    ThreadSafeVector<std::string> &managePortLists, ThreadSafeVector<std::string> &interCommonPortLists,
    std::string &manageIP, std::string &managePort)
{
    nlohmann::json instancesInfo;
    instancesInfo["ids"] = nlohmann::json::array();
    instancesInfo["instances"] = nlohmann::json::array();

    auto peers = nlohmann::json::array();

    for (auto i = 0; i < predictIPs.size(); i++) {
        instancesInfo["ids"].emplace_back(i);
        nlohmann::json instance;
        instance["id"] = i;
        instance["ip"] = predictIPs[i];
        instance["port"] = predictPorts[i];
        instance["metric_port"] = managePortLists[i];
        instance["inter_comm_port"] = interCommonPortLists[i];
        instance["model_name"] = "llama_65b";
        nlohmann::json static_info;
        nlohmann::json dynamic_info;
        static_info["group_id"] = 0;
        static_info["max_seq_len"] = 2048;
        static_info["max_output_len"] = 512;
        static_info["total_slots_num"] = 200;
        static_info["total_block_num"] = 1024;
        static_info["p_percentage"] = 0;
        static_info["block_size"] = 128;
        static_info["label"] = 0;
        static_info["role"] = 85;
        static_info["virtual_id"] = i;

        dynamic_info["avail_slots_num"] = 200;
        dynamic_info["avail_block_num"] = 1024;
        instance["static_info"] = static_info;
        instance["dynamic_info"] = dynamic_info;
        instancesInfo["instances"].emplace_back(instance);
    }

    auto lambdaFunc = [manageIP, managePort, instancesInfo]() -> bool {
        TlsItems tlsItems;
        std::string response;
        auto ret = SendInferRequest(manageIP, managePort, tlsItems, "/v1/instances/refresh",
            instancesInfo.dump(), response);
        std::cout << "------------ SetPDRole ret ----------------" << ret << std::endl;
        if (ret == 0) {
            std::cout << "------------ SetPDRole Ready ----------------" << std::endl;
            return true;
        } else {
            return false;
        }
    };
    // 15s内1s间隔检查是否ready

    return WaitUtilTrue(lambdaFunc, 15, 1000);
}

nlohmann::json SetPDRoleByList(ThreadSafeVector<std::string> &predictIPs, ThreadSafeVector<std::string> &predictPorts,
    ThreadSafeVector<std::string> &managePortLists, ThreadSafeVector<std::string> &interCommonPortLists,
    uint8_t numPRole, uint8_t numDRole)
{
    nlohmann::json instancesInfo;
    instancesInfo["ids"] = nlohmann::json::array();
    instancesInfo["instances"] = nlohmann::json::array();

    auto peers = nlohmann::json::array();
    for (auto i = 0; i < numPRole; i++) {
        peers.emplace_back(i);
    }

    for (auto i = 0; i < numPRole + numDRole; i++) {
        instancesInfo["ids"].emplace_back(i);
        nlohmann::json instance;
        instance["id"] = i;
        instance["ip"] = predictIPs[i];
        instance["port"] = predictPorts[i];
        instance["metric_port"] = managePortLists[i];
        instance["inter_comm_port"] = interCommonPortLists[i];
        instance["model_name"] = "llama_65b";
        nlohmann::json static_info;
        nlohmann::json dynamic_info;
        static_info["group_id"] = 0;
        static_info["max_seq_len"] = 2048;
        static_info["max_output_len"] = 512;
        static_info["total_slots_num"] = 200;
        static_info["total_block_num"] = 1024;
        static_info["p_percentage"] = 0;
        static_info["block_size"] = 128;
        static_info["virtual_id"] = i;
        if (i < numDRole) {
            static_info["label"] = 2;
            static_info["role"] = int('P');
        } else {
            static_info["label"] = 3;
            static_info["role"] = int('D');
            dynamic_info["peers"] = peers;
        }

        dynamic_info["avail_slots_num"] = 200;
        dynamic_info["avail_block_num"] = 1024;

        instance["static_info"] = static_info;
        instance["dynamic_info"] = dynamic_info;
        instancesInfo["instances"].emplace_back(instance);
    }

    return instancesInfo;
}

bool SetPDRole(ThreadSafeVector<std::string> &predictIPs, ThreadSafeVector<std::string> &predictPorts,
    ThreadSafeVector<std::string> &managePortLists, ThreadSafeVector<std::string> &interCommonPortLists,
    uint8_t numPRole, uint8_t numDRole, std::string &manageIP, std::string &managePort)
{
    auto pdInstanceInfo = SetPDRoleByList(predictIPs, predictPorts, managePortLists, interCommonPortLists,
        numPRole, numDRole);

    auto lambdaFunc = [manageIP, managePort, pdInstanceInfo]() -> bool {
        TlsItems tlsItems;
        std::string response;
        auto ret = SendInferRequest(manageIP, managePort, tlsItems, "/v1/instances/refresh",
            pdInstanceInfo.dump(), response);
        std::cout << "------------ SetPDRole ret ----------------" << ret << std::endl;
        if (ret == 0) {
            std::cout << "------------ SetPDRole Ready ----------------" << std::endl;
            return true;
        } else {
            return false;
        }
    };
    // 15s内1s间隔检查是否ready
    return WaitUtilTrue(lambdaFunc, 15, 1000);
}

bool SetPDRoleSSL(ThreadSafeVector<std::string> &predictIPs, ThreadSafeVector<std::string> &predictPorts,
    ThreadSafeVector<std::string> &managePortLists, ThreadSafeVector<std::string> &interCommonPortLists,
    uint8_t numPRole, uint8_t numDRole, std::string &manageIP, std::string &managePort, MINDIE::MS::TlsItems tlsItems)
{
    auto pdInstanceInfo = SetPDRoleByList(predictIPs, predictPorts, managePortLists, interCommonPortLists,
        numPRole, numDRole);

    auto lambdaFunc = [manageIP, managePort, tlsItems, pdInstanceInfo]() -> bool {
        std::string response;
        auto ret = SendSSLInferRequest(manageIP, managePort, tlsItems, "/v1/instances/refresh",
            pdInstanceInfo.dump(), response);
        std::cout << "------------ SetPDRoleSSL ret ----------------" << ret << std::endl;
        if (ret == 0) {
            std::cout << "------------ SetPDRoleSSL Ready ----------------" << std::endl;
            return true;
        } else {
            return false;
        }
    };
    // 15s内1s间隔检查是否ready
    return WaitUtilTrue(lambdaFunc, 15, 1000);
}

int32_t WaitCoordinatorReady(std::string &manageIP, std::string &managePort)
{
    auto lambdaFunc = [manageIP, managePort]() -> bool {
        TlsItems tlsItems;
        std::string response;
        auto ret = GetInferRequest(manageIP, managePort, tlsItems, "/v2/health/ready", response);
        if (ret == 200) {
            std::cout << "------------ Coordinator Ready ----------------" << std::endl;
            return true;
        } else {
            return false;
        }
    };
    // 15s内1s间隔检查是否ready
    return WaitUtilTrue(lambdaFunc, 15, 1000);
}

int32_t WaitCoordinatorReadySSL(std::string &manageIP, std::string &managePort, TlsItems tlsItems)
{
    auto lambdaFunc = [manageIP, managePort, tlsItems]() -> bool {
        std::string response;
        auto ret = GetInferRequest(manageIP, managePort, tlsItems, "/v2/health/ready", response, true);
        if (ret == 200) {
            std::cout << "------------ Coordinator Ready ----------------" << std::endl;
            return true;
        } else {
            return false;
        }
    };
    // 15s内1s间隔检查是否ready
    return WaitUtilTrue(lambdaFunc, 15, 1000);
}

struct Item {
    int index;
    std::string value;
};

bool Compare(const Item& a, const Item& b)
{
    return a.index < b.index;
}

std::string ProcessStreamRespond(std::string response)
{
    // 使用大括号作为分隔符分割字符串
    std::istringstream iss(response);
    std::string item;
    std::vector<Item> items;

    while (std::getline(iss, item, '}')) {
        // 去除多余的字符
        item.erase(std::remove(item.begin(), item.end(), '\"'), item.end());
        item.erase(std::remove(item.begin(), item.end(), '{'), item.end());
        item.erase(std::remove(item.begin(), item.end(), ','), item.end());

        // 查找index和value的位置
        size_t indexPos = item.find("index");
        size_t valuePos = item.find("value");
        if (indexPos == std::string::npos || valuePos == std::string::npos) {
            break;
        }

        // 提取index和value
        std::string indexStr = item.substr(indexPos + 6, valuePos);
        std::cout << "indexStrindexStrindexStr: " << indexStr << std::endl;
        int index = std::stoi(indexStr);
        std::string value = item.substr(valuePos + 6, item.size());
        std::cout << "valuevaluevalue: " << value << std::endl;

        // 存储item
        items.push_back({index, value});
    }

    // 排序items
    std::sort(items.begin(), items.end(), Compare);

    // 拼接value
    std::string concatenatedValue;
    for (const auto& item : items) {
        concatenatedValue += item.value;
    }

    // 输出结果
    std::cout << concatenatedValue << std::endl;
    return concatenatedValue;
}

bool AddReqMock(boost::beast::string_view reqId, ReqInferType type, std::shared_ptr<ServerConnection> connection,
                boost::beast::http::request<boost::beast::http::dynamic_body> &req)
{
    return false;
}

size_t GetReqNumMock()
{
    return 4294967290;
}

bool IsAvailableMock()
{
    std::cout << "IsAvailableMockIsAvailableMockIsAvailableMock" << std::endl;
    return false;
}

bool IsMasterMock()
{
    std::cout << "IsMasterMockIsMasterMockIsMasterMock" << std::endl;
    return false;
}

int LinkWithDNodeMock(const std::string &ip, const std::string &port)
{
    std::cout << "enter LinkWithDNodeMock" << std::endl;
    return 1;
}

int32_t LinkWithDNodeInMaxRetryMock(const std::string &ip, const std::string &port, uint64_t insId)
{
    (void)insId;
    std::cout << "enter LinkWithDNodeInMaxRetryMock" << std::endl;
    return 1;
}

std::string CreateOpenAIRequest(std::vector<std::string> contents, bool stream)
{
    // OpenAI request
    nlohmann::json request;
    request["model"] = "llama2_7b";
    request["messages"] = nlohmann::json::array({});
    for (const auto &content : contents) {
        nlohmann::json message;
        message["role"] = "user";
        message["content"] = content;
        request["messages"].push_back(message);
    }
    request["max_tokens"] = 20; // 设置最大生成token数为20
    request["presence_penalty"] = 1.03; // 设置 presence penalty 为 1.03
    request["frequency_penalty"] = 1.0; // 设置 frequency penalty 为 1.0
    request["seed"] = nullptr;
    request["temperature"] = 0.5; // 设置 temperature 为 0.5
    request["top_p"] = 0.95; // 设置 top_p 为 0.95
    request["stream"] = stream;
    return request.dump();
}

std::string CreateOpenAICompletionsRequest(std::string content, bool stream)
{
    // OpenAI request
    nlohmann::json request;
    request["model"] = "llama2_7b";
    request["prompt"] = content;
    request["max_tokens"] = 20; // 设置最大生成token数为20
    request["presence_penalty"] = 1.03; // 设置 presence penalty 为 1.03
    request["frequency_penalty"] = 1.0; // 设置 frequency penalty 为 1.0
    request["seed"] = nullptr;
    request["temperature"] = 0.5; // 设置 temperature 为 0.5
    request["top_p"] = 0.95; // 设置 top_p 为 0.95
    request["stream"] = stream;
    return request.dump();
}

std::string CreateOpenAIExceptionRequest(std::vector<std::string> contents, bool stream)
{
    // OpenAI request
    nlohmann::json request;
    request["model"] = "llama2_7b";
    request["mess"] = nlohmann::json::array({});
    for (const auto &content : contents) {
        nlohmann::json message;
        message["role"] = "user";
        message["content"] = content;
        request["mess"].push_back(message);
    }
    request["max_tokens"] = 20; // 设置最大生成token数为20
    request["presence_penalty"] = 1.03; // 设置 presence penalty 为 1.03
    request["frequency_penalty"] = 1.0; // 设置 frequency penalty 为 1.0
    request["seed"] = nullptr;
    request["temperature"] = 0.5; // 设置 temperature 为 0.5
    request["top_p"] = 0.95; // 设置 top_p 为 0.95
    request["stream"] = stream;
    return request.dump();
}

std::string CreateOpenAICompletionsExceptionRequest(std::string content, bool stream)
{
    // OpenAI request
    nlohmann::json request;
    request["model"] = "llama2_7b";
    request["pro"] = content;
    request["max_tokens"] = 20; // 设置最大生成token数为20
    request["presence_penalty"] = 1.03; // 设置 presence penalty 为 1.03
    request["frequency_penalty"] = 1.0; // 设置 frequency penalty 为 1.0
    request["seed"] = nullptr;
    request["temperature"] = 0.5; // 设置 temperature 为 0.5
    request["top_p"] = 0.95; // 设置 top_p 为 0.95
    request["stream"] = stream;
    return request.dump();
}