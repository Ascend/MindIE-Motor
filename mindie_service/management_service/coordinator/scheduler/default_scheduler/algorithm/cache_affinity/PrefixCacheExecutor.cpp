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
#include "PrefixCacheExecutor.h"
#include <vector>
#include "Util.h"

namespace MINDIE::MS {
bool PrefixCacheExecutor::IsNodeResourceAvail(uint64_t nodeId)
{
    MINDIE::MS::DIGSInstanceStaticInfo staticInfo;
    MINDIE::MS::DIGSInstanceDynamicInfo dynamicInfo;
    auto ret = nodeStore->GetNodeById(nodeId, staticInfo, dynamicInfo);
    if (!ret) {
        LOG_E("[%s] [PrefixCacheExecutor] Node with ID %lu not exsit.",
            GetErrorCode(ErrorType::NOT_FOUND, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str(),
            nodeId);
        return false;
    }
    double totalSlots = static_cast<double>(staticInfo.totalSlotsNum);
    double totalBlock = static_cast<double>(staticInfo.totalBlockNum);
    double availSlots = static_cast<double>(dynamicInfo.availSlotsNum);
    double availBlock = static_cast<double>(dynamicInfo.availBlockNum);

    if (totalSlots < 1 || totalBlock < 1) {
        return false;
    }

    auto slotAvail = (availSlots / totalSlots > slotsThresh);
    if (!slotAvail) {
        return false;
    }

    auto blockAvail = (availBlock / totalBlock > blockThresh);
    if (!blockAvail) {
        return false;
    }

    return true;
}

// 单机版轮询
bool PrefixCacheExecutor::RoundRobin(uint64_t &bestNode)
{
    auto nodeIndexList = nodeStore->GetNodeList();
    if (nodeIndexList.size() == 0) {
        return false;
    }
    curNodeIndex++;
    if (curNodeIndex >= nodeIndexList.size()) {
        curNodeIndex = 0;
    }
    // 轮询选中该序列
    uint64_t oldNodeIndex = curNodeIndex; // 轮询的起点

    do {
        bestNode = nodeIndexList[curNodeIndex];
        auto ret = false;
        std::string msg = "[PrefixCacheExcutor] IsNodeResourceAvail failed for node ";
        try {
            ret = IsNodeResourceAvail(bestNode);
        } catch (const std::exception& e) {
            std::string errorMsg = msg + std::to_string(bestNode) + ": " + std::string(e.what());
            LOG_E(errorMsg);
            throw;
        } catch (...) {
            std::string errorMsg = msg + std::to_string(bestNode) + "For details, check the IsNodeResourceAvail"
                "function.";
            LOG_E(msg);
            throw;
        }
        if (ret) {
            return true; // 资源充分则直接选中
        }
        // 否则，继续轮询
        curNodeIndex++;
        if (curNodeIndex >= nodeIndexList.size()) {
            curNodeIndex = 0;
        }
    } while (curNodeIndex != oldNodeIndex);  // 轮询的终点 oldNodeIndex == curNodeIndex; 转了一圈

    return false; // 所有节点资源都不达标
}

bool PrefixCacheExecutor::PreProcessMessage(const std::string &requestBody, size_t &historyHash, size_t &newHash) const
{
    try {
        // parse request and get message
        nlohmann::json dataObj = nlohmann::json::parse(requestBody, CheckJsonDepthCallBack);
        if (!dataObj.is_array() || !(dataObj.size() >= 2)) { // 2是最小的messages条数
            // 关注，调度失败
            LOG_E("[%s] [PrefixCacheExecutor] Invalid request format. Expected a JSON array with at least 2 elements, "
                "but received an invalid format.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
            return false;
        }

        // hash
        std::hash<std::string> hasher;
        std::string message = "";
        size_t messageSize = dataObj.size();
        for (uint32_t i = 0; i + 2 < messageSize; i++) { // 最后2行不计入
            message += dataObj[i].dump();
        }

        if (message.size() > 0) {
            historyHash = hasher(message);
            if (historyHash == 0) {
                LOG_E("[%s] [PrefixCacheExecutor] Computed history message hash is 0, which is invalid.",
                    GetErrorCode(ErrorType::INVALID_PARAMETER, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
                return false;
            }
        }

        for (uint32_t i = 0; i < 2; i++) { // 最后2行
            message += dataObj[messageSize - 2 + i].dump();
        }

        newHash = hasher(message);
        if (newHash == 0) {
            LOG_E("[%s] [PrefixCacheExecutor] Computed new message hash is 0, which is invalid.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
            return false;
        }

        return true;
    } catch (const std::exception &e) {
        LOG_E("[%s] [PrefixCacheExecutor] Exception occurred while preprocessing message. Error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str(), e.what());
        return false;
    }
}

bool PrefixCacheExecutor::CacheAffinity(size_t &historyHash, uint64_t &pickedNode)
{
    // Cache命中且资源要求满足
    if (!cache->Get(historyHash, pickedNode)) {
        LOG_D("[PrefixCacheExecutor] Match prefix catch failed.");
        return false;
    }

    if (!IsNodeResourceAvail(pickedNode)) {
        LOG_D("[PrefixCacheExecutor] Matched node %lu resource is not enough.", pickedNode);
        return false;
    }

    LOG_D("[PrefixCacheExecutor] Matched node %lu.", pickedNode);
    return true;
}

int32_t PrefixCacheExecutor::ProcessFirstRequest(const std::string &requestBody, std::vector<uint64_t> &pickedNodes)
{
    try {
        // parse request and get message
        nlohmann::json dataObj = nlohmann::json::parse(requestBody, CheckJsonDepthCallBack);
        if (!dataObj.is_array() || !(dataObj.size() > 0)) {
            LOG_E("[%s] [PrefixCacheExecutor] Invalid requestBody, not array or size = 0.",
                  GetErrorCode(ErrorType::INVALID_PARAMETER, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
            return -1;
        }

        if (dataObj.size() > 2) { // 长度大于2表明不是首轮对话
            return 1;
        }

        // hash
        std::hash<std::string> hasher;
        std::string message = "";
        size_t messageSize = dataObj.size();
        for (size_t i = 0; i < messageSize; i++) {
            message += dataObj[i].dump();
        }
        size_t newHashValue = hasher(message);
        if (newHashValue == 0) {
            LOG_E("[%s] [PrefixCacheExecutor] Computed new message hash is 0, which is invalid.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
            return -1;
        }

        uint64_t pickedNode = 0;
        auto ret = RoundRobin(pickedNode);
        if (!ret) {
            LOG_E("[%s] [PrefixCacheExecutor] RoundRobin failed, all nodes are not avaliable.",
                GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
            return -1;
        }

        cache->Put(newHashValue, pickedNode);
        pickedNodes.resize(1);
        pickedNodes[0] = pickedNode;
        return 0;
    } catch (const std::exception &e) {
        LOG_E("[%s] [PrefixCacheExecutor] Unknown exception occur.",
              GetErrorCode(ErrorType::INTERNAL_UNKNOWN, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
        return -1;
    }
}

int32_t PrefixCacheExecutor::DoRoundRobin(std::vector<uint64_t> &pickedNodes)
{
    uint64_t pickedNode = 0;
    if (!RoundRobin(pickedNode)) {
        LOG_E("[%s] [PrefixCacheExecutor] Execute RoundRobin failed.",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
        return -1;
    }
    pickedNodes.resize(1);
    pickedNodes[0] = pickedNode;
    return 0;
}

int32_t PrefixCacheExecutor::Execute(DeployMode deployMode, std::string requestBody, std::vector<uint64_t> &pickedNodes,
    int type)
{
    if (deployMode == DeployMode::PD_SEPARATE) {
        // 关注，暂不支持的部署模式
        LOG_E("[%s] [PrefixCacheExecutor] PD separate deploy mode is not supported.",
            GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
        return -1;
    }
    if (type != 0) { // 非OpenAI协议
        return DoRoundRobin(pickedNodes);
    }
    int32_t firstRet = ProcessFirstRequest(requestBody, pickedNodes);
    if (firstRet <= 0) {
        if (firstRet < 0) {
            return DoRoundRobin(pickedNodes);
        }
        return 0;
    }
    uint64_t pickedNode = 0;
    size_t historyHash = 0;
    size_t newHashValue = 0;
    auto ret = PreProcessMessage(requestBody, historyHash, newHashValue);
    if (!ret) {
        LOG_E("[%s] [PrefixCacheExecutor] Preprocess message failed.",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
        return DoRoundRobin(pickedNodes);
    }
 
    ret = CacheAffinity(historyHash, pickedNode);
    if (ret) { // Cache已命中场景
        cache->UpdateKey(historyHash, newHashValue);
        pickedNodes.resize(1);
        pickedNodes[0] = pickedNode;
        return 0;
    }

    LOG_D("[PrefixCacheExecutor] Cache not match, choose round robin.");

    ret = RoundRobin(pickedNode);
    if (!ret) {
        LOG_E("[%s] [PrefixCacheExecutor] RoundRobin failed, all nodes are not avaliable.",
            GetErrorCode(ErrorType::UNAVAILABLE, CoordinatorFeature::PREFIXCACHE_EXECUTOR).c_str());
        return -1;
    }
    cache->Erase(historyHash);
    cache->Put(newHashValue, pickedNode);
    pickedNodes.resize(1);
    pickedNodes[0] = pickedNode;
    return 0;
}
}