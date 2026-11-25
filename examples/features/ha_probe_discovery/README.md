# Docker 静态候选地址与后台探针

[`user_config.json`](user_config.json) 是需要合并到已有模型配置中的功能片段，不是完整推理部署配置。示例地址是文档地址；部署前替换为调用方可达的宿主机 IP 和实际对外端口。完整启动步骤见 [Docker 主备部署指南](../../../docs/zh/user_guide/deployment/docker/ha.md)，字段与默认值见[配置参考](../../../docs/zh/user_guide/configuration/config_reference.md#service-discovery-config)。

## 配置合并

将示例中的主备、ETCD、候选地址和固定管理端口配置合并到各运行进程使用的本地 `user_config.json`，保留原模型、引擎、镜像及资源配置。同一套服务各调用方使用一致的目标候选和端口；不同引擎节点的模型、设备、本实例 IP 等配置可以不同。调用方读取自己的配置文件，不读取对端组件所在机器的文件。

不要用本功能片段直接替换完整模型配置，也不要把同一实例的 `pod-ip` 复制到不同宿主机。标准 Docker 部署器会统一内部客户端与组件的配置路径；手工启动进程时，须在启动前设置 `USER_CONFIG_PATH`，与 `--config`（如使用）指向同一文件。

## 配置规则

- `motor_deploy_config.service_discovery_config.probe_ips.controller`、`probe_ips.coordinator` 分别配置目标组件的候选 IP 数组。配置某个目标的非空列表即启用后台探针，只有一个 IP 也启用；未配置该目标时沿用原 K8s Service、DNS 或固定 IP 路径，两种目标可以混合配置。
- 同一目标的调用方应配置同一部署的完整候选成员。每个进程只启动其消费目标的探针；成员清单不表示启用主备。
- 主备由各组件自己的 `standby_config` 控制。Docker 主备开关不创建第二个实例，需要在不同节点分别启动副本。ETCD 使用静态 `etcd_hosts`，兼容旧 `etcd_host` 单值；同时填写时只允许相同单地址，无需挂载 AgentOS 配置。
- 仅使用候选探针的单实例部署可以关闭 HA，并按原组件要求关闭 ETCD 持久化；探针本身不访问 ETCD。
- 端口复用本地统一配置中的 `motor_controller_config.api_config.controller_api_port` 和 `motor_coordinator_config.api_config.coordinator_api_mgmt_port`；启用对应候选探测时必须显式填写。同组件所有候选端口一致，不增加逐成员端口或推理端口配置。
- Controller 启用 Coordinator 指标代理时，还须显式配置 `motor_coordinator_config.api_config.coordinator_obs_port`。Coordinator 候选探测要求其 `port_allocator_config.enable=false`，避免管理／观测端口自动避让。端口冲突由部署解决；修改成员或端口后需重启消费进程。
- 独立管理组件与 `coordinator_controller` 合容器入口均按实际调用目标校验候选及所需端口。
- 管理 API 的 TLS 和 API key 沿用现有配置，与 ETCD TLS 独立。ETCD 单地址支持 DNS 或 IP，多地址须为同地址族的静态 IP；本示例开启 ETCD 双向 TLS，需先完成下节证书准备。

## ETCD 双向 TLS

示例使用 `motor_deploy_config.tls_config.etcd_tls_config.enable_tls=true`，并在 Controller、Coordinator 的 `etcd_config` 中填写 `etcd_tls_server_name="motor-etcd.internal"`。部署前确认：

- 所有 ETCD 成员的独立服务器证书都包含共同的 DNS SAN `motor-etcd.internal`，且能由 Motor 配置的 CA 验证。该名称是证书身份，不是节点名；无需修改 ETCD `--name`，也无需部署 DNS，Motor 仍直连 `etcd_hosts` 中的 IP。
- ETCD 客户端接口使用 HTTPS，并开启客户端证书认证；集群内部 peer TLS 由 ETCD 部署独立配置。
- 为连接 ETCD 的 Motor 容器提供 `ca.crt`、`client.crt` 和与客户端证书匹配的无密码 PEM 私钥 `client.key`。按[部署指南的证书准备步骤](../../../docs/zh/user_guide/deployment/docker/ha.md#etcd-mtls)只读挂载至 `/run/motor-etcd-tls`，配置填写容器内路径。部署器不会根据这三个字段自动挂载或签发证书。

共同 SAN 是多 IP TLS 的接入前提，不具备时需先由 ETCD 提供方补齐证书。仅配置 Motor 开关不能将明文 ETCD 改为 TLS。若环境使用明文，将 `enable_tls` 设为 `false`；此时不读取证书文件，也不使用 `etcd_tls_server_name`。主备和探针开关不受该选择影响。既有单地址 TLS 未填写新字段时保留原证书校验方式，K8s 旧配置不必修改。

## 候选输入方式

Docker 部署器支持两种候选输入方式，针对同一目标选择一种：

| 方式 | 示例 |
| --- | --- |
| JSON | `"probe_ips": {"controller": ["192.0.2.11", "192.0.2.12"]}` |
| 命令行 | `--controller-probe-ips 192.0.2.11,192.0.2.12` 或 `--coordinator-probe-ips 192.0.2.11,192.0.2.12` |

CLI 候选会写入本次生成的配置副本，不修改输入配置；端口仍从本地 JSON 的已有字段获取。使用 CLI 时应删去该目标在 JSON 中的候选列表，重复提供即报错，不覆盖或合并。

使用 `--show-commands` 生成命令时，必须将候选写入 JSON；该选项与 `--controller-probe-ips` 或 `--coordinator-probe-ips` 同时提供会报错退出。生成命令仍需由部署者分别安排到各宿主机执行，不会自动创建跨节点主备拓扑。

旧 `--controller-ip`／`--coordinator-ip` 仍表示固定访问目标，不能与同一目标的 CLI 或 JSON 候选同时提供。模板中的 `*_api_dns` 默认字段不属于显式 CLI 单 IP，不与候选冲突。本容器的 `--pod-ip` 等运行身份仍需配置，候选列表不能代替自身 IP。

## 运行行为与边界

Controller、Coordinator 的内部调用目标均由 `/readiness` 的 HTTP 200 筛选，与原 K8s 探针一致。Coordinator 正文 `ready:false` 供 Controller 原拓扑恢复逻辑触发全量 SET，不用于摘除管理目标；拓扑同步仍发向所有配置成员，包括备实例。本配置不为外部推理客户端选择访问地址。

业务请求只读后台快照，没有合格目标时走原错误处理。首轮探测完成前短暂不可用是预期行为；不自动回退到列表首地址，不增加通用业务请求重放。外部推理客户端与固定 URL 入口不在本次改造范围。

默认探测间隔为 `2` 秒，连接/读取等待超时为 `1` 秒，结果有效期为 `6` 秒。读取等待超时不是整个响应的总时限；持续缓慢返回响应的异常成员可能占用探针线程并延迟进程正常退出。具体规则见[配置参考](../../../docs/zh/user_guide/configuration/config_reference.md#service-discovery-config)。

现有完整单机 `single_container` 保持兼容；本次不新增“两台机器各一个完整单容器共同组成一套服务”的部署能力。Controller 与 Coordinator 合在一个管理容器时仍独立选主，各自的主实例可能位于不同机器。

完整配置和部署步骤见 [Docker 主备部署指南](../../../docs/zh/user_guide/deployment/docker/ha.md)。
