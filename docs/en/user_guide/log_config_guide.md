# Log Configuration

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:05:38.061Z pushedAt=2026-07-01T08:47:49.998Z -->

## Feature Introduction

The pyMotor logging capability is enhanced based on the third-party component `logging`.

After modifying the `user_config.json` configuration file, you can complete service deployment using the `deploy.py` script, and logs will be retained during service operation. Log persistence to drive is an optional setting; the default is non-persistent storage.

## Configuration Guide

To enable log persistence for pyMotor, modify the `user_config.json` configuration file and then deploy the service using the `deploy.py` script. The specific process is as follows.

### Configuring `user_config.json`

Using the `user_config.json` instance in [PyMotor Quick Start](../user_guide/quick_start.md) as a reference baseline, the configuration snippet for enabling log persistence is as follows:

```json
{
  "motor_controller_config": {
    "logging_config": {
      "log_level": "INFO",
      "log_max_line_length": 8192,
      "log_format": "%(asctime)s  [%(levelname)s][%(name)s][%(filename)s:%(lineno)d][proc:%(processName)s]  %(message)s",
      "log_date_format": "%Y-%m-%d %H:%M:%S",
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

The `logging_config` configuration item can be added under the three configuration items: `motor_controller_config`, `motor_coordinator_config`, and `motor_nodemanger_config`.
Description of the `logging_config` configuration item:

- `log_level`: log level, options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, defaulting to `INFO`

- `log_max_line_length`: maximum length of a log line, defaulting to `8192`

- `log_format`: log format, defaulting to `%(asctime)s  [%(levelname)s][%(name)s][%(filename)s:%(lineno)d][proc:%(processName)s]  %(message)s`

- `log_date_format`: log date format, defaulting to `%Y-%m-%d %H:%M:%S`

- `host_log_dir`: log storage directory, defaulting to `/root/ascend/log/motor`, where logs are persisted

- `log_rotation_size`: maximum size of a single log file, defaulting to `10`, in MB. Log rotation occurs when this size is exceeded

- `log_rotation_count`: Maximum number of log files, defaulting to `10` (also applies to compressed logs). Log rotation occurs when this number is exceeded.

- `log_compress`: Whether to enable log compression, defaulting to `false`. When enabled, rotated log files will be compressed into `.gz` format.

- `log_compress_level`: Log compression level, defaulting to `6`. Value range is 1-9. A larger number provides better compression but slower compression speed.

- `log_max_total_size`: Maximum total size of log files for a single component and single thread, defaulting to `200`, in MB. Historical log files will be deleted when this size is exceeded.

When both `log_max_total_size` and `log_rotation_count` are configured, log rotation and log deletion occur when either condition is met.

### Deploying the Service

Deploy the service using the `deploy.py` script in the `examples/deployer` directory. You can specify a configuration directory or individual configuration files:

```bash
cd examples/deployer
# Method 1: Specify a configuration directory (recommended)
python deploy.py --config_dir ../infer_engines/vllm

# Method 2: Specify individual configuration files
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```
