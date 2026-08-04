# Issue Reporting — 定位问题后自动提交 ISSUE

> **渐进式披露**：本文件只在「bug 已确认 + 用户同意提交 ISSUE」后才读取（见 SKILL.md 的
> Debugging Workflow 第 6 步）。平时不要加载本文件——它包含 token 处理细节，与日常开发无关。

## 触发时机

在以下流程完成后触发（由 SKILL.md 的 Debugging Workflow 驱动）：

1. Bug 已定位、修复完成、测试拦截通过（测试绿）
2. 案例已固化到 `bug-fix-history/`（INDEX.md + 案例文件）

此时向用户提问是否提交 ISSUE，**仅在用户同意后**读取本文件并执行。

## 交互流程

``` text
案例固化完成
  │
  ▼
① 搜索历史 ISSUE（防重复，硬性检查）：
   └─ 命中相似 issue → 告知用户已有 issue 链接，**不提交**，结束
   └─ 未命中 → 继续
  │
  ▼
② 生成 ISSUE 草稿（按 Motor bug-report 模板，字段从案例自动填充）
  │
  ▼
③ 询问用户提交方式（三选一）：
   ├─ A) 用你的 GitCode token 直接提交   → 走「路径 A」
   ├─ B) 生成 markdown 草稿自己复制      → 走「路径 B」
   └─ C) 不需要提交                      → 结束（案例已固化，知识不丢失）
```

## 模板字段映射（bug-report.yml → 案例字段）

仓库模板：`.gitcode/ISSUE_TEMPLATE/bug-report.yml`（title 前缀 `[Bug-Report|缺陷反馈]:`，labels `["bug-report"]`）。

| 模板字段 | 必填 | 从案例哪里取 |
|----------|------|-------------|
| 搜索过现有 issues 确认 | 必填 | 提交前先搜索确认（见「提交前检查」） |
| 问题描述 | 必填 | 案例标题 + **现象 (Symptom)** |
| 环境信息 | 必填 | 从用户/上下文获取（硬件型号、MindIE 版本、Python、部署方式） |
| 重现步骤 | 必填 | **场景 (Scenario)** + **根因 (Root cause)** 转步骤 |
| 预期结果 | 必填 | 从「现象」反推（正确行为应该是什么） |
| 日志 / 截图 | 必填 | **现象**中的关键日志行 |
| 备注 | 选填 | **为什么会写出 (Why)** + **修复 (Fix)** + **测试拦截**（帮助维护者理解） |

### 防重复检查（硬性，提交前必须执行）

**有相似历史 issue 就绝不提交**——重复 issue 会污染仓库、浪费维护者时间。检查逻辑：

1. 用案例的关键词 + 核心症状搜索历史 issue（GitCode 开放 API，公开仓库无需 token）：

   ```bash
   # 关键词搜索（GitCode 兼容 Gitee API v5）
   curl -sS "https://gitcode.com/api/v5/search/issues?q=repo:Ascend/MindIE-Motor+<关键词1>+<关键词2>"
   ```

   `search/issues` 无结果时，再用全量列表 + 本地过滤兜底：

   ```bash
   # 拉取全部 issue（含已关闭），本地按标题/正文匹配关键词
   curl -sS "https://gitcode.com/api/v5/repos/Ascend/MindIE-Motor/issues?state=all&per_page=100&page=1"
   ```

2. **重复判定**：历史 issue 的标题或正文命中案例的**关键词**（3-5 个）中 ≥2 个，或命中**核心症状**（如报错码、异常特征），即视为相似。

3. **命中即终止**：告知用户「已有类似 issue：#<编号> <标题>（<链接>）」，附上对比说明（相似点/差异点），**不提交**，流程结束。若差异确实重大（如不同模块、不同版本、新场景），先向用户说明差异、由用户决定是否仍要提交。

4. **未命中**：继续生成草稿。

### 提交前确认

- 草稿先展示给用户确认（标题 + 问题描述 + 重现步骤），用户批准后才提交

## 路径 A：GitCode API 直接提交（需 token）

### Token 获取（首次）

1. 打开 <https://gitcode.com/> → 右上角头像 → 「设置」→「访问令牌」（或「私人令牌」）
2. 勾选 `projects` / `issues` 权限，生成 token
3. 导出为环境变量：`export GITCODE_TOKEN=<token>`（建议写入 `~/.bashrc` 或 shell 配置）

### 提交命令

GitCode 开放 API 与 Gitee API v5 兼容：

```bash
# 创建 issue（GITCODE_TOKEN 从环境变量读取，绝不写死在脚本/文档里）
curl -sS -X POST "https://gitcode.com/api/v5/repos/Ascend/MindIE-Motor/issues" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg t "$GITCODE_TOKEN" \
    --arg title "[Bug-Report|缺陷反馈]: <标题>" \
    --arg body "<markdown 正文（模板字段）>" \
    '{access_token: $t, title: $title, body: $body, labels: ["bug-report"]}')"
```

响应中的 `html_url` 即新 ISSUE 地址，报告给用户。

### Token 安全检查

- **绝不**把 token 写入日志、skill 文档、bug-fix-history 案例或任何 commit
- 只从环境变量 `GITCODE_TOKEN` 读取，用完即忘
- 如果环境变量未设置：**不要**要求用户把 token 贴到对话里（可能被记录），降级到路径 B，并提示如何设置环境变量

## 路径 B：生成 markdown 草稿（无需 token）

当用户不想提供 token 时：

1. 按模板字段生成草稿，写入 `docs/issue_drafts/<YYYY-MM-DD>-<short-title>.md`（仓库根下，方便用户打开；**先 `mkdir -p docs/issue_drafts`**，该目录不随仓库存在）
2. 草稿内容 = bug-report 模板的完整填写版（含「搜索确认」勾选提示）
3. 提示用户：打开 <https://gitcode.com/Ascend/MindIE-Motor/issues/new> → 选择「🐛 Bug-Report|缺陷反馈」模板 → 粘贴草稿内容

## 结束条件

- 提交成功 → 报告 issue URL，收尾
- 生成草稿 → 告知文件路径，收尾
- 用户拒绝 → 静默结束（案例已固化，知识不丢失）
