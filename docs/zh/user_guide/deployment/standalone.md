# Coordinator 裸机独立部署

在物理机 / 虚拟机（无 K8s）上单独部署 Motor Coordinator，对接已存在的原生 vLLM Prefill / Decode 引擎，支持多 P 多 D。Coordinator 是纯 CPU 服务，不依赖 NPU、Controller、Node Manager。

实例注册（安装 motor 后即可用）：`python3 -m motor.coordinator.register`。对外监听时可选编写配置，见第二节。

日常增删查见 [附录 C](#附录-c实例增删查)。KV Cache 亲和为可选增强，见 [附录 B](#附录-bkv-cache-亲和可选)。

## 部署总流程

1. 获取代码并安装依赖 + motor
2. （可选）编写 `coordinator.json`
3. 拉起 Coordinator
4. 注册 P/D 实例
5. 端到端验证

---

## 零、前置条件

| 项 | 要求 |
|----|------|
| OS | Linux（aarch64 / x86_64） |
| Python | `>= 3.11` |
| 网络 | 可达各 P/D 引擎 HTTP 端口（常见 `8000` / `10000`） |
| 端口 | 本机 `1025`（推理）/ `1026`（管理）/ `1027`（观测）空闲 |

---

## 一、安装

```bash
git clone https://gitcode.com/Ascend/MindIE-Motor.git
cd MindIE-Motor
git checkout master

python3 -m venv /opt/motor-coord/venv
source /opt/motor-coord/venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 安装 motor（三选一）
# A. 源码可编辑：
pip install --no-deps -e .
# B. 自打包 whl（无 cargo / 不需要 KV 亲和时可：SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh）：
bash build.sh
pip install --no-deps --force-reinstall dist/motor-*.whl
# C. 现成 whl：
# pip install --no-deps --force-reinstall /path/to/motor-*.whl
```

`--no-deps`：whl 不声明依赖，须先装 `requirements.txt`。离线场景见 [附录 A](#附录-a离线安装)。

---

## 二、配置文件（可选）

主流程默认在 Coordinator 所在机器完成启动、注册和验证，无需配置文件。Coordinator 默认监听 `127.0.0.1`，后续命令可直接复制执行。

需要跨主机访问，或使用 Docker / Kubernetes 部署时，按 [附录 D](#附录-d监听地址) 配置监听地址。管理端口 `1026` 对本机以外开放时，生产环境还应按 [附录 E](#附录-e管理面-api-key) 开启独立 API Key。

---

## 三、拉起 Coordinator

```bash
source /opt/motor-coord/venv/bin/activate
export MOTOR_LOG_PATH=/opt/motor-coord/logs
mkdir -p "$MOTOR_LOG_PATH"
python3 -m motor.coordinator.main
```

```bash
curl http://127.0.0.1:1026/liveness
curl http://127.0.0.1:1026/readiness   # 此时 instance_count=0 属正常
```

---

## 四、注册 P/D 实例

各引擎已 `vllm serve` 且可达。一个 `--prefill` / `--decode` = 一个实例；同一实例的多个 DP endpoint 用逗号连接（单 DP 只写一个 `IP:PORT`）。首次部署用默认 `set` 覆盖全表；之后增删查见 [附录 C](#附录-c实例增删查)。

```bash
python3 -m motor.coordinator.register \
    --prefill "10.10.0.11:8000,10.10.0.11:8001" \
    --prefill "10.10.0.13:8000,10.10.0.13:8001" \
    --decode  "10.10.0.12:8000,10.10.0.12:8001" \
    --decode  "10.10.0.15:8000,10.10.0.15:8001"
```

```bash
curl http://127.0.0.1:1026/instances   # 列出已登记实例
curl http://127.0.0.1:1026/readiness   # 调度是否就绪（需同时有可用 P 和 D）
```

---

## 五、端到端验证

```bash
curl http://127.0.0.1:1025/v1/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"Qwen3-8B","prompt":"Hello, how are you?","max_tokens":16}'
```

期望 HTTP 200。`model` 填引擎实际对外的模型名。

---

## 六、常见问题

| 现象 | 处理 |
|------|------|
| `ImportError`（uvicorn / zmq 等） | 先装 `requirements.txt`，再 `--no-deps` 装 motor |
| 外部 / Docker 映射 curl 不通 1025 | 配置 `coordinator_api_host` 为 `0.0.0.0` |
| 注册脚本报 not live | 先拉起 Coordinator 再注册 |
| 注册后推理 503（Service is not available） | 核对引擎 `IP:PORT`；`--dry-run` 看 payload |
| 日志目录不对 | 设 `MOTOR_LOG_PATH` |

---

## 附录 A：离线安装

```bash
# 有源机：
#   pip download -r requirements.txt -d /tmp/offline_pkgs \
#       --python-version 3.11 --platform manylinux2014_aarch64 --only-binary=:all:
# 目标机：
#   pip install /tmp/offline_pkgs/*.whl
#   pip install --no-deps --force-reinstall /path/to/motor-*.whl
```

含二进制扩展的包须匹配目标机架构与 Python 版本。

---

## 附录 B：KV Cache 亲和（可选）

仅 `scheduler_type=kv_cache_affinity` 时需要。完整字段见 [KV Cache 亲和](../features/kvcache_affinity.md)。

```json
{
  "motor_coordinator_config": {
    "api_config": { "coordinator_api_host": "0.0.0.0" },
    "scheduler_config": { "scheduler_type": "kv_cache_affinity" }
  },
  "kv_conductor_config": {
    "conductor_service": "<KV_CONDUCTOR_IP>",
    "http_server_port": 13333,
    "npu_endpoint": "tcp://*:5557",
    "replay_endpoint": "tcp://*:6667",
    "engine_type": "vLLM",
    "model_path": "/mnt/weight/Qwen3-8B",
    "block_size": 128,
    "re_register_interval_sec": 30
  }
}
```

`npu_endpoint` 与 P 引擎 `--kv-events-config` 一致；`block_size` 与引擎 `--block-size` 一致（默认 128），否则命中率恒为 0。

```bash
source /opt/motor-coord/venv/bin/activate
python3 -c "from motor.kv_conductor import get_binary_path; p=get_binary_path(); print(p); assert p and p.is_file()"
export RUST_LOG=info,kv_conductor=debug
export MOTOR_LOG_PATH=/opt/motor-coord/logs
nohup python3 -m motor.kv_conductor --host 0.0.0.0 --port 13333 &
```

改 `conductor_service` 后重启 Coordinator。同一长前缀发两次请求，热请求日志 `matched` 应大于 0。

---

## 附录 C：实例增删查

`set`（默认）替换整张实例表；`add` / `del` 按同样的 `IP:PORT` 增量；`list` 查询当前登记。实例 `id` 由 `role` + 排序后的完整 endpoint 组派生，endpoint 输入顺序不影响注册结果，增删不必另记数字 id。

如果开启了管理面 API Key，以下所有 `register` 命令都需追加
`--mgmt-api-key-file /opt/motor-coord/secrets/mgmt-api-key`，见 [附录 E](#附录-e管理面-api-key)。

### 查

```bash
python3 -m motor.coordinator.register list
# 等价：curl http://127.0.0.1:1026/instances
```

返回 `count` 与 `instances[]`（`id` / `role` / `job_name` / `model_name` / `status` / `endpoints`）。完整字段见 [实例查询接口](../api/management_interfaces.md#实例查询接口)。

### 增

```bash
python3 -m motor.coordinator.register add --prefill "10.10.0.17:8000,10.10.0.17:8001"
python3 -m motor.coordinator.register add --decode  "10.10.0.18:8000,10.10.0.18:8001"
```

单 DP 省略逗号即可：`--prefill 10.10.0.17:8000`。

### 删

```bash
# 与 add 时相同的 endpoint 组（顺序可不同，删除前会查询实际注册 ID）
python3 -m motor.coordinator.register del --prefill "10.10.0.17:8000,10.10.0.17:8001"

# 或先 list 再按 id
python3 -m motor.coordinator.register del --id 1234
```

### 其它常用参数

```bash
# 只打印将提交的 JSON，不真正注册
python3 -m motor.coordinator.register add --prefill "10.10.0.17:8000,10.10.0.17:8001" --dry-run

# 引擎 /v1/models 不可达或有多个模型时显式指定
python3 -m motor.coordinator.register add --prefill "10.10.0.17:8000,10.10.0.17:8001" \
    --model-name Qwen3-8B --no-health-check

# 远程 Coordinator（默认 http://127.0.0.1:1026）
python3 -m motor.coordinator.register list --coordinator http://10.10.0.1
python3 -m motor.coordinator.register --help
```

---

## 附录 D：监听地址

`coordinator_api_host` 同时作用于推理端口 `1025`、管理端口 `1026` 和观测端口 `1027`。

### 裸机 / 虚拟机跨主机访问

`coordinator_api_host` 是 Coordinator 所在机器的本地监听地址，不是 P/D 引擎或客户端地址。建议绑定 Coordinator 的业务网卡 IP：

```json
{
  "motor_coordinator_config": {
    "api_config": {
      "coordinator_api_host": "10.10.0.20"
    }
  }
}
```

将 `10.10.0.20` 替换为实际业务网卡 IP，并在启动 Coordinator 前指定配置文件：

```bash
export USER_CONFIG_PATH=/opt/motor-coord/coordinator.json
```

远程注册时通过 `--coordinator` 指定该地址。管理端口默认使用 `1026`，无需写入 `--coordinator`：

```bash
python3 -m motor.coordinator.register list --coordinator http://10.10.0.20
```

### Docker

配置为 `0.0.0.0`，让服务监听容器的所有网卡；不要查询或固定容器 IP，容器 IP 可能变化。客户端通过宿主机端口映射地址或容器网络中的服务名访问。

```json
{
  "motor_coordinator_config": {
    "api_config": {
      "coordinator_api_host": "0.0.0.0"
    }
  }
}
```

### Kubernetes

Coordinator 代码不会主动查询 Pod IP，只会读取 `POD_IP` 环境变量。Pod YAML 应通过 Downward API 注入：

```yaml
env:
  - name: POD_IP
    valueFrom:
      fieldRef:
        fieldPath: status.podIP
```

仓库的 Coordinator 部署模板已包含该配置。注入后 Coordinator 默认绑定 Pod IP，客户端通常通过 Service DNS 访问。自定义 YAML 如果未注入 `POD_IP`，也未配置 `coordinator_api_host`，将回退到 `127.0.0.1`，Service 无法访问。

---

## 附录 E：管理面 API Key

默认不开启管理面 API Key，不影响本机启动、注册和推理。管理端口 `1026` 可被本机以外的主体访问时，生产环境应开启独立 API Key。

先生成密钥文件：

```bash
# 创建仅当前用户可访问的密钥目录
install -d -m 700 /opt/motor-coord/secrets

# 生成高强度随机 API Key 并写入文件
python3 -c "import secrets; print(secrets.token_urlsafe(32))" \
    > /opt/motor-coord/secrets/mgmt-api-key

# 密钥文件仅允许属主读写
chmod 600 /opt/motor-coord/secrets/mgmt-api-key
```

执行命令的用户应与 Coordinator 运行用户一致；否则需调整文件属主，确保服务可以读取。然后在 `coordinator.json` 中增加 `mgmt_api_key_config`：

```json
{
  "motor_coordinator_config": {
    "api_config": {
      "coordinator_api_host": "10.10.0.20"
    },
    "mgmt_api_key_config": {
      "enable_api_key": true,
      "api_key_file": "/opt/motor-coord/secrets/mgmt-api-key"
    }
  }
}
```

启动 Coordinator 前指定配置文件：

```bash
export USER_CONFIG_PATH=/opt/motor-coord/coordinator.json
```

开启后，所有 `register` 命令都需携带同一个密钥文件：

```bash
python3 -m motor.coordinator.register list \
    --coordinator http://10.10.0.20 \
    --mgmt-api-key-file /opt/motor-coord/secrets/mgmt-api-key
```

直接调用受保护的管理接口时，请求头为 `X-Motor-Management-Key`：

```bash
curl http://10.10.0.20:1026/instances \
    -H "X-Motor-Management-Key: $(< /opt/motor-coord/secrets/mgmt-api-key)"
```

管理 API Key 保护实例查询、实例刷新和精度告警状态清理接口；启动、存活和就绪探针不鉴权。API Key 只做身份校验，不加密流量。跨主机访问管理端口时，还应配置 `mgmt_tls_config` 或由受信代理终止 TLS，并通过防火墙、容器网络或 NetworkPolicy 限制访问范围；不要把 `1026` 直接暴露到公网。
