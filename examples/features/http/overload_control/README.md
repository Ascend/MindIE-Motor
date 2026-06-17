# 功能介绍

一个强大的过载控制库，支持多种 Web 框架的限流和过载保护。

## 特性

- 🚀 **高性能限流**: 支持 QPS 限流、并发限流、配额限流等多种限流策略
- 🎯 **精准匹配**: 基于标签的请求匹配和分组限流

### 快速开始

1. 安装

```bash
pip install git+https://gitcode.com/openFuyao/olc-python.git@v0.1.0
```

这会安装 OLC 核心库。
显示如下内容后表示安装成功

```bash
Successfully built olc
Installing collected packages: olc
Successfully installed olc-0.1.0
```

#### 示例

目前在examples/features/http/overload_control/config已有默认配置文件：
默认配置文件效果为：
规则 completions_flow_group： 对 /v1/completions,/v1/chat/completions 接口，限流没60秒通过 1000个
规则 completions_concurrent_group： 对 /v1/completions,/v1/chat/completions 接口，进行并发控制， 并发数为100

规则配置详情参考 [OLC 官方文档](https://gitcode.com/openFuyao/olc-python)

**config/overload-config.properties**：

```properties
# 项目名称，与规则中domain对应
olc.sdk.domain=coordinator
# 动态开关
olc.sdk.switch=on

# 从本地读取限流规则
olc.sdk.config.stub=jsonfile
# 规则文件名
olc.stub.jsonfile.name=olc.json
```

**config/olc.json**：

```json

{
  "domain": "coordinator",
  "rules": [
    {
      "group": {
        "priority": 50,
        "enabled": true,
        "name": "completions_flow_group",
        "tags": [
          {
            "tag": "URL",
            "match": "equal",
            "values": [
              "/v1/completions",
              "/v1/chat/completions"
            ]
          }
        ]
      },
      "flow": {
        "name": "completions_flow",
        "enabled": true,
        "flowControlMode": "quota",
        "timeUnit": "second",
        "timeInterval": 60,
        "rateLimit": 1000,
        "burstLimit": 1000
      }
    },
    {
      "group": {
        "priority": 50,
        "enabled": true,
        "name": "completions_concurrent_group",
        "tags": [
          {
            "tag": "URL",
            "match": "equal",
            "values": [
              "/v1/completions",
              "/v1/chat/completions"
            ]
          }
        ]
      },
      "flow": {
        "name": "completions_concurrent",
        "enabled": true,
        "flowControlMode": "concurrent",
        "rateLimit": 100
      }
    }
  ]
}
```

#### 在coordinator中启用OLC

修改 user_config.json 中相关配置项

```json

{
  "version": "v2.0",
  "motor_coordinator_config": {
    "rate_limit_config": {
      "provider": "olc",
      "olc_config_path": "examples/features/http/overload_control/config"
    }
  }
}

```

### 扩展说明

当需要添加更多维度标签时，可以在_extract_tags_from_request 方法中继续为请求添加标签提取方法
