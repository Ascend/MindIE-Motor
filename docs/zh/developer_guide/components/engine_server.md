# Motor Engine Server（推理引擎侧进程）

## 功能介绍

在本仓库中，**Engine Server** 指可执行入口 **`engine_server`**（`setup.py` 中 entry point：`engine_server = motor.engine_server.cli.main:main`），对应实现为 `motor/engine_server/cli/main.py`。

主要职责：

1. **解析端点配置**：`EndpointConfig.init_endpoint_config()`，经 `ConfigFactory` 得到具体引擎配置（`motor/engine_server/factory/config_factory.py` 中按 `vllm` / `sglang` 等类型选择配置类）。
2. **管理面 HTTP（MgmtEndpoint）**：`motor/engine_server/core/mgmt_endpoint.py` 内在 `mgmt_port` 上启动 uvicorn，挂载 Prometheus 相关路由及 **`GET /status`**（路径常量 `STATUS_INTERFACE`，值为 `/status`）。状态字段键为 `STATUS_KEY` 对应常量（与同文件 `NORMAL_STATUS` / `ABNORMAL_STATUS` / `INIT_STATUS` 等配合使用）。可选 **TLS**（`mgmt_tls_config.enable_tls`）。
3. **推理面（InferEndpoint）**：由 `EndpointFactory.get_infer_endpoint(config)` 按引擎类型构造（如 `VLLMEndpoint`、`SGLangEndpoint`，见 `motor/engine_server/factory/endpoint_factory.py`），与 `MgmtEndpoint` 并行 `run()`，主线程在 `infer_endpoint.wait()` 阻塞直至退出，再 `shutdown` 两端点。

Node Manager 侧通过子进程命令 **`engine_server`** 拉起本进程，参数在 `motor/node_manager/core/daemon.py` 的 `pull_engine` 中写死格式，包括 `--dp-rank`、`--instance-id`、`--role`、`--host`、`--port`、`--mgmt-port`、`--master-dp-ip`、`--config-path`（值为 `Env.user_config_path`）；单容器模式下还会追加 `--kv-port`、`--dp-rpc-port` 等（见同方法）。

## 与 Node Manager / Controller 的关系

- **启动**：Controller 经组装实例后，由 Node Manager 的 `POST /node-manager/start` 触发 `Daemon.pull_engine`，从而 `subprocess.Popen` 执行 `engine_server ...`。
- **健康检查**：`motor/node_manager/api_client/engine_server_api_client.py` 使用 `SafeHTTPSClient` 对 `{ip}:{mgmt_port}` 发起 **`GET /status`**，TLS 选项来自 `NodeManagerConfig.from_json().mgmt_tls_config`。
- **停止**：`POST /node-manager/stop` 调用 `Daemon.stop`，对记录的 PID `SIGKILL`。

### 虚推（虚拟推理）健康探测

虚推用于在业务低负载时主动探测推理引擎是否可用，配置项位于 `user_config` 中 `motor_engine_prefill_config` / `motor_engine_decode_config` 的 **`health_check_config`**，参数说明见 [配置参考第 6 节](../../user_guide/deployment/k8s/config_reference.md#6-motor_engine_prefill_config--motor_engine_decode_configpd-引擎)。

**启用条件**（须同时满足）：

1. `health_check_config.enable_virtual_inference` 为 `true`
2. `0 < health_check_config.npu_usage_threshold <= 100`
3. 推理面 `GET /health` 返回正常（由 `HealthCollector` 探测，`health_collector_timeout` 控制超时）

满足条件后，`mgmt_endpoint.py` 在首次 `/status` 请求时调用 `run_virtual_inference()` 启动虚推循环。

**虚推请求**：向推理面 `POST /v1/completions`，请求体为 `prompt: "1"`、`max_tokens: 1`。Decode 角色额外携带 `kv_transfer_params.do_virtual: true` 及 PD 分离相关字段。

**动态探测间隔**：

| AICore 峰值（约 3 秒采样窗口） | 下一轮间隔 |
|-------------------------------|------------|
| ≥ 80% | 20 秒 |
| < `npu_usage_threshold` | 5 秒（默认） |
| `[npu_usage_threshold, 80%)` | 保持当前间隔不变 |

**异常判定**：当 AICore 峰值低于 `npu_usage_threshold` 且虚推请求失败时，累计连续失败次数；达到 `max_failure_count` 后，`GET /status` 返回 `abnormal`。Node Manager 的 `HeartbeatManager` 连续 5 次收到 abnormal 后触发自杀重调度。

**vLLM 指标过滤（v0.18+）**：启用虚推时，Engine Server 会 patch vLLM `OutputProcessor._update_stats_from_finished`，在写入 per-request 指标前跳过 `external_req_id` 含 `_virtual` 后缀的虚推请求（对应虚推 `X-Request-Id: {timestamp}_virtual`）。仅过滤 `request_success_total` 等 per-request 指标；`prompt_tokens` / `generation_tokens` 等 iteration 级 counter 仍会累计。

## 配置说明

引擎与端点相关字段分布在 `user_config` 的引擎配置与 **`motor_nodemanger_config`** 等中；与 vLLM/SGLang 引擎子字段的对照见 [配置参考](../../user_guide/deployment/k8s/config_reference.md) 及其中 **`motor_engine_prefill_config` / `motor_engine_decode_config`** 等章节。TLS 等与 `EndpointConfig` 交叉的项以 `motor/config/endpoint.py` 及 `config_reference` 为准。

## 使用样例（本地调试）

与 Node Manager 下发命令一致，需满足 `EndpointConfig` 所需环境/参数，例如：

```bash
engine_server --dp-rank 0 --instance-id 1 --role prefill \
  --host 127.0.0.1 --port 8000 --mgmt-port 8001 \
  --master-dp-ip 127.0.0.1 --config-path /path/to/user_config.json
```

实际端口与角色以调度结果为准；单机调试请参考测试与示例配置。

## 报错与日志

- 默认日志文件路径常量见 `motor/engine_server/constants/constants.py`（如 `LOG_DEFAULT_FILE` 相对 `./engine_server_log/`）。
- Mgmt 面 `/status` 在健康检查异常时返回 `ABNORMAL_STATUS` 等（见 `mgmt_endpoint.get_status` 实现）；Node Manager 侧据此更新 endpoint 状态并可能参与自杀判断（见 `HeartbeatManager`）。
