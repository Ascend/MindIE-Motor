# PD 分离（特性说明）

**Prefill / Decode 分离**指将预填充与解码阶段调度到不同实例协同完成。部署配置与 KV 传输参数见 [PD 分离服务部署](../user_guide/deployment/k8s/pd_disaggregation_deployment.md)。

## 自动识别拓扑

Coordinator 不再读取 `motor_coordinator_config.scheduler_config.deploy_mode`。请求进入 `motor/coordinator/router/dispatch.py` 后，根据当前可用实例角色自动选择 Router：

| 可用角色 | Router |
|----------|--------|
| 同时存在 `prefill` 和 `decode` | `UnifiedPDRouter` |
| 存在 `union` | `PDHybridRouter` |
| 仅存在 `prefill` | `PDHybridRouter`，用于 PD 降级 |
| 其他组合 | 返回 503 |

`motor_deploy_config.deploy_mode` 仍然保留，只用于选择 `infer_service_set`、`multi_deployment` 或 `single_container` 等部署形态，不参与推理行为选择。

## Connector 驱动执行计划

P/D 实例的协同行为由引擎 Connector 推导出的 `dispatch_capabilities` 决定：

| Connector 能力 | Dispatch plan |
|----------------|---------------|
| `concurrent_engine_sync` | P/D 并发执行，由引擎同步 KV |
| `prefill_handoff_decode` | Prefill 完成后将结果交给 Decode |

NodeManager 会从 vLLM 的 `kv_transfer_config.kv_connector` 或显式 `dispatch_profile` 推导 capability；SGLang 自动上报 `concurrent_engine_sync`。Coordinator 只选择 P/D 两端共同支持的能力，不再通过 `pd_separate`、`cpcd_separate` 等人工模式名称猜测行为。

### vLLM Connector 识别白名单

vLLM 引擎按 `kv_connector` 名称（大小写不敏感）推导 capability，目前内置识别下表中的连接器：

| `kv_connector` | 推导出的 capability |
|----------------|---------------------|
| `MooncakeConnectorV1` | `prefill_handoff_decode` |
| `MooncakeHybridConnector` | `prefill_handoff_decode` |
| `NixlConnector` | `prefill_handoff_decode` |
| `MooncakeLayerwiseConnector` | `concurrent_engine_sync` |
| `MultiConnector` | 取 `kv_connector_extra_config.connectors[0]`（传输连接器，要求至少 2 个）递归判定 |

- **`MultiConnector` 只看 `connectors[0]`（传输层）**。KV 池/存储类连接器（如 `AscendStoreConnector`、`MooncakeConnectorStoreV1`、`UCMConnector`、`LMCacheAscendConnector`）一般作为 `connectors[1]` 的后端使用，不参与 capability 判定，因此**无需**出现在白名单中。
- 不在上表内、且 `connectors[0]` 也无法识别的连接器会被判为 `unknown`，**不产生任何 capability**。

> ⚠️ **fail-closed**：当 P/D 两端没有共同 capability 时（例如顶层 `kv_connector` 或 `connectors[0]` 用了未识别的连接器），Coordinator 不会强行配对——`select_pair_and_allocate` 返回空、就绪判定为 `UNKNOWN`、路由返回 503。这是有意的保护，避免把不兼容的 P/D 配在一起、直到 KV 传输阶段才失败。

`dispatch_capabilities` 是 NodeManager 向 Coordinator 上报的内部字段，不支持在用户配置中显式填写。若需让**未被识别的连接器**作为 P/D 传输使用，请在 `motor_engine_prefill_config` / `motor_engine_decode_config` **顶层**（与 `engine_type` 同级，**不是** `engine_config` 内部）显式声明 `dispatch_profile` 作为逃生口：

| `dispatch_profile` | 推导出的 capability | 协同行为 |
|--------------------|---------------------|----------|
| `handoff` | `prefill_handoff_decode` | Prefill 完成后将结果交给 Decode |
| `trigger` | `concurrent_engine_sync` | P/D 并发执行，由引擎同步 KV |

**配置示例**（自定义 connector 不在白名单内时）：

```json
"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "dispatch_profile": "handoff",
  "engine_config": {
    "kv_transfer_config": {
      "kv_connector": "YourCustomConnector",
      "kv_role": "kv_producer"
    }
  }
},
"motor_engine_decode_config": {
  "engine_type": "vllm",
  "dispatch_profile": "handoff",
  "engine_config": {
    "kv_transfer_config": {
      "kv_connector": "YourCustomConnector",
      "kv_role": "kv_consumer"
    }
  }
}
```

> Prefill 与 Decode **两端 `dispatch_profile` 必须一致**，且取值须与 connector 实际协同语义匹配，否则 Coordinator 仍无法配对（503）。字段说明见 [user_config 全量参数说明](../user_guide/configuration/config_reference.md#dispatch_profile)。

## 数据流

```mermaid
flowchart LR
    Client[Client] --> Coord[Coordinator]
    Coord --> Roles[Inspect instance roles]
    Roles -->|P + D| Unified[UnifiedPDRouter]
    Roles -->|Union or P only| Hybrid[PDHybridRouter]
    Unified --> Capability[Select shared connector capability]
    Capability --> EngineP[Prefill instance]
    Capability --> EngineD[Decode instance]
    Hybrid --> EngineU[Union or fallback Prefill instance]
```
