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
#include <sys/stat.h>
#include <unistd.h>
#include <fcntl.h>
#include <ctime>
#include <fstream>
#include <set>
#include <regex>
#include <boost/uuid/uuid.hpp>
#include <boost/uuid/uuid_generators.hpp>
#include <boost/uuid/uuid_io.hpp>
#include "Logger.h"
#include "Util.h"

namespace MINDIE {
namespace MS {
constexpr uint32_t MAX_FILE_SIZE = 101 * 1024 * 1024; // 101 * 1024 * 1024: 文件支持101M文件
constexpr uint32_t MAX_DEPTH = 10;      // 支持新创建文件夹的深度


static bool IsSymlink(const std::string &filePath)
{
    struct stat buf;
    if (lstat(filePath.c_str(), &buf) != 0) {
        return false;
    }
    return S_ISLNK(buf.st_mode);
}

static bool IsSymlink(int fd)
{
    struct stat buf;
    if (fstat(fd, &buf) != false) {
        return false;
    }
    return S_ISLNK(buf.st_mode);
}

static bool CheckOwner(const struct stat& st, const std::string &filePath)
{
    auto uid = getuid();
    if (st.st_uid != uid) {
        LOG_E("[%s] [Util] The owner ID for the file '%s' does not match. The current process's user ID is '%s', "
            "while the file's owner ID is '%s'. Please check the ownership of the file.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(),
            filePath.c_str(), std::to_string(uid).c_str(), std::to_string(st.st_uid).c_str());
        return false;
    }
    return true;
}

static bool CreateDirectorys(const std::string& path)
{
    uint32_t depth = 0;
    std::string lastRealPath = "";
    while (++depth <= MAX_DEPTH + 1) {
        char tempPath[PATH_MAX + 1] = {0x00};
        auto absPath = realpath(path.c_str(), tempPath);
        if (absPath == nullptr && errno != ENOENT) {
            LOG_E("[%s] [Util] Failed to parse realpath for file.",
                GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str());
            LOG_E("[%s] [Util] Failed to resolve the real path for '%s'."
                " Please check the validity of the path and ensure proper permissions.",
                GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), path.c_str());
            return false;
        }
        std::string subDir = std::string(tempPath);
        if (subDir == lastRealPath || depth == MAX_DEPTH + 1) {
            if (subDir == lastRealPath) {
                depth--;
            }
            break;
        }
        if (access(subDir.c_str(), 0) != 0 && mkdir(subDir.c_str(), S_IRWXU) != 0) {    // 700
            LOG_E("[%s] [Util] Failed to create directory '%s'. Please check permissions or path validity.",
                GetErrorCode(ErrorType::CALL_ERROR, CommonFeature::UTIL).c_str(), subDir.c_str());
            return false;
        }
        lastRealPath = subDir;
    }
    if (depth > MAX_DEPTH) {
        LOG_E("[%s] [Util] Directory creation failed because the folder depth exceeds the maximum allowed limit "
            "of %d.", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), MAX_DEPTH);
        return false;
    }
    return true;
}

static bool DirectoryExists(const std::string& path, bool createDir = false)
{
    struct stat info;
    if (stat(path.c_str(), &info) != 0) {
        if (!createDir) {
            return false;
        } else {
            return CreateDirectorys(path);
        }
    } else if ((info.st_mode & S_IFDIR) != 0) {
        return true;
    }
    return false;
}

static bool PermissionCheck(const std::string &filePath, const struct stat& buf, uint32_t mode)
{
    mode_t maxMode = static_cast<mode_t>(mode);
    mode_t mask = 07;
    const int perPermWidth = 3;
    std::vector<std::string> permMsg = { "Other group permission", "Owner group permission", "Owner permission" };

    for (int i = perPermWidth; i > 0; i--) {
        uint32_t curPerm = (buf.st_mode & (mask << ((i - 1) * perPermWidth))) >> ((i - 1) * perPermWidth);
        uint32_t maxPerm = (maxMode & (mask << ((i - 1) * perPermWidth))) >> ((i - 1) * perPermWidth);

        if ((curPerm | maxPerm) != maxPerm) {
            LOG_E("[%s] [Util] Check %s for file %s failed. Current permission is %u, but required no greater than %u.",
                GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(),
                permMsg[i - 1].c_str(), filePath.c_str(), curPerm, maxPerm);
            return false;
        }
    }
    return true;
}

