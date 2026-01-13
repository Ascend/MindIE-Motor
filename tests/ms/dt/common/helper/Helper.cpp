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
#include "Helper.h"
#include <iostream>
#include <fstream>
#include <string>
#include <sys/stat.h>
#include <dirent.h>
#include <cassert>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <cstring>
#include <securec.h>
#include "ConfigParams.h"
#include <boost/beast/ssl.hpp>
#include <boost/beast/http.hpp>
#include <boost/beast/version.hpp>
#include <boost/beast/core.hpp>
#include <boost/asio/ssl/stream.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/config.hpp>
#include <boost/asio.hpp>


namespace Beast = boost::beast;
using std::chrono::high_resolution_clock;
using std::chrono::microseconds;
using std::chrono::milliseconds;
using std::chrono::nanoseconds;
using std::chrono::seconds;

struct Response {
    std::string message {};
    std::string data {};
};
#include "JsonFileManager.h"
#include "Logger.h"

using namespace MINDIE::MS;

std::string GetExecutablePath()
{
    std::vector<char> path;
#ifdef _WIN32
    DWORD len = GetModuleFileNameA(NULL, &path[0], path.size());
    if (len == 0) {
        return "";
    }
    path.resize(len);
#else
    path.resize(PATH_MAX);
    ssize_t len = readlink("/proc/self/exe", &path[0], path.size());
    if (len == -1) {
        return "";
    }
    path.resize(len);
#endif
    return std::string(path.begin(), path.end());
}

std::string GetParentPath(const std::string &path)
{
    auto pos = path.rfind('/');
    if (pos == std::string::npos) {
        return "";
    }
    return path.substr(0, pos);
}

std::string GetAbsolutePath(const std::string &base, const std::string &relative)
{
    std::vector<std::string> baseParts;
    std::stringstream ss(base);
    std::string item;
    while (std::getline(ss, item, '/')) {
        baseParts.push_back(item);
    }
    std::vector<std::string> relativeParts;
    ss = std::stringstream(relative);
    while (std::getline(ss, item, '/')) {
        relativeParts.push_back(item);
    }
    std::vector<std::string> parts;
    for (const auto &part : baseParts) {
        if (part == "..") {
            if (!parts.empty()) {
                parts.pop_back();
            }
        } else if (part != ".") {
            parts.push_back(part);
        }
    }
    for (const auto &part : relativeParts) {
        if (part == "..") {
            if (!parts.empty()) {
                parts.pop_back();
            }
        } else if (part != ".") {
            parts.push_back(part);
        }
    }
    std::string result;
    for (const auto &part : parts) {
        result += "/" + part;
    }
    if (result.empty()) {
        result = "/";
    }
    return result;
}

std::string GetJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(exePath);
    std::string jsonPath = GetAbsolutePath(parentPath, "a.json");
    return jsonPath;
}

std::string GetMSControllerBinPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "mindie_service/management_service/output/bin/ms_controller");
    return jsonPath;
}

std::string GetMSCoordinatorBinPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "mindie_service/management_service/output/bin/ms_coordinator");
    return jsonPath;
}


std::string GetMSControllerConfigJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "mindie_service/management_service/config/ms_controller.json");
    return jsonPath;
}

std::string GetMSControllerTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/ms_controller.json");
    return jsonPath;
}

std::string GetMSControllerTestTmpJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/ms_controller.json");
    return jsonPath;
}

std::string GetServerRequestHandlerTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath,
        "mindie_service/management_service/output/config/ms_controller_1.json");
    return jsonPath;
}

std::string GetProbeServerTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath,
        "mindie_service/management_service/output/config/ms_controller_2.json");
    return jsonPath;
}

std::string GetAlarmManagerTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath,
        "mindie_service/management_service/output/config/ms_controller_3.json");
    return jsonPath;
}

std::string GetModelConfigJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath,
        "mindie_service/management_service/config/model_config/llama2-70B.json");
    return jsonPath;
}

std::string GetMachineConfigJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath,
        "mindie_service/management_service/config/machine_config/800IA2.json");
    return jsonPath;
}

std::string GetModelConfigTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/llama2-70B.json");
    return jsonPath;
}

std::string GetMachineConfigTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/800IA2.json");
    return jsonPath;
}

std::string GetMSGlobalRankTableTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/global_rank_table.json");
    return jsonPath;
}

std::string GetMSRankTableLoaderTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/global_rank_table_tmp.json");
    return jsonPath;
}

std::string GetCrossNodeMSRankTableLoaderJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/global_rank_table_cross_node.json");
    return jsonPath;
}

std::string GetA2CrossNodeMSRankTableLoaderJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath,
        "tests/ms/common/.mindie_ms/global_rank_table_cross_node_a2.json");
    return jsonPath;
}

std::string GetElasticScalingJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath,
        "tests/ms/common/.mindie_ms/elastic_scaling.json");
    return jsonPath;
}

std::string GetA3CrossNodeMSRankTableLoaderJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath,
        "tests/ms/common/.mindie_ms/global_rank_table_cross_node_a3.json");
    return jsonPath;
}

std::string GetMSDeployConfigJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "mindie_service/management_service/config/infer_server.json");
    return jsonPath;
}

std::string GetMSDeployTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/infer_server.json");
    return jsonPath;
}

std::string GetMSCoordinatorConfigJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "mindie_service/management_service/config/ms_coordinator.json");
    return jsonPath;
}

std::string GetMSCoordinatorTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/ms_coordinator.json");
    return jsonPath;
}

std::string GetMSCoordinatorLogPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/dt/logs");
    return jsonPath;
}


std::string GetMSCoordinatorRunLogPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/dt/logs/coordinator_run_log.log");
    return jsonPath;
}

std::string GetMSCoordinatorOperationLogPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/dt/logs/coordinator_operation_path.log");
    return jsonPath;
}

std::string GetMSControllerStatusJsonTestJsonPath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/status_json.json");
    return jsonPath;
}

std::string GetMSTestHomePath()
{
    std::string exePath = GetExecutablePath();
    std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
    std::string jsonPath = GetAbsolutePath(parentPath, "tests/ms/common/");
    return jsonPath;
}


bool RemoveDirectoryRecursively(const std::string& path)
{
    DIR *dir;
    struct dirent *entry;
    if ((dir = opendir(path.c_str())) == NULL) {
        std::cerr << "Failed to open directory: " << strerror(errno) << std::endl;
        return false;
    }

    while ((entry = readdir(dir)) != nullptr) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
            continue; // 忽略当前目录和父目录
        }

        std::string entryPath = path + "/" + std::string(entry->d_name);
        struct stat statbuf;
        if (stat(entryPath.c_str(), &statbuf) == -1) {
            std::cerr << "Failed to stat entry: " << strerror(errno) << std::endl;
            closedir(dir);
            return false;
        }

        if (S_ISDIR(statbuf.st_mode)) {
            // 如果是目录，递归删除
            if (!RemoveDirectoryRecursively(entryPath)) {
                closedir(dir);
                return false;
            }
        } else {
            // 如果是文件，直接删除
            if (remove(entryPath.c_str()) != 0) {
                std::cerr << "Failed to remove file: " << strerror(errno) << std::endl;
                closedir(dir);
                return false;
            }
        }
    }

    closedir(dir);

    return true;
}

