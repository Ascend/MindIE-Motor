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
#ifndef REQUESTHELPER_H
#define REQUESTHELPER_H

#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include "HttpClient.h"
#include "ServerConnection.h"
#include "RequestMgr.h"
#include "ThreadSafeVector.h"

using namespace MINDIE::MS;

int32_t SendInferRequest(std::string dstIp, std::string dstPort, MINDIE::MS::TlsItems tlsItems,
    std::string url, std::string reqBody, std::string &response);

int32_t SendSSLInferRequest(std::string dstIp, std::string dstPort, MINDIE::MS::TlsItems tlsItems,
    std::string url, std::string reqBody, std::string &response);

int32_t GetInferRequest(std::string dstIp, std::string dstPort, MINDIE::MS::TlsItems tlsItems,
    std::string url, std::string &response, bool tls = false);

int32_t GetMetricsRequest(std::string dstIp, std::string dstPort, MINDIE::MS::TlsItems tlsItems,
    std::string url, std::string &response, bool tls);

bool SetSingleRole(ThreadSafeVector<std::string> &predictIPs, ThreadSafeVector<std::string> &predictPorts,
    ThreadSafeVector<std::string> &managePortLists, ThreadSafeVector<std::string> &interCommonPortLists,
    std::string &manageIP, std::string &managePort);

bool SetPDRole(ThreadSafeVector<std::string> &predictIPs, ThreadSafeVector<std::string> &predictPorts,
    ThreadSafeVector<std::string> &managePortLists, ThreadSafeVector<std::string> &interCommonPortLists,
    uint8_t numPRole, uint8_t numDRole, std::string &manageIP, std::string &managePort);

bool SetPDRoleSSL(ThreadSafeVector<std::string> &predictIPs, ThreadSafeVector<std::string> &predictPorts,
    ThreadSafeVector<std::string> &managePortLists, ThreadSafeVector<std::string> &interCommonPortLists,
    uint8_t numPRole, uint8_t numDRole, std::string &manageIP, std::string &managePort, MINDIE::MS::TlsItems tlsItems);

bool AddReqMock(boost::beast::string_view reqId, MINDIE::MS::ReqInferType type,
    std::shared_ptr<MINDIE::MS::ServerConnection> connection,
    boost::beast::http::request<boost::beast::http::dynamic_body> &req);

nlohmann::json SetPDRoleByList(ThreadSafeVector<std::string> &predictIPs, ThreadSafeVector<std::string> &predictPorts,
    ThreadSafeVector<std::string> &managePortLists, ThreadSafeVector<std::string> &interCommonPortLists,
    uint8_t numPRole, uint8_t numDRole);

int32_t WaitCoordinatorReady(std::string &manageIP, std::string &managePort);

int32_t WaitCoordinatorReadySSL(std::string &manageIP, std::string &managePort, TlsItems tlsItems);

size_t GetReqNumMock();

bool IsAvailableMock();

bool IsMasterMock();

int LinkWithDNodeMock(const std::string &ip, const std::string &port);

int32_t LinkWithDNodeInMaxRetryMock(const std::string &ip, const std::string &port, uint64_t insId);

std::string ProcessStreamRespond(std::string response);

std::string CreateOpenAIRequest(std::vector<std::string> contents, bool stream);

std::string CreateOpenAICompletionsRequest(std::string content, bool stream);

std::string CreateOpenAIExceptionRequest(std::vector<std::string> contents, bool stream);

std::string CreateOpenAICompletionsExceptionRequest(std::string content, bool stream);

#endif