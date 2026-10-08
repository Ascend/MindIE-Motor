# Union 单容器模式强制读取 vLLM DP RPC 配置

## Symptom
xLLM union 的 `SingleContainerNodemanagerConfig.from_json` 在配置没有
`dp_rpc_port` 时抛出 KeyError，NodeManager 尚未启动引擎就退出。

## Root cause
union 分支直接索引 ConfigResolver 返回的 `dp_rpc_port`，将 vLLM 的
数据并行控制端口当成所有引擎的必填字段。xLLM 通过原生 `master_node_addr`
管理 RPC，不提供这个字段。

## Why
并行拓扑元数据与某个引擎的控制协议配置混用；没有测试新引擎的完整
single-container 配置解析入口。

## Fix
将 union DP RPC 偏移设为可选：有配置则保留原有端口加 INDEX 的行为，
无配置则保持 None，让引擎 backend 自己处理 RPC 地址。

## Test interception
`test_single_container_xllm_has_independent_devices_without_vllm_rpc_config`
在 ROLE=union、INDEX=1 下验证独立设备和端口偏移，且 DP RPC 为 None。
旧代码在同一输入上抛出 KeyError，新代码通过。

## Scenario
无需 Kubernetes 的 single_container、原生 xLLM union 多副本。

## Keywords
xLLM, union, single_container, ConfigResolver, dp_rpc_port, KeyError