std::string SetMSServerJsonDefault(const std::string &jsonPath)
{
    JsonFileManager manager(jsonPath);
    manager.Load();
    manager.Set("ip", "127.0.0.1");
    manager.Set("port", 9789); // 9789 端口
    manager.Set("k8s_apiserver_ip", "127.0.0.1");
    manager.Set("k8s_apiserver_port", 6443); // 6443 k8s端口

    std::string exeDir = GetParentPath(GetExecutablePath());
    std::string logDir = JoinPathComponents({ exeDir, "log" });
    RemoveDirectoryRecursively(logDir);
    CreateDirectory(logDir);
    manager.SetList({ "log_info", "log_level" }, "DEBUG");
    manager.SetList({ "log_info", "run_log_path" }, "./log/run_log.txt");
    manager.SetList({ "log_info", "operation_log_path" }, "./log/operation_log.txt");
    manager.Set("ms_status_file", "./log/status.json");

    std::string certDir = JoinPathComponents({ exeDir, "cert_dir" });
    CreateDirectory(certDir);

    auto lambdaFun = [exeDir, &manager](std::string items) mutable {
        auto ca_cert = JoinPathComponents({ exeDir, "cert_dir", items + "_ca_cert.crt" });
        CreateFile(ca_cert, "ca_cert");
        ChangeCertsFileMode(ca_cert, S_IRUSR);

        auto tls_cert = JoinPathComponents({ exeDir, "cert_dir", items + "_tls_cert.crt" });
        CreateFile(tls_cert, "tls_cert");
        ChangeCertsFileMode(tls_cert, S_IRUSR);

        auto tls_key = JoinPathComponents({ exeDir, "cert_dir", items + "_tls_key.key" });
        CreateFile(tls_key, "tls_key");
        ChangeCertsFileMode(tls_key, S_IRUSR);

        auto tls_crl = JoinPathComponents({ exeDir, "cert_dir", items + "_tls_crl.crl" });
        CreateFile(tls_crl, "tls_crl");
        ChangeCertsFileMode(tls_crl, S_IRUSR);

        manager.SetList({ items, "ca_cert" }, ca_cert);
        manager.SetList({ items, "tls_cert" }, tls_cert);
        manager.SetList({ items, "tls_key" }, tls_key);
        manager.SetList({ items, "tls_passwd" }, "aaaa");
        manager.SetList({ items, "tls_crl" }, tls_crl);
    };

    mode_t oldMask = umask(0277);
    lambdaFun("server_tls_items");

    // 补充，需要把开关打开
    manager.Set("client_k8s_tls_enable", false);
    lambdaFun("client_k8s_tls_items");

    manager.Set("client_mindie_server_tls_enable", false);

    lambdaFun("client_mindie_tls_items");
    umask(oldMask);
    manager.Save();
    return manager.Dump();
}


std::string SetMSCoordinatorJsonDefault(const std::string &jsonPath)
{
    JsonFileManager manager(jsonPath);
    manager.Load();
    manager.SetList({ "http_config", "predict_ip" }, "127.0.0.1");
    manager.SetList({ "http_config", "predict_port" }, "2020");
    manager.SetList({ "http_config", "manage_ip" }, "127.0.0.1");
    manager.SetList({ "http_config", "manage_port" }, "2226");
    manager.SetList({ "http_config", "server_thread_num" }, 1);
    manager.SetList({ "http_config", "connection_pool_max_connections" }, 10000);
    manager.SetList({ "http_config", "client_thread_num" }, 1);
    manager.SetList({ "http_config", "http_timeout_seconds" }, 600);
    manager.SetList({ "http_config", "keep_alive_seconds" }, 180);
    manager.SetList({ "http_config", "server_name" }, "MindIE-MS");
    manager.SetList({ "http_config", "user_agent" }, "Coordinator/1.0");

    manager.SetList({ "request_limit", "connection_max_requests" }, 200);
    manager.SetList({ "request_limit", "single_node_max_requests" }, 1000);
    manager.SetList({ "request_limit", "max_requests" }, 10000);

    manager.SetList({ "metrics_config", "enable" }, false);
    manager.SetList({ "metrics_config", "trigger_size" }, 100);

    manager.SetList({ "exception_config", "max_retry" }, 5);
    manager.SetList({ "exception_config", "schedule_timeout" }, 60);
    manager.SetList({ "exception_config", "first_token_timeout" }, 60);
    manager.SetList({ "exception_config", "infer_timeout" }, 300);

    manager.SetList({ "log_info", "log_level" }, "DEBUG");
    manager.SetList({ "log_info", "to_file" }, false);
    manager.SetList({ "log_info", "to_stdout" }, true);
    manager.SetList({ "log_info", "run_log_path" }, "./log/run/log.txt");
    manager.SetList({ "log_info", "operation_log_path" }, "./log/operation/log.txt");
    manager.SetList({ "log_info", "max_log_str_size" }, 4096);

    manager.SetList({ "digs_scheduler_config", "deploy_mode" }, "pd_separate");
    manager.SetList({ "digs_scheduler_config", "scheduler_type" }, "digs_scheduler");
    manager.SetList({ "digs_scheduler_config", "algorithm_type" }, "load_balance");
    manager.SetList({ "digs_scheduler_config", "cache_size" }, "100");
    manager.SetList({ "digs_scheduler_config", "slots_thresh" }, "0.05");
    manager.SetList({ "digs_scheduler_config", "block_thresh" }, "0.05");
    manager.SetList({ "digs_scheduler_config", "max_schedule_count" }, "10000");
    manager.SetList({ "digs_scheduler_config", "reordering_type" }, "1");
    manager.SetList({ "digs_scheduler_config", "select_type" }, "2");
    manager.SetList({ "digs_scheduler_config", "alloc_file" }, "");
    manager.SetList({ "digs_scheduler_config", "pool_type" }, "1");
    manager.SetList({ "digs_scheduler_config", "block_size" }, "128");
    manager.SetList({ "digs_scheduler_config", "export_type" }, "0");
    manager.SetList({ "digs_scheduler_config", "pull_request_timeout" }, "500");
    manager.SetList({ "digs_scheduler_config", "max_res_num" }, "5000");
    manager.SetList({ "digs_scheduler_config", "res_view_update_timeout" }, "500");
    manager.SetList({ "digs_scheduler_config", "metaResource_names" }, "");
    manager.SetList({ "digs_scheduler_config", "load_cost_values" }, "1, 0");
    manager.SetList({ "digs_scheduler_config", "load_cost_coefficient" }, "0, 1, 1024, 24, 6, 0, 0, 1, 1");
    manager.SetList({ "digs_scheduler_config", "tokenizer_weight_path" }, "/data/atb_testdata/weights/llama1-7b");
    manager.SetList({ "digs_scheduler_config", "standInferEnabled" }, "0");
    manager.SetList({ "digs_scheduler_config", "tokenizer_number" }, "4");
    manager.SetList({ "digs_scheduler_config", "backend_type" }, "atb");

    manager.SetList({ "tls_config", "controller_server_tls_enable" }, false);
    manager.SetList({ "tls_config", "request_server_tls_enable" }, false);
    manager.SetList({ "tls_config", "status_tls_enable" }, false);
    manager.SetList({ "tls_config", "external_tls_enable" }, false);
    manager.SetList({ "tls_config", "mindie_client_tls_enable" }, false);

    manager.Save();
    return manager.Dump();
}