bool IsFileSizeValid(const std::string &path)
{
    struct stat buf;
    if (stat(path.c_str(), &buf) != 0) {
        LOG_E("Failed to stat file '%s'", path.c_str());
        return false;
    }
    auto fileSize = static_cast<uint64_t>(buf.st_size);
    if (fileSize > MAX_FILE_SIZE) {
        LOG_E("[%s] [Util] The size of the file '%s' exceeds the maximum allowed limit. File size is %llu bytes, "
            "Maximum allowed size is %d bytes.", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(),
            path.c_str(), fileSize, MAX_FILE_SIZE);
        return false;
    }
    return true;
}

bool IsFileSizeValid(const struct stat& st, const std::string &path)
{
    if (st.st_size < 0) {
        LOG_E("[%s] [Util] Invalid file size (negative) for file %s",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), path.c_str());
        return false;
    }

    auto fileSize = static_cast<uint64_t>(st.st_size);
    if (fileSize > MAX_FILE_SIZE) {
        LOG_E("[%s] [Util] The size of the file '%s' exceeds the maximum allowed limit. File size is %llu bytes, "
            "Maximum allowed size is %d bytes.", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(),
            path.c_str(), fileSize, MAX_FILE_SIZE);
        return false;
    }
    return true;
}

static bool CheckPathLengthAndLink(std::string &path)
{
    if (path.empty()) {
        LOG_E("[%s] [Util] File path %s is empty", path.c_str(),
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str());
        return false;
    }
    if (path.length() > 256) { // 256 限制路径最大长度
        LOG_E("[%s] [Util] The file path exceeds the maximum allowed length of %d characters. "
            "Provided path length: %zu.", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(),
            256, path.length()); // 256 限制路径最大长度
        return false;
    }
    if (IsSymlink(path)) {
        LOG_E("[%s] [Util] File %s a symbolic link, which is not allowed.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), path.c_str());
        return false;
    }

    return true;
}

bool PathCheckForCreate(std::string &path)
{
    if (!CheckPathLengthAndLink(path)) {
        return false;
    }

    const std::regex pathPattern(R"(^(?!.*\.\.)[a-zA-Z0-9_./~-]+$)");
    if (!std::regex_match(path, pathPattern)) {
        return false;
    }

    return true;
}

static bool CheckFileSecurity(int fd, const std::string &path, uint32_t mode, bool checkOwner, bool &isFileExist)
{
    if (IsSymlink(fd)) {
        return false;
    }
    struct stat st;
    if (fstat(fd, &st) != 0) {
        LOG_E("[%s] [Util] Failed to get file status for '%s'.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(),
            path.c_str());
        return false;
    }
    bool permissionOK = PermissionCheck(path, st, mode);
    bool sizeOk = permissionOK ? IsFileSizeValid(st, path) : false;
    if (permissionOK && sizeOk) {
        isFileExist = true;
        bool ownerOk = (sizeOk && checkOwner) ? CheckOwner(st, path) : true;
        return ownerOk;
    }
    return false;
}

bool PathCheck(std::string &path, bool &isFileExist, uint32_t mode, bool checkOwner, bool createDir)
{
    if (!CheckPathLengthAndLink(path)) {
        return false;
    }
    // 检查路径是否匹配模式
    const std::regex pathPattern(R"(^(?!.*\.\.)[a-zA-Z0-9_./~-]+$)");
    if (!std::regex_match(path, pathPattern)) {
        return false;
    }

    std::size_t pos = path.find_last_of('/');
    std::string dir = (pos == std::string::npos) ? path : path.substr(0, pos);
    if (dir != path && !DirectoryExists(dir, createDir)) {
        LOG_E("[%s] [Util] Directory of path %s not exists and create failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::UTIL).c_str(), path.c_str());
        return false;
    }

    auto tempPath = new (std::nothrow) char[PATH_MAX + 1] {};
    if (tempPath == nullptr) {
        LOG_E("[%s] [Util] Failed to allocate memory for temporary path storage. Please check system resources.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, CommonFeature::UTIL).c_str());
        return false;
    }

    auto absPath = realpath(path.c_str(), tempPath);
    if (absPath == nullptr && errno != ENOENT) {
        delete[] tempPath;
        tempPath = nullptr;
        LOG_E("[%s] [Util] Failed to resolve the real path for the file '%s'. Please check the validity of "
            "the path.", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), path.c_str());
        return false;
    }
    path = std::string(tempPath);
    delete[] tempPath;
    tempPath = nullptr;

    if (absPath == nullptr && errno == ENOENT) {
        isFileExist = false;
        return true;
    }
    int fd = open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) {
        LOG_E("[%s] [Util] Failed to open file '%s'.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(),
            path.c_str());
        return false;
    }
    bool result = CheckFileSecurity(fd, path, mode, checkOwner, isFileExist);
    close(fd);
    return result;
}

int32_t FileToBuffer(const std::string &path, std::string &content, uint32_t mode, bool checkOwner)
{
    std::string absPath = path;
    bool isFileExist = false;
    if (!PathCheck(absPath, isFileExist, mode, checkOwner)) {
        return -1;
    }
    if (!isFileExist) {
        LOG_E("[%s] [Util] The specified file '%s' does not exist. Please check the file path and ensure the file "
            "is available.", GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::UTIL).c_str(), path.c_str());
        return -1;
    }
    // --- 新增：文件大小校验逻辑 ---
    const size_t maxUtilFileSize = 500 * 1024 * 1024;
    struct stat fileStat;
    if (stat(absPath.c_str(), &fileStat) != 0) {
        LOG_E("[%s] [Util] Failed to get file size for '%s'.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(),
            absPath.c_str());
        return -1;
    }
    if (static_cast<size_t>(fileStat.st_size) > maxUtilFileSize) {
        LOG_E("[%s] [Util] File '%s' exceeds size limit (%zu > %zu bytes).",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(),
            path.c_str(), static_cast<size_t>(fileStat.st_size), maxUtilFileSize);
        return -1;
    }
    // --- 校验结束 ---
    std::ifstream fin;
    try {
        fin.open(absPath.data(), std::ifstream::in);
        if (!fin.is_open()) {
            LOG_E("[%s] [Util] Failed to open the file '%s'. Please check the file permissions or the file format.",
                GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), path.c_str());
            return -1;
        }
        std::ostringstream output;
        output << fin.rdbuf();
        content = output.str();
        fin.close();
    } catch (const std::exception& e) {
        if (fin.is_open()) {
            fin.close();
        }
        LOG_E("[%s] [Util] Unexpected error while reading file '%s': %s",
            GetErrorCode(ErrorType::EXCEPTION, CommonFeature::UTIL).c_str(),
            absPath.c_str(), e.what());
        return -1;
    }
    return 0;
}

