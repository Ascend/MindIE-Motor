# 说明

本文描述 **Coordinator 推理面**对外提供的 HTTP 路径及其在代码中的入口；推理端口、管理端口、TLS、API Key 与限流等横切配置见 [接口说明](../../api_reference/interface_description.md)。

## 推理应用注册的路径

`motor/coordinator/api_server/inference_server.py` 中 `InferenceServer._register_routes` 在推理用 FastAPI 应用上注册：

| 方法 | 路径 | 行为概要 |
|------|------|----------|
| `POST` | `/v1/completions` | 校验 API Key（若启用）后进入 `_handle_openai_request`，校验 OpenAI 风格 body，再调用 `motor.coordinator.router.dispatch.handle_request` |
| `POST` | `/v1/chat/completions` | 同上 |
| `GET` | `/v1/models` | 基于 `CoordinatorConfig.get_aigw_models()` 与调度器中的 P/D 可用实例数组装列表；未配置 AIGW 模型时返回 503 |

`handle_request` 根据当前可用实例角色自动选择 Router：P+D 使用统一分离 Router，union 或仅 P 场景使用混部 Router。P/D 协同行为由实例上报的 Connector capability 决定，详见 [PD 分离](../../features/PD_disaggregation.md)。

## Metaserver（独立端口上的 `POST /v1/metaserver`）

在 `motor/coordinator/process/inference_manager.py` 中，当配置存在 `worker_metaserver_port` 时，会为该 Worker 额外挂载一个仅包含 **`POST /v1/metaserver`** 的 FastAPI 应用；该端点调用 `InferenceServer.handle_metaserver_request`，最终进入 `motor.coordinator.router.dispatch.handle_metaserver_request`。

P/D 协同已统一通过请求中的 `_motor_dispatch` 上下文以及引擎 Connector adapter 处理，不再根据 Coordinator 的部署模式字段选择 metaserver 行为。

## 与「面向服务」的关系

对用户暴露的入口为上述 OpenAI 兼容路径及条件启用的 metaserver；调度、转发与错误处理由 Coordinator 内 Router / Scheduler 模块完成，无需客户端感知具体 P/D Pod 地址（由服务端根据实例与端点选择）。
