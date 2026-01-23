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
#include <thread>
#include "Util.h"
#include "LeaderAgent.h"

namespace MINDIE::MS {

#ifdef UT_FLAG
bool LeaderAgent::IsLeader() {return isLeader.load();}
#endif // UT_FLAG

LeaderAgent::~LeaderAgent()
{
    Stop();
}

void LeaderAgent::Start()
{
    if (!lock_strategy_) {
        LOG_E("[LeaderAgent] Lock strategy not initialized!");
        return;
    }
    
    lock_strategy_->RegisterCallBack([this](bool locked) {
        if (locked && !isLeader.load()) {
            PromoteToLeader();
        } else if (!locked && isLeader.load()) {
            DemoteToFollower();
        }
    });
    Campaign();
}

void LeaderAgent::Stop()
{
    running_.store(false);
    if (lock_strategy_ && isLeader.load()) {
        lock_strategy_->Unlock();
    }
    isLeader.store(false);
}

int LeaderAgent::RegisterStrategy(std::unique_ptr<DistributedLockPolicy> lock_strategy)
{
    if (!lock_strategy) {
        LOG_E("[LeaderAgent] Register strategy failed: null strategy");
        return -1;
    }
    lock_strategy_ = std::move(lock_strategy);
    return 0;
}

bool LeaderAgent::WriteNodes(const std::string& value)
{
    return lock_strategy_ ? lock_strategy_->SafePut(keyForNodeData, value) : false;
}

bool LeaderAgent::ReadNodes(nlohmann::json& value)
{
    std::string valueStr;
    if (!lock_strategy_ || !lock_strategy_->GetWithRevision(keyForNodeData, valueStr)) {
        LOG_E("[LeaderAgent] Failed to read nodes data");
        return false;
    }
    
    if (!nlohmann::json::accept(valueStr)) {
        LOG_E("[LeaderAgent] Invalid JSON data received");
        return false;
    }
    
    value = nlohmann::json::parse(valueStr, CheckJsonDepthCallBack);
    return true;
}

bool LeaderAgent::WriteFaultsValue(const std::string& value)
{
    return lock_strategy_ ? lock_strategy_->SafePut(keyForSwitchFaults, value) : false;
}

bool LeaderAgent::ReadFaultsValue(nlohmann::json& value)
{
    std::string valueStr;
    if (!lock_strategy_ || !lock_strategy_->GetWithRevision(keyForSwitchFaults, valueStr)) {
        LOG_E("[LeaderAgent] Failed to read faults data");
        return false;
    }
    
    if (!nlohmann::json::accept(valueStr)) {
        LOG_E("[LeaderAgent] Invalid JSON faults received");
        return false;
    }
    
    value = nlohmann::json::parse(valueStr, CheckJsonDepthCallBack);
    return true;
}

// 私有方法实现
void LeaderAgent::Campaign()
{
    if (isLeader.load()) {
        LOG_W("[LeaderAgent Campaign] It is already the leader.");
        return;
    }
    
    if (lock_strategy_->TryLock()) {
        PromoteToLeader();
    } else {
        DemoteToFollower();
    }
}

void LeaderAgent::PromoteToLeader()
{
    if (isLeader.exchange(true)) {return;}
    Slave2Master();
    // 该标志位用于检查是否是第二次升主操作。第一次升主为正常；第二次升主需上报事件
    if (mHasSetRole.load()) {
        Slave2MasterEvent();
    }
    mHasSetRole.store(true);
    LOG_I("[LeaderAgent] Promoted to leader. Running as leader...");
}

void LeaderAgent::DemoteToFollower()
{
    mHasSetRole.store(true);
    if (!isLeader.exchange(false)) {return;}
    Master2Slave();
    LOG_I("[LeaderAgent] Demoted to follower. Waiting in slience...");
}

} // namespace MINDIE::MS