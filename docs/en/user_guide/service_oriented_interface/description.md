# Description

This document describes the HTTP paths provided by the **Coordinator inference plane** for external systems and their entry points in the code. For details about cross-cutting configurations such as the inference port, management port, TLS, API key, and rate limiting, see [API Description](../../api_reference/interface_description.md).

## Inference Application Registration Path

Register `InferenceServer._register_routes` in `motor/coordinator/api_server/inference_server.py` with the FastAPI application for inference.

| Method| Path| Behavior Summary|
|------|------|----------|
| `POST` | `/v1/completions` | After validating the API key (if enabled), the request proceeds to `_handle_openai_request`, where the OpenAI-style body is validated before calling `motor.coordinator.router.dispatch.handle_request`.|
| `POST` | `/v1/chat/completions` | Same as above|
| `GET` | `/v1/models` | Based on `CoordinatorConfig.get_aigw_models()` and available P/D instances in the scheduler, assemble the list. If no AIGW model is configured, return `503`.|

`handle_request` selects a Router based on `CoordinatorConfig.scheduler_config.deploy_mode` and the instance readiness status returned by the scheduler. For details, see `_ROUTER_MAP` and the fallback logic in the [PD disaggregation](../../features/PD_disaggregation.md) feature.

## Metaserver (`POST /v1/metaserver` on an Independent Port)

In `motor/coordinator/process/inference_manager.py`, when `worker_metaserver_port` is set in the configuration, an additional FastAPI app is mounted for that Worker, exposing only the `POST /v1/metaserver` endpoint. This endpoint invokes `InferenceServer.handle_metaserver_request`, which ultimately calls `motor.coordinator.router.dispatch.handle_metaserver_request`.

`handle_metaserver_request`: Decode-side interface for forwarding prefill-related requests to the Prefill instance. In the source code, processing proceeds only when `deploy_mode` is `CDP_SEPARATE`, `PD_SEPARATE`, or `PD_DISAGGREGATION_SINGLE_CONTAINER`; otherwise, an `HTTP 500` error is thrown. The processing logic is delegated to `SeparateCDPRouter.handle_metaserver_request`.

For the logic on the Decode side that constructs the Worker metaserver URL, refer to `_worker_metaserver_url` in `motor/coordinator/router/strategies/cdp_separate.py` (formatted as `http://{host}:{worker_port}/v1/metaserver`). This implementation relies on configurations such as `worker_metaserver_base_port` in `inference_workers_config`, consistent with the in-class comments.

## Relationship with "Service-oriented"

The entry exposed to users is the OpenAI-compatible path and conditionally enabled metaserver. Scheduling, forwarding, and error handling are managed by the Router and Scheduler modules within the Coordinator, eliminating the need for clients to be aware of specific P/D Pod addresses—these are selected server-side based on instances and endpoints.
