---
name: motor-diagnosis-startup
description: Explicit atomic workflow under motor-diagnosis for a failed Motor deploy or startup after evidence preservation. Use for deploy.py failures, unready workloads, missing Services/endpoints, or Coordinator readiness timeout.
---

# Motor startup diagnosis dispatcher

这是 `motor-diagnosis` 下的 deploy/startup 只读归因流程。先由父 Skill 保存首次失败，
再按证据分类；当前版本不提供四个领域的自动修复或独立原子 Skill。

## 入口

- `deploy.py` 或 `deploy.py --dry-run` 非零退出；
- 预期 workload 不存在或在有界等待内未 Ready；
- 必需 Service/Endpoint 缺失；
- Coordinator `/readiness` 未达到 `ready=true`。

保存 endpoint、context、namespace、失败阶段、UTC 时间窗、原命令、退出码、完整
stdout/stderr、预期结果、观察结果和已生成文件。某类对象尚未创建时，只收集该阶段
真实存在的证据。

## 分类框架

| Category | 典型证据 |
|---|---|
| environment | API/RBAC/admission/operator/scheduler/NPU/image/storage/network |
| deployer | 参数、traceback、模板/YAML 生成、apply 编排 |
| config | 原生 config 无效或 config→YAML→ConfigMap→Pod 漂移 |
| runtime-code | 进程已启动后 crash、hang、注册失败或运行时集成错误 |

失败阶段只是路由提示，不是根因。配置里的坏 image 值属于 config；合法 image 无法
从 registry 获取属于 environment；合法值在 YAML 生成时丢失属于 deployer。

## 停止规则与输出

证据链足以支持根因时停止；无法用只读证据区分时返回 `unknown`、缺失证据和最小
判别检查。输出失败阶段、主分类、可能的 contributing category、时间化证据链、
已排除项、置信度和下一步。

分类不授权 retry、restart、delete、repair、config edit、scale、namespace 创建或
故障注入。当前没有领域子 Skill 时，继续用源码和只读事实分析，不生成不存在的路由。