std::string SetMSControllerJsonDefault(const std::string &jsonPath)
{
    RemoveDirectoryRecursively("./logs");
    CreateDirectory("./logs");
    JsonFileManager manager(jsonPath);
    manager.Load();
    manager.Set("deploy_mode", "pd_separate");
    manager.Set("role_decision_time_period", 3600); // 身份切换的时间间隔为3600秒
    manager.Set("digs_model_config_path", GetModelConfigTestJsonPath());
    manager.Set("digs_machine_config_path", GetMachineConfigTestJsonPath());

    manager.SetList({ "http_server", "ip" }, "127.0.0.1");
    manager.SetList({ "http_server", "port" }, 1027);

    std::string exeDir = GetParentPath(GetExecutablePath());
    std::string logDir = JoinPathComponents({ exeDir, "log" });
    CreateDirectory(logDir);
    manager.SetList({ "log_info", "log_level" }, "DEBUG");
    manager.SetList({ "log_info", "run_log_path" }, "./logs/run_log.txt");
    manager.SetList({ "log_info", "operation_log_path" }, "./logs/operation_log.txt");
    manager.SetList({"process_manager", "file_path"}, "./logs/controller_process_status.json");
    manager.SetList({"cluster_status", "file_path"}, "./logs/cluster_status_output.json");

    std::string certDir = JoinPathComponents({ exeDir, "cert_dir" });
    CreateDirectory(certDir);

    auto lambdaFun = [exeDir, &manager](std::string items) mutable {
        auto ca_cert = JoinPathComponents({ exeDir, "cert_dir", items + "_ca_cert.crt" });
        CreateFile(ca_cert, "ca_cert");
        ChangeCertsFileMode(ca_cert, S_IRUSR);

        auto tls_cert = JoinPathComponents({ exeDir, "cert_dir", items + "_tls_cert.crt" });
        CreateFile(tls_cert, "tls_cert");
        ChangeCertsFileMode(tls_cert, S_IRUSR);

        auto tls_key = JoinPathComponents({ exeDir, "cert_dir", items + "_tls_key.key" });
        CreateFile(tls_key, "tls_key");
        ChangeCertsFileMode(tls_key, S_IRUSR);

        auto tls_crl = JoinPathComponents({ exeDir, "cert_dir", items + "_tls_crl.crl" });
        CreateFile(tls_crl, "tls_crl");
        ChangeCertsFileMode(tls_crl, S_IRUSR);

        manager.SetList({ "tls_config", items, "ca_cert" }, ca_cert);
        manager.SetList({ "tls_config", items, "tls_cert" }, tls_cert);
        manager.SetList({ "tls_config", items, "tls_key" }, tls_key);
        manager.SetList({ "tls_config", items, "tls_passwd" }, "aaaa");
        manager.SetList({ "tls_config", items, "tls_crl" }, tls_crl);
    };

    mode_t oldMask = umask(0277);
    manager.SetList({ "tls_config", "request_coordinator_tls_enable" }, false);
    lambdaFun("request_coordinator_tls_items");

    // 补充，需要把开关打开

    manager.SetList({ "tls_config", "request_server_tls_enable" }, false);
    lambdaFun("request_server_tls_items");

    manager.SetList({ "tls_config", "http_server_tls_enable" }, false);
    lambdaFun("http_server_tls_items");
    umask(oldMask);
    manager.Save();
    return manager.Dump();
}


