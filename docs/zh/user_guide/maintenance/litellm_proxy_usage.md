# 使用 LiteLLM 代理对接 MindIE Motor

## 简介

LiteLLM 是一个开源的 LLM 网关与 Python SDK，提供统一的 OpenAI 兼容接口，可在 100 余个模型与服务商之间路由请求。其 Proxy（代理）模式可作为独立网关部署，对上游客户端同时暴露 OpenAI（`/v1/chat/completions`）、Anthropic（`/v1/messages`）等多种协议入口，并在内部完成请求/响应格式适配后转发给后端推理服务。

- 官方文档：https://docs.litellm.ai/
- 开源仓库：https://github.com/BerriAI/litellm
- 配置文件参考：https://docs.litellm.ai/docs/proxy/configs
- CLI 快速开始：https://docs.litellm.ai/docs/proxy/quick_start

## 适用场景

MindIE Motor 原生对外暴露 OpenAI 兼容接口（`/v1/chat/completions`、`/v1/completions`、`/v1/responses` 等），详见[业务接口](../api/service_interfaces.md)。Motor 的 Anthropic Messages（`/v1/messages`）接口采用透传机制（见 [Anthropic Messages 接口](../api/service_interfaces.md#anthropic-messages-接口)），是否可用取决于后端推理引擎及其版本。当后端推理引擎版本较低、未提供或未稳定支持 `/v1/messages` 时，使用 Anthropic 协议的上游客户端（如 Claude Code、Anthropic SDK、部分 Agent 框架）将无法直接接入 Motor。

>[!NOTE]说明
>该机制下，Coordinator 仅做轻量参数校验，不做 Anthropic↔OpenAI 协议翻译，按原始路径将请求转发至后端推理引擎，响应同样原样返回客户端。目前 vLLM 与 SGLang 均已在较新版本中提供该端点，但需关注以下版本条件：
>
>- **vLLM 版本**：于 **v0.11.1**（2025-11）由 [PR #22627](https://github.com/vllm-project/vllm/pull/22627) 引入 Anthropic Messages 端点，并在 [v0.11.1 版本说明](https://github.com/vllm-project/vllm/releases/tag/v0.11.1) 中正式公告；**此前版本不提供该接口**。早期实现（约 v0.11.1 ~ v0.14.x）还存在流式首包 `input_tokens` 缺失、工具调用参数流式、`stop_reason` 处理等协议兼容问题，后续版本逐步修复。
>- **vllm-ascend 版本**（MindIE Motor 所用，版本号与上游 vLLM 对齐）：**自 v0.12.0rc1 起**包含 `/v1/messages`，[v0.11.0 及更早版本不提供](https://github.com/vllm-project/vllm-ascend/releases/tag/v0.11.0)。
>- **SGLang 版本**：SGLang 自 2026-02 起（[PR #18630](https://github.com/sgl-project/sglang/pull/18630)）原生提供 `/v1/messages` 与 `/v1/messages/count_tokens` 端点，服务器启动后自动注册，无需额外参数启用；**此前版本不提供该接口**。
>- 若后端推理引擎已原生提供可用的 `/v1/messages`，客户端可直接调用其 [Anthropic Messages 接口](../api/service_interfaces.md#anthropic-messages-接口)，无需引入 LiteLLM；否则可使用下文的 LiteLLM 适配层。

此时可在 Motor 前部署 LiteLLM 作为协议适配网关，工作流程如下：

1. 上游客户端以 Anthropic Messages 协议（`POST /v1/messages`）向 LiteLLM 发起请求；
2. LiteLLM 解析 Anthropic 请求格式，将其转换为 OpenAI Chat Completions 格式；
3. LiteLLM 将转换后的请求转发至 Motor 的 `/v1/chat/completions` 接口；
4. Motor 返回 OpenAI 格式响应，LiteLLM 再将其逆向转换为 Anthropic Messages 响应格式（含 SSE 流式事件）返回给客户端。

因此，Anthropic 协议客户端无需修改请求格式，即可通过 LiteLLM 访问仅提供 OpenAI 兼容接口的 Motor 服务。实际兼容性还取决于 LiteLLM 版本和后端推理引擎能力，停止词和工具调用需分别按[停止词验证](#停止词验证)、[工具调用验证](#工具调用验证)确认；其他专有字段的处理见[常见问题](#后端推理引擎不支持某些参数导致报错)。

### Claude Code 等 Agent 场景

LiteLLM 只负责 Anthropic 与 OpenAI 协议之间的转换，不提供工具调用能力。后端推理引擎必须支持工具调用并正确配置工具解析器，否则 Claude Code 等 Agent 客户端只能完成普通对话，无法真正触发工具调用。

- **vLLM**：启动时需开启自动工具调用，并按模型选择解析器，例如 `--enable-auto-tool-choice --tool-call-parser hermes`。解析器需与模型匹配（Qwen 系列常用 `hermes`，DeepSeek 系列常用 `deepseek_v3`，具体以模型说明为准），参考 [vLLM 自动函数调用说明](https://docs.vllm.ai/en/v0.11.0/features/tool_calling.html#automatic-function-calling)。
- **接入前验证**：建议先按[工具调用验证](#工具调用验证)确认非流式、流式工具调用与工具结果回传均正常，再接入 Claude Code。
- **Claude Code 接入示例**：在客户端设置以下环境变量后执行 `claude` 启动 Claude Code。`ANTHROPIC_API_KEY` 在 LiteLLM 未启用鉴权时可填任意非空值，启用鉴权时填 master key（见[配置说明](#配置说明)）。

    ```bash
    export ANTHROPIC_BASE_URL="http://127.0.0.1:<LITELLM_PORT>"
    export ANTHROPIC_API_KEY="<LITELLM_MASTER_KEY_OR_ANY>"
    export ANTHROPIC_MODEL="<MODEL_NAME>"
    ```

## 环境要求

- Python 3.10 ~ 3.15（LiteLLM 1.109.0 要求 Python ≥ 3.10 且 < 3.16；本示例使用 Python 3.11，对应离线包名中的 `py311`）
- 目标服务器为 ARM64（aarch64）Linux，LiteLLM 1.109.0 的 `manylinux_2_28` 预编译 wheel 要求 glibc ≥ 2.28；若目标系统的 glibc 或发行版版本不满足该约束，需在与目标一致的镜像中重新下载或补充下载依赖。
- 已部署并可访问的 MindIE Motor 服务，对外暴露 OpenAI 兼容接口（参考[业务接口的IP/端口与配置](../api/README.md#业务接口的ip端口与配置)）
- pip 可用的 Python 环境

## 在线环境安装与使用

部署机器可访问公网（PyPI）时，按以下步骤在线安装。

1. 安装 LiteLLM Proxy。

    ```bash
    pip install 'litellm[proxy]==1.109.0'
    ```

    >[!NOTE]说明
    > LiteLLM `>=1.80.5,<1.83.7` 的 `/prompts/test` 接口存在模板注入漏洞（[GHSA-xqmj-j6mv-4862 / CVE-2026-42203](https://github.com/BerriAI/litellm/security/advisories/GHSA-xqmj-j6mv-4862)），请勿使用该区间版本。本文以 `1.109.0` 为例，该版本已修复 1.84.0 存在的跨请求流式事件串扰，并会将 Anthropic `stop_sequences` 转换为 OpenAI `stop`；安装后请通过 `litellm --version` 确认实际版本。

2. 编写配置文件 `config.yaml`。

    ```yaml
    model_list:
      - model_name: "*"
        litellm_params:
          model: hosted_vllm/<MODEL_NAME>
          api_base: http://<MOTOR_HOST>:<MOTOR_PORT>/v1
          api_key: "none"
          drop_params: true
    ```

    各字段含义见[配置说明](#配置说明)。

3. 启动 LiteLLM 代理。

    ```bash
    litellm --config config.yaml --port <LITELLM_PORT>
    ```

    启动后 LiteLLM 监听 `<LITELLM_PORT>` 端口，对外提供 `/v1/messages`、`/v1/chat/completions` 等接口。验证与调用方式见[调用验证](#调用验证)。

## 离线环境安装与使用

### 准备离线安装包

在可访问公网的下载机上执行以下步骤，下载 LiteLLM 及其全部依赖，打包后传输到离线环境。本节命令以 x86_64 Linux 下载机、aarch64 Linux + Python 3.11 目标环境为例；实际请以下载机和目标系统的架构、操作系统及 Python 版本为准，相应调整目录名和 `--platform`、`--python-version` 等参数。

1. 在联网的下载机上执行：

    ```bash
    # 1. 创建存放安装包的文件夹
    mkdir litellm_offline_packages_py311_11090

    # 2. 下载适配环境的 litellm[proxy] 及所有依赖
    pip download 'litellm[proxy]==1.109.0' \
      -d ./litellm_offline_packages_py311_11090 \
      --platform manylinux_2_28_aarch64 \
      --platform manylinux2014_aarch64 \
      --python-version 311 \
      --only-binary=:all: \
      --trusted-host mirrors.aliyun.com \
      -i https://mirrors.aliyun.com/pypi/simple/
    ```

2. 打包为 `tar.gz`。

    ```bash
    tar -czvf litellm_offline_packages_py311_11090.tar.gz litellm_offline_packages_py311_11090
    ```

3. 将 `litellm_offline_packages_py311_11090.tar.gz`、`config.yaml`、`start.sh`（见下文）一并传输到离线部署机器的 `/xxx/litellm/` 目录下。

>[!NOTE]说明
>
> - `--platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 --python-version 311 --only-binary=:all:` 需同时指定：1.109.0 自身是 manylinux_2_28 aarch64 wheel，而 `fastuuid` 等依赖只提供 manylinux2014/manylinux_2_17 aarch64 wheel；只指定 manylinux_2_28 时 pip 不会接受这些旧标签，离线包准备会中断。增加 manylinux2014 仅用于匹配依赖的 wheel 标签，目标系统仍需满足[环境要求](#环境要求)中的 glibc ≥ 2.28。示例目标为 Python 3.11；若下载机即为 aarch64 架构且 Python 版本与目标环境一致，可省略这些参数。
> - `--platform` 只决定下载哪个目标平台的 wheel；在 Windows 上下载时，`uvloop`、`pyroscope-io` 等仅在 Linux 安装的依赖会被跳过，LiteLLM 在部分 Python 版本下还可能因 `mcp` 的 `pywin32` 依赖而下载失败。若使用非 Linux 下载机，需确认离线包中已包含目标系统所需的依赖（或目标环境已预装）。
> - `pip download` 必须带 `[proxy]` extra，否则会遗漏 Proxy 运行所需依赖。

### 离线安装与启动

在离线机器上执行如下 `start.sh`，完成解压、安装与启动：

```bash
echo "start litellm"
cd /xxx/litellm
tar -zxvf litellm_offline_packages_py311_11090.tar.gz

cd litellm_offline_packages_py311_11090
pip install 'litellm[proxy]==1.109.0' \
  --no-index \
  --find-links ./ \
  --no-cache-dir

cd /xxx/litellm
litellm --config config.yaml --port <LITELLM_PORT>
```

>[!NOTE]说明
>
> - `--no-index --find-links ./` 强制 pip 只从本地目录解析依赖，不访问任何在线源。

## 配置说明

`config.yaml` 示例及字段含义如下：

```yaml
model_list:
  - model_name: "*"
    litellm_params:
      model: hosted_vllm/<MODEL_NAME>
      api_base: http://<MOTOR_HOST>:<MOTOR_PORT>/v1
      api_key: "none"
      drop_params: true
```

| 字段 | 说明 |
|---|---|
| `model_list` | LiteLLM 路由模型列表。 |
| `model_name` | 对客户端暴露的模型别名。设为 `"*"` 表示通配，客户端传入任意模型名均按该条目转发。 |
| `litellm_params.model` | 后端模型标识。`hosted_vllm/` 前缀声明后端为 hosted vLLM（OpenAI 兼容）服务端点，LiteLLM 据此以 OpenAI 协议（`/v1/chat/completions`）调用 `api_base`；`<MODEL_NAME>` 为实际部署的模型名称（替换为后端服务真实模型名，如 `deepseek`、`qwen3` 等），需与 Motor `user_config.json` 中配置的 `served_model_name` 一致。 |
| `litellm_params.api_base` | Motor 服务的 OpenAI 兼容接口地址，格式 `http://<MOTOR_HOST>:<MOTOR_PORT>/v1`，参考[业务接口的IP/端口与配置](../api/README.md#业务接口的ip端口与配置)。 |
| `litellm_params.api_key` | 用于访问 Motor 的 API Key。Motor 未开启鉴权时填 `"none"`。 |
| `litellm_params.drop_params` | 设为 `true` 时，LiteLLM 依据参数支持表过滤请求字段；该表不代表后端推理引擎版本的实际支持情况，若仍报参数错误，需显式移除对应字段（见[常见问题](#后端推理引擎不支持某些参数导致报错)）。 |

>[!NOTE]说明
> LiteLLM Proxy 的鉴权由 master key 控制：当 `config.yaml` 的 `general_settings.master_key` 或环境变量 `LITELLM_MASTER_KEY` 任一存在时启用鉴权，否则不鉴权。未启用鉴权的部署仅建议用于测试环境，并限制服务端口只对可信网络开放。启用鉴权后，Anthropic 协议客户端需携带该 Key（如 `x-api-key: <MASTER_KEY>` 或 `Authorization: Bearer <MASTER_KEY>`），OpenAI 协议客户端使用 `Authorization: Bearer <MASTER_KEY>`。
>
> 若 Motor 端启用了 API Key 鉴权（`api_key_config.enable_api_key=true`），需将 `api_key` 改为实际 Key 值，并按 Motor 的 `header_name`/`key_prefix` 配置调整请求头，参考[安全、认证与限流](../api/README.md#安全认证与限流)。

## 调用验证

LiteLLM 启动后，可使用 Anthropic 协议直接调用其 `/v1/messages` 接口，LiteLLM 会自动适配并转发至 Motor。

- 非流式调用：

    ```bash
    curl -X POST "http://127.0.0.1:<LITELLM_PORT>/v1/messages" \
      -H "Content-Type: application/json" \
      -H "x-api-key: any" \
      -d '{
        "model": "<MODEL_NAME>",
        "messages": [
          { "role": "user", "content": "你好" }
        ],
        "max_tokens": 128
      }'
    ```

- 流式调用：

    ```bash
    curl -N -X POST "http://127.0.0.1:<LITELLM_PORT>/v1/messages" \
      -H "Content-Type: application/json" \
      -H "x-api-key: any" \
      -d '{
        "model": "<MODEL_NAME>",
        "messages": [
          { "role": "user", "content": "你好" }
        ],
        "max_tokens": 128,
        "stream": true
      }'
    ```

>[!NOTE]说明
> 由于 `config.yaml` 中 `model_name` 设为通配 `"*"`，客户端传入任意模型名均会路由到 `hosted_vllm/<MODEL_NAME>`。

返回为 Anthropic Messages 响应格式（`type: message`、`content` 数组、`usage` 字段等），结构详见 [Anthropic Messages 接口响应样例](../api/service_interfaces.md#anthropic-messages-接口)。

### 停止词验证

LiteLLM 会将 Anthropic `stop_sequences` 转换为 OpenAI `stop`，后端推理引擎需支持 `stop`。以下示例验证停止词是否生效：

```bash
curl -X POST "http://127.0.0.1:<LITELLM_PORT>/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: any" \
  -d '{
    "model": "<MODEL_NAME>",
    "max_tokens": 128,
    "messages": [
      { "role": "user", "content": "请连续输出 A、B、C，然后输出 END。" }
    ],
    "stop_sequences": ["END"]
  }'
```

停止词生效时，响应内容应在 `END` 之前结束。需要注意，LiteLLM 当前将 OpenAI 的 `stop` 统一映射为 Anthropic `end_turn`，不会回填命中的具体停止词，因此 `stop_reason` 通常为 `end_turn`、`stop_sequence` 为 `null`；若客户端依赖 `stop_sequence` 字段，需按响应内容自行判断或直接使用后端推理引擎原生的 Anthropic 接口。若模型仍输出 `END`，请确认 LiteLLM 版本并检查后端推理引擎是否支持 OpenAI `stop` 参数。

### 工具调用验证

Claude Code 等 Agent 场景需额外验证工具调用，以下示例使用 Anthropic 格式的 `tools` 字段。

1. 非流式工具调用：

    ```bash
    curl -X POST "http://127.0.0.1:<LITELLM_PORT>/v1/messages" \
      -H "Content-Type: application/json" \
      -H "x-api-key: any" \
      -d '{
        "model": "<MODEL_NAME>",
        "max_tokens": 256,
        "messages": [
          { "role": "user", "content": "北京今天天气如何？请调用工具查询。" }
        ],
        "tools": [
          {
            "name": "get_weather",
            "description": "查询指定城市的天气",
            "input_schema": {
              "type": "object",
              "properties": {
                "city": { "type": "string" }
              },
              "required": ["city"]
            }
          }
        ]
      }'
    ```

    预期响应中 `stop_reason` 为 `tool_use`，`content` 中包含 `type: tool_use` 的内容块，`input` 为合法 JSON；若只返回普通文本或报错，请检查后端推理引擎的工具调用配置（见 [Claude Code 等 Agent 场景](#claude-code-等-agent-场景)）。

2. 工具结果回传：将上一步的 `tool_use` 块和工具执行结果 `tool_result` 拼入 `messages` 再请求一次，预期返回正常文本且 `stop_reason` 为 `end_turn`，用于验证工具结果能被正确回传给后端推理引擎。

    ```bash
    curl -X POST "http://127.0.0.1:<LITELLM_PORT>/v1/messages" \
      -H "Content-Type: application/json" \
      -H "x-api-key: any" \
      -d '{
        "model": "<MODEL_NAME>",
        "max_tokens": 256,
        "messages": [
          { "role": "user", "content": "北京今天天气如何？请调用工具查询。" },
          { "role": "assistant", "content": [
            { "type": "tool_use", "id": "<TOOL_USE_ID>", "name": "get_weather", "input": { "city": "北京" } }
          ]},
          { "role": "user", "content": [
            { "type": "tool_result", "tool_use_id": "<TOOL_USE_ID>", "content": "晴，25℃" }
          ]}
        ]
      }'
    ```

3. 流式工具调用：在非流式工具调用请求体中增加 `"stream": true` 并用 `curl -N` 接收 SSE，预期事件流中出现 `content_block_start`（`content_block.type` 为 `tool_use`）与 `input_json_delta`，最终 `message_delta` 的 `stop_reason` 为 `tool_use`。

也可通过 Anthropic SDK 直接调用 LiteLLM。先在客户端安装 SDK：

```bash
pip install anthropic
```

再执行以下 Python 示例：

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:<LITELLM_PORT>",
    api_key="any",
)
msg = client.messages.create(
    model="<MODEL_NAME>",
    max_tokens=128,
    messages=[{"role": "user", "content": "你好"}],
)
print(msg.content)
```

## 常见问题

### 调用 `/v1/messages` 返回 404 或 401

- 返回 404：确认安装的是 `litellm[proxy]`（带 `[proxy]` extra），而非仅 `litellm`；Proxy 路由由该 extra 提供。
- 返回 401：LiteLLM 在配置了 master key（`general_settings.master_key` 或 `LITELLM_MASTER_KEY`）时启用鉴权，请求需携带该 Key（传递方式见[配置说明](#配置说明)）。测试环境如需免鉴权，启动前执行 `unset LITELLM_MASTER_KEY` 并移除配置中的 `master_key`。

### 转发至 Motor 报错连接拒绝

- 检查 `api_base` 中的 IP 与端口是否指向 Motor 推理业务接口，端口取值参考[业务接口的IP/端口与配置](../api/README.md#业务接口的ip端口与配置)。
- 确认 `config.yaml` 中 `<MOTOR_HOST>`、`<MOTOR_PORT>`、`<MODEL_NAME>` 等占位符均已替换为实际部署值后再启动。

### 离线安装时部分依赖找不到

- 确认 `pip download` 在 Linux 下载机执行；若在非 Linux 下载机执行，需核对 `uvloop`、`pyroscope-io` 等 Linux 专属依赖是否已包含在离线包中（原因见[准备离线安装包](#准备离线安装包)）。
- 若个别依赖在 aarch64 无预编译 wheel，`--only-binary=:all:` 会报错并指出缺失包名，可尝试降低该依赖版本或在目标架构机上补充下载后合并至同一目录。

### 后端推理引擎不支持某些参数导致报错

后端推理引擎版本较旧时，可能不认识 LiteLLM 转换后的字段（例如 `reasoning_effort`），而 `drop_params` 依据 LiteLLM 的静态参数支持表，无法识别这种版本差异。

- `drop_params: true` 只能按该表过滤，仍可能将字段透传给后端推理引擎后报错；`litellm_settings.drop_params` 只是对未单独配置的模型套用同一规则，无法覆盖所有后端推理引擎版本差异。
- 若已确认某个字段导致报错，可从客户端请求中删除，或在对应模型的 `litellm_params` 中通过 `additional_drop_params` 指定协议转换后的字段名：

    ```yaml
    litellm_params:
      additional_drop_params: ["reasoning_effort", "top_k"]
    ```

    Anthropic 的 `thinking` 会先转换为 OpenAI 的 `reasoning_effort`，因此要丢弃思考参数时应填写 `reasoning_effort`，填写 `thinking` 不会生效。

### Claude Code 工具调用不生效

- 确认后端推理引擎已启用自动工具调用并配置了与模型匹配的解析器（配置方法见 [Claude Code 等 Agent 场景](#claude-code-等-agent-场景)）。
- 先按[工具调用验证](#工具调用验证)用 `curl` 验证非流式、流式工具调用与工具结果回传；若普通对话正常但 `stop_reason` 始终不是 `tool_use`，多为后端推理引擎未启用工具调用或解析器与模型不匹配。
