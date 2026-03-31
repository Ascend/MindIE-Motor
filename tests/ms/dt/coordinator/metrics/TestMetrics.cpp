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
#include <map>
#include <iostream>
#include <string>
#include "gtest/gtest.h"
#include "Metrics.h"
#include "Helper.h"
#include "stub.h"
#include "RequestMgr.h"

using namespace MINDIE::MS;

class TestMetrics : public testing::Test {
protected:
    void SetUp()
    {
        std::string exePath = GetExecutablePath();
        std::string parentPath = GetParentPath(GetParentPath(GetParentPath(GetParentPath(exePath))));
        msMetricsBin = GetAbsolutePath(parentPath, "tests/ms/common/.mindie_ms/ms_metrics.bin");
    }

    void TearDown()
    {
    }
    std::string msMetricsBin;
};

// 打桩
nlohmann::json GetServerMetircsStub(const std::map<uint64_t, InstanceInfo> &podInfos)
{
    std::string metrics = R"(# HELP request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
num_request_success_total{model_name=\"llama_65b\"} 400
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json collects = nlohmann::json::parse(s);
    collects[0]["metrics_str"] = metrics;
    collects[1]["metrics_str"] = metrics;
    return collects;
}

int32_t ParseMetricsStub(nlohmann::json &podMetric)
{
    return 0;
}

nlohmann::json AggregateMetricsStub(nlohmann::json &podMetric)
{
    nlohmann::json aggregate = {
        {
            {"HELP", "Count of successfully processed requests."},
            {"TYPE", "counter"},
            {"LABEL", {"request_success_total{model_name=\"llama_65b\"}"}},
            {"VALUE", {400}}
        }
    };
    return aggregate;
}

int32_t ParseMetricsFailStub(nlohmann::json &podMetric)
{
    return -1;
}

nlohmann::json AggregateMetricsEmptyStub(nlohmann::json &podMetric)
{
    return nlohmann::json::array();
}

static nlohmann::json BuildSinglePodMetric(const std::string &metrics, const std::string &identity = "U")
{
    nlohmann::json podMetric = nlohmann::json::array();
    podMetric.push_back({
        {"NPU_mem_size", 320},
        {"identity", identity},
        {"ip", "172.17.0.7"},
        {"metrics_str", metrics},
        {"port", "1027"}
    });
    return podMetric;
}

static std::map<std::string, uint64_t> BuildEmptyStats()
{
    return {
        {"numAllRequests", 0},
        {"numFailRequests", 0},
        {"numSuccessRequests", 0}
    };
}

static void ExpectMetricValue(const nlohmann::json &metrics, const std::string &name, double expected)
{
    bool found = false;
    for (const auto &item : metrics) {
        if (item["NAME"] == name) {
            EXPECT_DOUBLE_EQ(item["VALUE"][0].get<double>(), expected);
            found = true;
        }
    }
    EXPECT_TRUE(found);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，podMetric为空
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC01)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string s = "[]";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，podMetric不包含metrics_str
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC02)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，metrics_str为非字符串
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC03)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": 80,
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": 80,
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，缺少# HELP
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC04)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
num_request_success_total{model_name=\"llama_65b\"} 400
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，缺少Name
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC05)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP #request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
num_request_success_total{model_name=\"llama_65b\"} 400
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，HELP匹配空格失败
测试步骤
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC06)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total
Count of successfully processed requests.
# TYPE request_success_total counter
request_success_total{model_name=\"llama_65b\"} 400
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，HELP匹配失败
测试步骤
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC07)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total )";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，HELP匹配换行符失败
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC08)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfully processed requests.)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，# TYPE 匹配失败
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC09)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfullyprocessedrequests.
request_success_total counter
num_request_success_total{model_name=\"llama_65b\"} 400
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，匹配metric name失败
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC10)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfullyprocessedrequests.
# TYPE #request_success_total counter
request_success_total{model_name=\"llama_65b\"} 400
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，匹配空格失败
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC11)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfullyprocessedrequests.
# TYPE request_success_total
counter
request_success_total{model_name=\"llama_65b\"} 400
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，匹配metric type失败
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC12)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfullyprocessedrequests.
# TYPE request_success_total #
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，TYPE匹配换行符失败
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC13)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfullyprocessedrequests.
# TYPE request_success_total counter)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，匹配label失败
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC14)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
 )";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，匹配空格失败
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC15)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
request_success_total{model_name=\"llama_65b\"}
400
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，缺少value
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC16)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
request_success_total{model_name=\"llama_65b\"} 
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，value非数字
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC17)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
request_success_total{model_name=\"llama_65b\"} invalid_number
)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，跳出ParseMetricBody内的循环返回-1
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC18)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics = R"(# HELP request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
request_success_total{model_name=\"llama_65b\"} 400

)";

    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics;
    podMetric[1]["metrics_str"] = metrics;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