// 设置 MS 客户端 JSON 文件的默认值
std::string SetMSClientJsonDefault(const std::string &jsonPath)
{
    JsonFileManager manager(jsonPath);
    manager.Load();

    std::string exeDir = GetParentPath(GetExecutablePath());
    std::string logDir = JoinPathComponents({ exeDir, "log" });
    CreateDirectory(logDir);

    std::string certDir = JoinPathComponents({ exeDir, "cert_dir" });
    CreateDirectory(certDir);

    auto ca_cert = JoinPathComponents({ exeDir, "cert_dir", "msctl_ca_cert.crt" });
    CreateFile(ca_cert, "ca_cert");
    ChangeCertsFileMode(ca_cert, S_IRUSR);

    auto tls_cert = JoinPathComponents({ exeDir, "cert_dir", "msctl_tls_cert.crt" });
    CreateFile(tls_cert, "tls_cert");
    ChangeCertsFileMode(tls_cert, S_IRUSR);

    auto tls_key = JoinPathComponents({ exeDir, "cert_dir", "msctl_tls_key.key" });
    CreateFile(tls_key, "tls_key");
    ChangeCertsFileMode(tls_key, S_IRUSR);

    auto tls_crl = JoinPathComponents({ exeDir, "cert_dir", "msctl_tls_crl.crl" });
    CreateFile(tls_crl, "tls_crl");
    ChangeCertsFileMode(tls_crl, S_IRUSR);

    manager.SetList({ "http", "ca_cert" }, ca_cert);
    manager.SetList({ "http", "tls_cert" }, tls_cert);
    manager.SetList({ "http", "tls_key" }, tls_key);
    manager.SetList({ "http", "tls_passwd" }, "aaaa");
    manager.SetList({ "http", "tls_crl" }, tls_crl);
    manager.SetList({ "http", "timeout" }, 10);
    // 设置 log_level
    manager.Set("log_level", "INFO");

    manager.Save();
    return manager.Dump();
}

std::string SetInferJsonDefault(const std::string &jsonPath)
{
    JsonFileManager manager(jsonPath);
    manager.Load();
    manager.Set("server_name", "mindie-server");
    manager.Set("replicas", 1);
    manager.Set("service_port", 31005); // 31005 service_port
    manager.Set("cross_node_num", 2);   // 2 cross_node_num
    manager.Set("service_type", "NodePort");
    manager.Set("server_type", "mindie_cross_node");
    manager.Set("scheduler", "default");
    manager.Set("init_delay", 180);                              // 180 init_delay
    manager.SetList({ "resource_requests", "memory" }, 256000);  // 500000 memory
    manager.SetList({ "resource_requests", "cpu_core" }, 32000); // 32000 cpu_core
    manager.SetList({ "resource_requests", "npu_type" }, "Ascend910");
    manager.SetList({ "resource_requests", "npu_chip_num" }, 8);          // 8 npu核心数
    manager.SetList({ "mindie_server_config", "infer_port" }, 1025);      // 1025 infer端口
    manager.SetList({ "mindie_server_config", "management_port" }, 1026); // 1026 management_port
    manager.SetList({ "mindie_server_config", "enable_tls" }, false);
    manager.Save();

    return manager.Dump();
}

