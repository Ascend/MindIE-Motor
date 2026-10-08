# xLLM 单机非 PD 接入

本例使用 Motor Coordinator → Controller → NodeManager → 原生 xLLM。
每个实例占用一张 NPU、一个 NodeManager 和一个 xLLM 进程，支持健康检查、
自动注册、现有负载均衡、OpenAI 非流式/流式转发，以及 Device/本地 Host KV 上报。
初始版本只支持文本 LLM、DP=TP=PP=CP=1；不启用 PD、Render、主备或 Kubernetes RAS。

## 安装与启动

修改 `user_config.json` 中的模型路径、模型名及空闲端口。
在目标 Ascend 容器中安装修改后的 Motor wheel 和 requirements，
将已编译且支持 `--kv_events_config` 的 xLLM 二进制目录放入 `PATH`。
先加载对应 CANN/ATB 环境，并设置 xLLM 原有的运行时库路径。

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
export USER_CONFIG_PATH=/absolute/path/user_config.json
export POD_IP=127.0.0.1
export CONTROLLER_SERVICE=127.0.0.1
export COORDINATOR_SERVICE=127.0.0.1
export PATH=/absolute/path/xllm/build/cmake.linux-aarch64-cpython-311/xllm:$PATH

# 在各自终端启动，保存自己的 PID；进程退出时只清理该进程及其拥有的引擎。
/absolute/path/venv/lib/python3.11/site-packages/motor/kv_conductor/bin/kv-conductor --host 127.0.0.1 --port 13333
python -m motor.coordinator.main
python -m motor.controller.main
ROLE=union INDEX=0 JOB_NAME=xllm-union-0 python -m motor.node_manager.main
```

Controller 收到 NodeManager 注册后，自动发出启动命令。不要另行手动启动 xLLM。
通过 Coordinator 推理端口发送请求：

```bash
curl http://127.0.0.1:1025/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3-8B","messages":[{"role":"user","content":"你好"}],"max_tokens":32,"stream":false}'
```

检查 Coordinator `1026/readiness`、观测端口 `1027/instances` 以及 Conductor
`13333/workers`。注册应显示 `xllm-union-<Motor instance id>`，包括同一实例的
`npu` 与 `cpu` 端点。非流式响应和 SSE 均通过 Coordinator。

## 副本与 KV 端口

在有空闲 NPU 时，可再启动 `ROLE=union INDEX=1 JOB_NAME=xllm-union-1` 的
NodeManager。同一配置下，INDEX=0/1 分别选择设备 0/1，业务端口 8000/8002，
NodeManager 端口 3026/3027。Controller 自动将可用实例同步给 Coordinator。
在多机部署时，各机通过可达的 POD_IP、Controller/Coordinator 地址和独立配置注册；
不要使用本例的回环地址。

| 端点 | INDEX=0 | INDEX=1 |
|---|---:|---:|
| xLLM 推理 | 8000 | 8002 |
| xLLM RPC | 13000 | 13002 |
| Device/Host 内容事件 | 18000 | 18002 |
| 本地 Host pool 确认 | 28000 | 28002 |
| 事件重放 | 38000 | 38002 |

三个 KV 端口通过 `kv_conductor_config.xllm_*_port_offset` 相对业务端口计算，
NodeManager 发布与 Coordinator 订阅使用同一函数。偏移配置必须在各组件一致，
且所有业务/RPC/KV/HCCL 端口应空闲并可达。Conductor 重启后按
`re_register_interval_sec` 自动恢复注册，xLLM 的重放和周期快照恢复缓存目录。
`type=vllm` 表示事件线协议，实际实例标签与缓存后端仍为 xLLM。

Host KV 只属于持有它的实例，不能把同机其他实例的 Host 视为本实例的命中。
`max_model_len` 为 Motor 的上下文预算元数据，不作为 xLLM CLI 参数。
KV 开启时要求 prefix cache、相同 block_size，以及不使用 speculative draft pool。
移除 `conductor_service` 可以关闭自动 KV 事件与注册。
本例先用既有 `load_balance`；层级 KV 路由可再使用已有 `kv_cache_affinity`，
需要 Coordinator 与 xLLM 的 tokenizer、chat template 和 block hash 身份一致。

原生 xLLM 尚无 vLLM Prometheus 指标接口，Motor 跳过这类引擎抓取；
基础负载均衡使用 Motor 的请求负载账本，KV 目录来自 Conductor 事件。
NPU 利用率和引擎内部延迟等指标需要另行接入。