/*
测试描述:ParseMetrics测试，测试函数在异常场景能够正常运行，两个pods内的metrics个数不同
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, UTMetricTC19)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string metrics0 = R"(# HELP request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
request_success_total{model_name=\"llama_65b\"} 400
)";

    std::string metrics1 = R"(# HELP request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
request_success_total{model_name=\"llama_65b\"} 400
# HELP request_success_total Count of successfully processed requests.
# TYPE request_success_total counter
request_success_total{model_name=\"llama_65b\"} 400
)";
    
    std::string s = R"(
    [
    {
    "NPU_mem_size": 320,
    "identity": 80,
    "ip": "172.17.0.7",
    "metrics_str": "",
    "port": "1027"
    },
    {
    "NPU_mem_size": 320,
    "identity": 68,
    "ip": "172.17.0.8",
    "metrics_str": "",
    "metrics": "",
    "port": "1027"
    }
    ]
    )";

    nlohmann::json podMetric = nlohmann::json::parse(s);
    std::cout << "pod json---------\n" << podMetric << std::endl;
    podMetric[0]["metrics_str"] = metrics0;
    podMetric[1]["metrics_str"] = metrics1;
    std::cout << "pod json with str---------\n" << podMetric << std::endl;
    auto ret = metricInstance.ParseMetrics(podMetric);
    EXPECT_EQ(ret, -1);
}

TEST_F(TestMetrics, UTMetricTC20)
{
    Metrics metricInstance;
    metricInstance.InitMetricsPattern();
    std::string hitOnlyMetrics = R"(# HELP all_radix_match_num all
# TYPE all_radix_match_num gauge
all_radix_match_num{model_name="llama_65b"} 0
# HELP npu_radix_match_hit_num hit
# TYPE npu_radix_match_hit_num gauge
npu_radix_match_hit_num{model_name="llama_65b"} 5
# HELP npu_prefix_cache_hit_rate rate
# TYPE npu_prefix_cache_hit_rate gauge
npu_prefix_cache_hit_rate{model_name="llama_65b"} 0.2
)";
    auto podMetric = BuildSinglePodMetric(hitOnlyMetrics);
    ASSERT_EQ(metricInstance.ParseMetrics(podMetric), 0);
    auto aggregated = metricInstance.AggregateMetrics(podMetric, BuildEmptyStats());
    ExpectMetricValue(aggregated, "npu_prefix_cache_hit_rate", 1.0);

    std::string invalidMetrics = R"(# HELP all_radix_match_num all
# TYPE all_radix_match_num gauge
all_radix_match_num{model_name="llama_65b"} 1
# HELP npu_radix_match_hit_num hit
# TYPE npu_radix_match_hit_num gauge
npu_radix_match_hit_num{model_name="llama_65b"} 1
# HELP npu_prefix_cache_hit_rate rate
# TYPE npu_prefix_cache_hit_rate gauge
npu_prefix_cache_hit_rate{model_name="llama_65b"} 0.2
)";
    podMetric = BuildSinglePodMetric(invalidMetrics);
    ASSERT_EQ(metricInstance.ParseMetrics(podMetric), 0);
    podMetric[0]["metrics"][0]["VALUE"][0] = -1.0;
    podMetric[0]["metrics"][1]["VALUE"][0] = -1.0;
    podMetric[0]["metrics"][2]["VALUE"][0] = -1.0;
    aggregated = metricInstance.AggregateMetrics(podMetric, BuildEmptyStats());
    ExpectMetricValue(aggregated, "npu_prefix_cache_hit_rate", 0.0);
}

TEST_F(TestMetrics, SerializeMetricsTC01)
{
    Metrics metricInstance;
    nlohmann::json metrics = {
        {
            {"NAME", "request_success_total"},
            {"HELP", "Count of successfully processed requests."},
            {"TYPE", "counter"},
            {"LABEL", {"request_success_total{model_name=\"llama_65b\"}"}},
            {"VALUE", {std::numeric_limits<double>::infinity()}}
        },
        {
            {"NAME", "request_failed_total"},
            {"HELP", "Number of responses failed so far"},
            {"TYPE", "counter"},
            {"LABEL", {"request_failed_total{model_name=\"llama_65b\"}"}},
            {"VALUE", {std::numeric_limits<double>::quiet_NaN()}}
        }
    };
    std::string result = metricInstance.SerializeMetrics(metrics);

    std::string expected = "# HELP request_success_total Count of successfully processed requests.\n"
                           "# TYPE request_success_total counter\n"
                           "request_success_total{model_name=\"llama_65b\"} +Inf\n"
                           "# HELP request_failed_total Number of responses failed so far\n"
                           "# TYPE request_failed_total counter\n"
                           "request_failed_total{model_name=\"llama_65b\"} Nan\n";

    EXPECT_EQ(result, expected);
}

/*
测试描述:GetAndAggregateMetrics打桩测试，测试SerializeMetrics函数在异常场景能够捕获异常,NAME缺失
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回-1
*/
TEST_F(TestMetrics, GetAndAggregateMetricsTC01)
{
    std::unique_ptr<MINDIE::MS::DIGSScheduler> scheduler = nullptr;
    std::unique_ptr<PerfMonitor> perfMonitor = nullptr;
    std::unique_ptr<ClusterNodes> instancesRecord = nullptr;
    ReqManage tempInstance(scheduler, perfMonitor, instancesRecord);
    Metrics metricInstance;
    InstanceInfo instance("127.0.0.1", "8080", MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, "model1");
    std::map<uint64_t, InstanceInfo> podInfos;
    uint64_t id = 1;
    podInfos[id] = instance;

    Stub stub;
    stub.set(ADDR(Metrics, GetServerMetircs), &GetServerMetircsStub);
    stub.set(ADDR(Metrics, AggregateMetrics), &AggregateMetricsStub);
    stub.set(ADDR(Metrics, ParseMetrics), &ParseMetricsStub);
    std::string result = metricInstance.GetAndAggregateMetrics(podInfos);
    std::string expected = "";
    EXPECT_EQ(result, expected);
    stub.reset(ADDR(Metrics, GetServerMetircs));
    stub.reset(ADDR(Metrics, AggregateMetrics));
    stub.reset(ADDR(Metrics, ParseMetrics));
}