// 拷贝指定文件到另一个路径，若目的路径文件存在，则删除覆盖
bool CopyFile(const std::string &sourcePath, const std::string &destPath)
{
    std::ifstream sourceFile(sourcePath, std::ios::binary);
    if (!sourceFile.is_open()) {
        std::cerr << "Error opening source file: " << sourcePath << std::endl;
        return false;
    }

    // 如果目标文件已存在，则删除它
    if (std::remove(destPath.c_str()) != 0) {
        std::cerr << "Error removing existing destination file: " << destPath << std::endl;
    }

    // 如果目标文件不存在，会被创建
    std::ofstream destFile(destPath, std::ios::binary | std::ios::out);
    if (!destFile.is_open()) {
        std::cerr << "Error creating destination file: " << destPath << std::endl;
        sourceFile.close();
        return false;
    }

    // 逐字节拷贝文件内容
    destFile << sourceFile.rdbuf();

    // 关闭文件
    sourceFile.close();
    destFile.close();

    // 修改文件为600权限
    ChangeFileMode600(destPath);
    return true;
}

void CopyDefaultConfig()
{
    std::string deployConfigJsonPath = GetMSDeployConfigJsonPath();
    std::string controllerConfigJsonPath = GetMSControllerConfigJsonPath();
    std::string coordinatorConfigJsonPath = GetMSCoordinatorConfigJsonPath();

    std::string deployTestJsonPath = GetMSDeployTestJsonPath();
    std::string controllerTestJsonPath = GetMSControllerTestJsonPath();
    std::string coordinatorTestJsonPath = GetMSCoordinatorTestJsonPath();

    mode_t oldMask = umask(027);
    umask(oldMask);
    CopyFile(deployConfigJsonPath, deployTestJsonPath);
    CopyFile(controllerConfigJsonPath, controllerTestJsonPath);
    CopyFile(coordinatorConfigJsonPath, coordinatorTestJsonPath);
    CopyFile(GetModelConfigJsonPath(), GetModelConfigTestJsonPath());
    CopyFile(GetMachineConfigJsonPath(), GetMachineConfigTestJsonPath());

    SetInferJsonDefault(deployTestJsonPath);
    SetMSCoordinatorJsonDefault(coordinatorTestJsonPath);
    SetMSControllerJsonDefault(controllerTestJsonPath);

    ChangeFileMode600(deployTestJsonPath);
    ChangeFileMode600(controllerTestJsonPath);
    ChangeFileMode600(coordinatorTestJsonPath);

    ChangeFileMode600(GetModelConfigTestJsonPath());
    ChangeFileMode600(GetMachineConfigTestJsonPath());
}

// Function to join path components into a full path
std::string JoinPathComponents(const std::vector<std::string> &components)
{
    std::string path;
    for (const auto &component : components) {
        if (!component.empty()) {
            // Avoid adding duplicate separators
            if (!path.empty() && path.back() != '/' && component.front() != '/') {
                path += '/';
            }
            path += component;
        }
    }
    return path;
}

bool CreateDirectory(const std::string &path)
{
    struct stat info;

    // 检查目录是否存在
    if (stat(path.c_str(), &info) != 0) {
        // 目录不存在，创建目录
        if (mkdir(path.c_str(), S_IRWXU | S_IRWXG | S_IROTH | S_IXOTH) != 0) {
            std::cerr << "Error creating directory: " << path << std::endl;
            return false;
        }
    } else if (!(info.st_mode & S_IFDIR)) {
        std::cerr << "Error creating directory: " << path << " (not a directory)" << std::endl;
        return false;
    }

    return true;
}

bool RemoveDirectory(const std::string &path)
{
    if (rmdir(path.c_str()) == 0) {
        std::cerr << "Error remove directory: " << path << std::endl;
        return false;
    } else {
        return true;
    }
}

