# [2026-11-10] Helm 策略更新脚本未通过提交检查

- **现象 (Symptom)**：策略热更新 PR 的 pylint 报文件编码缺失，Bandit 报固定临时路径和非绝对路径命令调用；PR 审计提示缺少测试。
- **根因 (Root cause)**：脚本在导入时执行集群操作，使用固定 kubeconfig 路径，文件先写入再 chmod，读写依赖默认编码，kubectl 直接通过命令名启动。
- **为什么会写出 (Why)**：只检查策略更新主流程，遗漏了临时资源生命周期和脚本可测试性。
- **修复 (Fix)**：拆分 main 与 run_update；使用 NamedTemporaryFile 安全创建并自动清理 kubeconfig，显式使用 UTF-8，以 shutil.which 解析 kubectl 路径。
- **测试拦截 (Test interception)**：test_helm_policy_update.py 验证字段合并、数组替换、JSON Pointer 转义、目标歧义、缺失策略、dry-run 失败停止写入、带旧策略保护的 Patch，以及成功和异常退出时的临时文件权限和清理；入口脚本用真实子进程验证参数边界。
- **场景 (Scenario)**：Helm Hook Job 读取 ServiceAccount 投影凭据并调用 Kubernetes API 修改已有策略。
- **关键词 (Keywords)**：Helm, scalingPolicy, kubeconfig, Bandit, pre-commit

## 审查补充

- Hook Job 固定的 120 秒活跃期限也覆盖 Pod 调度和镜像拉取，可能在脚本启动前终止更新。
  移除固定期限，使用 Helm 的 `--timeout` 与 kubectl 单次请求超时。
- `jobs.deploy.args`、`jobs.scale.args` 补充空数组默认值及字符串数组 Schema，提前拒绝错误类型。
- 使用实际 Helm 渲染验证 deploy/scale 的空参数及带空格参数、非法参数类型拦截，
  并验证策略更新 Job 不含固定活跃期限。未安装 Helm 时只跳过渲染测试。
