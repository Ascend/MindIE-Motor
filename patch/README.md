# MindIE Motor补丁

## 补丁应用场景

**为解决MindIE Motor仓与开源社区代码依赖的协同问题，对开源社区的代码修改使用补丁方式实现**。

## 补丁范围

| 补丁包名       | 补丁修复场景              |
| ------------ | ----------------- |
| **vllm_multi_connector.patch** | 修复vllm代码中layerwise叠加multi_connector方式无法推理问题 |
| **vllm_anthropic_serving.patch** | 修复vllm代码中Anthropic Messages serving在pyMotor PD分离部署下的协同问题（面向 vLLM releases/v0.23.0）：①`AnthropicMessagesRequest`补充`request_id`字段并透传（缺失时trigger/metaserver模式下prefill KV注册到随机id，decode侧永久挂起；`kv_transfer_params`上游0.23.0已自带，不再补丁）；②usage补充`cache_read_input_tokens`/`cache_creation_input_tokens`（映射`prompt_tokens_details.cached_tokens`，`input_tokens`非负保底）；③`messages_full_converter`的tool_call参数`json.loads`容错（短生成如prefill回放`max_tokens=1`时残缺参数导致JSONDecodeError）；④响应含tool_use块时`stop_reason`修正为`tool_use`（tool parser返回`finish_reason="stop"`时避免客户端退出工具循环） |

## 操作步骤

执行以下命令，将补丁应用到代码中。

```bash
python patch_apply.py
```

>说明: 拉起服务中指定的镜像需要提前完成了打补丁操作。