void CreateFile(std::string filename, std::string content)
{
    // 检查文件是否存在
    if (remove(filename.c_str()) != 0) {
        // 文件不存在或删除失败，不做处理
    }

    // 创建文件并写入内容
    std::ofstream outfile(filename);
    outfile << content;
    outfile.close();
}

bool ReturnFalseStub(const char *format)
{
    std::cout << "ReturnFalseStub" << std::endl;
    return false;
}

bool ReturnTrueStub(const char *format)
{
    std::cout << "ReturnTrueStub" << std::endl;
    return true;
}

void *ReturnNullptrStub(const char *format)
{
    std::cout << "ReturnNullptrStub" << std::endl;
    return nullptr;
}

int32_t ReturnZeroStub(const char *format)
{
    std::cout << "ReturnZeroStub" << std::endl;
    return 0;
}


int32_t ReturnOneStub(const char *format)
{
    std::cout << "ReturnOneStub" << std::endl;
    return 1;
}

int32_t ReturnNeOneStub(const char *format)
{
    std::cout << "ReturnNeOneStub" << std::endl;
    return -1;
}

int32_t ChangeFileMode(const std::string &filePath)
{
    int result = chmod(filePath.c_str(), S_IRUSR | S_IWUSR | S_IRGRP);
    if (result != 0) {
        std::cout << "change failed !" << std::endl;
        return -1;
    }
    return 0;
}

// 400
int32_t ChangeFileMode400(const std::string &filePath)
{
    int result = chmod(filePath.c_str(), S_IRUSR);
    if (result != 0) {
        std::cout << "change failed !" << std::endl;
        return -1;
    }
    return 0;
}

// 640
int32_t ChangeFileMode600(const std::string &filePath)
{
    int result = chmod(filePath.c_str(), S_IRUSR | S_IWUSR);
    if (result != 0) {
        std::cout << "change failed !" << std::endl;
        return -1;
    }
    return 0;
}

int32_t ChangeCertsFileMode(const std::string &filePath, mode_t mode)
{
    int result = chmod(filePath.c_str(), mode);
    if (result != 0) {
        std::cout << "change failed !" << std::endl;
        return -1;
    }
    return 0;
}


std::string GetHSECEASYPATH()
{
    const char* thirdPartyDir = std::getenv("BUILD_MIES_3RDPARTY_INSTALL_DIR");
    std::string parentPath = "";
    std::string jsonPath = "";

    if (thirdPartyDir == nullptr) {
        std::string exePath = GetExecutablePath();
        parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
        jsonPath = GetAbsolutePath(parentPath, "mindie_service/management_service/build/open_source/kmc/lib");
    } else {
        parentPath = thirdPartyDir;
        jsonPath = GetAbsolutePath(parentPath, "hseceasy/lib");
    }

    return jsonPath;
}

