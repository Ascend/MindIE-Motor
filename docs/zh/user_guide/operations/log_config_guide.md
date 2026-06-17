# 日志配置

## 特性介绍

pyMotor 日志能力基于三方件`logging`能力增强。

通过修改user_config.json配置文件后即可通过deploy.py脚本完成服务部署，业务运行均会保留日志。日志持久化磁盘为可选配置项，默认为非持久化磁盘。

## 配置指导

pyMotor开启日志持久化需修改user_config.json配置文件后，通过deploy.py脚本即可完成服务部署，具体流程如下。

### 配置user_config.json

以[PyMotor快速开始](../quick_start.md)中实例user_config.json为参考基线，开启日志持久化的配置片段如下：

```json
{
  "motor_controller_config": {
    "logging_config": {
      "log_level": "INFO",
      "log_max_line_length": 8192,
      "log_format": "(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s [%(name)s][%(fileinfo)s:%(lineno)d] %(message)s",
      "log_date_format": "%m-%d %H:%M:%S",
      "host_log_dir": "/root/ascend/log/motor",
      "log_rotation_size": 10,
      "log_rotation_count": 10,
      "log_compress": false,
      "log_compress_level": 6,
      "log_max_total_size": 200
    }
  }
}
```

logging_config配置项可补充在`motor_controller_config`、`motor_coordinator_config`、`motor_nodemanger_config`三个配置项下。
logging_config配置项说明：

- `log_level`：日志级别，可选：`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`，默认为`INFO`
- `log_max_line_length`：日志行最大长度，默认为8192
- `log_format`：日志格式，默认为`(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s [%(name)s][%(fileinfo)s:%(lineno)d] %(message)s`
- `log_date_format`：日志时间格式，默认为`%m-%d %H:%M:%S`
- `host_log_dir`：日志保存目录，默认为`/root/ascend/log/motor`，持久化到该文件夹。
- `log_rotation_size`：单个日志文件最大值，默认10，单位为MB，超过此大小则进行日志轮转
- `log_rotation_count`：日志文件最大数量，默认10个（压缩日志同样生效），超过此数量则进行日志轮转
- `log_compress`：是否开启日志压缩，默认为false。开启后轮转的日志文件会进行压缩，为`.gz`格式
- `log_compress_level`：日志压缩等级，默认6。取值范围1-9，数字越大压缩效果越好，但压缩速度越慢
- `log_max_total_size`：单组件单线程日志文件最大总大小，默认200，单位为MB，超过此大小历史日志文件会进行删除

当log_max_total_size和log_rotation_count同时配置时，达到任意一个条件时进行日志轮转和日志删除。

### 部署服务

在 `examples/deployer` 目录下通过`deploy.py`脚本部署服务。支持指定配置目录或单独指定配置文件：

```bash
cd examples/deployer
# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```
