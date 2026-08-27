# Coordinator readiness semantics

当前事实必须从匹配 revision 的以下位置确认：

- `motor/coordinator/api_server/management_server.py`：`GET /readiness` 路由与响应；
- `motor/coordinator/domain/probe.py`：readiness 判定；
- `motor/config/coordinator.py`：management/inference 默认端口；
- `examples/deployer/yaml_template/`：实际 Service/探针模板。

响应至少包含布尔字段 `ready`。HTTP 200 只表示端点成功处理请求，不能单独证明服务
就绪；`ready=false` 表示尚未满足 Coordinator 当前 readiness 条件。

PASS 必须同时满足：

1. 访问的是当前部署的 Coordinator management endpoint；
2. HTTP 状态为 200；
3. 响应是 JSON；
4. `ready` 严格为布尔值 `true`。

不要由固定 Pod/Service 名猜目标。用原生 config、Service selector、owner reference、
Endpoint 和 Pod UID 证明访问对象属于本次部署。
