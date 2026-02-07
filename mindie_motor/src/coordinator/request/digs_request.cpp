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
#include "digs_request.h"

namespace MINDIE::MS {

DIGSRequest::DIGSRequest(std::string& reqId, size_t inputLen)
    : reqId_(std::move(reqId)), inputLen_(inputLen), outputLen_(0),
      scheduleInfo_(nullptr), state_(DIGSReqState::WAITING), createTime_(common::GetTimeNow()),
      scheduleTime_(0), startTime_(0), prefillEndTime_(0), decodeEndTime_(0), prefillCostTime_(0)
{
}

DIGSRequest::DIGSRequest()
    : reqId_("invalid_ID"), inputLen_(0), outputLen_(0),
      scheduleInfo_(nullptr), state_(DIGSReqState::INVALID), createTime_(common::GetTimeNow()),
      scheduleTime_(0), startTime_(0), prefillEndTime_(0), decodeEndTime_(0), prefillCostTime_(0)
{
}

DIGSRequest::~DIGSRequest()
{
    releaseCallback_ = nullptr;
    auto state = State();
    if (state != DIGSReqState::DECODE_END && state != DIGSReqState::INVALID) {
        LOG_W("[%s] [DIGS] Request release with unexpected state, request ID is %s state: %d",
              GetWarnCode(ErrorType::WARNING, CommonFeature::DIGS).c_str(),
              reqId_.c_str(),
              static_cast<int32_t>(state));
    }
}

void DIGSRequest::InitScheduleInfo(std::unique_ptr<MetaResource>&& demand)
{
    scheduleInfo_ = std::make_unique<ReqScheduleInfo>(reqId_, std::move(demand));
    scheduleTime_ = common::GetTimeNow();
}

int32_t DIGSRequest::UpdateState2PrefillEnd(uint64_t prefillEndTime)
{
    auto state = state_.load(std::memory_order_acquire);
    if (state == DIGSReqState::ALLOCATED) {
        prefillEndTime_ = prefillEndTime;
        LOG_I("[DIGS] update state to PREFILL_END(4), request ID is " << reqId_);
        state_.store(DIGSReqState::PREFILL_END, std::memory_order_release);
        if (releaseCallback_ != nullptr) {
            releaseCallback_(true);
        }
        return static_cast<int32_t>(common::Status::OK);
    }
    LOG_W("[%s] [DIGS] Cannot update state to PREFILL_END(4), request ID is %s State: %d",
          GetWarnCode(ErrorType::WARNING, CommonFeature::DIGS).c_str(),
          reqId_.c_str(),
          static_cast<int32_t>(state));
    return static_cast<int32_t>(common::Status::STATE_ERROR);
}

int32_t DIGSRequest::UpdateState2DecodeEnd(uint64_t prefillEndTime, uint64_t decodeEndTime, size_t outputLength)
{
    auto state = state_.load(std::memory_order_acquire);
    if (state == DIGSReqState::PREFILL_END or state == DIGSReqState::ALLOCATED) {
        LOG_I("[DIGS] Update state to DECODE_END(5) from %d, Request ID: %s State: %d, decode token count: %zu",
              static_cast<int32_t>(state),
              reqId_.c_str(),
              static_cast<int32_t>(state),
              outputLength);
        if (prefillEndTime != 0) {
            prefillEndTime_ = prefillEndTime;
        }
        outputLen_ = outputLength;
        decodeEndTime_ = decodeEndTime;
        state_.store(DIGSReqState::DECODE_END, std::memory_order_release);
        if (releaseCallback_ != nullptr) {
            releaseCallback_(true);
        }
        return static_cast<int32_t>(common::Status::OK);
    }

    // 请求超时，直接通过状态更新清理请求
    if (state == DIGSReqState::SCHEDULING) {
        LOG_W("[%s] [DIGS] Update state to DECODE_END(5) from SCHEDULING(2), Request ID: %s, decode token count: %zu",
              GetWarnCode(ErrorType::WARNING, CommonFeature::DIGS).c_str(),
              reqId_.c_str(),
              outputLength);
        state_.store(DIGSReqState::DECODE_END, std::memory_order_release);
        return static_cast<int32_t>(common::Status::OK);
    }

    LOG_W("[%s] [DIGS] Cannot update state to DECODE_END(5), Request ID: %s State: %d",
          GetWarnCode(ErrorType::WARNING, CommonFeature::DIGS).c_str(),
          reqId_.c_str(),
          static_cast<int32_t>(state));
    return static_cast<int32_t>(common::Status::STATE_ERROR);
}

int32_t DIGSRequest::UpdateState2Invalid()
{
    auto state = state_.load(std::memory_order_acquire);
    if (state == DIGSReqState::WAITING) {
        state_.store(DIGSReqState::INVALID, std::memory_order_release);
        LOG_I("[DIGS] update state to INVALID(0), Request ID: " << reqId_);
        return static_cast<int32_t>(common::Status::OK);
    }
    LOG_W("[%s] [DIGS] Cannot update state to INVALID(0), Request ID: %s State: %d",
          GetWarnCode(ErrorType::WARNING, CommonFeature::DIGS).c_str(),
          reqId_.c_str(),
          static_cast<int32_t>(state));
    return static_cast<int32_t>(common::Status::STATE_ERROR);
}

int32_t DIGSRequest::UpdateState2Scheduling()
{
    auto state = state_.load(std::memory_order_acquire);
    if (state == DIGSReqState::WAITING) {
        state_.store(DIGSReqState::SCHEDULING, std::memory_order_release);
        LOG_I("[DIGS] Update state to SCHEDULING(2), Request ID: " << reqId_);
        return static_cast<int32_t>(common::Status::OK);
    }

    if (state == DIGSReqState::ALLOCATED) {
        state_.store(DIGSReqState::SCHEDULING, std::memory_order_release);
        LOG_I("[DIGS] update state from ALLOCATED(3) to SCHEDULING(2), Request ID: " << reqId_);
        return static_cast<int32_t>(common::Status::OK);
    }
    LOG_W("[%s] [DIGS] Cannot update state to SCHEDULING(2), Request ID: %s State: %d",
          GetWarnCode(ErrorType::WARNING, CommonFeature::DIGS).c_str(),
          reqId_.c_str(),
          static_cast<int32_t>(state));
    return static_cast<int32_t>(common::Status::STATE_ERROR);
}

int32_t DIGSRequest::UpdateState2Allocated()
{
    auto state = state_.load(std::memory_order_acquire);
    if (state == DIGSReqState::SCHEDULING) {
        state_.store(DIGSReqState::ALLOCATED, std::memory_order_release);
        startTime_ = common::GetTimeNow();
        LOG_I("DIGS: update state to ALLOCATED(3), RequestID: " << reqId_);
        return static_cast<int32_t>(common::Status::OK);
    }
    LOG_W("[%s] [DIGS] Cannot update state to ALLOCATED(3), Request ID: %s State: %d",
          GetWarnCode(ErrorType::WARNING, CommonFeature::DIGS).c_str(),
          reqId_.c_str(),
          static_cast<int32_t>(state));
    return static_cast<int32_t>(common::Status::STATE_ERROR);
}

void DIGSRequest::GetRequestControlCallback(DIGSRequest::ControlCallback& callback)
{
    callback = [this](DIGSReqOperation op, uint64_t prefillEndTime, uint64_t decodeEndTime, size_t outputLength) {
        switch (op) {
            case DIGSReqOperation::REQUEST_UPDATE:
                return this->UpdateState2PrefillEnd(prefillEndTime);
            case DIGSReqOperation::REQUEST_REMOVE:
                return this->UpdateState2DecodeEnd(prefillEndTime, decodeEndTime, outputLength);
            default:
                break;
        }
        return static_cast<int32_t>(common::Status::ILLEGAL_PARAMETER);
    };
}
}