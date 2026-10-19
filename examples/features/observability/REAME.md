# 功能介绍

CCAE(Cluster Computing Autonomous Engine)是华为开发的一套集群自治引擎系统。Motor 推理服务可纳管至 CCAE。

CCAE Reporter 负责与 CCAE 对接，采集 Motor 的运行信息（告警、日志、实例信息和metrics 等），上报到 CCAE。

# 配置说明

## user_config.json 配置

在 `user_config.json` 中添加 CCAE 配置：

```json
{
  "motor_deploy_config": {
    "tls_config": {
      "north_tls_config": {
        "enable_tls": true,
        "ca_file": "",
        "cert_file": "",
        "key_file": "",
        "passwd_file": ""
      }
    }
  },
  "north_config": {
    "name": "ccae_reporter",
    "ip": "xxx",
    "port": 31948
  }
}
```

> 说明：该配置支持动态修改，Motor 运行过程中可以直接对接 CCAE，无需重启 Motor 推理服务。

## 环境变量

| 环境变量 | 说明 |
| --- | --- |
| `CCAE_REPORTER_LOG_LEVEL` | Reporter 自身日志级别。可选 `DEBUG` / `INFO` / `WARN` / `ERROR` / `CRITICAL` / `NULL`（关闭文件日志）。未设置或为空时为 `INFO`。非法非空值会输出 warning 并回退到 `INFO`。`WARNING` 视为 `WARN`。 |
| `MOTOR_LOG_PATH` | Reporter 自身日志目录与采集根目录。未设置时默认 `/root/ascend/log`。 |

日志采集只监控 Controller / Coordinator 日志，文件名需匹配 `*-controller-*.log` / `*-coordinator-*.log`（对应默认开启 `logging_config.log_collector_enabled` 后的 `{HOSTNAME}.log`）。关闭 collector 后的 `{HOSTNAME}_{pid}.log` 不会被采集。

修改完配置后，在 `examples/deployer` 目录下执行以下命令更新配置：

```bash
cd examples/deployer
# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../../model_configs/vllm --update_config

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../../model_configs/vllm/user_config.json --env_config_path ../../model_configs/vllm/env.json --update_config
```
