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

修改完配置后，在 `examples/deployer` 目录下执行以下命令更新配置：

```bash
cd examples/deployer
# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm --update_config

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json --update_config
```
