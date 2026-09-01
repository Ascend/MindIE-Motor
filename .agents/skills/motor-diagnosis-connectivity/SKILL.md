---
name: motor-diagnosis-connectivity
description: Atomic read-only diagnosis when curl cannot establish an HTTP exchange with a Motor endpoint. Use for DNS resolution, connection refused or reset, timeout, TLS handshake, kubectl port-forward, ClusterIP, NodePort, node IP, Service port, targetPort, selector, or Endpoint path failures; not after a valid HTTP response is received.
---

# Motor service connectivity diagnosis

这是 `motor-diagnosis-startup` 下的服务连通性原子诊断 Skill，也就是“curl 不通”问题的
专用入口。目标是在修改配置或重启服务前，确定故障位于客户端访问方式、DNS、K8s
Service/Endpoint、端口映射、Pod 监听、网络路径还是 TLS。首期只覆盖 Motor + vLLM +
vLLM-Ascend。

## 入口与边界

仅适用于尚未获得有效 HTTP 响应的故障：`Could not resolve host`、connection refused、
connection reset、connect/read timeout、TLS handshake/certificate、`kubectl port-forward`
失败，以及 ClusterIP/NodePort/node IP/Service port/targetPort 选择错误。

一旦收到 HTTP 状态和响应体，连接链路已经成立：`ready=false`/503 路由
[`motor-diagnosis-control-plane`](../motor-diagnosis-control-plane/SKILL.md)；推理接口
HTTP 4xx/5xx、payload 或模型问题转到 `motor-validation-functional` 或运行时代码分析。
Pod 未启动、未监听原因来自容器启动失败时，将根因交给
[`motor-diagnosis-k8s`](../motor-diagnosis-k8s/SKILL.md)。

只执行读取和有限探测；不改 Service、Endpoint、NetworkPolicy、证书、DNS、配置或
workload，不重启、删除或注入故障。请求必须设置超时，避免无限等待；Authorization、
Cookie、token、证书私钥和响应中的敏感内容必须脱敏。

## 诊断流程

1. 保存原始 curl 命令的脱敏版本、执行位置、UTC 时间、退出码、stderr、URL、scheme、
   host、port 和 path。先区分 DNS、TCP connect、TLS handshake、HTTP response、body
   五个阶段；不得把全部错误统称为“网络不通”。
2. 识别客户端位置：集群外通常不能直接访问 ClusterIP；NodePort 必须使用实际 Node
   InternalIP 和当前 Service nodePort；port-forward 必须保持进程存活并核对其 stderr。
3. 从当前 config 和 live Service 确认 inference/management Service、port、targetPort、
   selector；默认 inference 1025、management 1026 只是线索，不是目标发现机制。
4. 沿链路逐段验证：客户端解析/路由 → Service 地址 → selector → EndpointSlice address
   与 ready condition → Pod IP → 容器监听端口。端点为空时继续判断 selector 不匹配、
   Pod 未 Ready 还是 targetPort 错误。
5. 用同一 URL 分别从原客户端和一个已授权、集群可见的位置做有界探测。两处结果差异
   只能界定故障边界，不能自动证明 NetworkPolicy、主机防火墙或 Motor 代码是根因。
6. TLS 场景记录 SNI、证书 subject/SAN、issuer、有效期和验证错误；禁止用 `-k` 把证书
   问题当作通过。确需 `-k` 的对比探测必须标为诊断证据而非修复。

```bash
curl --verbose --connect-timeout 5 --max-time 10 <url>
kubectl --context "$CTX" get svc,endpoints,endpointslice -n "$NS" -o wide
kubectl --context "$CTX" get svc -n "$NS" <service> -o yaml
kubectl --context "$CTX" get endpointslice -n "$NS" -l kubernetes.io/service-name=<service> -o yaml
kubectl --context "$CTX" get pod -n "$NS" -l '<service-selector>' -o wide --show-labels
kubectl --context "$CTX" get node -o wide
kubectl --context "$CTX" logs -n "$NS" <pod> --all-containers --timestamps
```

需要 port-forward 时，先展示目标并取得执行授权，使用有界后台进程保存启动错误，在
成功、失败、取消和异常路径都清理本地转发进程。port-forward 失败只说明该访问通道
失败，不直接归因为 Motor 服务故障。

## 归因规则

| 证据链 | 主分类 |
|---|---|
| 集群 DNS、CNI、NetworkPolicy、节点路由、防火墙、证书基础设施异常 | environment |
| deployer 生成的 Service/selector/port 与原始配置不一致 | deployer |
| 客户端目标、Service 类型、host、port、targetPort、selector 或 TLS 参数配置错误 | config |
| 进程已正常启动但未按配置监听，或服务端 accept/TLS 处理代码异常 | runtime-code |

`timeout` 不能单独证明网络策略；`connection refused` 不能单独证明进程崩溃；空 Endpoint
也不能单独证明 selector 错误。必须用相邻两段的成功/失败证据收窄边界。

## 输出

输出失败的网络层级、客户端位置与访问方式、主分类、可能的 contributing category、
端口映射和 Endpoint 摘要、时间化证据链、已排除项、置信度和下一步。下一步只给最小
判别或修复建议及精确命令，未经授权不执行；证据不足时返回 `unknown` 并列出缺失证据。