/*
测试描述:GetAndAggregateMetrics测试，测试在异常场景能够正常运行,无Server进程
测试步骤:
    1.调用函数进行操作
预期结果:
    1.调用失败，返回""
*/
TEST_F(TestMetrics, GetAndAggregateMetricsTC02)
{
    Metrics metricInstance;
    InstanceInfo instance("127.0.0.1", "8080", MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE, "model1");
    std::map<uint64_t, InstanceInfo> podInfos;
    uint64_t id = 1;
    podInfos[id] = instance;

    std::string result = metricInstance.GetAndAggregateMetrics(podInfos);
    std::string expected = "";
    EXPECT_EQ(result, expected);
}

TEST_F(TestMetrics, GetAndAggregateMetricsTC03)
{
    Metrics metricInstance;
    std::map<uint64_t, InstanceInfo> podInfos;
    podInfos[1] = InstanceInfo(
        "127.0.0.1",
        "8080",
        MINDIE::MS::DIGSInstanceRole::PREFILL_INSTANCE,
        "model1");
    Stub stub;
    stub.set(ADDR(Metrics, GetServerMetircs), &GetServerMetircsStub);
    stub.set(ADDR(Metrics, ParseMetrics), &ParseMetricsFailStub);
    EXPECT_EQ(metricInstance.GetAndAggregateMetrics(podInfos), "");
    stub.reset(ADDR(Metrics, GetServerMetircs));
    stub.reset(ADDR(Metrics, ParseMetrics));

    stub.set(ADDR(Metrics, ParseMetrics), &ParseMetricsStub);
    stub.set(ADDR(Metrics, AggregateMetrics), &AggregateMetricsEmptyStub);
    EXPECT_EQ(metricInstance.GetAndAggregateMetrics(podInfos), "");
    stub.reset(ADDR(Metrics, GetServerMetircs));
    stub.reset(ADDR(Metrics, ParseMetrics));
    stub.reset(ADDR(Metrics, AggregateMetrics));
}

TEST_F(TestMetrics, TryUpdateTokenDistributionFromUsageTC01)
{
    token_distribution.clear();
    std::string body = "data: {\"usage\":{\"prompt_tokens\":12,\"completion_tokens\":55}}\n";
    TryUpdateTokenDistributionFromUsage(body);
    EXPECT_EQ(token_distribution[50][100], 1);
    token_distribution.clear();
    TryUpdateTokenDistributionFromUsage("data: [DONE]\n");
    EXPECT_TRUE(token_distribution.empty());
    TryUpdateTokenDistributionFromUsage("data: {\"usage\":{\"prompt_tokens\":1}}\n");
    TryUpdateTokenDistributionFromUsage("data: {\"usage\": }\n");
    EXPECT_TRUE(token_distribution.empty());
}

/*
测试描述: 测试服务监控指标查询，异常metrics string
测试步骤:
    1. 创建异常metrics string
    2. 调用Metrics的ParseMetrics接口
预期结果:
    1. 返回正常
*/
TEST_F(TestMetrics, TestRandomMetricsStrTC01)
{
    int fuzzCount = 3000;

    std::ifstream readFile(msMetricsBin);
    std::stringstream dataout;
    dataout << readFile.rdbuf();
    std::string metricStr = dataout.str();
    readFile.close();

    if (!nlohmann::json::accept(metricStr)) {
        LOG_E("[%s] invalid json input",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::JSON_FILE_LOADER).c_str());
    }
    auto metricsJson = nlohmann::json::parse(metricStr);
    std::string originMetric = metricsJson[0]["metrics_str"];
    printf("%s\n", originMetric.c_str());

    srand(0);
    Metrics m;
    m.InitMetricsPattern();
    for (size_t i = 0; i < originMetric.size(); i++) {
        std::string tmpStr = originMetric;
        int fuzzChar = rand() % 128; // ascii [0, 127]
        printf("%d, %d\n", i, fuzzChar);
        if (char(fuzzChar) == '\\') {
            continue;
        }
        tmpStr[i] = char(fuzzChar);
        metricsJson[0]["metrics_str"] = tmpStr;
        m.ParseMetrics(metricsJson);
    }
}