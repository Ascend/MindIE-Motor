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
#ifndef MINDIE_MS_UTIL
#define MINDIE_MS_UTIL

#include <cstdint>
#include <sys/types.h>
#include <string>
#include "nlohmann/json.hpp"
namespace MINDIE {
namespace MS {

bool PathCheck(std::string &path, bool &isFileExist, uint32_t mode = 0777, bool checkOwner = true,
    bool createDir = false);

bool PathCheckForCreate(std::string &path);

int32_t FileToBuffer(const std::string &path, std::string &content, uint32_t mode = 0777, bool checkOwner = true);

bool IsAbsolutePath(const std::string &path);

std::string GetTimeStrNow();

int64_t GetTimeStampNowInMillisec();

int64_t GetLocalTimesMillisec();

std::time_t GetTimeStampNow();

std::string GetUUID();

constexpr size_t JSON_STR_SIZE_HEAD = 1024; // 日志仅打印前1k个字符

bool CheckJsonStringSize(const std::string &jsonstr);
bool CheckJsonDepth(int depth, nlohmann::json::parse_event_t ev);
bool CheckJsonDepthCallBack(int depth, nlohmann::json::parse_event_t ev, nlohmann::json& parsed);

}
}
#endif // MINDIE_MS_UTIL