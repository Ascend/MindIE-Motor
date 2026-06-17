# 单容器pd分离部署指南

## 特性介绍

pyMotor支持单个容器内启动PD分离服务:Coordinator/controller/PD实例。

## 部署流程

pyMotor修改user_config.json配置文件后，通过deploy.py脚本即可完成服务部署，具体流程如下。

### 配置user_config.json

以[PyMotor快速开始](../../../../docs/zh/user_guide/quick_start.md)中示例 `user_config.json` 为参考基线，相关适配点如下：

```json
  "motor_deploy_config": {
    ...
    "deploy_mode": "single_container"
  },
  "motor_controller_config": {
    ...
    "fault_tolerance_config": {
      "enable_fault_tolerance": false,
      "enable_scale_p2d": true,
      "enable_token_reinference": true
    },
    "api_config": {
      "controller_api_port": 2026
    }
  },
  "motor_coordinator_config": {
    ...
    "api_config": {
      "coordinator_api_infer_port": 1025,
      "coordinator_api_mgmt_port": 1026,
      "coordinator_obs_port": 1027
    }
  },
  "motor_engine_prefill_config": {
    ...
    "motor_nodemanger_config": {
      "api_config": {
        "node_manager_port": 3026
      }
    },
    "model_config": {
      ...
      "prefill_parallel_config": {
        ...
        "dp_rpc_port": 9000
      }
    },
    "engine_config": {
      ...
      "kv_transfer_config": {
        ...
        "kv_port": "20001",
        ...
      }
    }
  },
  ...
}
```

#### 说明

(1)不支持RAS特性，需将enable_fault_tolerance设置为false
(2)各组件端口需确保互不重叠
i. node_manager_port/dp_rpc_port/lookup_rpc_port需确保每个实例不重叠，实际部署时会自动按照先P后D的顺序，依次偏移1，取值范围[基础端口, 基础端口 + 总实例数)。其中dp_rpc_port/lookup_rpc_port基础端口以prefill配置为准
ii. kv_port需确保每个dp组不重叠，实际部署时会自动按照先P后D的顺序，依次偏移dp组卡数，取值范围[kv_port, kv_port + 总卡数)

### 部署服务

当前目录提供了 user_config 模板——`user_config.json`，在 `examples/deployer` 目录下执行以下命令即可完成服务部署：

```bash
cd examples/deployer
# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm/single_container

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/single_container/user_config.json --env_config_path ../infer_engines/vllm/single_container/env.json
```
