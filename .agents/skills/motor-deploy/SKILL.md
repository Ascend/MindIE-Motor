---
name: motor-deploy
description: "Motor deployment entry point for 拉起服务、启动/部署/重启/停止/查看 Motor、部署前检查、配置校验或 wheel/whl 替换. Route ordinary deployment requests through this Skill before selecting an atomic deployment workflow."
---

# Motor deployment dispatcher

这是 Motor 运维请求的唯一部署入口。本 Skill 只解析意图和编排步骤，不实现第二套
部署器，也不把一次旧运行记录当作当前事实。

## 路由

| 意图 | Skill / 原生能力 |
|---|---|
| 生成或修改原生部署配置 | [`motor-deploy-config-edit`](../motor-deploy-config-edit/SKILL.md) |
| 部署前只读环境检查 | [`motor-deploy-preflight`](../motor-deploy-preflight/SKILL.md) |
| `deploy.py --dry-run` 和 YAML 检查 | [`motor-deploy-configure`](../motor-deploy-configure/SKILL.md) |
| deploy、status、restart、stop | [`motor-deploy-k8s`](../motor-deploy-k8s/SKILL.md) |
| 在线 P/D 扩缩容 | [`motor-scale`](../motor-scale/SKILL.md) |
| RAS / 故障注入 / 恢复验证 | [`motor-reliability`](../motor-reliability/SKILL.md) |
| 构建并替换 Motor wheel | [`motor-deploy-build-wheel`](../motor-deploy-build-wheel/SKILL.md) |
| 部署后 readiness、功能或性能验证 | [`motor-validation`](../motor-validation/SKILL.md) |
| 集群级全量验收套组 | [`motor-smoke-suite`](../motor-smoke-suite/SKILL.md) |
| 失败现场采证 | [`motor-diagnosis`](../motor-diagnosis/SKILL.md) |
| deploy/startup 失败归因 | [`motor-diagnosis`](../motor-diagnosis/SKILL.md) → [`motor-diagnosis-startup`](../motor-diagnosis-startup/SKILL.md) |

路由命中原子 Skill 后，读取并完整遵循对应目录的 `SKILL.md`。不要在本入口中重新实现
原子流程。用户显式调用原子 Skill 时可直接进入该流程。

RAS / 扩缩容 / 全量验收请求分别路由到 `motor-reliability`、`motor-scale`、
`motor-smoke-suite`。不得在本入口临时拼接 `kill`、删 Pod 或 `hccn_tool` 命令。

## 标准链路

```text
解析当前执行环境和原生配置
→ 必要时编辑 user_config.json + env.json
→ read-only preflight
→ deploy.py --dry-run
→ 展示目标和命令并取得明确授权
→ deploy.py
→ 当前资源检查
→ readiness / functional validation
```

代码同步、SSH/MCP、共享目录等执行方式由使用者环境提供，不属于 Motor 仓协议。
每一步都重新读取当前配置、当前 kube context 和当前集群状态。

## 授权与边界

- 只读检查和 dry-run 不授权配置写入、apply、restart、stop、namespace 创建、远端
  源码覆盖或 `boot.sh` 修改。
- 配置修改、deploy、restart、stop、wheel 替换分别对明确 endpoint、context、
  namespace、配置目录和目标取得授权。
- 使用 `examples/deployer/deploy.py`、`delete.sh` 和生成的 YAML；禁止创建第二套
  deploy engine、bundle、run gate 或成功标记。
- 禁止用源码树 `PYTHONPATH` 冒充运行时代码替换。镜像包或 `motor-deploy-build-wheel`
  构建的 wheel 才是部署路径。
- 失败后先保存原命令、stdout/stderr、时间窗和集群证据，再进入
  `motor-diagnosis`；诊断不授权自动重试或修复。

报告实际执行命令、目标、观察结果和未验证项，不生成虚构的 workflow run ID。
