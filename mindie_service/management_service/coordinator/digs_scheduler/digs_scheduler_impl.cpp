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
#include "digs_scheduler_impl.h"
#include "scheduler/SchedulerFactory.h"

namespace MINDIE::MS {
DIGSSchedulerImpl::DIGSSchedulerImpl(DIGSScheduler::Config config) : config_(std::move(config))
{
    SchedulerConfig schedConfig = {};
    GenerateScheduleConfig(schedConfig);

    common::SetBlockSize(schedConfig.blockSize);

    RequestConfig requestConfig = {};
    GenerateRequestConfig(requestConfig);

    ResourceConfig resourceConfig = {};
    GenerateResourceConfig(resourceConfig);
    RequestManager::Create(requestManager_, requestConfig);
    ResourceManager::Create(resourceManager_, resourceConfig);
    GlobalScheduler::Create(schedConfig, resourceManager_, requestManager_, globalScheduler_);
}

DIGSSchedulerImpl::~DIGSSchedulerImpl()
{
    globalScheduler_->Stop();
}

int32_t DIGSSchedulerImpl::RegisterInstance(const std::vector<DIGSInstanceStaticInfo>& instances)
{
    return resourceManager_->RegisterInstance(instances);
}

int32_t DIGSSchedulerImpl::UpdateInstance(const std::vector<DIGSInstanceDynamicInfo>& instances)
{
    return resourceManager_->UpdateInstance(instances);
}

int32_t DIGSSchedulerImpl::QueryInstanceScheduleInfo(std::vector<DIGSInstanceScheduleInfo>& info)
{
    return resourceManager_->QueryInstanceScheduleInfo(info);
}

int32_t DIGSSchedulerImpl::CloseInstance(const std::vector<uint64_t>& instances)
{
    return resourceManager_->CloseInstance(instances);
}

int32_t DIGSSchedulerImpl::ActivateInstance(const std::vector<uint64_t>& instances)
{
    return resourceManager_->ActivateInstance(instances);
}

int32_t DIGSSchedulerImpl::RemoveInstance(const std::vector<uint64_t>& instances)
{
    return resourceManager_->RemoveInstance(instances);
}

int32_t DIGSSchedulerImpl::UpdateReq(std::string reqId, MINDIE::MS::DIGSReqStage stage, uint64_t prefillEndTime,
                                     uint64_t decodeEndTime, size_t outputLength)
{
    switch (stage) {
        case DIGSReqStage::PREFILL:
            LOG_D("[DIGS] Updating request %s in PREFILL stage with end time %lu", reqId.c_str(), prefillEndTime);
            return requestManager_->UpdateReq(reqId, prefillEndTime);
        case DIGSReqStage::DECODE:
            LOG_D("[DIGS] Removing request %s in DECODE stage with prefill end time %lu, decode end time %lu",
                  reqId.c_str(), prefillEndTime, decodeEndTime);
            return requestManager_->RemoveReq(reqId, prefillEndTime, decodeEndTime, outputLength);
        default:
            LOG_W("[%s] [DIGS] Unsupported request stage %d",
                  GetWarnCode(ErrorType::WARNING, CommonFeature::DIGS).c_str(),
                  static_cast<int32_t>(stage));
            return static_cast<int32_t>(common::Status::ILLEGAL_PARAMETER);
    }
}

int32_t DIGSSchedulerImpl::QueryRequestSummary(MINDIE::MS::DIGSRequestSummary &summary)
{
    double inputLen;
    double outputLen;
    requestManager_->CalculateSummary();
    requestManager_->GetAvgLength(inputLen, outputLen);
    summary.inputLength = static_cast<size_t>(inputLen);
    summary.outputLength = static_cast<size_t>(outputLen);
    return static_cast<int32_t>(common::Status::OK);
}

int32_t DIGSSchedulerImpl::RegisterPDNotifyAllocation(DIGSScheduler::NotifyPDAllocation callback)
{
    callback_ = std::move(callback);
    globalScheduler_->RegisterNotifyAllocation([this](const std::unique_ptr<ReqScheduleInfo>& info) -> int32_t {
        return callback_(info->ReqId(), info->PrefillInst(), info->DecodeInst());
    });
    return static_cast<int32_t>(common::Status::OK);
}

int32_t DIGSSchedulerImpl::ProcReq(std::string reqId, size_t promptLen, __attribute__((unused)) ReqType type)
{
    auto inputLen = static_cast<size_t>(std::ceil(static_cast<double>(promptLen) * MetaResource::ResWeight()[1]));
    auto req = std::make_unique<DIGSRequest>(reqId, inputLen);
    return requestManager_->AddReq(std::move(req));
}

int32_t DIGSSchedulerImpl::ProcReq(std::string reqId, const std::string& prompt, __attribute__((unused)) ReqType type)
{
    auto inputLen = static_cast<size_t>(std::ceil(static_cast<double>(prompt.size()) * MetaResource::ResWeight()[1]));
    auto req = std::make_unique<DIGSRequest>(reqId, inputLen);
    return requestManager_->AddReq(std::move(req));
}

int32_t DIGSSchedulerImpl::ProcReq(std::string reqId, const std::vector<uint32_t> &tokenList)
{
    auto req = std::make_unique<DIGSRequest>(reqId, tokenList.size());
    return requestManager_->AddReq(std::move(req));
}

template<typename T>
void DIGSSchedulerImpl::GenerateConfig(const std::string& configName,
                                       const DIGSScheduler::Config& configSource,
                                       T& targetField) const
{
    if constexpr (std::is_same_v<T, bool>) {
        std::string item;
        if (MINDIE::MS::common::GetConfig(configName, item, configSource)) {
            common::Str2Bool(item, targetField);
        }
    } else {
        T item;
        if (MINDIE::MS::common::GetConfig(configName, item, configSource)) {
            targetField = item;
        }
    }
}

void DIGSSchedulerImpl::GenerateScheduleConfig(MINDIE::MS::SchedulerConfig& schedConfig)
{
    GenerateConfig("max_schedule_count", config_, schedConfig.maxScheduleCount);
    GenerateConfig("reordering_type", config_, schedConfig.reorderingType);
    GenerateConfig("select_type", config_, schedConfig.selectType);
    GenerateConfig("pool_type", config_, schedConfig.poolType);
    GenerateConfig("block_size", config_, schedConfig.blockSize);
}

void DIGSSchedulerImpl::GenerateRequestConfig(MINDIE::MS::RequestConfig& requestConfig)
{
    GenerateConfig("pull_request_timeout", config_, requestConfig.pullRequestTimeout);
    GenerateConfig("max_summary_count", config_, requestConfig.maxSummaryCount);
}

void DIGSSchedulerImpl::GenerateResourceConfig(MINDIE::MS::ResourceConfig& resourceConfig)
{
    GenerateConfig("max_res_num", config_, resourceConfig.maxResNum);
    GenerateConfig("res_view_update_timeout", config_, resourceConfig.resViewUpdateTimeout);
    GenerateConfig("res_limit_rate", config_, resourceConfig.resLimitRate);
    GenerateConfig("max_dynamic_res_rate_count", config_, resourceConfig.maxDynamicResRateCount);
    GenerateConfig("dynamic_res_rate_unit", config_, resourceConfig.dynamicResRateUnit);
    GenerateConfig("metaResource_names", config_, resourceConfig.metaResourceNames);
    GenerateConfig("load_cost_values", config_, resourceConfig.metaResourceValues);
    GenerateConfig("load_cost_coefficient", config_, resourceConfig.metaResourceWeights);
    GenerateConfig("dynamic_max_res", config_, resourceConfig.dynamicMaxResEnable);
}

void DIGSSchedulerImpl::SetBlockSize(size_t blockSize)
{
    common::SetBlockSize(blockSize);
}
MINDIE_SCHEDULER_REGISTER("digs_scheduler", DIGSSchedulerImpl);
}
