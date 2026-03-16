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
#ifndef NODE_MANAGER_REQUEST_SENDER_H
#define NODE_MANAGER_REQUEST_SENDER_H

#include <memory>
#include <string>
#include <vector>
#include <unordered_set>
#include "HttpClient.h"
#include "NodeStatus.h"

namespace MINDIE {
namespace MS {

// node_manager command enumeration
enum class NodeManagerCmd {
    PAUSE_ENGINE = 0,
    REINIT_NPU,
    START_ENGINE,
    STOP_ENGINE,
    PAUSE_ENGINE_ROCE
};

enum class NPUStatus {
    READY = 0,
    INIT,
    NORMAL,
    PAUSE,
    ABNORMAL,
    UNKNOWN
};

// Fault node information structure
struct FaultNodeInfo {
    std::string nodeIP;
    std::string nodeSN;
    std::string faultLevel;
};

class NodeManagerRequestSender {
public:
    NodeManagerRequestSender() = default;
    ~NodeManagerRequestSender() = default;

    // Initialize with NodeStatus
    void Init(std::shared_ptr<NodeStatus> nodeStatus);

    // Send fault information to node_manager
    int32_t SendCommandToNodeManager(HttpClient& client, const std::string& nodeManagerIP, NodeManagerCmd cmd);

    // Get node_manager node status
    int32_t GetNodeManagerNodeStatus(HttpClient& client, const std::string& nodeManagerIP, NPUStatus& status);
    std::string NodeManagerCmdToString(NodeManagerCmd cmd);
    NPUStatus StringToNPUStatus(const std::string& status);
private:
    std::shared_ptr<NodeStatus> mNodeStatus = nullptr;
    int32_t ParseNodeStatusResponse(const std::string& response, NPUStatus& status);
};

} // namespace MS
} // namespace MINDIE

#endif // NodeManagerREQUEST_SENDER_H