static bool IsValidPathChar(char c)
{
    return std::isalnum(c) || c == '-' || c == '_' || c == '/' || c == '.';
}

bool IsAbsolutePath(const std::string& path)
{
    // 检查路径是否为空
    if (path.empty()) {
        LOG_E("[%s] [Util] The length of file %s is empty. Please check the file path.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::UTIL).c_str(), path.c_str());
        return false;
    }

    // 检查路径长度是否超过最大限制
    if (path.length() > 4096) { // 4096 path len cannot be greater than 4096
        LOG_E("[%s] [Util] The provided file path exceeds the maximum allowed length of 4096 characters. Provided"
            " length: %zu.", GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), path.length());
        return false;
    }

    // 检查路径是否以 '/' 开始
    if (path[0] != '/') {
        LOG_E("[%s] [Util] The file path '%s' does not start with a '/'.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), path.c_str());
        return false;
    }
    // 检查路径中是否都是合法字符
    bool ret = std::all_of(path.begin(), path.end(), IsValidPathChar);
    if (!ret) {
        LOG_E("[%s] [Util] The file path '%s' contains invalid characters. "
            "Only alphanumeric characters, '-', '_', '/', and '.' are allowed.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), path.c_str());
        return false;
    }
    return true;
}

std::time_t GetTimeStampNow()
{
    auto now = std::chrono::system_clock::now();
    std::time_t timeStampNow = std::chrono::system_clock::to_time_t(now);
    return timeStampNow;
}

int64_t GetTimeStampNowInMillisec()
{
    auto now = std::chrono::system_clock::now();
    auto nowMs = std::chrono::time_point_cast<std::chrono::milliseconds>(now);
    auto epoch = nowMs.time_since_epoch();
    return static_cast<int64_t>(epoch.count());
}

int64_t GetLocalTimesMillisec()
{
    using namespace std::chrono;

    // Get the current system time point
    auto now = system_clock::now();

    // UTC timestamp in milliseconds
    int64_t utcMs = duration_cast<milliseconds>(now.time_since_epoch()).count();

    // Convert current time point to time_t (seconds since epoch)
    time_t currentTimeT = system_clock::to_time_t(now);
    tm localTm;

    // Get local time from UTC time considering the system's timezone
    localtime_r(&currentTimeT, &localTm);  // Linux thread-safe function

    // Convert local time back to time_t (seconds), then to milliseconds
    time_t localTimeT = mktime(&localTm);
    if (localTimeT == static_cast<time_t>(-1)) {
        LOG_E("[%s] [Util] Failed to get loacl time.",
            GetErrorCode(ErrorType::EXCEPTION, CommonFeature::UTIL).c_str());
    }

    // Keep millisecond precision: seconds * 1000 + remaining milliseconds
    int64_t localMs = static_cast<int64_t>(localTimeT) * 1000 + (utcMs % 1000);

    return localMs;
}

std::string GetTimeStrNow()
{
    auto now = std::chrono::system_clock::now();
    std::time_t nowC = std::chrono::system_clock::to_time_t(now);
    struct std::tm nowTm;
    localtime_r(&nowC, &nowTm);

    std::stringstream ss;
    ss << std::put_time(&nowTm, "[%Y-%m-%d %H:%M:%S");

    auto us = std::chrono::duration_cast<std::chrono::microseconds>(now.time_since_epoch()) % 1000000;
    ss << "." << std::setfill('0') << std::setw(6) << us.count();  // 6位精确到微秒
    auto offset = nowTm.tm_gmtoff;
    if (offset >= 0) {
        ss << "+";
    } else {
        ss << "-";
        offset = -offset;
    }
    ss << std::setfill('0') << std::setw(2) << (offset / 3600) << ":";  // 2位小时,一小时3600秒
    ss << std::setfill('0') << std::setw(2) << ((offset % 3600) / 60);  // 2位分钟,每分钟60秒
    if (nowTm.tm_isdst == 1) {
        ss << " DST";
    }
    ss << "] ";
    return ss.str();
}

std::string GetUUID()
{
    boost::uuids::uuid uuid = boost::uuids::random_generator()();
    auto uuidStr = boost::uuids::to_string(uuid);
    return uuidStr;
}

static constexpr size_t JSON_STR_SIZE_MAX = 1024 * 1024; // 限制1M
static constexpr int JSON_STR_DEP_MAX = 50; // 限制嵌套层次50层

bool CheckJsonStringSize(const std::string &jsonstr)
{
    return jsonstr.size() <= JSON_STR_SIZE_MAX;
}

bool CheckJsonDepth(int depth, nlohmann::json::parse_event_t ev)
{
    switch (ev) {
        case nlohmann::json::parse_event_t::object_start:
            return depth <= JSON_STR_DEP_MAX;
        case nlohmann::json::parse_event_t::array_start:
            return depth <= JSON_STR_DEP_MAX;
        default:
            return true;
    }
}

bool CheckJsonDepthCallBack(int depth, nlohmann::json::parse_event_t ev, nlohmann::json& parsed)
{
    if (!CheckJsonDepth(depth, ev)) {
        LOG_E("[%s] [Util] Failed to parse json: depth is %d, object is %zu",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::UTIL).c_str(), depth, sizeof(parsed));
        return false;
    }
    return true;
}

}
}