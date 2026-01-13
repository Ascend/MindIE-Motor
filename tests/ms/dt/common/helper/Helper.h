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
#ifndef HELPER_H
#define HELPER_H

#include <iostream>
#include <fstream>
#include <string>
#include <functional>
#include "ConfigParams.h"
#include <vector>
#include <nlohmann/json.hpp>
#include <sys/stat.h>
#include <cstdlib>

std::string GetExecutablePath();

std::string GetParentPath(const std::string &path);

std::string GetAbsolutePath(const std::string &base, const std::string &relative);

std::string GetJsonPath();

// 删除指定目录
bool RemoveDirectoryRecursively(const std::string& path);

std::string SetMSServerJsonDefault(const std::string &jsonPath);
std::string SetMSClientJsonDefault(const std::string &jsonPath);
std::string SetInferJsonDefault(const std::string &jsonPath);
std::string SetMSCoordinatorJsonDefault(const std::string &jsonPath);
std::string SetMSControllerJsonDefault(const std::string &jsonPath);

// 拷贝指定文件到另一个路径，若目的路径文件存在，则删除覆盖
bool CopyFile(const std::string &sourcePath, const std::string &destPath);

// 拷贝config目录下的文件MindIE_MS\config至测试配置目录下 tests\ms\common\.mindie_ms

void CopyDefaultConfig();

// 获取可执行文件的路径
std::string GetMSControllerBinPath();
std::string GetMSCoordinatorBinPath();

// 获取依赖库的路径
std::string GetLdLibraryPath();

// 模型配置文件路径
std::string GetModelConfigJsonPath();
std::string GetModelConfigTestJsonPath();

// 集群配置文件路径
std::string GetMachineConfigJsonPath();
std::string GetMachineConfigTestJsonPath();

std::string GetMSDeployConfigJsonPath();
std::string GetMSControllerConfigJsonPath();
std::string GetMSDeployTestJsonPath();
std::string GetMSCoordinatorTestJsonPath();

// 获取数据面日志目录
std::string GetMSCoordinatorLogPath();

// 获取数据面运行日志路径
std::string GetMSCoordinatorRunLogPath();

// 获取数据面操作日志路径
std::string GetMSCoordinatorOperationLogPath();

std::string GetMSControllerTestJsonPath();
std::string GetMSControllerStatusJsonTestJsonPath();

std::string GetServerRequestHandlerTestJsonPath();
std::string GetAlarmManagerTestJsonPath();
std::string GetProbeServerTestJsonPath();
std::string GetMSGlobalRankTableTestJsonPath();
std::string GetMSRankTableLoaderTestJsonPath();
std::string GetCrossNodeMSRankTableLoaderJsonPath();
std::string GetA2CrossNodeMSRankTableLoaderJsonPath();
std::string GetA3CrossNodeMSRankTableLoaderJsonPath();
std::string GetElasticScalingJsonPath();
std::string GetMSTestHomePath();

std::string GetMSCoordinatorConfigJsonPath();
void CreateFile(std::string filename, std::string content);
bool CreateDirectory(const std::string &path);
bool RemoveDirectory(const std::string &path);
std::string JoinPathComponents(const std::vector<std::string> &components);

bool ReturnFalseStub(const char *format);
bool ReturnTrueStub(const char *format);
void *ReturnNullptrStub(const char *format);
int32_t ReturnZeroStub(const char *format);
int32_t ReturnOneStub(const char *format);
int32_t ReturnNeOneStub(const char *format);

int32_t ChangeFileMode(const std::string &filePath);
int32_t ChangeCertsFileMode(const std::string &filePath, mode_t mode);

int32_t ChangeFileMode400(const std::string &filePath);

int32_t ChangeFileMode600(const std::string &filePath);

std::string GetHSECEASYPATH();

uint16_t GetUnBindPort();

template <typename T>
int ModifyJsonItem(std::string jsonPath, std::string key1, std::string key2, T value)
{
    std::ifstream ifs(jsonPath);
    nlohmann::json jsonData;
    ifs >> jsonData;
    if (jsonData.contains(key1)) {
        auto& elementKey = jsonData[key1];
        if (key2 != "") {
            if (elementKey.contains(key2)) {
                elementKey[key2] = value;
            } else {
                std::cout << "error: key2 not exit !" << std::endl;
                return -1;
            }
        } else {
            jsonData[key1] = value;
        }
    } else {
        std::cout << "error: key1 not exit !" << std::endl;
    }
 
// 将修改后的JSON数据写回文件
    std::ofstream ofs(jsonPath);
    ofs << jsonData.dump(2);
    int result = chmod(jsonPath.c_str(), S_IRUSR | S_IWUSR | S_IRGRP);
    if (result != 0) {
        std::cout << "change failed !" << std::endl;
    }
    std::cout << "change success !" << std::endl;
    return 0;
}

/*
参数
func: 检测函数，需要退出检测，返回true，否则返回false
timeoutSeconds: 设置的超时时间，单位为秒
intervalMilliSeconds: 设置的间隔检查时间，单位毫秒
返回值
true: 超时时间内没有得到固定值
false: 超时时间内得到预期值
*/
using WaitFunc = std::function<bool (void)>;
bool WaitUtilTrue(WaitFunc func, uint32_t timeoutSeconds = 10, uint32_t intervalMilliSeconds = 100);

bool FindStringInLogFile(const std::string& filename, const std::string& searchString);


template <typename T, typename MemberType>
MemberType GetPrivateMember(T* obj, MemberType T::*member)
{
    return obj->*member;
}

template <typename T, typename MemberType>
void SetPrivateMember(T* obj, MemberType T::*member, MemberType value)
{
    obj->*member = value;
}


template <typename T, typename Ret, typename... Args>
Ret InvokePrivateMethod(const T* obj, Ret (T::*method)(Args...) const, Args&&... args)
{
    return (obj->*method)(std::forward<Args>(args)...);
}
// Function to handle non-const member functions
template <typename T, typename Ret, typename... Args>
Ret InvokePrivateMethod(T* obj, Ret (T::*method)(Args...), Args&&... args)
{
    return (obj->*method)(std::forward<Args>(args)...);
}

// 写全局信息表配置文件
void SetRankTableByServerNum(uint8_t serverNum, std::string rankTableFile);
#endif