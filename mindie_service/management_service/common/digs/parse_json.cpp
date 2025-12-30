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
#include "parse_json.h"
#include "Logger.h"
#include "common.h"

using namespace MINDIE::MS;

namespace MINDIE::MS::common {

// 该函数用于格式化json中的数字，若某键值对不存在，使用默认值，若值为字符串则转为double
int32_t Object2Double(Json& config, const std::string& key)
{
    if (config == nullptr || config[key] == nullptr) {
        LOG_W("[%s] [DIGS] Parameter %s is not set.",
              GetWarnCode(ErrorType::WARNING, CommonFeature::DIGS).c_str(), key);
        return static_cast<int32_t>(common::Status::ILLEGAL_PARAMETER);
    }
    if (config[key].is_string()) {
        try {
            config[key] = std::stod(config[key].get<std::string>());
        } catch (const std::invalid_argument& e) {
            std::string str(e.what());
            LOG_E("[%s] [DIGS] Parameter %s can not be convert to double. Exception: %s",
                  GetErrorCode(ErrorType::EXCEPTION, CommonFeature::DIGS).c_str(), key, str);
            return static_cast<int32_t>(common::Status::ILLEGAL_PARAMETER);
        } catch (std::out_of_range& e) {
            std::string str(e.what());
            LOG_E("[%s] [DIGS] Parameter %s can not be convert to double. Exception: %s",
                  GetErrorCode(ErrorType::EXCEPTION, CommonFeature::DIGS).c_str(), key, str);
            return static_cast<int32_t>(common::Status::ILLEGAL_PARAMETER);
        }
        return static_cast<int32_t>(common::Status::OK);
    }
    if (config[key].is_number_float() || config[key].is_number_integer()) {
        return static_cast<int32_t>(common::Status::OK);
    }

    return static_cast<int32_t>(common::Status::OK);
}

}