uint16_t GetUnBindPort()
{
    int32_t sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        std::cerr << "Error creating socket" << std::endl;
        return -1;
    }

    sockaddr_in addr;
    memset_s(&addr, sizeof(struct sockaddr_in), 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(0); // 0表示让系统自动分配端口号
    addr.sin_addr.s_addr = htonl(INADDR_ANY);

    if (bind(sock, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
        std::cerr << "Error binding socket" << std::endl;
        close(sock);
        return -1;
    }

    socklen_t addrlen = sizeof(addr);
    if (getsockname(sock, reinterpret_cast<sockaddr *>(&addr), &addrlen) < 0) {
        std::cerr << "Error getting socket name" << std::endl;
        close(sock);
        return -1;
    }

    close(sock);
    return ntohs(addr.sin_port);
}

bool WaitUtilTrue(WaitFunc func, uint32_t timeoutSeconds, uint32_t intervalMilliSeconds)
{
    bool timeOutFlag = false;
    high_resolution_clock::time_point startTime = high_resolution_clock::now();
    while (true) {
        auto ret = func();
        if (ret) { // 返回真，则退出，超时标记设置为false
            break;
        }
        high_resolution_clock::time_point currentTime = high_resolution_clock::now();
        usleep(intervalMilliSeconds * 1000); // 休眠时间，默认100毫秒，外部可配置
        auto passTime = std::chrono::duration_cast<seconds>(currentTime - startTime).count();
        if (passTime >= timeoutSeconds) {
            // 超时退出，超时标记设置为true
            timeOutFlag = true;
            break;
        }
    }
    return timeOutFlag;
}

bool FindStringInLogFile(const std::string& filename, const std::string& searchString)
{
    // 打开文件
    std::ifstream file(filename);
    if (!file.is_open()) {
        std::cerr << "无法打开文件 " << filename << std::endl;
        return false;
    }

    // 逐行读取文件内容
    std::string line;
    while (std::getline(file, line)) {
        // 在每行中查找目标字符串
        if (line.find(searchString) != std::string::npos) {
            // 如果找到了，关闭文件并返回true
            file.close();
            return true;
        }
    }

    // 关闭文件并返回false
    file.close();
    return false;
}


void SetRankTableByServerNum(uint8_t serverNum, std::string rankTableFile)
{
    nlohmann::json ranktables;
    ranktables["version"] = "1.0";
    ranktables["status"] = "completed";
    ranktables["server_group_list"] = nlohmann::json();

    // 设置coordinator的配置
    {
        nlohmann::json coordinator;
        coordinator["group_id"] = "0";
        coordinator["server_count"] = "1";
        coordinator["server_list"] = nlohmann::json();
        nlohmann::json server;
        server["server_id"] = "coordinator";
        server["server_ip"] = "127.0.0.1";
        coordinator["server_list"].push_back(server);
        ranktables["server_group_list"].push_back(coordinator);
    }

    // 设置controller的配置
    {
        nlohmann::json controller;
        controller["group_id"] = "1";
        controller["server_count"] = "1";
        controller["server_list"] = nlohmann::json();
        nlohmann::json server;
        server["server_id"] = "controller";
        server["server_ip"] = "127.0.0.1";
        controller["server_list"].push_back(server);
        ranktables["server_group_list"].push_back(controller);
    }

    // 设置mindie server的配置
    {
        nlohmann::json mindieServers;
        mindieServers["group_id"] = "2";
        mindieServers["server_count"] = std::to_string(serverNum);
        mindieServers["server_list"] = nlohmann::json();
        for (auto i = 0; i < serverNum; i++) {
            nlohmann::json server;
            server["server_id"] = "127.0.0.1";
            server["server_ip"] = "172.16.0." + std::to_string(i + 1);
            nlohmann::json device;
            device["device_id"] = std::to_string(i);
            device["device_logical_id"] = std::to_string(i);
            device["device_ip"] = "172.16.0." + std::to_string(i + 1);
            device["rank_id"] = std::to_string(i);
            server["device"].push_back(device);

            mindieServers["server_list"].push_back(server);
        }
        ranktables["server_group_list"].push_back(mindieServers);
    }
    auto context = ranktables.dump();
    CreateFile(rankTableFile, context);
    ChangeFileMode600(rankTableFile);
}

std::string GetLdLibraryPath()
{
    const char* thirdPartyDir = std::getenv("BUILD_MIES_3RDPARTY_INSTALL_DIR");
    const char* miesInstallDir = std::getenv("BUILD_MINDIE_SERVICE_INSTALL_DIR");
    std::string parentPath = "";
    std::string jsonPath = "";

    if (thirdPartyDir != nullptr && miesInstallDir != nullptr) {
        parentPath = miesInstallDir;
        jsonPath = GetAbsolutePath(parentPath, "lib");

        parentPath = thirdPartyDir;
        jsonPath += ":" + GetAbsolutePath(parentPath, "openssl/lib");
        jsonPath += ":" + GetAbsolutePath(parentPath, "hseceasy/lib");
        jsonPath += ":" + GetAbsolutePath(parentPath, "libboundscheck/lib");
    } else {
        std::string exePath = GetExecutablePath();
        parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
        jsonPath = GetAbsolutePath(parentPath, "mindie_service/management_service/output/lib");
        jsonPath += ":" + GetAbsolutePath(parentPath, "mindie_service/management_service/build/open_source/openssl");
        jsonPath += ":" + GetAbsolutePath(parentPath, "mindie_service/management_service/build/open_source/kmc/lib");
        jsonPath += ":" + GetAbsolutePath(parentPath,
            "mindie_service/management_service/build/open_source/libboundscheck/lib");
    }

    return jsonPath;
}